"""Synthetic future PDF captions: real graph/manifest/SQL/context; no providers."""
import copy
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from app.models import CustomModel, DocumentChunk, FileDocument, KnowledgeBase, ModelType
from app.services.parent_child import (ParentChildConfig, build_parent_child_chunks,
    derive_image_description_chunk, remap_and_validate_graph, IMAGE_DESCRIPTION_LABEL as LABEL)
from app.services.parser import ParsedChunk, FileParser
from app.services.pdf_provenance import write_manifest
from app.services.index_builds import verified_provenance
from app.services.context_builder import build_chat_context
from app.services.parent_context import expand_parent_context
from app.services.agent_workflow import evidence_text, _register_sources, generated_image_description_provenance
from app.tasks import dispatch, staged_pipeline as pipeline


def digest(value):
    return hashlib.sha256(value.encode('utf-8') if isinstance(value, str) else value).hexdigest()


def image(page=7, path='scan/page7.png'):
    return ParsedChunk('[图片: '+path+']', page, 'image', path,
        {'source_locator': {'kind':'pdf-image', 'page':page, 'bbox':[0,0,400,700], 'source_sha256':'a'*64}})


def caption_graph(page=7, body=None):
    original=image(page)
    derived=derive_image_description_chunk(original, body or ('设备温度读数为三十度。 侧边标注为模型识别内容。 '*24),
        source_sha256='a'*64,image_sha256='b'*64,model='vlm-test',prompt_sha256='c'*64,
        max_tokens=1000,effect_key='d'*64)
    return original,build_parent_child_chunks([original,derived],ParentChildConfig(400,0,80,12),count_tokens=len)


def test_original_image_unchanged_and_child_graph_has_exact_containment_and_page():
    original,graph=caption_graph()
    assert graph[0] is original and original.content=='[图片: scan/page7.png]'
    parents=[c for c in graph if (c.metadata or {}).get('chunk_type')=='parent']
    children=[c for c in graph if (c.metadata or {}).get('chunk_type')=='child']
    assert len(parents)>1 and len(children)>3
    by_id={p.metadata['id']:p for p in parents}
    for child in children:
        parent=by_id[child.metadata['parent_id']]
        assert child.content in parent.content and child.page_number==parent.page_number==7
        assert child.image_path is None and child.metadata['heading_text']==LABEL
        assert child.metadata['image_description']['indexed_text_sha256']==digest(child.content)
        assert child.content==parent.content[child.metadata['parent_local_start']:child.metadata['parent_local_end']]
    assert all(parent.content.startswith(LABEL+'\n') for parent in parents)
    remap_and_validate_graph([pipeline._chunk_data(c) for c in graph],'file')


@pytest.fixture
def persisted(tmp_path):
    original,graph=caption_graph()
    checkpoint={'source_sha256':'a'*64,'provenance_manifest':write_manifest(str(tmp_path/'audit'),'a'*64,[],graph,flat=True)}
    rows=remap_and_validate_graph([pipeline._chunk_data(c) for c in graph],'file')
    provenance=verified_provenance(checkpoint,rows)
    engine=create_engine('sqlite:///'+str(tmp_path/'ledger.sqlite'))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb',name='test',embedding_model='unused',vlm_model='unused'))
        session.add(FileDocument(id='file',kb_id='kb',name='scan.pdf',local_path=str(tmp_path/'scan.pdf'),size=0))
        for index,row in enumerate(rows):
            session.add(DocumentChunk(**{key:row[key] for key in ('id','content','page_number','content_type','image_path','parent_id','heading_text','chunk_type','token_count')},
                file_id='file',original_index=index,source_metadata_json=provenance.get(row['id'])))
        session.commit()
        yield session,rows,provenance
    engine.dispose()


def seed_from(session,row):
    chunk=session.get(DocumentChunk,row['id'])
    return dict(id=chunk.id,chunk_id=chunk.id,content=chunk.content,content_type=chunk.content_type,
        file_id='file',file_name='scan.pdf',kb_id='kb',page_number=chunk.page_number,
        heading_text=chunk.heading_text,source_metadata=json.loads(chunk.source_metadata_json),score=.9)


def citation(row,text):
    return dict(chunk_id=row['id'],content=text,content_type=row.get('content_type'),
        fileId=row['file_id'],fileName=row['file_name'],page_number=row.get('page_number'),
        source_metadata=row.get('source_metadata'),heading_text=row.get('heading_text'))


@pytest.mark.parametrize('context_budget',[5000,0])
def test_real_parent_expansion_and_child_fallback_deliver_labeled_chat_and_agent_evidence(persisted,context_budget):
    session,rows,_=persisted
    children=[r for r in rows if r['chunk_type']=='child']
    seed=seed_from(session,children[2])
    assert not seed['content'].startswith(LABEL)
    expanded=expand_parent_context([seed],session,window_size=0,
        max_context_chars=context_budget,include_source_members=True,allowed_kb_ids=['kb'])
    citations,summary=build_chat_context(expanded,citation)
    assert len(citations)==1 and citations[0]['content'].count(LABEL)==1
    delivered=citations[0]
    assert delivered['evidence_status']=='MODEL_GENERATED_IMAGE_DESCRIPTION'
    assert delivered['requires_original_verification'] is True and delivered['page_number']==7
    assert delivered['source_image_references'][0]['image_sha256']=='b'*64
    assert delivered['source_image_references'][0]['source_page']==7
    assert delivered['source_content_hash']==digest(session.get(DocumentChunk,delivered['chunk_id']).content)
    registry=[]
    registered=_register_sources({'citations':citations},registry)
    assert registered['source_ids']==[1] and registered['context'].count(LABEL)==1
    assert registry[0]['chunk_id']==delivered['chunk_id']
    assert registry[0]['content']==delivered['content']


@pytest.mark.parametrize('damage',['metadata','page','image_hash','content','type_image','label_only','malformed_origins'])
def test_untrusted_edited_or_mismatched_caption_is_not_text_evidence(persisted,damage):
    session,rows,_=persisted
    seed=seed_from(session,next(r for r in rows if r['chunk_type']=='child'))
    if damage=='metadata':seed['source_metadata']={}
    elif damage=='page':seed['page_number']=8
    elif damage=='image_hash':seed['source_metadata']['image_descriptions'][0]['image_sha256']='bad'
    elif damage=='content':seed['content']='edited/rerank-truncated unrelated text'
    elif damage=='type_image':seed['content_type']='image'
    elif damage=='label_only':seed['source_metadata']={};seed['heading_text']=None;seed['content']=LABEL+'\nspoof'
    elif damage=='malformed_origins':seed['source_metadata']['content_origins']=123
    assert evidence_text(seed)==('',True)
    citations,summary=build_chat_context([seed],citation)
    assert citations==[] and summary['changes'][0]['omitted_from_llm'] is True


def test_caption_parents_never_merge_pages_and_page_only_locator_is_honest():
    graph=[]
    for page in (7,8):
        original=image(page)
        original.metadata={}
        derived=derive_image_description_chunk(original,'同一张扫描图的描述。 '*30,source_sha256='a'*64,
            image_sha256='b'*64,model='test',prompt_sha256='c'*64,max_tokens=1000)
        graph.extend(build_parent_child_chunks([original,derived],ParentChildConfig(300,0,60,10)))
    parents={row.metadata['id']:row for row in graph if (row.metadata or {}).get('chunk_type')=='parent'}
    for child in graph:
        if (child.metadata or {}).get('chunk_type')=='child':
            assert child.page_number==parents[child.metadata['parent_id']].page_number
            assert child.metadata['source_locator']['geometry_status']=='PAGE_ONLY'
            assert 'bbox' not in child.metadata['source_locator']


def test_registered_json_uses_distinct_hash_and_original_pdf_locator_without_fake_json_page(persisted):
    session,rows,_=persisted
    seed=seed_from(session,next(r for r in rows if r['chunk_type']=='child'))
    seed.update(file_name='derived.json',page_number=None,heading_text=LABEL+' 原 PDF 第 7 页')
    source=seed['source_metadata'];source['source_sha256']='e'*64
    source['provenance_schema']='registered-image-caption-json-v1'
    original=dict(original_source_sha256='a'*64,original_file_id='original-pdf',original_build_id='build',
        original_generation=2,original_chunk_id='original-image')
    for descriptor in source['image_descriptions']:
        descriptor.update(original)
    value=source['image_descriptions'][0]
    source['source_locators']=[dict(kind='pdf-image-description',derivation='image-caption-text-v1',
        **original,original_chunk_sha256='f'*64,source_manifest_sha256='1'*64,page=7,
        source_image_path=value['source_image_path'],image_sha256=value['image_sha256'],derived_document_sha256='e'*64)]
    citations,_=build_chat_context([seed],citation)
    assert len(citations)==1 and citations[0]['page_number'] is None
    assert '原 PDF 第 7 页' in citations[0]['location']
    assert citations[0]['source_metadata']['source_sha256']=='e'*64
    assert citations[0]['source_image_references'][0]['original_source_sha256']=='a'*64
    source['source_locators'][0]['derived_document_sha256']='a'*64
    assert evidence_text(seed)==('',True)
    seed['source_metadata']={}
    assert evidence_text(seed)==('',True)


def test_actual_chat_provider_request_and_sse_keep_child_caption_label_and_original_page(persisted,monkeypatch):
    from app.schemas import Message,MessageRole
    from app.services import chat,observability,pdf_table_grounding,spreadsheet_grounding
    session,rows,_=persisted
    seed=seed_from(session,[row for row in rows if row['chunk_type']=='child'][2])
    expanded=expand_parent_context([seed],session,window_size=0,
        max_context_chars=5000,include_source_members=True,allowed_kb_ids=['kb'])
    monkeypatch.setenv('RAG_CHAT_CONTEXT_STRATEGY','EXPANDED_CONTENT')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','false')
    monkeypatch.setenv('LANGFUSE_ENABLED','false')
    monkeypatch.setattr(observability,'_client',None)
    monkeypatch.setattr(observability,'_attempted',True)
    class FakeSession:
        def __init__(self,*args):pass
        def __enter__(self):return self
        def __exit__(self,*args):return False
    monkeypatch.setattr(chat,'Session',FakeSession)
    monkeypatch.setattr(spreadsheet_grounding,'prepare_spreadsheet_grounding',lambda *args:None)
    monkeypatch.setattr(pdf_table_grounding,'prepare_pdf_table_grounding',lambda *args:None)
    create=AsyncMock(return_value=SimpleNamespace(id='test',model='test',usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='这是模型图片描述 [1]'),finish_reason='stop')]))
    service=chat.ChatService.__new__(chat.ChatService)
    service._runtime_snapshot=lambda:SimpleNamespace(max_chat_history_rounds=20)
    service._resolved_llm=lambda model:service.llm_service
    service.llm_service=SimpleNamespace(model='test',_get_client=lambda:SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    service._retrieve_with_new_service=AsyncMock(return_value=(copy.deepcopy(expanded),{'retrieval_status':'SUCCESS'}))
    async def run():
        return [event async for event in service._chat_stream_normal(
            [Message(role=MessageRole.USER,content='这页图片描述什么？')],['kb'],5,0,None,False,0,None,context_window=0)]
    events=asyncio.run(run())
    assert create.await_count==1
    payload=str(create.await_args.kwargs['messages'])
    assert LABEL in payload and '[图片: scan/page7.png]' not in payload
    citations=next(value['citations'] for name,value in events if name=='rag_result')
    expected_parent_id=session.get(DocumentChunk,seed['id']).parent_id
    assert citations[0]['chunk_id']==expected_parent_id and citations[0]['page_number']==7
    assert citations[0]['seed_child_ids']==[seed['id']]
    assert citations[0]['content'].startswith(LABEL+'\n')
    assert citations[0]['evidence_status']=='MODEL_GENERATED_IMAGE_DESCRIPTION'


@pytest.mark.parametrize('enabled',[False,True])
def test_staged_parse_calls_vlm_before_graph_and_keeps_legacy_behavior_when_disabled(tmp_path,monkeypatch,enabled):
    from app.services import runtime_settings
    from app.services.vlm import TaskVLMService
    engine=create_engine('sqlite:///'+str(tmp_path/'tasks.sqlite'))
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(dispatch,'engine',engine)
    settings=pipeline.get_settings().model_copy(update={'IMAGE_DIR':str(tmp_path/'images'),
        'UPLOAD_DIR':str(tmp_path/'uploads'),'FILE_TASK_ARTIFACT_DIR':str(tmp_path/'artifacts')})
    monkeypatch.setattr(pipeline,'get_settings',lambda:settings)
    monkeypatch.setattr(runtime_settings,'get_runtime_settings',lambda session=None:SimpleNamespace(
        revision=1,chunk_size=500,chunk_overlap=50,default_vlm_model='vlm'))
    source=tmp_path/'scan.pdf';source.write_bytes(b'synthetic PDF parser input; parser is replaced explicitly')
    path=Path(settings.IMAGE_DIR)/'scan/page7.png';path.parent.mkdir(parents=True);path.write_bytes(b'synthetic image bytes'*200)
    original=image();original.metadata['source_locator']['source_sha256']=digest(source.read_bytes())
    second=copy.deepcopy(original)
    def parsed(self,*args,**kwargs):
        from app.services.parsed_document import ParsedDocument
        self._document = ParsedDocument([copy.deepcopy(original),copy.deepcopy(second)],digest(source.read_bytes()))
        self._chunk_parameters = dict(size=500,overlap=50,mode='custom')
        self.splitter=SimpleNamespace(count_tokens=len)
        return [copy.deepcopy(original),copy.deepcopy(second)]
    monkeypatch.setattr(FileParser,'parse',parsed)
    calls=[]
    async def describe(self,path):
        calls.append(path);return '温度为三十度。 图像信息需要核对。 '*30
    monkeypatch.setattr(TaskVLMService,'describe_image',describe)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb',name='test',embedding_model='emb',vlm_model='vlm'))
        for key,kind in [('emb',ModelType.EMBEDDING),('vlm',ModelType.VLM)]:
            session.add(CustomModel(id=key,name=key,model_type=kind,base_url='https://provider.invalid/v1',model_name=key))
        session.add(FileDocument(id='file',kb_id='kb',name='scan.pdf',local_path=str(source),size=source.stat().st_size))
        session.commit()
    result=dispatch.dispatch_file_task('file','parse',{'parent_child':enabled,'parent_chunk_size':400,
        'parent_chunk_overlap':0,'child_chunk_size':80,'child_chunk_overlap':12})
    claim=dispatch.claim_task(result['task_id'],'parse')
    pipeline.execute_stage(claim)
    with Session(engine) as session:
        rows=session.exec(select(DocumentChunk).where(DocumentChunk.file_id=='file')).all()
        images=[row for row in rows if row.content_type=='image']
        children=[row for row in rows if row.chunk_type=='child']
        assert len(images)==2 and len(calls)==1  # real EffectRunner cache suppresses duplicate paid call.
        if enabled:
            assert all(row.content=='[图片: scan/page7.png]' for row in images)
            assert children
            assert [row for row in rows if row.chunk_type=='parent']
            assert all(json.loads(row.source_metadata_json)['content_origins']==['model_generated_image_description'] for row in children)
            assert all(json.loads(row.source_metadata_json)['image_descriptions'][0]['model']=='vlm' for row in children)
        else:
            assert children==[] and all(LABEL in row.content for row in images)
    assert source.read_bytes()==b'synthetic PDF parser input; parser is replaced explicitly'
    engine.dispose()

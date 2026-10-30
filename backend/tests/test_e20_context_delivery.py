"""E2.0 source-preserving expansion checks; all providers and data are synthetic."""
import asyncio
import copy
import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.models import DocumentChunk, FileDocument, KnowledgeBase
from app.services import rerank


def digest(text):
    return hashlib.sha256(text.encode('utf8')).hexdigest()


@pytest.fixture(autouse=True)
def isolate_test_telemetry(monkeypatch, tmp_path):
    # Never append fixture events to the application or historical ledgers.
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'synthetic-events.jsonl'))
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')


@pytest.fixture
def db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='Synthetic', embedding_model='none', vlm_model='none'))
        session.add(FileDocument(id='file', kb_id='kb', name='fixture.pdf', local_path='synthetic-only', size=0))
        session.add_all([
            DocumentChunk(id='previous', file_id='file', parent_id=None, original_index=0, page_number=6, content='IRRELEVANT_NEIGHBOR'),
            DocumentChunk(id='seed', file_id='file', parent_id='parent', original_index=1, page_number=7, content='SEED_PART'),
            DocumentChunk(id='sibling', file_id='file', parent_id='parent', original_index=2, page_number=8, content='SIBLING_GOLD'),
            DocumentChunk(id='next', file_id='file', parent_id=None, original_index=3, page_number=9, content='NEXT_GOLD'),
        ])
        session.commit()
        yield session
    engine.dispose()


def seed_row():
    return {'id':'seed', 'file_id':'file', 'file_name':'fixture.pdf', 'kb_id':'kb', 'kb_name':'Synthetic',
            'page_number':7, 'content':'SEED_PART', 'score':.8}


def citation_formatter(row, content):
    return {'chunk_id':row.get('id'), 'content':content, 'fileId':row.get('file_id'),
            'fileName':row.get('file_name'), 'location':('page '+str(row['page_number']) if row.get('page_number') is not None else 'UNKNOWN'),
            'page_number':row.get('page_number'), 'source_metadata':row.get('source_metadata') or {}}


def build(rows, strategy='EXPANDED_CONTENT'):
    from app.services.context_builder import build_chat_context
    return build_chat_context(copy.deepcopy(rows), citation_formatter, strategy=strategy)


def test_annotation_opt_in_keeps_legacy_agent_default(db):
    row=seed_row()
    legacy=rerank.expand_context([row],db,window_size=1)
    annotated=rerank.expand_context([row],db,window_size=1,include_source_members=True)
    assert 'context_group' not in legacy[0]
    assert annotated[0]['content']==legacy[0]['content']
    assert annotated[0]['original_content']==legacy[0]['original_content']
    group=annotated[0]['context_group']
    assert group['context_group_id']=='group:seed' and group['seed_evidence_ids']==['seed']
    members=group['source_members']
    assert {'seed','sibling','previous'} <= {m['id'] for m in members}
    for m in members:
        assert m['file_id']=='file' and m['content_hash']==digest(m['content'])
        assert m['id'] in group['seed_evidence_ids'] + group['expanded_evidence_ids']
        assert m['relation'] and 'parent_id' in m


def test_frozen_group_ab_delivers_seed_neighbor_and_real_cross_page_citations(db):
    frozen=rerank.expand_context([seed_row()],db,window_size=1,include_source_members=True)
    old, _=build(frozen,'ORIGINAL_CONTENT')
    new, summary=build(frozen,'EXPANDED_CONTENT')
    assert [c['chunk_id'] for c in old]==['seed']
    assert [c['content'] for c in old]==['SEED_PART']
    by_id={c['chunk_id']:c for c in new}
    assert {'seed','sibling','previous'} <= set(by_id)
    assert by_id['seed']['content']=='SEED_PART' and by_id['sibling']['content']=='SIBLING_GOLD'
    assert by_id['sibling']['page_number']==8 and by_id['previous']['page_number']==6
    assert all(c['fileId']=='file' for c in new)
    assert len(by_id)==len(new), 'a chunk repeated as sibling and neighbor must be delivered once'
    assert 'SIBLING_GOLD' not in str(old) and 'SIBLING_GOLD' in str(new)
    assert isinstance(summary,dict)


def test_duplicate_groups_never_duplicate_source_and_irrelevant_content_is_not_hidden(db):
    frozen=rerank.expand_context([seed_row()],db,window_size=1,include_source_members=True)
    citations,_=build(frozen+copy.deepcopy(frozen))
    ids=[c['chunk_id'] for c in citations]
    assert len(ids)==len(set(ids))
    assert 'IRRELEVANT_NEIGHBOR' in ''.join(c['content'] for c in citations)


def test_missing_database_seed_remains_uninvented(db):
    row={'id':'missing','file_id':'file','file_name':'fixture.pdf','page_number':None,'content':'ORIGINAL_ONLY','score':.5}
    expanded=rerank.expand_context([row],db,window_size=1,include_source_members=True)
    assert expanded[0]['id']=='missing' and expanded[0]['content']=='ORIGINAL_ONLY'
    citations,summary=build(expanded)
    assert len(citations)==1 and citations[0]['chunk_id']=='missing'
    assert citations[0]['page_number'] is None
    assert summary['source_mapping_status']=='UNKNOWN'


def test_empty_input_and_unknown_group_do_not_fabricate_members():
    citations,_=build([])
    assert citations==[]
    row={**seed_row(), 'content':'SEED_PART\nUNVERIFIED_NEIGHBOR', 'original_content':'SEED_PART',
         'metadata':{'is_expanded':True}, 'context_group':{'context_group_id':'group:seed',
         'seed_evidence_ids':['seed'], 'expanded_evidence_ids':['seed','mystery'], 'source_members':[]}}
    citations,summary=build([row])
    assert [c['chunk_id'] for c in citations]==['seed']
    assert citations[0]['content']=='SEED_PART' and 'UNVERIFIED_NEIGHBOR' not in str(citations)
    assert summary['source_mapping_status']=='UNKNOWN'


def test_length_limit_never_claims_truncated_out_sibling(db):
    row=rerank.expand_context([seed_row()],db,window_size=0,max_parent_length=12,include_source_members=True)[0]
    citations,_=build([row])
    assert all(c['content'] in row['content'] or c['content']==row['original_content'] for c in citations)
    assert all(c['chunk_id']!='sibling' for c in citations if 'SIBLING_GOLD' not in row['content'])


@pytest.mark.parametrize('enabled', ['false','true'])
def test_actual_fake_provider_messages_contain_only_builder_citations(db,monkeypatch,enabled):
    from app.services import grounded_generation, observability
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED',enabled)
    monkeypatch.setenv('LANGFUSE_ENABLED','true' if enabled == 'true' else 'false')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED','false')
    monkeypatch.setattr(observability,'_client',None)
    monkeypatch.setattr(observability,'_attempted',True)
    frozen=rerank.expand_context([seed_row()],db,window_size=1,include_source_members=True)
    a,_=build(frozen,'ORIGINAL_CONTENT'); b,_=build(frozen,'EXPANDED_CONTENT')
    response=SimpleNamespace(id='fake',model='fake',usage=None,choices=[SimpleNamespace(message=SimpleNamespace(content='synthetic [1]'),finish_reason='stop')])
    create=AsyncMock(return_value=response)
    llm=SimpleNamespace(model='fake',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    async def run():
        await grounded_generation.generate_grounded_candidate(llm,[{'role':'user','content':'synthetic question'}],a)
        await grounded_generation.generate_grounded_candidate(llm,[{'role':'user','content':'synthetic question'}],b)
    asyncio.run(run())
    assert create.await_count==2
    calls = [call.kwargs for call in create.await_args_list]
    request_a,request_b=[call['messages'] for call in calls]
    assert {k:v for k,v in calls[0].items() if k != 'messages'} == {k:v for k,v in calls[1].items() if k != 'messages'}
    assert 'SEED_PART' in str(request_a) and 'SIBLING_GOLD' not in str(request_a)
    assert 'SEED_PART' in str(request_b) and 'SIBLING_GOLD' in str(request_b)
    assert all(c['content'] in str(request_b) for c in b)


def test_corrupt_member_mapping_fails_closed_instead_of_forging_evidence(db):
    frozen=rerank.expand_context([seed_row()],db,window_size=1,include_source_members=True)
    corrupt=copy.deepcopy(frozen)
    group=corrupt[0]['context_group']
    member=next(m for m in group['source_members'] if m['id']=='sibling')
    member['content']='TAMPERED_CONTENT'
    citations,summary=build(corrupt)
    assert 'TAMPERED_CONTENT' not in str(citations)
    assert 'SIBLING_GOLD' not in str(citations)
    assert [c['chunk_id'] for c in citations]==['seed']
    assert summary['source_mapping_status']=='UNKNOWN'


def test_incomplete_mapping_never_invents_spreadsheet_locator(db):
    frozen=rerank.expand_context([seed_row()],db,window_size=1,include_source_members=True)
    citations,_=build(frozen)
    for c in citations:
        # PDF members have no workbook metadata; a neighboring block does not imply a cell.
        assert c['source_metadata'].get('sheet') is None
        assert c['source_metadata'].get('cell') is None


def test_builder_does_not_add_input_clipping_beyond_expander(db):
    row=db.get(DocumentChunk,'sibling')
    row.content='NEIGHBOR_START:' + 'x'*6000 + ':NEIGHBOR_END'
    db.add(row); db.commit()
    expanded=rerank.expand_context([seed_row()],db,window_size=0,max_parent_length=10000,include_source_members=True)
    citations,summary=build(expanded)
    neighbor=next(c for c in citations if c['chunk_id']=='sibling')
    assert neighbor['content']==row.content and 'NEIGHBOR_END' in neighbor['content']
    assert summary['context_token_count'] is None and summary['token_count_status']=='UNKNOWN'


def test_duplicate_id_with_conflicting_source_location_is_unknown():
    first={'id':'same','file_id':'first-file','page_number':7,'file_name':'first.pdf','content':'SAME_TEXT'}
    second={'id':'same','file_id':'other-file','page_number':8,'file_name':'other.pdf','content':'SAME_TEXT'}
    citations,summary=build([first,second])
    assert len(citations)==1
    assert summary['source_mapping_status']=='UNKNOWN'


def test_office_index_page_is_not_fabricated_as_source_pagination(db):
    from app.services.context_builder import source_member
    row = seed_row()
    row['file_name'] = 'source.docx'
    member = source_member(db.get(DocumentChunk, 'seed'), row, 'seed')
    assert member['page_number'] is None
    assert member['indexed_page_number'] == 7
    assert member['source_metadata'].get('page') is None


def test_context_trace_controlled_statuses_remain_readable():
    from app.services.observability import sanitize
    values = {'arm': 'B', 'source_mapping_status': 'VERIFIED',
              'token_count_status': 'UNKNOWN', 'token_count_method': 'model_tokenizer_not_configured',
              'context_strategy': 'EXPANDED_CONTENT', 'delivery_status': 'PREPARED_FOR_PROVIDER'}
    assert sanitize(values, final_export=True) == values


@pytest.mark.parametrize('enabled', ['false','true'])
def test_chat_path_sends_actual_expanded_members_to_provider(db, monkeypatch, enabled):
    from app.schemas import Message, MessageRole
    from app.services import chat, observability, pdf_table_grounding, spreadsheet_grounding
    monkeypatch.setenv('RAG_GROUNDED_GENERATION','true')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED',enabled)
    monkeypatch.setenv('LANGFUSE_ENABLED','true' if enabled == 'true' else 'false')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED','false')
    monkeypatch.setattr(observability,'_client',None)
    monkeypatch.setattr(observability,'_attempted',True)
    class FakeSession:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): return False
    monkeypatch.setattr(chat,'Session',FakeSession)
    monkeypatch.setattr(spreadsheet_grounding,'prepare_spreadsheet_grounding',lambda *a:None)
    monkeypatch.setattr(pdf_table_grounding,'prepare_pdf_table_grounding',lambda *a:None)
    frozen=rerank.expand_context([seed_row()],db,window_size=1,include_source_members=True)
    create=AsyncMock(return_value=SimpleNamespace(id='fake',model='fake',usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='synthetic answer [1]'),finish_reason='stop')]))
    service=chat.ChatService.__new__(chat.ChatService)
    service.llm_service=SimpleNamespace(model='fake',_get_client=lambda:SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    service._retrieve_with_new_service=AsyncMock(return_value=(copy.deepcopy(frozen),{'retrieval_status':'SUCCESS'}))
    async def run(strategy):
        monkeypatch.setenv('RAG_CHAT_CONTEXT_STRATEGY',strategy)
        return [event async for event in service._chat_stream_normal([Message(role=MessageRole.USER, content='synthetic question')],
            ['kb'],5,0,None,False,0,None,context_window=1)]
    old=asyncio.run(run('ORIGINAL_CONTENT'))
    new=asyncio.run(run('EXPANDED_CONTENT'))
    assert create.await_count==2
    req_a,req_b=[call.kwargs['messages'] for call in create.await_args_list]
    assert 'SIBLING_GOLD' not in str(req_a) and 'SIBLING_GOLD' in str(req_b)
    old_c=next(data['citations'] for name,data in old if name=='rag_result')
    new_c=next(data['citations'] for name,data in new if name=='rag_result')
    assert [c['chunk_id'] for c in old_c]==['seed']
    sibling=next(c for c in new_c if c['chunk_id']=='sibling')
    assert sibling['content']=='SIBLING_GOLD' and sibling['fileId']=='file'
    assert sibling['source_metadata']['page']==8 and sibling['location']=='第 8 页'
    assert len({c['chunk_id'] for c in new_c})==len(new_c)
    assert old[-1][0]=='done' and new[-1][0]=='done'


def test_observation_on_off_does_not_change_provider_request_or_sse(db, monkeypatch):
    from app.schemas import Message, MessageRole
    from app.services import chat, observability, pdf_table_grounding, spreadsheet_grounding
    monkeypatch.setenv('RAG_GROUNDED_GENERATION','true')
    monkeypatch.setattr(observability,'_client',None)
    monkeypatch.setattr(observability,'_attempted',True)
    class FakeSession:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): return False
    monkeypatch.setattr(chat,'Session',FakeSession)
    monkeypatch.setattr(spreadsheet_grounding,'prepare_spreadsheet_grounding',lambda *a:None)
    monkeypatch.setattr(pdf_table_grounding,'prepare_pdf_table_grounding',lambda *a:None)
    frozen=rerank.expand_context([seed_row()],db,window_size=1,include_source_members=True)
    async def run(enabled):
        monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED',enabled)
        monkeypatch.setenv('LANGFUSE_ENABLED','true' if enabled=='true' else 'false')
        monkeypatch.setenv('RAG_CHAT_CONTEXT_STRATEGY','EXPANDED_CONTENT')
        create=AsyncMock(return_value=SimpleNamespace(id='fake',model='fake',usage=None,
            choices=[SimpleNamespace(message=SimpleNamespace(content='synthetic answer [1]'),finish_reason='stop')]))
        service=chat.ChatService.__new__(chat.ChatService)
        service.llm_service=SimpleNamespace(model='fake',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        service._retrieve_with_new_service=AsyncMock(return_value=(copy.deepcopy(frozen),{'retrieval_status':'SUCCESS'}))
        events=[event async for event in service._chat_stream_normal([Message(role=MessageRole.USER,content='synthetic question')],
            ['kb'],5,0,None,False,0,None,context_window=1)]
        assert create.await_count==1
        return events, create.await_args.kwargs
    off=asyncio.run(run('false')); on=asyncio.run(run('true'))
    assert on==off

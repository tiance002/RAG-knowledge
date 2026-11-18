"""Regression for real parents being duplicated as legacy neighbour context."""
import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import Session, SQLModel, create_engine
from app.models import DocumentChunk, FileDocument, KnowledgeBase
from app.services.rerank import expand_context
from app.services.context_builder import build_chat_context


@pytest.fixture
def graph(tmp_path,monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED','false')
    monkeypatch.setenv('RAG_OBSERVATION_LOG',str(tmp_path/'events.jsonl'))
    engine=create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb',name='fixture',embedding_model='none',vlm_model='none'))
        session.add(FileDocument(id='f',kb_id='kb',name='source.pdf',local_path='synthetic',size=0))
        session.add_all([
            DocumentChunk(id='p',file_id='f',chunk_type='parent',content='intro alpha beta tail',original_index=0,
                          source_metadata_json=json.dumps({'source_sha256':'frozen-source','page':3,'table_bbox':[1,2,3,4]}),page_number=3),
            DocumentChunk(id='a',file_id='f',chunk_type='child',parent_id='p',content='alpha',original_index=1),
            DocumentChunk(id='b',file_id='f',chunk_type='child',parent_id='p',content='beta',original_index=2),
        ])
        session.commit();yield session
    engine.dispose()


def hit(cid):
    return dict(id=cid,file_id='f',kb_id='kb',file_name='source.pdf',chunk_type='child',score=.8)


def formatter(row,text):
    return dict(chunk_id=row['id'],content=text,fileId=row['file_id'],source_metadata=row.get('source_metadata',{}))


def test_public_expander_uses_one_real_parent_and_keeps_all_hits(graph):
    rows=expand_context([hit('a'),hit('b')],graph,window_size=1,include_source_members=True,
                        allowed_kb_ids=['kb'],max_context_chars=24)
    assert len(rows)==1 and rows[0]['content']=='intro alpha beta tail'
    assert rows[0]['matched_child_ids']==['a','b']
    assert [(m['id'],m['relation']) for m in rows[0]['context_group']['source_members']]==[('p','parent')]
    citations,summary=build_chat_context(rows,formatter)
    assert summary['expansion_delivery_status']=='DELIVERED'
    assert summary['seed_evidence_ids']==['a','b']
    assert citations[0]['parent_chunk_id']=='p' and citations[0]['seed_child_ids']==['a','b']
    assert citations[0]['content_hash']==hashlib.sha256(b'intro alpha beta tail').hexdigest()
    assert citations[0]['source_metadata']['source_sha256']=='frozen-source'
    assert citations[0]['source_metadata']['table_bbox']==[1,2,3,4]


def test_public_expander_preserves_whole_seeds_when_budget_cannot_fit_parent(graph):
    rows=expand_context([hit('a'),hit('b')],graph,window_size=0,include_source_members=True,
                        allowed_kb_ids=['kb'],max_context_chars=0)
    assert rows[0]['content']=='alpha\nbeta'
    assert rows[0]['delivery_budget']['mandatory_overrun']
    assert rows[0]['context_group']['omitted_seed_ids']==[]
    citations,summary=build_chat_context(rows,formatter)
    assert {c['chunk_id'] for c in citations}=={'a','b'}
    assert summary['expansion_delivery_status']=='PARTIAL'


@pytest.mark.parametrize('window,allowance,want_neighbor,expected_status',[
    (1,14,True,'PARTIAL'),(0,100,False,'DELIVERED'),(1,0,False,'PARTIAL'),(1,100,False,'DELIVERED')])
def test_parent_uses_whole_body_or_budget_fallback_without_cross_source_neighbors(
        graph,window,allowance,want_neighbor,expected_status):
    graph.add(DocumentChunk(id='header',file_id='f',chunk_type='child',parent_id='p',
        content='intro',original_index=0,source_metadata_json=json.dumps({'units':'CNY'})))
    # Reused parent ID in another file and another parent in the same file
    # must never broaden the authorized neighborhood after fallback.
    graph.add(FileDocument(id='other-file',kb_id='other-kb',name='other.txt',local_path='synthetic',size=0))
    graph.add_all([
        DocumentChunk(id='foreign',file_id='other-file',chunk_type='child',parent_id='p',content='foreign secret',original_index=0),
        DocumentChunk(id='different-parent',file_id='f',chunk_type='child',parent_id='elsewhere',content='another section',original_index=0),
    ])
    graph.commit()
    rows=expand_context([hit('a')],graph,window_size=window,
        max_context_chars=allowance,allowed_kb_ids=['kb'],include_source_members=True)
    citations,summary=build_chat_context(rows,formatter)
    assert 'alpha' in rows[0]['content']
    relations=[m['relation'] for m in rows[0]['context_group']['source_members']]
    assert ('neighbor' in relations) is want_neighbor
    assert 'foreign secret' not in rows[0]['content'] and 'another section' not in rows[0]['content']
    assert rows[0]['context_group']['omitted_seed_ids']==[]
    assert summary['expansion_delivery_status']==expected_status
    if want_neighbor:
        assert [c['chunk_id'] for c in citations]==['header','a']
        assert citations[0]['source_metadata']['units']=='CNY'
        assert [m['relation'] for m in rows[0]['context_group']['source_members']]==['neighbor','seed']


def test_normal_qa_parent_expansion_does_not_depend_on_neighbour_window(graph):
    from app.services.chat import ChatService
    service=ChatService.__new__(ChatService)
    service.retrieval_service=SimpleNamespace(search=AsyncMock(return_value=(
        [hit('a'),hit('b')],{'retrieval_status':'SUCCESS'})))
    rows,metrics=asyncio.run(service._retrieve_with_new_service(graph,'q',['kb'],top_k=50,
        score_threshold=0,search_mode='hybrid',vector_weight=.7,bm25_weight=.3,
        rerank_enabled=False,rerank_score_threshold=0,rerank_model_id=None,
        rerank_top_k=20,context_window=0))
    assert len(rows)==1 and rows[0]['parent_chunk_id']=='p'
    assert rows[0]['content']=='intro alpha beta tail' and metrics['expanded']


def test_agent_tool_parent_expands_with_zero_neighbour_window(graph,monkeypatch):
    from app.tools import retrieval_tool
    service=SimpleNamespace(search=AsyncMock(return_value=([hit('a'),hit('b')],{'retrieval_status':'SUCCESS'})))
    monkeypatch.setattr(retrieval_tool,'engine',graph.bind)
    monkeypatch.setattr(retrieval_tool,'get_retrieval_service',lambda:service)
    result=json.loads(asyncio.run(retrieval_tool.search_knowledge_base.coroutine(
        query='q',kb_ids=['kb'],context_window=0)))
    assert result['expansion_delivery_status']=='DELIVERED'
    assert len(result['citations'])==1 and result['citations'][0]['chunk_id']=='p'
    assert result['citations'][0]['seed_child_ids']==['a','b']


@pytest.mark.parametrize('multi',[False,True])
def test_recall_zero_window_exposes_actual_parent_source_id(graph,monkeypatch,multi):
    from app.api import recall
    from app.schemas import RecallRequest
    service=SimpleNamespace(search=AsyncMock(return_value=([hit('a'),hit('b')],{'retrieval_status':'SUCCESS'})))
    monkeypatch.setattr(recall,'get_retrieval_service',lambda:service)
    data=RecallRequest(query='q',context_window=0)
    response=asyncio.run(recall.recall_test_multi(data=data,kb_ids=['kb'],session=graph)
                         if multi else recall.recall_test(kb_id='kb',data=data,session=graph))
    assert response.data.expansion_delivery_status=='DELIVERED'
    row=response.data.results[0]
    assert row.content=='intro alpha beta tail'
    assert row.source_metadata['source_type']=='context_group'
    assert row.source_metadata['members'][0]['chunk_id']=='p'
    assert row.source_metadata['members'][0]['page_number']==3

"""Synthetic v2 graph contract; no providers or production data used."""
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy import event
from app.models import DocumentChunk, FileDocument, KnowledgeBase
from app.services.parent_context import expand_parent_context
from app.services.context_builder import build_chat_context


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events.jsonl'))
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb', name='test', embedding_model='none', vlm_model='none'))
        s.add_all([FileDocument(id=f, kb_id='kb', name=f+'.txt', local_path='synthetic', size=0) for f in ['f','other']])
        s.add_all([
            DocumentChunk(id='p', file_id='f', chunk_type='parent', content='intro alpha beta tail'),
            DocumentChunk(id='a', file_id='f', chunk_type='child', parent_id='p', content='alpha', original_index=1),
            DocumentChunk(id='b', file_id='f', chunk_type='child', parent_id='p', content='beta', original_index=2),
            DocumentChunk(id='p2', file_id='other', chunk_type='parent', content='gamma body'),
            DocumentChunk(id='c', file_id='other', chunk_type='child', parent_id='p2', content='gamma'),
            DocumentChunk(id='plain', file_id='f', content='ordinary', original_index=10),
        ])
        s.commit()
        yield s
    engine.dispose()


def hit(cid, file='f', **extra):
    return dict(id=cid, file_id=file, file_name=file+'.txt', kb_id='kb', score=.7, **extra)


def expand(db, rows, **kw):
    return expand_parent_context(rows, db, include_source_members=True, allowed_kb_ids=['kb'], **kw)


def test_batch_unique_parent_and_all_hits_citations(db):
    queries=[]
    def capture(conn,cursor,statement,*args): queries.append(statement)
    event.listen(db.bind,'before_cursor_execute',capture)
    rows=expand(db,[hit('a',rerank_score=.91), hit('b',rerank_score=.84), hit('c','other',rerank_score=.78)],window_size=0)
    event.remove(db.bind,'before_cursor_execute',capture)
    assert len(rows)==2 and rows[0]['matched_child_ids']==['a','b']
    assert rows[0]['content']=='intro alpha beta tail'
    assert rows[0]['score_origin']=='best_seed_rerank'
    assert len([q for q in queries if q.startswith('SELECT')])==3
    citations, summary=build_chat_context(rows,lambda r,t:dict(chunk_id=r['id'],fileId=r['file_id'],score=r.get('score'),rerank_score=r.get('rerank_score')))
    assert [c['chunk_id'] for c in citations]==['p','p2']
    assert citations[0]['seed_child_ids']==['a','b'] and citations[0]['parent_chunk_id']=='p'
    assert citations[0]['score'] is None and citations[0]['rerank_score'] is None
    assert summary['seed_evidence_ids']==['a','b','c']
    assert expand(db,[hit('a')],window_size=0)[0]['content']=='intro alpha beta tail'


@pytest.mark.parametrize('parent', ['missing','p2','plain','chunk_1'])
def test_invalid_parent_keeps_verified_seed(db,parent):
    a=db.get(DocumentChunk,'a'); a.parent_id=parent; db.commit()
    row=expand(db,[hit('a')],window_size=1)[0]
    assert row['content']=='alpha' and row['parent_chunk_id'] is None
    assert row['source_mapping_status']=='UNKNOWN'
    assert row['context_group']['degradation_reason']=='INVALID_PARENT_LINK'


def test_scope_verified_against_sql(db):
    assert expand(db,[hit('c')])==[]
    assert expand_parent_context([hit('a')],db,allowed_kb_ids=['other'])==[]


def test_parent_limit_preserves_all_seeds_and_total_budget(db):
    rows=expand(db,[hit('a'),hit('b')],window_size=1,max_parent_length=4,max_context_chars=20)
    assert rows[0]['content']=='alpha\nbeta'
    assert rows[0]['context_group']['truncation_status']=='CHAR_LIMIT_APPLIED'
    rows=expand(db,[hit('a'),hit('b')],max_context_chars=1)
    assert rows[0]['content']==''
    assert rows[0]['context_group']['omitted_seed_ids']==['a','b']
    assert rows[0]['expansion_delivery_status']=='OMITTED'


def test_plain_legacy_and_neighbor_heading_boundary(db):
    db.add(DocumentChunk(id='n', file_id='f',content='neighbor',original_index=11,heading_text='different'))
    plain=db.get(DocumentChunk,'plain'); plain.heading_text='section'; db.commit()
    row=expand(db,[hit('plain')],window_size=1)[0]
    assert row['content']=='ordinary'
    assert row['context_group']['seed_evidence_ids']==['plain']


def test_bad_parent_content_never_loses_seed(db):
    p=db.get(DocumentChunk,'p'); p.content='not the seed'; db.commit()
    row=expand(db,[hit('a')])[0]
    assert row['content']=='alpha'
    assert row['context_group']['degradation_reason']=='SEED_NOT_IN_PARENT'


def test_best_seed_original_identity_and_neighbor_never_consumes_other_seed(db):
    rows=expand(db,[hit('a',rerank_score=.1),hit('b',rerank_score=.9)],window_size=0)
    assert rows[0]['id']=='b' and rows[0]['original_content']=='beta'
    a=db.get(DocumentChunk,'a'); a.parent_id=None
    b=db.get(DocumentChunk,'b'); b.parent_id=None
    db.commit()
    rows=expand(db,[hit('a'),hit('b')],window_size=1,max_context_chars=13)
    assert [r['content'] for r in rows]==['alpha','beta']
    assert rows[1]['seed_child_ids']==['b']


def test_parent_cannot_starve_later_true_seed(db):
    rows=expand(db,[hit('a'),hit('c','other')],window_size=0,max_context_chars=21)
    assert rows[0]['content']=='alpha' and rows[1]['content']=='gamma body'


def test_final_request_budget_blocks_before_provider(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services.grounded_generation import generate_grounded_candidate
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(),'GROUNDED_REQUEST_MAX_CHARS',10)
    create=AsyncMock()
    llm=SimpleNamespace(model='fake',_get_client=lambda:SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    with pytest.raises(ValueError,match='FINAL_REQUEST_OVER_CHAR_BUDGET'):
        asyncio.run(generate_grounded_candidate(llm,[{'role':'user','content':'query'}],[{'content':'seed'}]))
    create.assert_not_awaited()


def test_chat_child_parent_expansion_with_zero_neighbor_window(db):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services.chat import ChatService
    service=ChatService.__new__(ChatService)
    service.retrieval_service=SimpleNamespace(search=AsyncMock(return_value=(
        [{**hit('a'), 'chunk_type':'child','content':'alpha'}, {**hit('b'), 'chunk_type':'child','content':'beta'}],
        {'retrieval_status':'SUCCESS'})))
    rows, metrics=asyncio.run(service._retrieve_with_new_service(db,'question',['kb'],
        top_k=5,score_threshold=0,search_mode='hybrid',vector_weight=.7,bm25_weight=.3,
        rerank_enabled=False,rerank_score_threshold=0,rerank_model_id=None,rerank_top_k=3,context_window=0))
    assert len(rows)==1 and rows[0]['content']=='intro alpha beta tail'
    assert rows[0]['seed_child_ids']==['a','b'] and metrics['expanded']


def test_expansion_diagnostics_are_readable_without_exposing_body():
    from app.services.observability import sanitize
    data=dict(seed_ids=['a','b'], parent_id='p', neighbor_ids=['n'], original_chars=100,
        delivered_chars=20, degradation_reason='PARENT_OVER_BUDGET',
        truncation_status='SEED_OMITTED_OVER_LIMIT', relation='parent', boundary_status='KNOWN')
    assert sanitize(data,final_export=True)==data
    assert sanitize({'content':'private body'})['content'] != 'private body'

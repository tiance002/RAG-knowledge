import pytest
from app.models import DocumentChunk, FileDocument, KnowledgeBase
from app.services import rerank
from test_e20_context_delivery import db, seed_row, build


def test_same_local_parent_never_crosses_file_or_kb(db):
    db.add(KnowledgeBase(id='other-kb', name='Other', embedding_model='none', vlm_model='none'))
    db.add_all([FileDocument(id='same-kb-file', kb_id='kb', name='other.txt', local_path='none', size=0), FileDocument(id='other-file', kb_id='other-kb', name='other.txt', local_path='none', size=0)])
    db.add_all([DocumentChunk(id='wrong-file', file_id='same-kb-file', parent_id='parent', original_index=0, content='WRONG_FILE'), DocumentChunk(id='wrong-kb', file_id='other-file', parent_id='parent', original_index=0, content='WRONG_KB')])
    db.commit()
    out=rerank.expand_context([seed_row()], db, window_size=0, include_source_members=True)
    assert 'SIBLING_GOLD' in out[0]['content']
    assert 'WRONG_FILE' not in out[0]['content'] and 'WRONG_KB' not in out[0]['content']


def test_independent_hit_not_swallowed_and_seed_survives_budget(db):
    sibling={**seed_row(), 'id':'sibling', 'content':'SIBLING_GOLD'}
    out=rerank.expand_context([seed_row(), sibling], db, window_size=1, max_parent_length=100)
    assert {x['id'] for x in out} == {'seed','sibling'}
    db.get(DocumentChunk,'seed').content='X'*100
    db.commit()
    out=rerank.expand_context([sibling],db,window_size=0,max_parent_length=15)
    assert 'SIBLING_GOLD' in out[0]['content']


def test_partial_rerank_empty_keeps_unknown_candidate():
    class Service:
        def __init__(self): self.n=0
        def rerank(self, query, texts, model=None):
            self.n+=1
            return [{'index':0,'score':.9}] if self.n==1 else []
    rows=[{'id':'a','content':'甲'*20,'score':.7}, {'id':'b','content':'乙'*20,'score':.1}]
    out=rerank.batch_rerank('q',rows,Service(),None,max_context_tokens=5,top_k=2,score_threshold=.5)
    assert {r['id'] for r in out} == {'a','b'}
    assert next(r for r in out if r['id']=='b')['rerank_score'] is None

def test_distinct_numeric_values_are_not_deduplicated():
    from app.services.retrieval import deduplicate_results
    rows=[{'id':'a','file_id':'f','kb_id':'k','content':'2023 revenue 100'}, {'id':'b','file_id':'f','kb_id':'k','content':'2023 revenue 001'}, {'id':'c','file_id':'f','kb_id':'k','content':'2023 revenue 100'}]
    assert [r['id'] for r in deduplicate_results(rows)] == ['a','b']

def test_recall_http_bodies_respect_file_scope_and_non_pdf_location(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    # Avoid importing unrelated API modules and their optional aiofiles dependency.
    import importlib.util
    spec = importlib.util.spec_from_file_location('isolated_recall_api', 'backend/app/api/recall.py')
    recall = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(__import__('sys').modules, spec.name, recall)
    spec.loader.exec_module(recall)
    from app.core.database import get_session
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    db.add(KnowledgeBase(id='other-kb', name='Other', embedding_model='none', vlm_model='none'))
    db.add(FileDocument(id='other-file', kb_id='other-kb', name='other.docx', local_path='none', size=0))
    db.add(DocumentChunk(id='foreign', file_id='other-file', parent_id='parent', original_index=0, content='SECRET_OTHER_KB'))
    db.commit()
    row={**seed_row(), 'file_name':'fixture.docx', 'page_number':9}
    fake=SimpleNamespace(search=AsyncMock(return_value=([row], {})))
    monkeypatch.setattr(recall,'get_retrieval_service',lambda: fake)
    app=FastAPI(); app.include_router(recall.router)
    app.dependency_overrides[get_session]=lambda: db
    client=TestClient(app)
    payload={'query':'evidence', 'context_window':1}
    for path, body in [('/knowledge-bases/kb/recall', payload), ('/recall/multi', {'data':payload, 'kb_ids':['kb','other-kb']})]:
        response=client.post(path,json=body)
        assert response.status_code == 200, response.text
        rows=response.json()['data']['results']
        assert rows and 'SEED_PART' in rows[0]['content'] and 'SIBLING_GOLD' in rows[0]['content']
        assert 'SECRET_OTHER_KB' not in response.text
        assert rows[0]['location'] == ''


def test_authorized_scope_rejects_forged_seed(db):
    row={**seed_row(),'kb_id':'other-kb'}
    assert rerank.expand_context([row],db,window_size=0,allowed_kb_ids=['kb']) == []
    row=seed_row()
    assert rerank.expand_context([row],db,window_size=0,allowed_kb_ids=['other-kb']) == []


def test_seed_larger_than_limit_is_explicitly_omitted(db):
    row=rerank.expand_context([seed_row()],db,window_size=0,max_parent_length=3,include_source_members=True)[0]
    assert row['content'] == '' and row['original_content'] == ''
    assert row['context_group']['truncation_status']=='SEED_OMITTED_OVER_LIMIT'

def test_hybrid_empty_or_failure_does_not_repeat_vector_call(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.services.retrieval import RetrievalService
    service=RetrievalService.__new__(RetrievalService)
    service._embed_query_for_kb=AsyncMock(return_value=[1.0])
    service.vector_store=SimpleNamespace(query=Mock(side_effect=RuntimeError('synthetic storage error')))
    service.bm25_index=SimpleNamespace(search=Mock(return_value=[]))
    results, metrics=asyncio.run(service.search(session=None,query='q',kb_ids=['kb'],search_mode='hybrid',log_retrieval=False))
    assert results == [] and metrics['retrieval_status'] == 'UNKNOWN'
    assert service.vector_store.query.call_count == 1


def test_successfully_scored_low_batch_is_filtered():
    class Service:
        def rerank(self,query,texts,model=None): return [{'index':0,'score':.1}]
    out=rerank.batch_rerank('q',[{'id':'a','content':'document','score':.9}],Service(),None,top_k=2,score_threshold=.5)
    assert out == []

def test_partial_rerank_api_delivers_unknown_score_on_both_routes(db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    import importlib.util, sys
    from app.core.database import get_session
    spec=importlib.util.spec_from_file_location('isolated_recall_rerank', 'backend/app/api/recall.py')
    recall=importlib.util.module_from_spec(spec); monkeypatch.setitem(sys.modules,spec.name,recall); spec.loader.exec_module(recall)
    rows=[{'id':'a','file_name':'a.pdf','content':'甲'*20,'score':.9}, {'id':'b','file_name':'b.pdf','content':'乙'*20,'score':.1}]
    monkeypatch.setattr(recall,'get_retrieval_service',lambda: SimpleNamespace(search=AsyncMock(return_value=(rows, {}))))
    class Service:
        def __init__(self,*args): self.calls=0
        def rerank(self,*args,**kwargs):
            self.calls+=1
            return [{'index':0,'score':.9}] if self.calls==1 else []
    monkeypatch.setattr(recall,'RerankService',Service)
    actual=rerank.batch_rerank
    monkeypatch.setattr(recall,'batch_rerank',lambda **kw: actual(**kw,max_context_tokens=5))
    app=FastAPI();app.include_router(recall.router);app.dependency_overrides[get_session]=lambda:db
    client=TestClient(app); payload={'query':'q','context_window':0,'rerank_enabled':True,'rerank_score_threshold':.5}
    for path,body in [('/knowledge-bases/kb/recall',payload),('/recall/multi',{'data':payload,'kb_ids':['kb']})]:
        response=client.post(path,json=body); assert response.status_code==200, response.text
        results={r['chunkId']:r for r in response.json()['data']['results']}
        assert results['a']['rerank_score']==.9
        assert results['b']['content']=='乙'*20 and results['b']['rerank_score'] is None and results['b']['rerank_status']=='UNKNOWN'

@pytest.mark.parametrize('vector,bm25,expected', [
    ([], [], 'NO_RESULTS'),
    (RuntimeError('private-vector'), [], 'UNKNOWN'),
    (RuntimeError('private-vector'), RuntimeError('private-sql'), 'RETRIEVAL_ERROR'),
    ([{'id':'c','score':.8}], RuntimeError('private-sql'), 'UNKNOWN'),
])
def test_hybrid_route_failures_have_distinct_statuses(vector,bm25,expected,capsys):
    import asyncio
    from unittest.mock import AsyncMock, Mock
    from types import SimpleNamespace
    from app.services.retrieval import RetrievalService
    service=RetrievalService.__new__(RetrievalService)
    service._embed_query_for_kb=AsyncMock(return_value=[1.])
    service.vector_store=SimpleNamespace(query=Mock(side_effect=vector if isinstance(vector,Exception) else None,return_value=vector if isinstance(vector,list) else None))
    service.bm25_index=SimpleNamespace(search=Mock(side_effect=bm25 if isinstance(bm25,Exception) else None,return_value=bm25 if isinstance(bm25,list) else None))
    service._enrich_results=lambda session,rows,kb_ids: rows
    rows,metrics=asyncio.run(service.search(None,'q',['kb'],search_mode='hybrid',log_retrieval=False))
    assert metrics['retrieval_status']==expected
    assert bool(rows)==(expected=='UNKNOWN' and isinstance(vector,list) and bool(vector))
    assert service.vector_store.query.call_count==1 and service.bm25_index.search.call_count==1
    assert 'private-vector' not in capsys.readouterr().out


def test_hybrid_postprocessing_failure_does_not_reissue_vector(capsys):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock,Mock
    from app.services.retrieval import RetrievalService
    service=RetrievalService.__new__(RetrievalService)
    service._embed_query_for_kb=AsyncMock(return_value=[1.])
    service.vector_store=SimpleNamespace(query=Mock(return_value=[{'id':'c','score':.8}]))
    service.bm25_index=SimpleNamespace(search=Mock(return_value=[]))
    service._enrich_results=Mock(side_effect=ValueError('sensitive-sql'))
    rows,metrics=asyncio.run(service.search(None,'q',['kb'],search_mode='hybrid',log_retrieval=False))
    assert rows==[] and metrics['retrieval_status']=='RETRIEVAL_ERROR'
    assert service.vector_store.query.call_count==1 and service._embed_query_for_kb.await_count==1
    assert 'sensitive-sql' not in capsys.readouterr().out

def test_recall_unknown_rerank_is_not_silent_or_retried(db,monkeypatch,capsys):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock,Mock
    from types import SimpleNamespace
    import importlib.util,sys,httpx
    from app.core.database import get_session
    spec=importlib.util.spec_from_file_location('isolated_recall_timeout', 'backend/app/api/recall.py')
    recall=importlib.util.module_from_spec(spec);monkeypatch.setitem(sys.modules,spec.name,recall);spec.loader.exec_module(recall)
    row={'id':'seed','file_name':'fixture.pdf','content':'source','score':.9}
    monkeypatch.setattr(recall,'get_retrieval_service',lambda:SimpleNamespace(search=AsyncMock(return_value=([row],{}))))
    failed=Mock(side_effect=httpx.ReadTimeout('secret-provider-message'))
    monkeypatch.setattr(recall,'batch_rerank',failed)
    app=FastAPI();app.include_router(recall.router);app.dependency_overrides[get_session]=lambda:db
    client=TestClient(app);payload={'query':'q','context_window':0,'rerank_enabled':True}
    response=client.post('/knowledge-bases/kb/recall',json=payload)
    assert response.status_code==500 and 'secret-provider-message' not in response.text
    assert failed.call_count==1 and 'secret-provider-message' not in capsys.readouterr().out

@pytest.mark.parametrize('mode',['vector','hybrid'])
def test_stale_foreign_vector_hit_never_exposes_body(db,mode):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock,Mock
    from app.services.retrieval import RetrievalService
    db.add(KnowledgeBase(id='foreign-kb', name='Foreign', embedding_model='none', vlm_model='none'))
    db.add(FileDocument(id='foreign-file',kb_id='foreign-kb',name='foreign.txt',local_path='none',size=0))
    db.add(DocumentChunk(id='foreign-chunk',file_id='foreign-file',content='FOREIGN_SECRET',original_index=0))
    db.commit()
    service=RetrievalService.__new__(RetrievalService)
    service._embed_query_for_kb=AsyncMock(return_value=[1.])
    service.vector_store=SimpleNamespace(query=Mock(return_value=[{'id':'foreign-chunk','score':.8}]))
    service.bm25_index=SimpleNamespace(search=Mock(return_value=[]))
    rows,_=asyncio.run(service.search(db,'q',['kb'],search_mode=mode,log_retrieval=False))
    assert rows == [] and 'FOREIGN_SECRET' not in str(rows)

def test_normal_chat_expansion_exception_keeps_seed_and_marks_unknown(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock,Mock
    from app.services import chat
    service=chat.ChatService.__new__(chat.ChatService)
    seed=seed_row()
    service.retrieval_service=SimpleNamespace(search=AsyncMock(return_value=([seed],{'retrieval_status':'SUCCESS'})))
    monkeypatch.setattr(chat,'expand_context',Mock(side_effect=RuntimeError('synthetic exception')))
    rows,metrics=asyncio.run(service._retrieve_with_new_service(session=None,query='q',kb_ids=['kb'],top_k=5,score_threshold=0,search_mode='vector',vector_weight=.7,bm25_weight=.3,rerank_enabled=False,rerank_score_threshold=0,rerank_model_id=None,rerank_top_k=5,context_window=1))
    assert rows == [seed] and metrics['retrieval_status']=='UNKNOWN'
    assert metrics['expansion_delivery_status']=='SEED_ONLY'


def test_recall_expansion_exception_http_reports_seed_fallback(db,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock,Mock
    from types import SimpleNamespace
    import importlib.util,sys
    from app.core.database import get_session
    spec=importlib.util.spec_from_file_location('isolated_recall_expansion','backend/app/api/recall.py')
    recall=importlib.util.module_from_spec(spec);monkeypatch.setitem(sys.modules,spec.name,recall);spec.loader.exec_module(recall)
    seed=seed_row();monkeypatch.setattr(recall,'get_retrieval_service',lambda:SimpleNamespace(search=AsyncMock(return_value=([seed],{'retrieval_status':'SUCCESS'}))))
    monkeypatch.setattr(recall,'expand_context',Mock(side_effect=RuntimeError('private')))
    app=FastAPI();app.include_router(recall.router);app.dependency_overrides[get_session]=lambda:db
    client=TestClient(app);payload={'query':'q','context_window':1}
    for path,body in [('/knowledge-bases/kb/recall',payload),('/recall/multi',{'data':payload,'kb_ids':['kb']})]:
        response=client.post(path,json=body);assert response.status_code==200,response.text
        data=response.json()['data'];assert data['results'][0]['content']=='SEED_PART'
        assert data['retrieval_status']=='UNKNOWN' and data['expansion_delivery_status']=='SEED_ONLY'

def test_chat_sse_exposes_expansion_exception_seed_fallback(monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.schemas import Message,MessageRole
    from app.services import chat
    service=chat.ChatService.__new__(chat.ChatService)
    service.llm_service=None
    service._retrieve_with_new_service=AsyncMock(return_value=([seed_row()],{'retrieval_status':'UNKNOWN','expansion_delivery_status':'SEED_ONLY'}))
    class FakeSession:
        def __init__(self,*args): pass
        def __enter__(self): return self
        def __exit__(self,*args): return False
    monkeypatch.setattr(chat,'Session',FakeSession)
    async def run():
        return [event async for event in service._chat_stream_normal([Message(role=MessageRole.USER,content='q')],['kb'],5,0,None,False,0,None,context_window=1)]
    events=asyncio.run(run())
    rag=next(data for kind,data in events if kind=='rag_result')
    done=next(data for kind,data in events if kind=='done')
    assert rag['citations'][0]['content']=='SEED_PART' and rag['expansion_delivery_status']=='SEED_ONLY'
    assert done['retrieval_status']=='UNKNOWN' and done['expansion_delivery_status']=='SEED_ONLY'

def test_numeric_dedup_preserves_signed_values():
    from app.services.retrieval import deduplicate_results
    prefix='same source long revenue description ' * 8
    rows=[{'id':str(i),'file_id':'f','kb_id':'k','content':prefix+number} for i,number in enumerate(['100','-100','−100'])]
    assert [r['id'] for r in deduplicate_results(rows)] == ['0','1','2']

def test_verified_xlsx_neighbor_locator_survives_expansion(db,tmp_path,monkeypatch):
    from openpyxl import Workbook
    from app.services import parser
    from app.services.context_builder import build_chat_context
    from app.services.pdf_provenance import source_metadata
    monkeypatch.setattr(parser.settings,'PDF_AUDIT_DIR',str(tmp_path/'audit'))
    monkeypatch.setattr(parser.settings,'IMAGE_DIR',str(tmp_path/'images'))
    source=tmp_path/'actual.xlsx'
    book=Workbook();sheet=book.active;sheet.title='Quarter';sheet.append(['label','value'])
    for i in range(22): sheet.append([f'item {i}',i])
    book.save(source);book.close()
    parsed=parser.FileParser('temporary')._parse_excel(str(source))
    assert len(parsed)>=2
    file=db.get(FileDocument,'file');file.name=source.name;file.local_path=str(source)
    for index,chunk in enumerate(parsed[:2]):
        db.add(DocumentChunk(id=f'xlsx-{index}',file_id='file',original_index=20+index,
             content=chunk.content,page_number=chunk.page_number,content_type=chunk.content_type))
    db.commit()
    seed={'id':'xlsx-0','file_id':'file','file_name':source.name,'kb_id':'kb',
          'content':parsed[0].content,'score':.8,
          'source_metadata':source_metadata(source,parsed[0].content,parsed[0].page_number,parsed[0].content_type)}
    group=rerank.expand_context([seed],db,window_size=1,include_source_members=True,allowed_kb_ids=['kb'])
    citations,_=build_chat_context(group,lambda row,text:{'chunk_id':row['id'],'content':text,'fileId':row['file_id'],'source_metadata':row.get('source_metadata') or {}})
    neighbor=next(c for c in citations if c['chunk_id']=='xlsx-1')
    assert neighbor['source_metadata']['sheet']=='Quarter'
    assert neighbor['source_metadata']['row_start']==22 and neighbor['source_metadata']['row_end']==23
    assert neighbor['page_number'] is None

def test_recall_composite_pdf_exposes_member_pages_not_seed_page(db,monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from app.core.database import get_session
    import importlib.util,sys
    spec=importlib.util.spec_from_file_location('isolated_recall_pages','backend/app/api/recall.py')
    recall=importlib.util.module_from_spec(spec);monkeypatch.setitem(sys.modules,spec.name,recall);spec.loader.exec_module(recall)
    monkeypatch.setattr(recall,'get_retrieval_service',lambda:SimpleNamespace(search=AsyncMock(return_value=([seed_row()],{'retrieval_status':'SUCCESS'}))))
    app=FastAPI();app.include_router(recall.router);app.dependency_overrides[get_session]=lambda:db
    client=TestClient(app);payload={'query':'q','context_window':1}
    for path,body in [('/knowledge-bases/kb/recall',payload),('/recall/multi',{'data':payload,'kb_ids':['kb']})]:
        response=client.post(path,json=body);assert response.status_code==200,response.text
        row=response.json()['data']['results'][0];members=row['source_metadata']['members']
        assert row['location']=='' and row['source_metadata']['source_type']=='context_group'
        assert {'seed','sibling'} <= {m['chunk_id'] for m in members}
        assert {7,8} <= {m['page_number'] for m in members}
        assert all('content' not in member for member in members)

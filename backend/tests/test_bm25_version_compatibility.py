"""Published version compatibility for lexical retrieval, using isolated SQLite."""
from sqlmodel import Session, SQLModel, create_engine
from app.models import FileDocument, FileStatus, DocumentChunk, KnowledgeBase
from app.services.bm25 import BM25Index
from app.services.file_versions import inactive_file_ids

def test_superseded_postings_do_not_crowd_results_or_affect_statistics():
    engine=create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb',name='kb',embedding_model='',vlm_model=''))
        old=FileDocument(id='old',kb_id='kb',name='old',local_path='old',size=1,status=FileStatus.READY)
        new=FileDocument(id='new',kb_id='kb',name='new',local_path='new',size=1,status=FileStatus.FAILED,
                         active_build_id='published',supersedes_file_id='old')
        s.add_all([old,new]);s.flush()
        s.add_all([DocumentChunk(id='old1',file_id='old',content='apple apple apple'),
                   DocumentChunk(id='old2',file_id='old',content='apple apple'),
                   DocumentChunk(id='new1',file_id='new',content='apple banana'),
                   DocumentChunk(id='new2',file_id='new',content='orange')]);s.flush()
        idx=BM25Index()
        for chunk_id,text in [('old1','apple apple apple'),('old2','apple apple'),('new1','apple banana'),('new2','orange')]:
            idx.index_chunk(s,chunk_id,text,'kb')
        s.commit()
        assert inactive_file_ids(s,['kb']) == {'old'}
        hits=idx.search(s,'apple','kb',1)
        expected=idx._calculate_bm25(['apple'],{'apple':1},2,2,1.5,{'apple':1})
        assert len(hits)==1 and hits[0]['chunk_id']=='new1'
        assert hits[0]['bm25_score'] == expected
        new.deleted_at=new.created_at;s.add(new);s.commit()
        assert inactive_file_ids(s,['kb']) == set()
    engine.dispose()

def test_rebuild_does_not_index_preview_or_overwrite_prior_committed_terms():
    engine=create_engine('sqlite://');SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb',name='kb',embedding_model='',vlm_model=''))
        s.add_all([FileDocument(id='preview',kb_id='kb',name='p',local_path='p',size=1,status=FileStatus.PENDING_CONFIRM),
                   FileDocument(id='published',kb_id='kb',name='r',local_path='r',size=1,status=FileStatus.READY)])
        s.add_all([DocumentChunk(id='old-preview',file_id='preview',content='uncommitted replacement'),
                   DocumentChunk(id='fresh-preview',file_id='preview',content='privatepreview'),
                   DocumentChunk(id='visible',file_id='published',content='publishedtext')]);s.flush()
        idx=BM25Index();idx.index_chunk(s,'old-preview','previouslycommitted','kb');s.commit()
        assert idx.rebuild_kb_index(s,'kb') == 1
        assert idx.search(s,'privatepreview','kb') == []
        assert idx.search(s,'uncommitted','kb') == []
        assert idx.search(s,'previouslycommitted','kb')[0]['chunk_id']=='old-preview'
        assert idx.search(s,'publishedtext','kb')[0]['chunk_id']=='visible'
    engine.dispose()

def test_retrieval_excludes_orphan_before_topk_and_live_statistics():
    from app.services.retrieval import RetrievalService
    engine=create_engine('sqlite://');SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb',name='kb',embedding_model='',vlm_model=''))
        s.add(FileDocument(id='live',kb_id='kb',name='live',local_path='x',size=1,status=FileStatus.READY))
        s.add(DocumentChunk(id='valid',file_id='live',content='apple banana'));s.flush()
        idx=BM25Index();idx.index_chunk(s,'orphan','apple apple apple apple apple','kb')
        idx.index_chunk(s,'valid','apple banana','kb');s.commit()
        assert idx.search(s,'apple','kb',1)[0]['chunk_id'] == 'orphan'
        from sqlalchemy import event
        queries=[]
        def capture(conn, cursor, sql, args, *unused):
            if 'FROM bm25_postings' in sql:
                queries.append((sql, args))
        event.listen(engine,'before_cursor_execute',capture)
        try:
            hit=idx.search(s,'apple','kb',1,live_only=True)[0]
        finally:
            event.remove(engine,'before_cursor_execute',capture)
        sql,args=queries[0]
        plan=[row[3] for row in s.connection().exec_driver_sql('EXPLAIN QUERY PLAN '+sql,args)]
        assert any('SEARCH bm25_postings' in step and 'term=?' in step for step in plan),plan
        assert hit['chunk_id']=='valid'
        assert hit['bm25_score']==idx._calculate_bm25(['apple'],{'apple':1},2,1,2.0,{'apple':1})
        service=RetrievalService.__new__(RetrievalService);service.bm25_index=idx
        assert service._bm25_query_results(s,['apple'],['kb'],1)[0][0]['chunk_id']=='valid'
    engine.dispose()

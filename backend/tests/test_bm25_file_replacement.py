"""Synthetic atomic BM25 replacement equivalence and SQL statement evidence."""
import json
import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select
from app.models import BM25Posting, ChunkIndex, DocumentChunk, FileDocument, IndexStats, KnowledgeBase
from app.services.bm25 import BM25Index

def make_fixture():
    e=create_engine('sqlite://');SQLModel.metadata.create_all(e)
    idx=BM25Index()
    with Session(e) as s:
        s.add(KnowledgeBase(id='kb',name='kb',embedding_model='',vlm_model=''))
        s.add_all([FileDocument(id='target',kb_id='kb',name='f',local_path='x',size=1),
                   FileDocument(id='other',kb_id='kb',name='o',local_path='x',size=1)])
        for i in range(100): s.add(DocumentChunk(id=f'old-{i}',file_id='target',content=f'old token{i} shared'))
        s.add(DocumentChunk(id='other',file_id='other',content='shared keep'));s.flush()
        for i in range(100): idx.index_chunk(s,f'old-{i}',f'old token{i} shared','kb')
        idx.index_chunk(s,'other','shared keep','kb');s.commit()
    return e,idx

def execute(e,idx,bulk):
    rows=[dict(id=f'new-{i}',content=f'new token{i} shared',chunk_type='content') for i in range(100)]
    rows += [dict(id='parent',content='parent term',chunk_type='parent'),dict(id='empty',content='',chunk_type='content')]
    prepared=idx.prepare_file_index(rows)
    counter=[]
    def capture(conn,cursor,statement,params,context,executemany): counter.append(statement)
    event.listen(e,'before_cursor_execute',capture)
    try:
        with Session(e) as s:
            if bulk: removed=idx.begin_file_replacement(s,'target','kb')
            old=s.exec(select(DocumentChunk).where(DocumentChunk.file_id=='target')).all()
            for chunk in old:
                if not bulk: idx.delete_chunk_index(s,chunk.id,'kb')
                s.delete(chunk)
            s.flush()
            for row in rows: s.add(DocumentChunk(id=row['id'],file_id='target',content=row['content'],chunk_type=row['chunk_type']))
            s.flush()
            if bulk: idx.finish_file_replacement(s,'kb',removed,prepared)
            else:
                for row in rows: idx.index_chunk(s,row['id'],row['content'],'kb')
            s.commit()
    finally: event.remove(e,'before_cursor_execute',capture)
    return len(counter)

def snapshot(e):
    with Session(e) as s:
        st=s.get(IndexStats,'kb')
        chunks=sorted((r.chunk_id,r.doc_length,json.loads(r.terms)) for r in s.exec(select(ChunkIndex)).all())
        ids={r.id:r.chunk_id for r in s.exec(select(ChunkIndex)).all()}
        postings=sorted((ids[r.index_id],r.term,r.tf) for r in s.exec(select(BM25Posting)).all())
        return (st.total_docs,st.avg_doc_length,json.loads(st.term_doc_freq),chunks,postings)

def test_bulk_replacement_matches_incremental_with_far_fewer_sql_statements():
    a,idxa=make_fixture();b,idxb=make_fixture()
    baseline=execute(a,idxa,False);batched=execute(b,idxb,True)
    left,right=snapshot(a),snapshot(b)
    assert left[0]==right[0]==101 and left[1]==pytest.approx(right[1])
    assert left[2:]==right[2:]
    print(f'SQL_STATEMENTS incremental={baseline} batched={batched} rows=100')
    assert batched < baseline / 4
    a.dispose();b.dispose()

def test_batch_failure_rolls_back_every_posting_and_statistic():
    e,idx=make_fixture();before=snapshot(e)
    with pytest.raises(RuntimeError,match='abort'):
        with Session(e) as s:
            removed=idx.begin_file_replacement(s,'target','kb')
            idx.finish_file_replacement(s,'kb',removed,[])
            s.flush()
            raise RuntimeError('abort')
    assert snapshot(e)==before
    e.dispose()


def test_unchanged_document_frequency_preserves_existing_json_bytes():
    e, idx = make_fixture()
    before = snapshot(e)
    with Session(e) as s:
        raw = s.get(IndexStats, 'kb').term_doc_freq
        existing = s.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'target')).all()
        prepared = idx.prepare_file_index([dict(id=c.id, content=c.content) for c in existing])
        removed = idx.begin_file_replacement(s, 'target', 'kb')
        idx.finish_file_replacement(s, 'kb', removed, prepared)
        s.commit()
        assert s.get(IndexStats, 'kb').term_doc_freq == raw
    assert snapshot(e) == before
    e.dispose()

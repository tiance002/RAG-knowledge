"""Synthetic BM25 legacy-snapshot, transactional and ranking compatibility."""
import json

import pytest
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select
from app.models import BM25Posting, ChunkIndex, DocumentChunk, FileDocument, IndexStats
from app.services.bm25 import BM25Index


@pytest.fixture
def store():
    engine = create_engine('sqlite://')
    SQLModel.metadata.create_all(engine)
    index = BM25Index()
    with Session(engine) as session:
        for file_id, kb in [('target', 'kb'), ('other', 'kb'), ('alien', 'elsewhere')]:
            session.add(FileDocument(id=file_id, kb_id=kb, name=file_id, local_path='unused', size=1))
        for chunk_id, file_id, text in [('old', 'target', 'common alpha alpha'),
                                        ('other', 'other', 'common beta'),
                                        ('alien', 'alien', 'common foreign')]:
            session.add(DocumentChunk(id=chunk_id, file_id=file_id, content=text))
        session.flush()
        for chunk_id, text, kb in [('old', 'common alpha alpha', 'kb'),
                                   ('other', 'common beta', 'kb'),
                                   ('alien', 'common foreign', 'elsewhere')]:
            index.index_chunk(session, chunk_id, text, kb)
        session.commit()
    yield engine, index
    engine.dispose()


def replace(session, index, rows, incremental=False):
    prepared = index.prepare_file_index(rows)
    if incremental:
        index.delete_file_index(session, 'target', 'kb')
    else:
        removed = index.begin_file_replacement(session, 'target', 'kb')
    for obj in session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'target')).all():
        session.delete(obj)
    session.flush()
    for row in rows:
        session.add(DocumentChunk(id=row['id'], file_id='target', content=row['content'],
                                  chunk_type=row.get('chunk_type', 'content')))
    session.flush()
    if incremental:
        for row in rows:
            index.index_chunk(session, row['id'], row['content'], 'kb')
    else:
        index.finish_file_replacement(session, 'kb', removed, prepared)


def state(engine):
    with Session(engine) as session:
        stats = sorted((s.kb_id, s.total_docs, s.avg_doc_length, s.term_doc_freq) for s in session.exec(select(IndexStats)))
        chunks = sorted((c.id, c.file_id, c.content, c.chunk_type) for c in session.exec(select(DocumentChunk)))
        indices = sorted((i.id, i.kb_id, i.chunk_id, i.doc_length, i.terms) for i in session.exec(select(ChunkIndex)))
        postings = sorted((p.kb_id, p.index_id, p.term, p.tf) for p in session.exec(select(BM25Posting)))
        return stats, chunks, indices, postings


@pytest.mark.parametrize('legacy', ['{"common":2,"alpha":1}', '{"outdated":999}', '{malformed'])
def test_legacy_snapshot_is_not_selected_or_updated(store, legacy):
    engine, index = store
    with Session(engine) as session:
        session.get(IndexStats, 'kb').term_doc_freq = legacy
        session.commit()
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with Session(engine) as session:
            replace(session, index, [{'id':'fresh', 'content':'common rare rare'}])
            session.commit()
            assert index.search(session, 'rare', 'kb')[0]['chunk_id'] == 'fresh'
            index.index_chunk(session, 'fresh', 'common delta', 'kb')
            session.commit()
            index.delete_chunk_index(session, 'fresh', 'kb')
            session.commit()
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    for sql in statements:
        if sql.lstrip().startswith(('select', 'update')):
            assert 'index_stats.term_doc_freq' not in sql
            if sql.lstrip().startswith('update index_stats'):
                assert 'term_doc_freq' not in sql
    with Session(engine) as session:
        stats = session.get(IndexStats, 'kb')
        assert stats.term_doc_freq == legacy
        assert stats.total_docs == 1
        assert stats.avg_doc_length == pytest.approx(2)
        assert session.get(IndexStats, 'elsewhere').total_docs == 1
        assert index.search(session, 'foreign', 'elsewhere')


def test_empty_parent_only_repeated_replacement_and_last_delete(store):
    engine, index = store
    parent = [{'id':'parent','content':'parentonly secret','chunk_type':'parent'},
              {'id':'empty','content':'','chunk_type':'content'}]
    with Session(engine) as session:
        replace(session, index, parent)
        session.commit()
        assert session.get(IndexStats,'kb').total_docs == 1
        assert not index.search(session,'parentonly','kb')
        rows = [{'id':'fresh','content':'common rare rare'}]
        replace(session, index, rows)
        session.commit()
        replace(session, index, rows)
        session.commit()
        stats=session.get(IndexStats,'kb',populate_existing=True)
        assert stats.total_docs == 2
        assert stats.avg_doc_length == pytest.approx(2.5)
        index.delete_file_index(session,'target','kb')
        index.delete_file_index(session,'other','kb')
        session.commit()
        stats=session.get(IndexStats,'kb',populate_existing=True)
        assert stats.total_docs == 0 and stats.avg_doc_length == 0
        assert session.get(IndexStats,'elsewhere').total_docs == 1


@pytest.mark.parametrize('failure', ['posting_delete','index_delete','index_insert','posting_insert','commit'])
def test_each_failure_rolls_back_real_transaction(store, failure):
    engine,index=store
    before=state(engine)
    triggered=[]
    def inject(connection,cursor,statement,parameters,context,executemany):
        sql=' '.join(statement.lower().split())
        targets={'posting_delete':'delete from bm25_postings', 'index_delete':'delete from chunk_index',
                 'index_insert':'insert into chunk_index', 'posting_insert':'insert into bm25_postings'}
        if failure in targets and sql.startswith(targets[failure]):
            triggered.append(failure)
            raise RuntimeError('injected failure')
    def fail_commit(session):
        triggered.append('commit')
        raise RuntimeError('injected failure')
    event.listen(engine,'after_cursor_execute',inject)
    try:
        with pytest.raises(RuntimeError,match='injected failure'):
            with Session(engine) as session:
                if failure=='commit': event.listen(session,'before_commit',fail_commit)
                replace(session,index,[{'id':'fresh','content':'common rare'}])
                session.commit()
    finally:
        event.remove(engine,'after_cursor_execute',inject)
    assert triggered == [failure]
    assert state(engine)==before


def test_executemany_posting_mid_batch_failure_rolls_back(store):
    engine,index=store
    before=state(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TRIGGER fail_rare BEFORE INSERT ON bm25_postings WHEN NEW.term = 'rare' BEGIN SELECT RAISE(ABORT, 'posting failure'); END")
    with pytest.raises(Exception,match='posting failure'):
        with Session(engine) as session:
            replace(session,index,[{'id':'fresh','content':'common rare'}])
            session.commit()
    assert state(engine)==before


def test_bulk_and_incremental_rankings_and_posting_df_match(store):
    engine,index=store
    rows=[{'id':'z-tie','content':'common rare 中文'}, {'id':'a-tie','content':'common rare 中文'},
          {'id':'last','content':'common unusual English'}, {'id':'parent','content':'rare','chunk_type':'parent'}]
    with Session(engine) as session:
        replace(session,index,rows)
        session.commit()
        queries=['common','rare','common rare','unusual','中文 English','nonexistent']
        actual={q:index.search(session,q,'kb',top_k=2) for q in queries}
        frequencies={term:count for term,count in session.exec(
            select(BM25Posting.term, __import__('sqlalchemy').func.count()).where(BM25Posting.kb_id=='kb').group_by(BM25Posting.term))}
        assert frequencies['rare']==2 and frequencies['common']==4
        assert [x['chunk_id'] for x in actual['rare']]==['z-tie','a-tie']
        assert not index.search(session,'parent','kb')
        replace(session,index,rows,incremental=True)
        session.commit()
        for query, expected in actual.items():
            found=index.search(session,query,'kb',top_k=2)
            assert [x['chunk_id'] for x in found]==[x['chunk_id'] for x in expected]
            assert [x['bm25_score'] for x in found]==pytest.approx([x['bm25_score'] for x in expected])


def test_sqlite_commit_failure_from_deferred_constraint_rolls_back(store):
    from sqlalchemy.exc import IntegrityError
    engine, index = store
    with engine.begin() as connection:
        connection.exec_driver_sql('PRAGMA foreign_keys=ON')
        connection.exec_driver_sql('CREATE TABLE synthetic_parent (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE synthetic_child (id INTEGER REFERENCES synthetic_parent(id) DEFERRABLE INITIALLY DEFERRED)')
    before = state(engine)
    with pytest.raises(IntegrityError, match='FOREIGN KEY constraint failed'):
        with Session(engine) as session:
            replace(session, index, [{'id':'fresh', 'content':'common rare'}])
            session.connection().exec_driver_sql('INSERT INTO synthetic_child (id) VALUES (99)')
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise
    assert state(engine) == before


def test_real_publication_commit_failure_is_atomic_for_fresh_and_pooled_readers(tmp_path, monkeypatch):
    import sqlite3
    import chromadb
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError
    from app.models import FileIndexBuild, KnowledgeBase
    from app.services import index_builds
    from app.services.vector_store import VectorStoreService
    from app.tasks import dispatch

    db_path = tmp_path / 'publication.sqlite'
    engine = create_engine('sqlite:///' + db_path.as_posix(), connect_args={'check_same_thread': False})
    @event.listens_for(engine, 'connect')
    def enforce_constraints(dbapi_connection, record):
        dbapi_connection.execute('PRAGMA foreign_keys=ON')
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(index_builds, 'engine', engine)
    monkeypatch.setattr(dispatch, 'engine', engine)
    monkeypatch.setattr(dispatch, 'notify_task', lambda *args: None)
    monkeypatch.setattr(VectorStoreService, '_client', chromadb.EphemeralClient())
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='model', vlm_model=''))
        session.add(FileDocument(id='file', kb_id='kb', name='file', local_path=str(tmp_path/'absent'),
                                 size=0, desired_generation=1))
        session.add(DocumentChunk(id='legacy', file_id='file', content='original'))
        session.add(dispatch.FileTaskReceipt(task_id='task', file_id='file', active_file='file',
            operation='index', stage='index', status='QUEUED', target_generation=1, payload='{}'))
        session.commit()
        BM25Index().index_chunk(session, 'legacy', 'original', 'kb')
        session.commit()
        session.get(IndexStats, 'kb').term_doc_freq = '{"original":1}'
        session.commit()
    claim = dispatch.claim_task('task', 'index', 'worker')
    assert claim is not None
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE synthetic_parent (id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE synthetic_child (id INTEGER REFERENCES synthetic_parent(id) DEFERRABLE INITIALLY DEFERRED)')
        connection.exec_driver_sql('CREATE TRIGGER synthetic_commit_failure AFTER INSERT ON chunk_index BEGIN INSERT INTO synthetic_child (id) VALUES (99); END')
    before = state(engine)
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'legacy','content':'original','metadata':{'file_id':'file'}}], [[1.,0.]])
    with pytest.raises(IntegrityError, match='FOREIGN KEY constraint failed'):
        index_builds.publish_index_build(claim, [{'content':'replacement evidence'}], [[0.,1.]], 'model')
    # Separate sqlite3 connection cannot see an uncommitted pooled transaction.
    with sqlite3.connect(str(db_path)) as independent:
        assert independent.execute('SELECT id FROM document_chunks WHERE file_id=?', ('file',)).fetchall() == [('legacy',)]
        assert independent.execute('SELECT active_build_id FROM file_documents WHERE id=?', ('file',)).fetchone()[0] is None
    # The original pool's next Session must also see exactly the old projection.
    assert state(engine) == before
    with Session(engine) as session:
        assert session.get(FileDocument,'file').active_build_id is None
        assert session.get(dispatch.FileTaskReceipt,'task').status == 'RUNNING'
        assert session.exec(select(FileIndexBuild).where(FileIndexBuild.state=='STAGING')).first() is not None
    engine.dispose()

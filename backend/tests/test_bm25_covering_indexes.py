"""Read indexes preserve live eligibility, exact scores and SQLite tie order."""
from sqlalchemy import event
from sqlmodel import SQLModel, Session, create_engine

from app.core.schema_migrations import apply_additive_schema_migrations
from app.models import KnowledgeBase, FileDocument, FileStatus, DocumentChunk
from app.services.bm25 import BM25Index


def test_covering_migration_preserves_live_search_and_is_idempotent(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'index.db'))
    SQLModel.metadata.create_all(engine)
    with engine.begin() as c:
        c.exec_driver_sql('DROP INDEX ix_chunk_index_bm25_live')
        c.exec_driver_sql('DROP INDEX ix_document_chunks_bm25_scope')
    index = BM25Index()
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb', name='kb', embedding_model='', vlm_model=''))
        s.add_all([FileDocument(id='live', kb_id='kb', name='live', local_path='live', size=1, status=FileStatus.READY),
                   FileDocument(id='gone', kb_id='kb', name='gone', local_path='gone', size=1, status=FileStatus.READY)])
        s.flush()
        for ident, file_id, content, kind in [('a','live','apple banana','child'),
                ('b','live','apple banana','child'), ('c','live','banana orange','content'),
                ('p','live','apple apple','parent'), ('d','gone','apple apple apple','content')]:
            s.add(DocumentChunk(id=ident, file_id=file_id, content=content, chunk_type=kind))
            s.flush()
            index.index_chunk(s, ident, content, 'kb')
        index.index_chunk(s, 'orphan', 'apple apple apple', 'kb')
        gone = s.get(FileDocument, 'gone'); gone.deleted_at = gone.created_at
        s.add(gone); s.commit()
        queries = ['apple', 'banana apple apple', 'orange', 'missing']
        before = [index.search(s, q, 'kb', 10, live_only=True) for q in queries]
    apply_additive_schema_migrations(engine)
    apply_additive_schema_migrations(engine)
    statements = []
    def capture(conn, cursor, sql, params, *unused):
        if 'sum(chunk_index.doc_length)' in sql:
            statements.append((sql, params))
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with Session(engine) as s:
            assert [index.search(s, q, 'kb', 10, live_only=True) for q in queries] == before
            assert [x['chunk_id'] for x in before[0]] == ['a', 'b']
            sql, params = statements[0]
            plan = [r[3] for r in s.connection().exec_driver_sql('EXPLAIN QUERY PLAN ' + sql, params)]
            assert any('COVERING INDEX ix_chunk_index_bm25_live' in step for step in plan), plan
            assert any('COVERING INDEX ix_document_chunks_bm25_scope' in step for step in plan), plan
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
        engine.dispose()

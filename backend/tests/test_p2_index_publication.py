"""P2 publication SQL contracts; SQLite and vector substitutes, no providers."""
import json
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import DocumentChunk, FileDocument, FileIndexBuild, FileStatus, KnowledgeBase
from app.services import index_builds
from app.tasks import dispatch


@pytest.fixture
def database(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(index_builds, 'engine', engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='m', vlm_model='v', chunk_count=900))
        session.add(FileDocument(id='f', kb_id='kb', name='f', local_path='f', size=1, desired_generation=2))
        session.commit()
    yield engine
    engine.dispose()


def build_record(manifest=None):
    raw, digest = index_builds._manifest(manifest or {'vector_ids': ['child'], 'chunk_ids': ['parent', 'child']})
    return FileIndexBuild(build_id='active', file_id='f', kb_id='kb', generation=1,
                          task_id='t0', attempt_no=1, state='ACTIVE', manifest=raw,
                          manifest_hash=digest, chunk_count=2)


@pytest.mark.parametrize('kind', ['valid', 'malformed', 'missing'])
def test_active_retirement_skips_unused_sql_ids_and_preserves_manifest(database, kind):
    with Session(database) as session:
        doc = session.get(FileDocument, 'f')
        doc.active_build_id = 'active'
        session.add(doc)
        if kind != 'missing':
            build = build_record()
            if kind == 'malformed':
                build.manifest, build.manifest_hash = 'pre-existing malformed', 'unchanged'
            session.add(build)
        session.add(DocumentChunk(id='projection-only', file_id='f', content='not cleanup authority'))
        session.commit()
    sql = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        sql.append(statement.lower())
    event.listen(database, 'before_cursor_execute', capture)
    try:
        with Session(database) as session:
            before = session.get(FileIndexBuild, 'active')
            frozen = (before.manifest, before.manifest_hash, before.chunk_count) if before else None
            retired = index_builds.retire_file_builds(session, 'f')
            session.commit()
            assert retired == ([] if kind == 'missing' else ['active'])
            assert session.get(FileDocument, 'f').active_build_id is None
            if before:
                assert (before.manifest, before.manifest_hash, before.chunk_count) == frozen
                assert before.state == 'RETIRED' and before.retired_at is not None
    finally:
        event.remove(database, 'before_cursor_execute', capture)
    assert not any('from document_chunks' in statement for statement in sql)


@pytest.mark.parametrize('populated', [False, True])
def test_legacy_retirement_records_exact_projection_and_empty_is_noop(database, populated):
    with Session(database) as session:
        if populated:
            session.add(DocumentChunk(id='legacy-parent', file_id='f', content='parent', chunk_type='parent'))
            session.add(DocumentChunk(id='legacy-child', file_id='f', content='child', parent_id='legacy-parent'))
            session.commit()
        retired = index_builds.retire_file_builds(session, 'f')
        session.commit()
        assert bool(retired) == populated
        builds = session.exec(select(FileIndexBuild)).all()
        assert len(builds) == int(populated)
        if populated:
            record = builds[0]
            assert record.state == 'RETIRED' and record.chunk_count == 2
            assert set(json.loads(record.manifest)['vector_ids']) == {'legacy-parent', 'legacy-child'}
            assert record.manifest_hash == index_builds._manifest(json.loads(record.manifest))[1]
        assert index_builds.retire_file_builds(session, 'absent') == []


def test_active_retirement_remains_caller_transactional(database):
    with Session(database) as session:
        doc = session.get(FileDocument, 'f'); doc.active_build_id = 'active'
        session.add(doc); session.add(build_record()); session.commit()
        assert index_builds.retire_file_builds(session, 'f') == ['active']
        session.flush(); session.rollback()
        assert session.get(FileDocument, 'f').active_build_id == 'active'
        assert session.get(FileIndexBuild, 'active').state == 'ACTIVE'
        assert session.get(FileIndexBuild, 'active').retired_at is None


class VectorSubstitute:
    def validate_dimension(self, kb, vector):
        pass

    def add_documents(self, kb, documents, embeddings):
        self.documents, self.embeddings = documents, embeddings

    def verify_documents(self, kb, documents, embeddings, ids):
        assert documents == self.documents and embeddings == self.embeddings
        assert ids == [row['id'] for row in documents]


@pytest.mark.parametrize('complete', [True, False])
def test_publication_recount_exact_scope_and_final_fence_rollback(database, monkeypatch, complete):
    monkeypatch.setattr(index_builds, 'VectorStoreService', VectorSubstitute)
    monkeypatch.setattr(dispatch, 'guard_claim_in_session', lambda *args: True)
    monkeypatch.setattr(dispatch, 'complete_in_session', lambda *args: complete)
    with Session(database) as session:
        session.add(KnowledgeBase(id='other', name='other', embedding_model='m', vlm_model='v'))
        for identifier, kb, deleted in [('failed', 'kb', None), ('deleted', 'kb', datetime.utcnow()), ('outside', 'other', None)]:
            session.add(FileDocument(id=identifier, kb_id=kb, name=identifier, local_path='f', size=1,
                                     status=FileStatus.FAILED, deleted_at=deleted))
        session.commit()
        session.add(DocumentChunk(id='old', file_id='f', content='old'))
        for identifier in ['failed', 'deleted', 'outside']:
            session.add(DocumentChunk(id=identifier + '-parent', file_id=identifier, content='parent', chunk_type='parent'))
            session.add(DocumentChunk(id=identifier + '-child', file_id=identifier, content='child'))
        session.commit()
    claim = SimpleNamespace(file_id='f', task_id='t', target_generation=2, attempt_no=1)
    parent_id, child_id = str(uuid4()), str(uuid4())
    rows = [{'id':parent_id, 'content':'parent text', 'chunk_type':'parent'},
            {'id':child_id, 'content':'text', 'chunk_type':'child', 'parent_id':parent_id}]
    if complete:
        index_builds.publish_index_build(claim, rows, [[1., 0.]], 'm')
    else:
        with pytest.raises(RuntimeError, match='BUILD_CLAIM_LOST'):
            index_builds.publish_index_build(claim, rows, [[1., 0.]], 'm')
    with Session(database) as session:
        assert session.get(KnowledgeBase, 'kb').chunk_count == (4 if complete else 900)
        current = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).all()
        assert len(current) == (2 if complete else 1)
        builds = session.exec(select(FileIndexBuild)).all()
        assert sorted(build.state for build in builds) == (['ACTIVE', 'RETIRED'] if complete else ['STAGING'])

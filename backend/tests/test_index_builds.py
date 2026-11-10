from types import SimpleNamespace

import chromadb
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase
from app.services import index_builds
from app.services.bm25 import BM25Index
from app.services.vector_store import VectorStoreService
from app.tasks import dispatch


@pytest.fixture
def environment(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    client = chromadb.EphemeralClient()
    monkeypatch.setattr(index_builds, 'engine', engine)
    monkeypatch.setattr(VectorStoreService, '_client', client)
    monkeypatch.setattr(dispatch, 'guard_claim_in_session', lambda session, claim: claim.valid)
    monkeypatch.setattr(dispatch, 'complete_in_session', lambda session, claim, result: claim.valid)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='m', vlm_model='v'))
        session.add(FileDocument(id='f', kb_id='kb', name='f', local_path='f', size=1, desired_generation=1))
        session.add(DocumentChunk(id='old', file_id='f', content='old evidence'))
        session.commit()
        BM25Index().index_chunk(session, 'old', 'old evidence', 'kb')
        session.commit()
    VectorStoreService().add_documents('kb', [{'id':'old', 'content':'old evidence', 'metadata':{'file_id':'f'}}], [[1., 0.]])
    return engine


def claim(valid=True):
    return SimpleNamespace(file_id='f', task_id='t', target_generation=1, attempt_no=1, valid=valid)


def test_build_publication_and_failed_fence_preserve_old_projection(environment):
    with pytest.raises(RuntimeError, match='BUILD_CLAIM_LOST'):
        index_builds.publish_index_build(claim(False), [{'content':'staged evidence'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        assert [c.content for c in session.exec(select(DocumentChunk)).all()] == ['old evidence']
        assert session.get(FileDocument, 'f').active_build_id is None
        assert BM25Index().search(session, 'old', 'kb')
        assert session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'STAGING')).first()
    result = index_builds.publish_index_build(claim(), [{'content':'new evidence'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        chunks = session.exec(select(DocumentChunk)).all()
        assert [c.content for c in chunks] == ['new evidence']
        assert session.get(FileDocument, 'f').active_build_id == result['build_id']
        assert BM25Index().search(session, 'new', 'kb')
        assert not BM25Index().search(session, 'old', 'kb')
        assert session.get(FileIndexBuild, result['build_id']).state == 'ACTIVE'
        retired = session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'RETIRED')).first()
        assert retired
    index_builds.cleanup_retired_builds([retired.build_id], grace_seconds=0)
    with Session(environment) as session:
        assert session.get(FileIndexBuild, retired.build_id).state == 'CLEANED'


def test_vector_allowlist_filters_before_top_k(environment):
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'stale', 'content':'stale', 'metadata':{'file_id':'f'}}], [[0., 1.]])
    rows = store.query('kb', [0., 1.], top_k=1, allowed_ids=['old'])
    assert [r['id'] for r in rows] == ['old']
    assert store.query('kb', [0., 1.], top_k=1, allowed_ids=[]) == []


def test_abandoned_build_is_swept_again_after_late_write(environment):
    with pytest.raises(RuntimeError, match='BUILD_CLAIM_LOST'):
        index_builds.publish_index_build(claim(False), [{'content':'late'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        build = session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'STAGING')).one()
        import json
        vector_id = json.loads(build.manifest)['vector_ids'][0]
    index_builds.sweep_builds()
    collection = VectorStoreService()._existing_collection('kb')
    assert not collection.get(ids=[vector_id])['ids']
    collection.add(ids=[vector_id], documents=['late'], metadatas=[{'file_id':'f'}], embeddings=[[0., 1.]])
    index_builds.sweep_builds()
    assert not collection.get(ids=[vector_id])['ids']
    with Session(environment) as session:
        assert session.get(FileIndexBuild, build.build_id).state == 'ABANDONED'


def test_published_metadata_and_legacy_exact_cleanup(environment):
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'other', 'content':'keep', 'metadata':{'file_id':'different'}}], [[.5, .5]])
    result = index_builds.publish_index_build(claim(), [{'content':'new', 'page_number':4,
        'heading_text':'Heading', 'heading_level':2, 'image_path':'p'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        chunk = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        value = store._existing_collection('kb').get(ids=[chunk.id], include=['metadatas'])
        metadata = value['metadatas'][0]
        assert metadata['build_id'] == result['build_id']
        assert (metadata['page_number'], metadata['heading_text'], metadata['image_path']) == (4, 'Heading', 'p')
    collection = store._existing_collection('kb')
    assert collection.get(ids=['old'])['ids'] == ['old']  # grace for in-flight readers
    with Session(environment) as session:
        retired = session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'RETIRED')).one()
    index_builds.cleanup_retired_builds([retired.build_id], grace_seconds=0)
    assert not collection.get(ids=['old'])['ids']
    assert collection.get(ids=['other'])['ids'] == ['other']


def test_real_async_vector_route_filters_unpublished_candidates(environment, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.retrieval import RetrievalService
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'staged', 'content':'staged', 'metadata':{'file_id':'f'}}], [[0., 1.]])
    service = RetrievalService()
    monkeypatch.setattr(service, '_embed_query_for_kb', AsyncMock(return_value=[0., 1.]))
    with Session(environment) as session:
        rows, metrics = asyncio.run(service._vector_search(session, 'q', ['kb'], 1, 0.0))
    assert [row['id'] for row in rows] == ['old']
    assert metrics['vector_count'] == 1


def test_http_chroma_query_supports_id_allowlist(tmp_path, monkeypatch):
    import os
    import socket
    import subprocess
    import time
    from pathlib import Path
    executable = Path(os.sys.executable).parent / 'chroma.exe'
    if not executable.exists():
        pytest.skip('Chroma server executable is unavailable')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = tmp_path / 'chroma.log'
    with log.open('wb') as output:
        process = subprocess.Popen([str(executable), 'run', '--host', '127.0.0.1', '--port', str(port),
                                    '--path', str(tmp_path / 'chroma')], stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    client = chromadb.HttpClient(host='127.0.0.1', port=port)
                    client.heartbeat()
                    break
                except Exception:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Chroma server did not start: ' + log.read_text(errors='replace'))
                    time.sleep(.2)
            monkeypatch.setattr(VectorStoreService, '_client', client)
            store = VectorStoreService()
            store.add_documents('kb', [{'id':'live', 'content':'live', 'metadata':{'file_id':'f', 'build_id':'active'}},
                                       {'id':'stale', 'content':'stale', 'metadata':{'file_id':'f', 'build_id':'old'}}],
                                [[1., 0.], [0., 1.]])
            assert [r['id'] for r in store.query('kb', [0., 1.], top_k=1, allowed_ids=['live'])] == ['live']
            assert [r['id'] for r in store.query('kb', [0., 1.], top_k=1, allowed_build_ids=['active'], allowed_ids=[])] == ['live']
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_verified_provenance_survives_unchanged_submit_only(environment, tmp_path):
    import json
    from app.services.pdf_provenance import write_manifest
    item = SimpleNamespace(content='new', page_number=4, content_type='text',
                           metadata={'source_locator': {'page':4, 'bbox':[1, 2, 3, 4]}})
    path = write_manifest(tmp_path, 'source-sha', [], [item])
    checkpoint = {'source_sha256':'source-sha', 'provenance_manifest':path}
    first = index_builds.publish_index_build(claim(), [{'content':'new', 'page_number':4}], [[0., 1.]], 'm', checkpoint)
    with Session(environment) as session:
        chunk = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        assert json.loads(chunk.source_metadata_json)['source_locators'] == [{'page':4, 'bbox':[1, 2, 3, 4]}]
        chunk_id = chunk.id
    index_builds.publish_index_build(claim(), [{'id':chunk_id, 'content':'new', 'page_number':4}], [[0., 1.]], 'm')
    with Session(environment) as session:
        chunk = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        assert json.loads(chunk.source_metadata_json)['source_sha256'] == 'source-sha'
        chunk_id = chunk.id
    index_builds.publish_index_build(claim(), [{'id':chunk_id, 'content':'edited', 'page_number':4}], [[0., 1.]], 'm')
    with Session(environment) as session:
        assert session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one().source_metadata_json is None


def test_late_worker_cannot_publish_after_delete(environment):
    from datetime import datetime
    with Session(environment) as session:
        doc = session.get(FileDocument, 'f')
        doc.deleted_at = datetime.utcnow()
        doc.desired_generation = 2
        session.add(doc)
        session.commit()
    with pytest.raises(RuntimeError, match='BUILD_GENERATION_CHANGED'):
        index_builds.publish_index_build(claim(), [{'content':'resurrection'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        doc = session.get(FileDocument, 'f')
        assert doc.deleted_at is not None and doc.active_build_id is None
        from app.services.retrieval import RetrievalService
        assert RetrievalService._vector_scope(session, 'kb') == ([], [])
        assert [c.content for c in session.exec(select(DocumentChunk)).all()] == ['old evidence']


def test_batched_vector_operations_merge_build_and_legacy_results(environment, monkeypatch):
    store = VectorStoreService()
    monkeypatch.setattr(store, '_batch_size', lambda: 2)
    docs = [{'id':f'new-{i}', 'content':f'new {i}',
             'metadata':{'file_id':'f', 'build_id':f'b-{i}'}} for i in range(5)]
    vectors = [[float(i) / 10, 1.] for i in range(5)]
    store.add_documents('kb', docs, vectors)
    fetched = store.get_documents('kb', [d['id'] for d in docs], include=['metadatas','embeddings'])
    assert set(fetched['ids']) == {d['id'] for d in docs}
    # More build IDs and legacy IDs than one batch; stale closer hits excluded.
    result = store.query('kb', [0., 1.], top_k=3, allowed_build_ids=['b-1','b-3','b-4'],
                         allowed_ids=['missing-1','old','missing-2'])
    assert [r['id'] for r in result] == ['new-1','new-3','new-4']
    store.delete_ids('kb', [d['id'] for d in docs])
    assert store.get_documents('kb', [d['id'] for d in docs])['ids'] == []
    store.delete_collection('kb')
    store.delete_ids('kb', ['already-absent'])


def test_mixed_legacy_and_versioned_retrieval_uses_sql_scope(environment, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.retrieval import RetrievalService
    result = index_builds.publish_index_build(claim(), [{'content':'versioned alpha'}], [[0.,1.]], 'm')
    with Session(environment) as session:
        session.add(FileDocument(id='legacy', kb_id='kb', name='legacy', local_path='legacy', size=1))
        session.add(DocumentChunk(id='legacy-id', file_id='legacy', content='legacy beta'))
        session.commit()
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'legacy-id','content':'legacy beta','metadata':{'file_id':'legacy'}},
                               {'id':'unpublished','content':'unpublished','metadata':{'file_id':'f','build_id':'other'}}],
                        [[1.,0.], [0.,1.]])
    svc = RetrievalService()
    monkeypatch.setattr(svc, '_embed_query_for_kb', AsyncMock(return_value=[0.,1.]))
    with Session(environment) as session:
        assert svc._vector_scope(session, 'kb') == (['legacy-id'], [result['build_id']])
        rows, _ = asyncio.run(svc._vector_search(session, 'q', ['kb'], 3, 0.0))
    assert {r['content'] for r in rows} == {'versioned alpha', 'legacy beta'}

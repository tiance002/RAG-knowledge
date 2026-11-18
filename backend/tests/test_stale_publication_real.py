"""Stale publication interleavings use the real ledger, SQLite, and Chroma."""
import asyncio
import json
import threading
from unittest.mock import AsyncMock

import chromadb
import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase
from app.services.bm25 import BM25Index
from app.services import index_builds
from app.services.retrieval import RetrievalService
from app.services.vector_store import VectorStoreService
from app.tasks import dispatch, file_task_recovery
from app.services import file_retirement  # register retirement SQL model


@pytest.mark.parametrize('resolution', ['successor', 'cancel_delete'])
def test_stale_writer_after_successor_or_cancel_delete(tmp_path, monkeypatch, resolution):
    engine = create_engine('sqlite:///' + (tmp_path/'isolated.sqlite').as_posix(),
                           connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(engine)
    client = chromadb.EphemeralClient()
    kb = 'kb-' + resolution
    monkeypatch.setattr(dispatch, 'engine', engine)
    monkeypatch.setattr(index_builds, 'engine', engine)
    monkeypatch.setattr(VectorStoreService, '_client', client)
    clock = [1_000_000]
    monkeypatch.setattr(dispatch, 'now_ms', lambda: clock[0])
    monkeypatch.setattr(dispatch, 'LEASE_MS', 1000)
    monkeypatch.setattr(dispatch, 'notify_task', lambda *args: None)
    with Session(engine) as session:
        session.add(KnowledgeBase(id=kb, name=kb, embedding_model='model', vlm_model=''))
        session.add(FileDocument(id='file', kb_id=kb, name='file', local_path=str(tmp_path/'absent'),
                                 size=0, desired_generation=1))
        session.add(DocumentChunk(id='legacy', file_id='file', content='original'))
        session.add(dispatch.FileTaskReceipt(task_id='task', file_id='file', active_file='file',
            operation='index', stage='index', status='QUEUED', target_generation=1, payload='{}'))
        session.commit()
        BM25Index().index_chunk(session, 'legacy', 'original', kb)
        session.commit()
    store = VectorStoreService()
    store.add_documents(kb, [{'id':'legacy','content':'original','metadata':{'file_id':'file'}},
                                {'id':'unrelated','content':'keep','metadata':{'file_id':'other'}}],
                        [[1.,0.], [.5,.5]])
    old = dispatch.claim_task('task', 'index', 'old-worker')
    assert old and old.attempt_no == 1
    written, release = threading.Event(), threading.Event()
    # Observe the actual SDK write used by operation-scoped publication too.
    original_add = VectorStoreService._ingest_add
    def blocked_add(self, *args, **kwargs):
        original_add(self, *args, **kwargs)
        if threading.current_thread().name == 'stale-publisher':
            written.set()
            if not release.wait(timeout=20):
                raise RuntimeError('TEST_BARRIER_TIMEOUT')
    monkeypatch.setattr(VectorStoreService, '_ingest_add', blocked_add)
    failures = []
    def stale_run():
        try:
            index_builds.publish_index_build(old, [{'content':'stale'}], [[0.,1.]], 'model')
        except Exception as exc:
            failures.append(exc)
            dispatch._fail_claim(old, exc)
    thread = threading.Thread(target=stale_run, name='stale-publisher')
    thread.start()
    try:
        assert written.wait(timeout=15)
        with Session(engine) as session:
            stale = session.exec(select(FileIndexBuild).where(FileIndexBuild.attempt_no == 1)).one()
            stale_id = json.loads(stale.manifest)['vector_ids'][0]
        assert store._existing_collection(kb).get(ids=[stale_id])['ids'] == [stale_id]
        if resolution == 'successor':
            clock[0] += 1001
            stats = file_task_recovery.scan_once(at=clock[0])
            assert stats['retried'] == 1
            clock[0] += 10_001
            current = dispatch.claim_task('task', 'index', 'new-worker')
            assert current and current.attempt_no == 2 and current.owner_token != old.owner_token
            result = index_builds.publish_index_build(current, [{'content':'successor'}], [[.2,.8]], 'model')
            with Session(engine) as session:
                current_id = session.exec(select(DocumentChunk.id).where(DocumentChunk.file_id == 'file')).one()
                assert session.get(FileDocument, 'file').active_build_id == result['build_id']
        else:
            from app.api.files import delete_file
            with Session(engine) as session:
                asyncio.run(delete_file('file', session=session, cancel_active=True))
        release.set()
        thread.join(timeout=15)
        assert not thread.is_alive()
        assert len(failures) == 1 and 'BUILD_CLAIM_LOST' in str(failures[0])
        with Session(engine) as session:
            receipt = session.get(dispatch.FileTaskReceipt, 'task')
            assert receipt.status == ('COMPLETED' if resolution == 'successor' else 'CANCELLED')
            if resolution == 'successor':
                assert session.get(FileDocument, 'file').active_build_id == result['build_id']
                assert session.get(DocumentChunk, current_id).content == 'successor'
            else:
                assert session.get(FileDocument, 'file') is None
            service = RetrievalService()
            service._embed_query_for_kb = AsyncMock(return_value=[0.,1.])
            hits, _ = asyncio.run(service._vector_search(session, 'q', [kb], 5, 0.0))
            assert [h['content'] for h in hits] == (['successor'] if resolution == 'successor' else [])
        index_builds.sweep_builds()
        collection = store._existing_collection(kb)
        assert not collection.get(ids=[stale_id])['ids']
        assert collection.get(ids=['unrelated'])['ids'] == ['unrelated']
        if resolution == 'successor':
            assert collection.get(ids=[current_id])['ids'] == [current_id]
    finally:
        release.set()
        thread.join(timeout=15)
        assert not thread.is_alive()
        engine.dispose()

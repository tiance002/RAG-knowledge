"""Offline P2 Chroma capacity, timeout, retrieval-scope and cancellation contracts."""
import asyncio
import contextvars
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.models import DocumentChunk, FileDocument, KnowledgeBase, FileStatus
from app.services import bounded_chroma_executor as executor
from app.services import retrieval, vector_store


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'false')
    monkeypatch.setenv('RAG_CHROMA_QUERY_WORKERS', '1')
    monkeypatch.setenv('RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS', '0.05')
    monkeypatch.setattr(vector_store.settings, 'CHROMA_HOST', None)
    connect = socket.socket.connect
    def guarded(sock, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return connect(sock, address)
        raise AssertionError('External connections forbidden')
    monkeypatch.setattr(socket.socket, 'connect', guarded)
    resolve = socket.getaddrinfo
    def local_dns(host, *args, **kwargs):
        if host not in ('127.0.0.1', b'127.0.0.1', '::1', b'::1'):
            raise AssertionError('External DNS forbidden')
        return resolve(host, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', local_dns)


def store_for(client):
    store = vector_store.VectorStoreService.__new__(vector_store.VectorStoreService)
    store.client = client
    return store


def test_default_off_keeps_original_query_and_creates_no_executor(monkeypatch):
    monkeypatch.setattr(executor, 'ThreadPoolExecutor', lambda **kwargs: pytest.fail('OFF must create no executor'))
    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    service.vector_store = SimpleNamespace(query=lambda **kwargs: [kwargs], query_async=AsyncMock())
    async def run():
        async with executor.managed_chroma_executor() as manager:
            assert manager is None
            return await service._query_vector_store(kb_id='kb', query_vector=[1.], top_k=3)
    assert asyncio.run(run()) == [{'kb_id': 'kb', 'query_vector': [1.], 'top_k': 3}]
    service.vector_store.query_async.assert_not_awaited()


def test_slow_job_yields_and_cancelled_job_keeps_capacity_until_true_completion(monkeypatch):
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    marker = contextvars.ContextVar('synthetic-marker', default=None)
    started, release, second_started = threading.Event(), threading.Event(), threading.Event()
    seen = []
    def first():
        seen.append((marker.get(), executor.in_chroma_read_worker()))
        started.set()
        if not release.wait(3): raise RuntimeError('test release missing')
        raise ValueError('late exception after caller cancelled')
    def second():
        second_started.set()
        return 'second result'
    async def run():
        errors = []
        asyncio.get_running_loop().set_exception_handler(lambda loop, context: errors.append(context))
        token = marker.set('copied-context')
        try:
            async with executor.managed_chroma_executor() as manager:
                task = asyncio.create_task(executor.run_chroma_query(first))
                assert await asyncio.to_thread(started.wait, 2)
                await asyncio.sleep(.01)
                assert not task.done()
                task.cancel()
                with pytest.raises(asyncio.CancelledError): await task
                assert manager.slots._value == 0 and len(manager.jobs) == 1
                waiting = asyncio.create_task(executor.run_chroma_query(second))
                await asyncio.sleep(.03)
                assert not second_started.is_set() and len(manager.jobs) == 1
                release.set()
                assert await asyncio.wait_for(waiting, 2) == 'second result'
                await asyncio.sleep(0)
                assert manager.slots._value == 1 and not manager.jobs
                assert not errors
        finally:
            marker.reset(token)
            release.set()
    asyncio.run(run())
    assert seen == [('copied-context', True)]


def test_cancelled_capacity_waiter_does_not_submit_and_shutdown_drains_real_job(monkeypatch):
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    started, release, unwanted = threading.Event(), threading.Event(), threading.Event()
    def blocked():
        started.set()
        if not release.wait(3): raise RuntimeError('test release missing')
    async def run():
        lifecycle = executor.managed_chroma_executor()
        manager = await lifecycle.__aenter__()
        task = asyncio.create_task(executor.run_chroma_query(blocked))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            waiter = asyncio.create_task(executor.run_chroma_query(unwanted.set))
            await asyncio.sleep(0)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError): await waiter
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            closing = asyncio.create_task(lifecycle.__aexit__(None, None, None))
            await asyncio.sleep(.02)
            assert not closing.done() and not unwanted.is_set()
            assert manager.closed and manager.slots._value == 0
            release.set()
            await asyncio.wait_for(closing, 2)
            assert manager.pool._shutdown and not manager.jobs
            with pytest.raises(executor.ChromaExecutorCapabilityError, match='NOT_MANAGED'):
                await executor.run_chroma_query(lambda: None)
        finally:
            release.set()
    asyncio.run(run())


class Collection:
    def __init__(self): self.calls = []
    def get(self, **kwargs):
        self.calls.append(('get', kwargs))
        return {'ids': [ident for ident in kwargs['ids'] if ident != 'missing']}
    def query(self, **kwargs):
        self.calls.append(('query', kwargs))
        names = ['active', 'foreign', 'retired', 'deleted'] if 'where' in kwargs else kwargs.get('ids', ['seed'])
        distances = {'active': .2, 'seed': .4, 'foreign': .1, 'retired': .15, 'deleted': .3}
        return {'ids': [names], 'documents': [[f'raw {name}' for name in names]],
                'metadatas': [[{'original': name} for name in names]],
                'distances': [[distances.get(name, .8) for name in names]]}


class Client:
    def __init__(self, collection): self.collection = collection
    def get_collection(self, **kwargs): return self.collection
    def get_max_batch_size(self): return 2


def test_exact_query_algorithm_scope_pages_mapping_order_and_failure_equivalence(monkeypatch):
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    collection = Collection()
    store = store_for(Client(collection))
    kwargs = dict(kb_id='kb', query_vector=[1., 0.], top_k=4, score_threshold=.6,
                  allowed_ids=['seed', 'missing'], allowed_build_ids=['build-live'])
    expected = store.query(**kwargs)
    calls = list(collection.calls)
    collection.calls.clear()
    async def run():
        async with executor.managed_chroma_executor(): return await store.query_async(**kwargs)
    assert asyncio.run(run()) == expected
    assert collection.calls == calls
    assert calls[0] == ('get', {'ids': ['seed', 'missing'], 'include': []})
    assert next(kwargs for name, kwargs in calls if name == 'query')['where'] == {'build_id': {'$in': ['build-live']}}
    collection.query = lambda **kw: (_ for _ in ()).throw(ValueError('known storage error'))
    with pytest.raises(ValueError, match='known storage error'): store.query(**kwargs)
    with pytest.raises(ValueError, match='known storage error'): asyncio.run(run())


@pytest.mark.parametrize('mode', ['vector', 'hybrid'])
def test_retrieval_sql_session_and_embedding_stay_on_owner_scope_and_results_match(monkeypatch, mode):
    owner = threading.get_ident()
    class GuardedSession(Session):
        def get(self, *args, **kwargs):
            assert threading.get_ident() == owner
            return super().get(*args, **kwargs)
        def exec(self, *args, **kwargs):
            assert threading.get_ident() == owner
            return super().exec(*args, **kwargs)
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    collection = Collection()
    store = store_for(Client(collection))
    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    service.vector_store = store
    async def embed(session, kb_id, query):
        assert threading.get_ident() == owner and not executor.in_chroma_read_worker()
        assert session.get(KnowledgeBase, kb_id)
        return [1., 0.]
    service._embed_query_for_kb = embed
    def lexical(*args, **kwargs):
        assert threading.get_ident() == owner
        return [], {'expansion_failed': False}
    service._expanded_bm25_results = lexical
    with GuardedSession(engine) as db:
        db.add_all([KnowledgeBase(id='kb', name='selected', embedding_model='none', vlm_model='none'),
                    KnowledgeBase(id='other', name='other', embedding_model='none', vlm_model='none')])
        db.add_all([FileDocument(id='legacy', kb_id='kb', name='legacy.txt', local_path='synthetic', size=0),
            FileDocument(id='live', kb_id='kb', name='live.txt', local_path='synthetic', size=0, active_build_id='build-live'),
            FileDocument(id='old', kb_id='kb', name='old.txt', local_path='synthetic', size=0),
            FileDocument(id='replacement', kb_id='kb', name='replacement.txt', local_path='synthetic', size=0, supersedes_file_id='old', status=FileStatus.READY),
            FileDocument(id='deleted-file', kb_id='kb', name='deleted.txt', local_path='synthetic', size=0, deleted_at=datetime.now()),
            FileDocument(id='foreign-file', kb_id='other', name='foreign.txt', local_path='synthetic', size=0)])
        db.add_all([DocumentChunk(id=ident, file_id=file_id, content='SQL evidence '+ident) for ident, file_id in
                    [('seed','legacy'),('active','live'),('retired','old'),('deleted','deleted-file'),('foreign','foreign-file')]])
        db.commit()
        async def request():
            if mode == 'vector': return await service._vector_search(db, 'q', ['kb'], 5, 0)
            return await service._hybrid_search(db, 'q', ['kb'], 5, 0, .7, .3)
        expected = asyncio.run(request())
        expected_calls = list(collection.calls)
        collection.calls.clear()
        monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
        async def candidate():
            async with executor.managed_chroma_executor(): return await request()
        actual = asyncio.run(candidate())
        assert actual[0] == expected[0]
        assert [row['id'] for row in actual[0]] == ['active', 'seed']
        assert all(row['kb_id'] == 'kb' for row in actual[0])
        assert collection.calls == expected_calls
        assert next(values for name, values in collection.calls if name == 'get')['ids'] == ['seed']
    engine.dispose()


def test_timeout_adapter_bounds_only_worker_reads_preserves_headers_writes_and_transport(monkeypatch):
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    monkeypatch.setattr(vector_store.settings, 'CHROMA_HOST', '127.0.0.1')
    seen = []
    def respond(request):
        seen.append((request.method, request.url.path, request.extensions['timeout'], dict(request.headers)))
        return httpx.Response(200, json={'ok': True})
    with httpx.Client(transport=httpx.MockTransport(respond), timeout=None, trust_env=False,
                      headers={'Authorization': 'synthetic-chroma', 'X-keep': 'same'}) as client:
        store = store_for(SimpleNamespace(_server=SimpleNamespace(_session=client)))
        original_transport, original_timeout, original_headers = client._transport, client.timeout, dict(client.headers)
        store._prepare_query_timeout_adapter()
        async def run():
            async with executor.managed_chroma_executor():
                for method, path in [('GET','/collections/c/count'),('POST','/collections/c/get'),('POST','/collections/c/query')]:
                    await executor.run_chroma_query(client.request, method, 'http://127.0.0.1:1'+path)
                with pytest.raises(executor.ChromaExecutorCapabilityError, match='NON_READ'):
                    await executor.run_chroma_query(client.post, 'http://127.0.0.1:1/collections/c/add')
        asyncio.run(run())
        client.post('http://127.0.0.1:1/collections/c/add')
        assert client._transport is original_transport and client.timeout == original_timeout
        assert dict(client.headers) == original_headers
        assert all(all(value == .05 for value in row[2].values()) for row in seen[:3])
        assert all(value is None for value in seen[-1][2].values())
        assert all(row[3]['authorization'] == 'synthetic-chroma' and row[3]['x-keep'] == 'same' for row in seen)
        assert len(seen) == 4


def test_real_loopback_http_read_timeout_finishes_job_and_releases_capacity(monkeypatch):
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    monkeypatch.setattr(vector_store.settings, 'CHROMA_HOST', '127.0.0.1')
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            time.sleep(.15)
            try:
                body = b'{"ok":true}'
                self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
                self.wfile.write(body)
            except OSError: pass  # The intentionally timed-out client closes first.
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with httpx.Client(timeout=None, trust_env=False) as client:
            store = store_for(SimpleNamespace(_server=SimpleNamespace(_session=client)))
            store._prepare_query_timeout_adapter()
            async def run():
                async with executor.managed_chroma_executor() as manager:
                    with pytest.raises(httpx.ReadTimeout):
                        await executor.run_chroma_query(client.get, f'http://127.0.0.1:{server.server_port}/collections/c/count')
                    await asyncio.sleep(0)
                    assert manager.slots._value == 1 and not manager.jobs
            asyncio.run(run())
    finally:
        server.shutdown(); server.server_close(); worker.join(2)
    assert not worker.is_alive()


@pytest.mark.parametrize('unsupported', ['version', 'session'])
def test_unsupported_http_adapter_capability_blocks_before_worker_submission(monkeypatch, unsupported):
    monkeypatch.setattr(vector_store.settings, 'CHROMA_HOST', '127.0.0.1')
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail('no HTTP expected'))) as client:
        store = store_for(SimpleNamespace(_server=SimpleNamespace(_session=client if unsupported == 'version' else object())))
        if unsupported == 'version': monkeypatch.setattr(vector_store.importlib.metadata, 'version', lambda name: 'unsupported')
        with pytest.raises(executor.ChromaExecutorCapabilityError, match='UNSUPPORTED_CHROMA_HTTP'):
            store._prepare_query_timeout_adapter()


@pytest.mark.parametrize('value', ['0', '-1', 'nan', 'inf', 'not-a-number'])
def test_invalid_io_timeout_capability_blocks_before_chroma_query(monkeypatch, value):
    monkeypatch.setenv('RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS', value)
    store = store_for(Client(Collection()))
    with pytest.raises(executor.ChromaExecutorCapabilityError, match='INVALID_CHROMA_QUERY_HTTP_TIMEOUT') as caught:
        asyncio.run(store.query_async(kb_id='kb', query_vector=[1.]))
    assert caught.value.capability_status == 'BLOCKED'
    assert store.client.collection.calls == []


def test_enabled_candidate_without_lifecycle_capability_blocks_without_query(monkeypatch):
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    store = store_for(Client(Collection()))
    with pytest.raises(executor.ChromaExecutorCapabilityError, match='NOT_MANAGED') as caught:
        asyncio.run(store.query_async(kb_id='kb', query_vector=[1.]))
    assert caught.value.capability_status == 'BLOCKED'
    assert store.client.collection.calls == []

"""P2 first-request bootstrap; all Chroma calls use local synthetic HTTP."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import chromadb
from chromadb.config import Settings as ChromaSettings
from chromadb.auth import ClientAuthProvider, ClientAuthHeaders
from overrides import override
from chromadb.api.shared_system_client import SharedSystemClient
from app.services import bounded_chroma_executor as executor, retrieval, vector_store


class FailingAuth(ClientAuthProvider):
    @override
    def authenticate(self) -> ClientAuthHeaders:
        raise ValueError('synthetic configured auth failure')


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    monkeypatch.setenv('RAG_CHROMA_QUERY_WORKERS', '1')
    monkeypatch.setenv('RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS', '.15')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('ANONYMIZED_TELEMETRY', 'false')
    monkeypatch.setenv('NO_PROXY', '*')
    original = socket.socket.connect
    def connect(sock, address):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original(sock, address)
        raise AssertionError('external network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', connect)
    resolve = socket.getaddrinfo
    def dns(host, *args, **kwargs):
        if host not in ('127.0.0.1', b'127.0.0.1', '::1', b'::1'):
            raise AssertionError('external DNS forbidden')
        return resolve(host, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', dns)


@pytest.fixture
def local_chroma(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.server.paths.append((self.path, self.headers.get('X-Unrelated')))
            if self.path == '/api/v2/auth/identity':
                body = {'user_id': 'fixture', 'tenant': 'default_tenant', 'databases': ['default_database']}
            elif self.path == '/api/v2/tenants/default_tenant':
                if self.server.block and not self.headers.get('X-Unrelated'):
                    entered.set(); release.wait(2)
                body = {'name': 'default_tenant'}
            elif self.path == '/api/v2/tenants/default_tenant/databases/default_database':
                body = {'id': '00000000-0000-0000-0000-000000000001', 'name': 'default_database', 'tenant': 'default_tenant'}
            else:
                self.send_error(404); return
            data=json.dumps(body).encode()
            try:
                self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
            except OSError: pass
    server=ThreadingHTTPServer(('127.0.0.1', 0), Handler); server.daemon_threads=True
    server.paths=[]; server.block=False
    thread=threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    monkeypatch.setattr(vector_store.settings, 'CHROMA_HOST', '127.0.0.1')
    monkeypatch.setattr(vector_store.settings, 'CHROMA_PORT', server.server_port)
    try:
        yield server, entered, release
    finally:
        release.set(); server.shutdown(); server.server_close(); thread.join(2)


def test_off_keeps_eager_and_on_retrieval_is_lazy(monkeypatch):
    called=[]
    monkeypatch.setattr(vector_store.VectorStoreService, 'get_client', classmethod(lambda cls: called.append(True) or object()))
    monkeypatch.setattr(retrieval, 'get_bm25_index', lambda: object())
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'false')
    assert retrieval.RetrievalService().vector_store.client is not None and called == [True]
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'true')
    assert retrieval.RetrievalService().vector_store.client is None and called == [True]


def test_real_sdk_normal_validation_in_worker_and_owned_cleanup(monkeypatch, local_chroma):
    server, _, _ = local_chroma
    descriptor=SharedSystemClient.__dict__['_create_system_if_not_exists']
    before=dict(SharedSystemClient._identifier_to_system)
    store=vector_store.VectorStoreService(lazy_retrieval=True)
    threads=[]
    def query(**kwargs):
        threads.append(threading.get_ident())
        return [kwargs]
    monkeypatch.setattr(store, 'query', query)
    async def run():
        async with executor.managed_chroma_executor():
            result=await store.query_async(kb_id='allowed', query_vector=[1.], allowed_ids=['c1'], allowed_build_ids=['b1'])
            owned=store._owned_http
            assert len(owned.systems) >= 2
            assert owned.client._server._session.timeout.read == .15
            assert owned.client._admin_client._server._session.timeout.read == .15
            assert result[0]['allowed_ids']==['c1'] and result[0]['allowed_build_ids']==['b1']
            assert threads[0] != threading.get_ident()
        assert store.client is None and not owned.systems
    asyncio.run(run())
    assert [path for path, _ in server.paths][:3] == ['/api/v2/auth/identity','/api/v2/tenants/default_tenant','/api/v2/tenants/default_tenant/databases/default_database']
    assert SharedSystemClient.__dict__['_create_system_if_not_exists'] is descriptor
    assert SharedSystemClient._identifier_to_system == before


def test_tenant_timeout_is_nonblocking_and_partial_systems_cleaned(monkeypatch, local_chroma):
    server, entered, release = local_chroma; server.block=True
    descriptor=SharedSystemClient.__dict__['_create_system_if_not_exists']
    before=dict(SharedSystemClient._identifier_to_system)
    store=vector_store.VectorStoreService(lazy_retrieval=True)
    async def run():
        async with executor.managed_chroma_executor():
            task=asyncio.create_task(store.query_async(kb_id='kb',query_vector=[1.]))
            assert await asyncio.to_thread(entered.wait, 2)
            await asyncio.sleep(.02)
            assert not task.done()
            with pytest.raises(ValueError): await asyncio.wait_for(task, 2)
            assert store._owned_http is not None and not store._owned_http.systems
            assert SharedSystemClient.__dict__['_create_system_if_not_exists'] is descriptor
            release.set()
        assert store.client is None
    asyncio.run(run())
    assert SharedSystemClient._identifier_to_system == before


def test_cancelled_bootstrap_retains_slot_then_drains(monkeypatch, local_chroma):
    server, entered, release = local_chroma; server.block=True
    monkeypatch.setenv('RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS', '1')
    store=vector_store.VectorStoreService(lazy_retrieval=True)
    monkeypatch.setattr(store, 'query', lambda **kw: [])
    async def run():
        async with executor.managed_chroma_executor() as manager:
            task=asyncio.create_task(store.query_async(kb_id='kb',query_vector=[1.]))
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError): await task
            assert manager.slots._value == 0 and manager.jobs
            release.set()
        assert not manager.jobs and store.client is None
    asyncio.run(run())


def test_unrelated_sdk_constructor_untouched_while_hook_installed(monkeypatch, local_chroma):
    server, entered, release = local_chroma; server.block=True
    monkeypatch.setenv('RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS', '1')
    descriptor=SharedSystemClient.__dict__['_create_system_if_not_exists']
    store=vector_store.VectorStoreService(lazy_retrieval=True)
    monkeypatch.setattr(store, 'query', lambda **kw: [])
    unrelated = None
    async def run():
        nonlocal unrelated
        async with executor.managed_chroma_executor():
            task=asyncio.create_task(store.query_async(kb_id='kb',query_vector=[1.]))
            assert await asyncio.to_thread(entered.wait, 2)
            unrelated=await asyncio.to_thread(chromadb.HttpClient, host='127.0.0.1', port=server.server_port,
                headers={'X-Unrelated': 'yes'}, settings=ChromaSettings(anonymized_telemetry=False))
            assert unrelated._server._session.timeout.read is None
            assert unrelated._admin_client._server._session.timeout.read is None
            assert SharedSystemClient.__dict__['_create_system_if_not_exists'] is not descriptor
            release.set()
            assert await asyncio.wait_for(task, 2) == []
            assert SharedSystemClient.__dict__['_create_system_if_not_exists'] is descriptor
    try:
        asyncio.run(run())
        assert unrelated._server._session.timeout.read is None
    finally:
        release.set()
        if unrelated is not None:
            systems={id(unrelated._server._system): unrelated._server._system,
                     id(unrelated._admin_client._server._system): unrelated._admin_client._server._system}
            for system in systems.values():
                server_api = next(component for component in system._instances.values() if hasattr(component, '_session'))
                server_api._session.close(); system.stop()
            cache=SharedSystemClient._identifier_to_system
            for key, value in list(cache.items()):
                if any(value is owned for owned in systems.values()) and cache.get(key) is value:
                    del cache[key]


def test_repeated_failed_bootstrap_does_not_grow_system_registry(monkeypatch, local_chroma):
    server, entered, release=local_chroma; server.block=True
    before=dict(SharedSystemClient._identifier_to_system)
    store=vector_store.VectorStoreService(lazy_retrieval=True)
    async def run():
        async with executor.managed_chroma_executor():
            for _ in range(3):
                with pytest.raises(ValueError):
                    await store.query_async(kb_id='kb', query_vector=[1.])
                assert not store._owned_http.systems
                assert SharedSystemClient._identifier_to_system == before
    try: asyncio.run(run())
    finally: release.set()
    assert SharedSystemClient._identifier_to_system == before



def test_configured_auth_failure_closes_unregistered_session(monkeypatch, local_chroma):
    import sys
    from types import ModuleType
    from chromadb.api.fastapi import FastAPI
    module = ModuleType('synthetic_chroma_auth')
    module.FailingAuth = FailingAuth
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setenv('CHROMA_CLIENT_AUTH_PROVIDER', 'synthetic_chroma_auth.FailingAuth')
    closed = []
    original_close = httpx.Client.close
    def track_close(client):
        closed.append(client)
        return original_close(client)
    monkeypatch.setattr(httpx.Client, 'close', track_close)
    expected_init = FastAPI.__dict__['__init__']
    expected_descriptor = SharedSystemClient.__dict__['_create_system_if_not_exists']
    before = dict(SharedSystemClient._identifier_to_system)
    server, _, _ = local_chroma
    store=vector_store.VectorStoreService(lazy_retrieval=True)
    async def run():
        async with executor.managed_chroma_executor():
            with pytest.raises(ValueError, match='configured auth failure'):
                await store.query_async(kb_id='kb',query_vector=[1.])
            assert closed and all(session.is_closed for session in closed)
            assert store._owned_http is not None and not store._owned_http.systems and not store._owned_http.sessions
    asyncio.run(run())
    assert not server.paths
    assert FastAPI.__dict__['__init__'] is expected_init
    assert SharedSystemClient.__dict__['_create_system_if_not_exists'] is expected_descriptor
    assert SharedSystemClient._identifier_to_system == before


def test_capability_failure_preserves_hybrid_bm25_fallback_and_diagnostic(monkeypatch):
    from unittest.mock import AsyncMock
    events=[]
    monkeypatch.setattr(retrieval, 'event', lambda name, **data: events.append((name, data)))
    service=retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    service.vector_store=SimpleNamespace(query_async=AsyncMock(side_effect=executor.ChromaExecutorCapabilityError('UNSUPPORTED_CHROMA_HTTP_TIMEOUT_ADAPTER_VERSION')))
    service._embed_query_for_kb=AsyncMock(return_value=[1.])
    service._vector_scope=lambda session,kb: (['allowed'], ['live'])
    service._expanded_bm25_results=lambda *args, **kwargs: ([{'id':'c1','chunk_id':'c1','kb_id':'kb','bm25_score':1.}], {'expansion_failed':False})
    service._enrich_results=lambda session,rows,kb: rows
    service._log_retrieval=lambda *args: None
    async def run():
        return await service.search(object(), 'question', ['kb'], search_mode='hybrid', top_k=1, log_retrieval=False)
    results, metrics=asyncio.run(run())
    assert [row['id'] for row in results] == ['c1']
    assert metrics['retrieval_status'] == 'SUCCESS' and metrics['fallback_used'] and metrics['degraded']
    assert service._embed_query_for_kb.await_count == 1
    assert any(name == 'chroma.capability' and data['status'] == 'BLOCKED'
               and data['reason_code'] == 'UNSUPPORTED_CHROMA_HTTP_TIMEOUT_ADAPTER_VERSION' for name, data in events)

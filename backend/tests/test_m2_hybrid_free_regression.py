import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('hybrid_preflight', Path(__file__).resolve().parents[2] / 'scripts/m2_hybrid_free_regression.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture(autouse=True)
def deny_all_network(monkeypatch):
    import ipaddress
    import socket
    import httpx
    from app.services import rerank
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    def permit(address):
        try:
            return (isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
                    and address[1] != 11434)
        except (ValueError, IndexError, TypeError):
            return False
    def connect(sock, address):
        if not permit(address):
            raise AssertionError("External network forbidden in offline tests")
        return original_connect(sock, address)
    def connect_ex(sock, address):
        if not permit(address):
            raise AssertionError("External network forbidden in offline tests")
        return original_connect_ex(sock, address)
    def deny(*args, **kwargs):
        raise AssertionError("External network forbidden in offline tests")
    def no_fallback(*args, **kwargs):
        raise mod.GuardStop("Rerank chat fallback forbidden in offline tests")
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(httpx.Client, "send", deny)
    monkeypatch.setattr(httpx.AsyncClient, "send", deny)
    monkeypatch.setattr(rerank.RerankService, "_rerank_via_chat", no_fallback)


def fixture(tmp_path, monkeypatch):
    chunks = [{'id': str(i), 'source_id': 's', 'content': str(i)} for i in range(80)]
    raw = json.dumps({'authorized': True, 'chunks': chunks}).encode()
    manifest = tmp_path / 'manifest.json'
    manifest.write_bytes(raw)
    monkeypatch.setattr(mod, 'EXPECTED_MANIFEST_SHA256', mod.hashlib.sha256(raw).hexdigest())
    db = tmp_path / 'index.db'
    with sqlite3.connect(db) as con:
        con.execute('create table document_chunks (id text, file_id text, content text)')
        con.executemany('insert into document_chunks values (?, ?, ?)', [(x['id'], x['source_id'], x['content']) for x in chunks])
    return manifest, db


def test_exact_corpus_is_read_only_and_execution_blocked(tmp_path, monkeypatch):
    manifest, db = fixture(tmp_path, monkeypatch)
    before = db.read_bytes()
    result = mod.inspect(manifest, db)
    assert result['corpus'] == 'PASS' and result['execution'] == 'BLOCKED'
    assert result['model_calls'] == 0 and db.read_bytes() == before


def test_rejects_modified_content_and_manifest(tmp_path, monkeypatch):
    manifest, db = fixture(tmp_path, monkeypatch)
    with sqlite3.connect(db) as con:
        con.execute("update document_chunks set content='wrong' where id='0'")
    with pytest.raises(ValueError, match='differs'):
        mod.inspect(manifest, db)
    manifest.write_text('{}')
    with pytest.raises(ValueError, match='hash mismatch'):
        mod.inspect(manifest, db)


def test_durable_guard_rejects_replay_and_records_unknown(tmp_path):
    import httpx
    guard = mod.DurableCalls(tmp_path / 'calls')
    req = httpx.Request('POST', 'https://api.siliconflow.cn/v1/embeddings', json={'model': 'BAAI/bge-m3', 'input': ['test']})
    key = guard.begin('index-batch-1', req)
    with pytest.raises(mod.GuardStop):
        guard.finish(key, error=TimeoutError())
    assert json.loads((guard.directory / (key + '.finished.json')).read_bytes())['state'] == 'UNKNOWN'
    fresh = mod.DurableCalls.__new__(mod.DurableCalls)
    import threading
    fresh.directory, fresh.lock, fresh.stopped, fresh.count = guard.directory, threading.Lock(), False, 0
    with pytest.raises(mod.GuardStop):
        fresh.begin('index-batch-1', req)


def test_guard_rejects_unapproved_endpoint_and_429(tmp_path):
    import httpx
    guard = mod.DurableCalls(tmp_path / 'calls')
    req = httpx.Request('POST', 'https://api.siliconflow.cn/v1/rerank', json={'model': 'BAAI/bge-reranker-v2-m3'})
    key = guard.begin('trial-rerank', req)
    with pytest.raises(mod.GuardStop):
        guard.finish(key, response=httpx.Response(429, content=b'limited'))
    assert guard.stopped
    other = mod.DurableCalls(tmp_path / 'other')
    with pytest.raises(mod.GuardStop):
        other.begin('bad', httpx.Request('POST', 'https://example.com/v1/embeddings', json={'model': 'BAAI/bge-m3'}))
    assert not list(other.directory.iterdir())


def test_actual_rerank_429_guard_prevents_chat_fallback(tmp_path, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from app.services import rerank
    guard = mod.DurableCalls(tmp_path / 'calls')
    sent = []
    def post(url, *, json, **kwargs):
        req = httpx.Request('POST', url, json=json)
        key = guard.begin('rerank-1', req)
        sent.append(req)
        guard.finish(key, response=httpx.Response(429, content=b'rate limited'))
    monkeypatch.setattr(rerank.httpx, 'post', post)
    service = rerank.RerankService(None)
    fallback = []
    monkeypatch.setattr(service, '_rerank_via_chat', lambda *args: fallback.append(True))
    model = SimpleNamespace(base_url='https://api.siliconflow.cn/v1', api_key='mock', model_name='BAAI/bge-reranker-v2-m3')
    with pytest.raises(mod.GuardStop):
        service.rerank('q', ['candidate'], model)
    assert len(sent) == 1 and fallback == []


def test_guard_rejects_unknown_path_without_model(tmp_path):
    import httpx
    guard = mod.DurableCalls(tmp_path / 'calls')
    with pytest.raises(mod.GuardStop):
        guard.begin('bad', httpx.Request('POST', 'https://api.siliconflow.cn/v1/unexpected', json={}))
    assert not list(guard.directory.iterdir())


def test_provider_adapter_uses_mock_async_transport_and_records_body(tmp_path):
    import asyncio
    import httpx
    guard = mod.DurableCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0, 0.0]}]}))
    adapter = mod.ProviderAdapter(guard, inner, inner)
    adapter.context = 'index'
    req = httpx.Request('POST', 'https://api.siliconflow.cn/v1/embeddings', json={'model': 'BAAI/bge-m3', 'input': ['a']})
    response = asyncio.run(adapter.async_send(req))
    assert response.json()['data'][0]['embedding'] == [1.0, 0.0]
    assert len(list(guard.directory.glob('*.started.json'))) == 1
    assert len(list(guard.directory.glob('*.response.body'))) == 1


def test_provider_adapter_empty_rerank_stops(tmp_path):
    import httpx
    guard = mod.DurableCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={'results': []}))
    adapter = mod.ProviderAdapter(guard, inner, inner)
    req = httpx.Request('POST', 'https://api.siliconflow.cn/v1/rerank', json={'model': 'BAAI/bge-reranker-v2-m3'})
    with pytest.raises(mod.GuardStop):
        adapter.sync_send(req)
    assert guard.stopped


def test_actual_embedding_service_through_mock_adapter(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from app.services.embedding import EmbeddingService
    guard = mod.DurableCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={'data': [{'index': 0, 'embedding': [1.0, 0.0]}]}))
    adapter = mod.ProviderAdapter(guard, inner, inner)
    async def send(client, request, **kwargs):
        return await adapter.async_send(request)
    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    service = EmbeddingService(base_url='https://api.siliconflow.cn/v1', api_key='mock', model='BAAI/bge-m3')
    assert asyncio.run(service.embed_documents(['frozen test chunk'])) == [[1.0, 0.0]]
    record = json.loads(next(guard.directory.glob('*.started.json')).read_bytes())
    assert record['endpoint'] == '/v1/embeddings' and record['model'] == 'BAAI/bge-m3'


def test_actual_rerank_service_through_mock_adapter(tmp_path, monkeypatch):
    import httpx
    from types import SimpleNamespace
    from app.services import rerank
    guard = mod.DurableCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={'results': [{'index': 0, 'relevance_score': 0.9}]}))
    adapter = mod.ProviderAdapter(guard, inner, inner)
    def post(url, *, json, headers, **kwargs):
        return adapter.sync_send(httpx.Request('POST', url, json=json, headers=headers))
    monkeypatch.setattr(rerank.httpx, 'post', post)
    monkeypatch.setattr(rerank.RerankService, '_rerank_via_chat', lambda *a: (_ for _ in ()).throw(mod.GuardStop('fallback blocked')))
    model = SimpleNamespace(base_url='https://api.siliconflow.cn/v1', api_key='mock', model_name='BAAI/bge-reranker-v2-m3')
    result = rerank.RerankService(None).rerank('q', ['candidate'], model)
    assert result[0]['score'] == 0.9 and result[0]['text'] == 'candidate'
    record = json.loads(next(guard.directory.glob('*.started.json')).read_bytes())
    assert record['endpoint'] == '/v1/rerank' and record['model'] == 'BAAI/bge-reranker-v2-m3'


def test_network_fixture_blocks_provider_and_local_model_port():
    import socket
    import httpx
    with socket.socket() as sock:
        with pytest.raises(AssertionError):
            sock.connect(('203.0.113.1', 443))
        with pytest.raises(AssertionError):
            sock.connect(('127.0.0.1', 11434))
    with pytest.raises(AssertionError):
        socket.getaddrinfo('api.siliconflow.cn', 443)
    with httpx.Client() as client:
        with pytest.raises(AssertionError):
            client.send(httpx.Request('POST', 'https://api.siliconflow.cn/v1/chat/completions'))


def test_wrong_embedding_count_records_failed_response(tmp_path):
    import asyncio
    import httpx
    guard = mod.DurableCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={'data': []}))
    adapter = mod.ProviderAdapter(guard, inner, inner)
    req = httpx.Request('POST', 'https://api.siliconflow.cn/v1/embeddings', json={'model': 'BAAI/bge-m3', 'input': ['a']})
    with pytest.raises(mod.GuardStop):
        asyncio.run(adapter.async_send(req))
    record = json.loads(next(guard.directory.glob('*.finished.json')).read_bytes())
    assert record['state'] == 'FAILED_RESPONSE' and guard.stopped


def test_openai_sdk_stream_through_mock_adapter(tmp_path, monkeypatch):
    import asyncio
    import httpx
    from openai import AsyncOpenAI
    guard = mod.DurableCalls(tmp_path / 'calls')
    body = (b'data: {"id":"mock","object":"chat.completion.chunk","model":"Qwen/Qwen3.5-4B",'
            b'"choices":[{"index":0,"delta":{"content":"hello"},"finish_reason":null}]}\n\n'
            b'data: {"id":"mock","object":"chat.completion.chunk","model":"Qwen/Qwen3.5-4B",'
            b'"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
            b'data: [DONE]\n\n')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/event-stream'}, content=body))
    adapter = mod.ProviderAdapter(guard, inner, inner)
    async def send(client, request, **kwargs):
        return await adapter.async_send(request)
    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    async def run():
        async with httpx.AsyncClient() as client:
            api = AsyncOpenAI(api_key='mock', base_url='https://api.siliconflow.cn/v1', max_retries=0, http_client=client)
            stream = await api.chat.completions.create(model='Qwen/Qwen3.5-4B', messages=[{'role':'user','content':'synthetic'}], stream=True)
            return [chunk async for chunk in stream]
    chunks = asyncio.run(run())
    assert chunks[0].choices[0].delta.content == 'hello'
    record = json.loads(next(guard.directory.glob('*.started.json')).read_bytes())
    assert record['endpoint'] == '/v1/chat/completions' and record['model'] == 'Qwen/Qwen3.5-4B'

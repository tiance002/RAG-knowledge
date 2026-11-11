"""Offline P1 contracts: async HTTP/SDK rerank and unchanged batch/route policy."""
import asyncio
from contextlib import asynccontextmanager
import json
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.services import rerank

MODEL = SimpleNamespace(base_url="https://provider.invalid/v1", api_key="synthetic-key", model_name="fixed-model")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    def forbidden(*args, **kwargs):
        raise AssertionError('Real network forbidden in rerank contract tests')
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def guarded(method):
        def invoke(sock, address):
            # Windows asyncio uses TCP loopback for its internal socketpair.
            if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
                return method(sock, address)
            return forbidden()
        return invoke
    monkeypatch.setattr(socket.socket, 'connect', guarded(connect))
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded(connect_ex))
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)


def bind_transport(monkeypatch, handler):
    clients = []
    @asynccontextmanager
    async def request(**kwargs):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=kwargs['timeout'], trust_env=False) as client:
            clients.append(client)
            yield client
    monkeypatch.setattr(rerank, 'provider_request', request)
    return clients


@pytest.mark.parametrize('response', [
    [], [{'index': 1, 'score': .8}, {'index': 0, 'score': .8}],
    [{'index': 0, 'score': .9}, {'index': 0, 'score': .1}, {'index': 1, 'score': float('nan')},
     {'index': -1, 'score': 1}, {'index': '1', 'score': 1}],
    [{'index': 0, 'score': .1}, {'index': 1, 'score': .9}],
])
def test_async_batch_matches_sync_mapping_order_filter_and_original(response):
    rows = [{'id': 'A', 'content': '甲' * 20, 'score': .2},
            {'id': 'B', 'content': '乙' * 20, 'score': .9},
            {'id': 'C', 'content': 'one two', 'score': .5}]
    sync = Mock(return_value=response)
    async_call = AsyncMock(return_value=response)
    kwargs = dict(query='q', candidates=rows, model=MODEL, max_context_tokens=5, top_k=3, score_threshold=.5)
    expected = rerank.batch_rerank(rerank_service=SimpleNamespace(rerank=sync), **kwargs)
    actual = asyncio.run(rerank.async_batch_rerank(rerank_service=SimpleNamespace(arerank=async_call), **kwargs))
    assert actual == expected
    assert sync.call_args_list == async_call.call_args_list
    assert len(sync.call_args_list) == 3
    assert rows[0]['content'] == '甲' * 20 and '_tokens' not in rows[0]
    assert all(row['content'] == rows['ABC'.index(row['id'])]['content'] for row in actual)


def test_async_batch_known_error_matches_sync_unknown_evidence():
    rows = [{'id': 'A', 'content': 'first words', 'score': .7}, {'id': 'B', 'content': 'next words', 'score': .6}]
    sync = Mock(side_effect=[ValueError('known'), [{'index': 0, 'score': .9}]])
    async_call = AsyncMock(side_effect=[ValueError('known'), [{'index': 0, 'score': .9}]])
    kwargs = dict(query='q', candidates=rows, model=MODEL, max_context_tokens=1, top_k=2)
    expected = rerank.batch_rerank(rerank_service=SimpleNamespace(rerank=sync), **kwargs)
    actual = asyncio.run(rerank.async_batch_rerank(rerank_service=SimpleNamespace(arerank=async_call), **kwargs))
    assert actual == expected
    assert actual[1]['rerank_status'] == 'UNKNOWN' and actual[1]['score'] == .7


def test_unknown_batch_stops_before_next_async_call():
    call = AsyncMock(side_effect=httpx.ReadTimeout('synthetic timeout'))
    with pytest.raises(httpx.ReadTimeout):
        asyncio.run(rerank.async_batch_rerank('q', [{'content': 'one document'}, {'content': 'two documents'}],
                    SimpleNamespace(arerank=call), MODEL, max_context_tokens=1))
    assert call.await_count == 1


def test_async_endpoint_unknown_has_no_fallback_retry_or_next_batch(monkeypatch):
    calls = []
    async def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('synthetic timeout', request=request)
    clients = bind_transport(monkeypatch, handler)
    service = rerank.RerankService(None)
    fallback = AsyncMock()
    monkeypatch.setattr(service, '_arerank_via_chat', fallback)
    with pytest.raises(httpx.ReadTimeout) as caught:
        asyncio.run(rerank.async_batch_rerank('q', [{'content': 'first document'}, {'content': 'second document'}],
                    service, MODEL, max_context_tokens=1))
    assert len(calls) == 1 and fallback.await_count == 0
    assert caught.value.provider_status == 'UNKNOWN'
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize('unavailable', ['status', 'empty', 'preconnect'])
def test_async_chat_fallback_preserves_prompt_and_is_nonblocking(monkeypatch, unavailable):
    sent = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def handler(request):
            sent.append((request.url.path, json.loads(request.content), request.headers['authorization']))
            if request.url.path.endswith('/rerank'):
                if unavailable == 'preconnect':
                    raise httpx.ConnectError('synthetic offline', request=request)
                return httpx.Response(404 if unavailable == 'status' else 200, json={'results': []})
            assert request.url.path == '/v1/chat/completions'
            started.set()
            await release.wait()
            return httpx.Response(200, json={'id': 'chat-synthetic', 'model': MODEL.model_name,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"candidates":[{"index":0,"score":0.9,"text":"provider replacement"}]}'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 5, 'completion_tokens': 4, 'total_tokens': 9}})
        clients = bind_transport(monkeypatch, handler)
        task = asyncio.create_task(rerank.RerankService(None).arerank('q', ['first\n' + 'x' * 550], MODEL))
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0)
        assert not task.done()  # A separate lightweight task ran while SDK HTTP was awaiting.
        release.set()
        result = await asyncio.wait_for(task, timeout=2)
        assert result == [{'index': 0, 'score': .9, 'text': 'provider replacement'}]
        assert all(client.is_closed for client in clients)
    asyncio.run(run())
    assert [path for path, _, _ in sent] == ['/v1/rerank', '/v1/chat/completions']
    payload = sent[-1][1]
    assert payload['messages'] == [{'role': 'user', 'content': rerank.RerankService._chat_prompt('q', ['first\n' + 'x' * 550])}]
    assert payload['temperature'] == 0 and payload['stream'] is False
    assert all(auth == 'Bearer synthetic-key' for _, _, auth in sent)


def test_async_chat_unknown_has_no_sdk_retry(monkeypatch):
    calls = []
    async def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith('/rerank'):
            return httpx.Response(404, json={})
        raise httpx.ReadTimeout('synthetic timeout', request=request)
    clients = bind_transport(monkeypatch, handler)
    with pytest.raises(Exception) as caught:
        asyncio.run(rerank.RerankService(None).arerank('q', ['doc'], MODEL))
    assert rerank.is_unknown_provider_error(caught.value)
    assert calls == ['/v1/rerank', '/v1/chat/completions']
    assert all(client.is_closed for client in clients)


@pytest.mark.parametrize('in_fallback', [False, True])
def test_slow_async_endpoint_yields_and_cancellation_closes_without_next_batch(monkeypatch, in_fallback):
    calls = []
    async def run():
        started = asyncio.Event()
        async def handler(request):
            calls.append(request)
            if in_fallback and request.url.path.endswith('/rerank'):
                return httpx.Response(404, json={})
            started.set()
            await asyncio.Event().wait()
        clients = bind_transport(monkeypatch, handler)
        task = asyncio.create_task(rerank.async_batch_rerank('q', [{'content': 'first document'}, {'content': 'second document'}],
                                   rerank.RerankService(None), MODEL, max_context_tokens=1))
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0)
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(calls) == (2 if in_fallback else 1) and all(client.is_closed for client in clients)
    asyncio.run(run())


def test_normal_agent_async_wiring_preserves_k_and_partial_status_difference(monkeypatch):
    from app.services import chat
    from app.tools import retrieval_tool
    rows = [{'id': 'A', 'content': 'source A', 'rerank_score': .9, 'rerank_status': 'SCORED'},
            {'id': 'B', 'content': 'source B', 'rerank_score': None, 'rerank_status': 'UNKNOWN'}]
    search = AsyncMock(side_effect=lambda **kw: (list(rows), {'retrieval_status': 'SUCCESS'}))
    normal_rerank = AsyncMock(return_value=list(rows))
    agent_rerank = AsyncMock(return_value=list(rows))
    monkeypatch.setattr(chat, 'async_batch_rerank', normal_rerank)
    monkeypatch.setattr(retrieval_tool, 'async_batch_rerank', agent_rerank)
    monkeypatch.setattr(chat, 'RerankService', lambda session: object())
    monkeypatch.setattr(retrieval_tool, 'RerankService', lambda session: object())
    monkeypatch.setattr(retrieval_tool, 'get_retrieval_service', lambda: SimpleNamespace(search=search))
    class FakeSession:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(retrieval_tool, 'Session', FakeSession)
    service = chat.ChatService.__new__(chat.ChatService)
    service.retrieval_service = SimpleNamespace(search=search)
    normal, metrics = asyncio.run(service._retrieve_with_new_service(None, 'q', ['kb'], 3, .1,
        'hybrid', .7, .3, True, .2, None, 2, 0))
    agent = json.loads(asyncio.run(retrieval_tool.search_knowledge_base.coroutine(query='q', kb_ids=['kb'],
        top_k=3, use_rerank=True, rerank_top_k=2, rerank_score_threshold=.2)))
    assert normal == rows and metrics['retrieval_status'] == 'UNKNOWN'
    assert agent['retrieval_status'] == 'SUCCESS'
    assert [call.kwargs['top_k'] for call in search.await_args_list] == [3, 6]
    assert normal_rerank.await_count == agent_rerank.await_count == 1
    assert normal_rerank.await_args.kwargs['top_k'] == agent_rerank.await_args.kwargs['top_k'] == 2


def test_real_loopback_http_rerank_keeps_loop_responsive(monkeypatch):
    """Exercise HTTPX over a real TCP socket; provider response stays synthetic."""
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        requests = []
        async def serve(reader, writer):
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                size = next(int(line.split(b':', 1)[1]) for line in header.split(b'\r\n') if line.lower().startswith(b'content-length:'))
                payload = json.loads(await reader.readexactly(size))
                requests.append(payload)
                started.set()
                await release.wait()
                body = json.dumps({'results': [{'index': 1, 'relevance_score': .9}, {'index': 0, 'score': .4}]}).encode()
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: ' + str(len(body)).encode() + b'\r\nConnection: close\r\n\r\n' + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(serve, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        model = SimpleNamespace(base_url=f'http://127.0.0.1:{port}/v1', api_key='synthetic', model_name='fixed-model')
        task = None
        try:
            task = asyncio.create_task(rerank.RerankService(None).arerank('q', ['first', 'second'], model))
            await asyncio.wait_for(started.wait(), 2)
            assert not task.done()
            # A lightweight callback advances before the delayed HTTP result is released.
            callback_ran = asyncio.Event()
            asyncio.get_running_loop().call_soon(callback_ran.set)
            await asyncio.wait_for(callback_ran.wait(), .5)
            assert not task.done()
            release.set()
            assert await asyncio.wait_for(task, 2) == [
                {'index': 1, 'score': .9, 'text': 'second'}, {'index': 0, 'score': .4, 'text': 'first'}]
            assert requests == [{'model': 'fixed-model', 'query': 'q', 'documents': ['first', 'second'], 'top_n': 2, 'return_documents': False}]
        finally:
            release.set()
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            server.close()
            await server.wait_closed()
    # All remote destinations remain forbidden; numeric loopback is local test traffic.
    import _socket
    def local_dns(host, *args, **kwargs):
        if host not in ('127.0.0.1', b'127.0.0.1', '::1', b'::1'):
            raise AssertionError('External DNS forbidden')
        return _socket.getaddrinfo(host, *args, **kwargs)
    monkeypatch.setattr(socket, 'getaddrinfo', local_dns)
    asyncio.run(run())


def test_async_fallback_preserves_native_trace_generation_and_usage(monkeypatch, tmp_path):
    from app.services import observability as obs
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-async-rerank-synthetic', secret_key='synthetic',
        tracer_provider=TracerProvider(), span_exporter=exporter, mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs, '_client', client)
    monkeypatch.setattr(obs, '_attempted', True)
    async def handler(request):
        if request.url.path.endswith('/rerank'):
            return httpx.Response(404, json={})
        return httpx.Response(200, json={'id': 'synthetic', 'model': 'fixed-model',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '{"candidates":[{"index":0,"score":0.9}]}'}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 5, 'completion_tokens': 4, 'total_tokens': 9}})
    bind_transport(monkeypatch, handler)
    try:
        async def run():
            with obs.span('qa.request'):
                return await rerank.async_batch_rerank('q', [{'id': 'A', 'content': 'evidence', 'score': .5}],
                    rerank.RerankService(None), MODEL, top_k=1)
        assert asyncio.run(run())[0]['rerank_score'] == .9
        client.flush()
        spans = exporter.get_finished_spans()
        names = [span.name for span in spans]
        assert names.count('rerank.batch') == names.count('rerank.call') == names.count('rerank.chat') == 1
        generation = next(span for span in spans if span.name == 'rerank.chat')
        attrs = dict(generation.attributes)
        assert attrs['langfuse.observation.type'] == 'generation'
        assert json.loads(attrs['langfuse.observation.usage_details']) == {'input': 5, 'output': 4, 'total': 9}
        assert json.loads(attrs['langfuse.observation.metadata.execution_status']) == 'RETURNED'
        assert generation.parent.span_id == next(span for span in spans if span.name == 'rerank.call').context.span_id
        events = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
        finished = [row for row in events if row.get('stage') == 'provider.request_finished']
        assert [row['metadata']['status'] for row in finished] == ['FAILED', 'RETURNED']
    finally:
        client.shutdown()

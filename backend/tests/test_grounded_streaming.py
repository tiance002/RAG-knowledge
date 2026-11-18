import asyncio
import json
import socket
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from openai import AsyncOpenAI
from app.schemas import Message, MessageRole
from app.services import chat, grounded_generation
from app.services.llm import LLMService


class FakeSession:
    def __init__(self, *args): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, *args): return object()


def make_service(llm):
    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = llm
    service._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=20)
    service._resolved_llm = lambda model: llm
    service._retrieve_with_new_service = AsyncMock(return_value=(
        [{'id': 'c1', 'file_id': 'f1', 'file_name': 'report', 'content': 'frozen evidence', 'score': .9}],
        {'retrieval_status': 'SUCCESS'}))
    return service


def setup(monkeypatch):
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    monkeypatch.setenv('RAG_GROUNDED_STREAM_ENABLED', 'true')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.delenv('RAG_MODEL_ROUTER_OFF_FILE', raising=False)
    monkeypatch.setattr(chat, 'Session', FakeSession)


@pytest.mark.parametrize('finish,expected', [('stop', 'COMPLETED'), ('length', 'TRUNCATED'), ('content_filter', 'UNKNOWN')])
def test_stream_preserves_partial_and_terminal(monkeypatch, finish, expected):
    setup(monkeypatch)
    traces = []
    monkeypatch.setattr(chat, 'trace_event', lambda name, **data: traces.append((name, data)))
    closed = []
    llm = LLMService(base_url='https://example.invalid', api_key='fake', model='synthetic')
    class Stream:
        def __aiter__(self):
            self.index = 0
            return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='partial [1]'), finish_reason=None)], usage=None)
            if self.index == 2:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None), finish_reason=finish)], usage=None)
            raise StopAsyncIteration
        async def close(self): closed.append(True)
    create = AsyncMock(return_value=Stream())
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    service = make_service(llm)
    async def run():
        return [item async for item in service._chat_stream_normal([Message(role=MessageRole.USER, content='question')], ['kb'], 5, 0, None, False, 0, None)]
    events = asyncio.run(run())
    assert [value['content'] for kind, value in events if kind == 'answer_chunk'] == ['partial [1]']
    assert next(value for kind, value in events if kind == 'done')['generation_status'] == expected
    assert next(value for kind, value in events if kind == 'done')['response_delivery'] == 'streaming'
    assert any(name == 'qa.answer' and data.get('response_delivery') == 'streaming' for name, data in traces)
    assert next(value for kind, value in events if kind == 'done')['usage_status'] == 'UNKNOWN'
    assert next(value for kind, value in events if kind == 'rag_result')['citations'][0]['chunk_id'] == 'c1'
    assert closed == [True] and create.await_count == 1
    kwargs = create.await_args.kwargs
    assert kwargs['stream'] is True and kwargs['max_tokens'] == grounded_generation.get_settings().LLM_ANSWER_MAX_TOKENS
    assert '[1] 来源：report' in kwargs['messages'][0]['content']


def test_router_on_keeps_buffered_and_flag_default_off(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    assert not grounded_generation.grounded_stream_enabled()
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.delenv('RAG_GROUNDED_STREAM_ENABLED')
    assert not grounded_generation.grounded_stream_enabled()


def test_timeout_after_partial_is_terminal_without_retry(monkeypatch):
    setup(monkeypatch)
    traces = []
    monkeypatch.setattr(chat, 'trace_event', lambda name, **data: traces.append((name, data)))
    llm = LLMService(base_url='https://example.invalid', api_key='fake', model='synthetic')
    closed = []
    class Stream:
        def __aiter__(self): self.index=0; return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='partial'), finish_reason=None)], usage=None)
            raise httpx.ReadTimeout('fake')
        async def close(self): closed.append(True)
    create=AsyncMock(return_value=Stream())
    llm._client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    service=make_service(llm)
    async def run():
        return [item async for item in service._chat_stream_normal([Message(role=MessageRole.USER, content='question')], ['kb'], 5, 0, None, False, 0, None)]
    events=asyncio.run(run())
    assert ('answer_chunk', {'content':'partial'}) in events
    assert events[-1][0] == 'error' and events[-1][1]['status'] == 'UNKNOWN' and events[-1][1]['partial']
    assert events[-1][1]['response_delivery'] == 'streaming'
    assert any(name == 'qa.error' and data.get('response_delivery') == 'streaming' for name, data in traces)
    assert not any(kind=='done' for kind, _ in events)
    assert create.await_count == 1 and closed == [True]


@pytest.mark.parametrize('fail_after_first', [False, True])
def test_real_sse_and_sdk_transport_deliver_first_chunk_before_completion(monkeypatch, fail_after_first):
    import uvicorn
    from fastapi import FastAPI, Request
    from app.api import chat as chat_api
    from app.schemas import ChatRequest
    setup(monkeypatch)
    requests=[]
    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            def chunk(content, finish=None):
                return ('data: '+json.dumps({'id':'synthetic','object':'chat.completion.chunk','created':1,'model':'synthetic',
                    'choices':[{'index':0,'delta':{'content':content},'finish_reason':finish}]})+'\n\n').encode()
            yield chunk('first [1]')
            await asyncio.sleep(.25)
            if fail_after_first:
                raise httpx.ReadTimeout('synthetic provider timeout')
            yield chunk(' second')
            yield chunk(None, 'stop')
            yield b'data: [DONE]\n\n'
    async def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=ProviderStream())
    transport=httpx.MockTransport(handler)
    llm=LLMService(base_url='https://example.invalid/v1',api_key='fake',model='synthetic')
    llm._client=AsyncOpenAI(base_url='https://example.invalid/v1',api_key='fake',max_retries=0,
                            http_client=httpx.AsyncClient(transport=transport))
    service=make_service(llm)
    from app.services import memory
    monkeypatch.setattr(chat, 'get_runtime_settings', lambda: SimpleNamespace(max_chat_history_rounds=20, revision=1, degraded=False))
    monkeypatch.setattr(memory, 'get_memory_service', lambda: SimpleNamespace(
        get_preferences=AsyncMock(return_value=memory.PreferencesSnapshot())))
    monkeypatch.setattr(chat_api,'ChatService',lambda: service)
    app=FastAPI()
    @app.post('/api/v1/chat/completions')
    async def endpoint(data: ChatRequest, request: Request):
        return await chat_api.chat_completions(data, session=FakeSession(), memory_scope=None, request=request)
    sock=socket.socket(); sock.bind(('127.0.0.1',0)); sock.listen(64)
    port=sock.getsockname()[1]
    server=uvicorn.Server(uvicorn.Config(app,log_level='error',lifespan='off'))
    thread=threading.Thread(target=server.run,kwargs={'sockets':[sock]},daemon=True); thread.start()
    try:
        deadline=time.monotonic()+5
        while not server.started and time.monotonic()<deadline: time.sleep(.01)
        assert server.started
        body={'messages':[{'role':'user','content':'question'}],'kb_ids':['kb'],'mode':'normal','stream':True}
        frames=[]; current=None
        with httpx.Client(timeout=5,trust_env=False) as client:
            with client.stream('POST',f'http://127.0.0.1:{port}/api/v1/chat/completions',json=body) as response:
                assert response.status_code==200
                for line in response.iter_lines():
                    if line.startswith('event:'): current=line[6:].strip()
                    elif line.startswith('data:'):
                        frames.append((current,json.loads(line[5:].strip()),time.monotonic()))
        chunks=[row for row in frames if row[0]=='answer_chunk']
        assert [row[1]['content'] for row in chunks] == (['first [1]'] if fail_after_first else ['first [1]', ' second'])
        assert frames[-1][2]-chunks[0][2] >= .18
        assert next(row for row in frames if row[0]=='rag_result')[2] <= chunks[0][2]
        if fail_after_first:
            assert frames[-1][0] == 'error' and frames[-1][1]['partial'] is True
            assert frames[-1][1]['generation_status'] == 'UNKNOWN'
        else:
            assert frames[-1][0] == 'done' and frames[-1][1]['generation_status'] == 'COMPLETED'
        assert frames[-1][1]['response_delivery'] == 'streaming'
        assert len(requests)==1 and requests[0]['stream'] is True
    finally:
        server.should_exit=True; thread.join(5); sock.close()
        assert not thread.is_alive()


def test_cancel_after_first_delta_closes_provider_stream(monkeypatch):
    setup(monkeypatch)
    llm = LLMService(base_url='https://example.invalid', api_key='fake', model='synthetic')
    closed = []
    class Stream:
        def __aiter__(self): self.index = 0; return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='partial'), finish_reason=None)], usage=None)
            await asyncio.Event().wait()
        async def close(self): closed.append(True)
    create = AsyncMock(return_value=Stream())
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    service = make_service(llm)
    async def run():
        stream = service._chat_stream_normal([Message(role=MessageRole.USER, content='question')], ['kb'], 5, 0, None, False, 0, None)
        while True:
            kind, value = await anext(stream)
            if kind == 'answer_chunk':
                assert value['content'] == 'partial'
                break
        await stream.aclose()
    asyncio.run(run())
    assert closed == [True] and create.await_count == 1


def test_missing_finish_is_unknown_no_fallback(monkeypatch):
    setup(monkeypatch)
    llm = LLMService(base_url='https://example.invalid', api_key='fake', model='synthetic')
    class Stream:
        def __aiter__(self): self.index = 0; return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='partial'), finish_reason=None)], usage=None)
            raise StopAsyncIteration
        async def close(self): pass
    create = AsyncMock(return_value=Stream())
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    service = make_service(llm)
    async def run():
        return [item async for item in service._chat_stream_normal([Message(role=MessageRole.USER, content='question')], ['kb'], 5, 0, None, False, 0, None)]
    events = asyncio.run(run())
    assert ('answer_chunk', {'content': 'partial'}) in events
    assert events[-1][0] == 'error' and events[-1][1]['generation_status'] == 'UNKNOWN'
    assert create.await_count == 1


def test_buffered_and_streamed_requests_use_identical_evidence_and_budget(monkeypatch):
    setup(monkeypatch)
    citations = [{'chunk_id': 'c1', 'fileName': 'a', 'location': 'P', 'content': 'evidence'}]
    messages = [{'role': 'user', 'content': 'question'}]
    llm = LLMService(base_url='https://example.invalid', api_key='fake', model='synthetic')
    class Stream:
        def __aiter__(self): self.index = 0; return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='answer'), finish_reason='stop')], usage=None)
            raise StopAsyncIteration
        async def close(self): pass
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='answer'), finish_reason='stop')], usage=None)
    create = AsyncMock(side_effect=[response, Stream()])
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    async def run():
        await grounded_generation.generate_grounded_candidate(llm, messages, citations, preference_policy='preference')
        return [item async for item in grounded_generation.stream_grounded_candidate(llm, messages, citations, preference_policy='preference')]
    asyncio.run(run())
    buffered, streamed = (call.kwargs for call in create.await_args_list)
    assert buffered['messages'] == streamed['messages']
    assert buffered['max_tokens'] == streamed['max_tokens']
    assert buffered['temperature'] == streamed['temperature']
    assert buffered['stream'] is False and streamed['stream'] is True


@pytest.mark.parametrize('native', [False, True])
def test_sdk_stream_cancel_releases_managed_provider_slot(monkeypatch, tmp_path, native):
    import hashlib
    from app.services import provider_http, observability
    setup(monkeypatch)
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true' if native else 'false')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events'))
    monkeypatch.setattr(observability, '_sdk', lambda: None)
    monkeypatch.setenv('RAG_PROVIDER_MAX_IN_FLIGHT', '1')
    closed = []
    class ProviderStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = {'id':'synthetic', 'object':'chat.completion.chunk', 'created':1, 'model':'synthetic',
                    'choices':[{'index':0, 'delta':{'content':'partial'}, 'finish_reason':None}]}
            yield ('data: ' + json.dumps(data) + '\n\n').encode()
            await asyncio.Event().wait()
        async def aclose(self):
            closed.append(True)
    async def handler(request):
        return httpx.Response(200, headers={'content-type':'text/event-stream'}, stream=ProviderStream())
    async def run():
        async with provider_http.managed_provider_http() as manager:
            inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            # Match the real SDK's phase-aware 120s transport pool identity.
            key = provider_http._pool_key('https://example.invalid/v1', 'fake', 120.0, True, False)
            manager.clients[key] = inner
            llm = LLMService(base_url='https://example.invalid/v1', api_key='fake', model='synthetic')
            service = make_service(llm)
            stream = service._chat_stream_normal([Message(role=MessageRole.USER, content='question')], ['kb'], 5, 0, None, False, 0, None)
            while True:
                kind, value = await anext(stream)
                if kind == 'answer_chunk':
                    assert value['content'] == 'partial'
                    break
            assert manager.slots._value == 0
            await stream.aclose()
            assert manager.slots._value == 1 and closed
        assert inner.is_closed
    asyncio.run(run())


def test_stream_usage_only_from_provider(monkeypatch):
    setup(monkeypatch)
    llm = LLMService(base_url='https://example.invalid', api_key='fake', model='synthetic')
    class Stream:
        def __aiter__(self): self.index=0; return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='answer'), finish_reason='stop')], usage=None)
            if self.index == 2:
                return SimpleNamespace(choices=[], usage={'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10})
            raise StopAsyncIteration
        async def close(self): pass
    create = AsyncMock(return_value=Stream())
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    service = make_service(llm)
    async def run():
        return [item async for item in service._chat_stream_normal([Message(role=MessageRole.USER, content='question')], ['kb'], 5, 0, None, False, 0, None)]
    events = asyncio.run(run())
    done = next(value for kind, value in events if kind == 'done')
    assert done['usage'] == {'prompt_tokens': 7, 'completion_tokens': 3, 'total_tokens': 10}
    assert done['usage_status'] == 'PROVIDER_REPORTED'


@pytest.mark.parametrize('native', [False, True])
def test_observe_closing_error_does_not_mask_cancel(monkeypatch, tmp_path, native):
    from app.services import observability
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true' if native else 'false')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events'))
    monkeypatch.setattr(observability, '_sdk', lambda: None)
    @observability.observe('llm.generate_stream')
    async def inner():
        try:
            yield 'partial'
        finally:
            raise RuntimeError('synthetic close failure')
    async def run():
        stream = inner()
        assert await anext(stream) == 'partial'
        await stream.aclose()
    asyncio.run(run())


def test_provider_timeout_remains_unknown_when_stream_close_fails(monkeypatch):
    setup(monkeypatch)
    llm = LLMService(base_url='https://example.invalid', api_key='fake', model='synthetic')
    class Stream:
        def __aiter__(self): self.index=0; return self
        async def __anext__(self):
            self.index += 1
            if self.index == 1:
                return SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content='partial'), finish_reason=None)], usage=None)
            raise httpx.ReadTimeout('provider timeout')
        async def close(self): raise RuntimeError('close failure')
    create = AsyncMock(return_value=Stream())
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    service = make_service(llm)
    async def run():
        return [item async for item in service._chat_stream_normal([Message(role=MessageRole.USER, content='question')], ['kb'], 5, 0, None, False, 0, None)]
    events = asyncio.run(run())
    assert events[-1][0] == 'error' and events[-1][1]['status'] == 'UNKNOWN'
    assert events[-1][1]['partial'] and create.await_count == 1


def test_trace_delivery_marker_is_controlled_enum():
    from app.services.observability import sanitize
    assert sanitize({'response_delivery': 'streaming'})['response_delivery'] == 'streaming'
    assert sanitize({'response_delivery': 'private-other'})['response_delivery'] != 'private-other'

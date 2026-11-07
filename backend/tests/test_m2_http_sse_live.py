"""Loopback HTTP/SSE integration with disposable Redis and deterministic model/retrieval."""
import json
import ipaddress
import socket
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from langchain_core.messages import AIMessage
from redis.asyncio import Redis

from test_memory_live import local_redis, KEY
from app.api.chat import router
from app.core.database import get_session
from app.services import agent_workflow, chat, grounded_generation, memory

pytestmark = pytest.mark.live


class FakeSession:
    def __init__(self, *args): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, model, key): return object()


def parse_sse(response):
    assert response.status_code == 200
    assert response.headers['content-type'].startswith('text/event-stream')
    events = []
    for block in response.text.replace('\r\n', '\n').split('\n\n'):
        lines = block.splitlines()
        kinds = [x[7:] for x in lines if x.startswith('event: ')]
        data = [x[6:] for x in lines if x.startswith('data: ')]
        if kinds:
            assert len(kinds) == len(data) == 1
            events.append((kinds[0], json.loads(data[0])))
    return events


@pytest.mark.parametrize('mode', ['chat', 'agent'])
@pytest.mark.parametrize('preference_state', ['saved', 'override', 'unavailable'])
def test_http_sse_preference_and_evidence(monkeypatch, mode, preference_state):
    monkeypatch.setenv('LANGFUSE_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def check_address(address):
        try:
            allowed = isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback
        except ValueError:
            allowed = False
        if not allowed:
            raise RuntimeError('Test forbids non-loopback network connections')

    def guarded_connect(sock, address):
        check_address(address)
        return original_connect(sock, address)

    def guarded_connect_ex(sock, address):
        check_address(address)
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, 'connect', guarded_connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', guarded_connect_ex)
    with socket.socket() as probe:
        with pytest.raises(RuntimeError, match='non-loopback'):
            probe.connect(('192.0.2.1', 443))
        with pytest.raises(RuntimeError, match='non-loopback'):
            probe.connect_ex(('192.0.2.1', 443))
    calls = []
    evidence = 'Disposable evidence: the value is 42.'
    llm = SimpleNamespace(model='offline', get_chat_model=lambda **kwargs: object())
    async def completion(**kwargs):
        calls.append(('final', kwargs['messages']))
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='Value is 42 [1]'), finish_reason='stop')], usage=None)
    llm._get_client = lambda: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = llm
    service._retrieve_with_new_service = AsyncMock(return_value=([{'id':'chunk-1', 'file_id':'file-1', 'kb_id':'kb-1', 'file_name':'source.txt', 'content':evidence, 'score':0.9}], {'retrieval_status':'SUCCESS'}))
    monkeypatch.setattr(chat.ChatService, '__init__', lambda self: self.__dict__.update(service.__dict__))
    monkeypatch.setattr(chat, 'Session', FakeSession)
    monkeypatch.setattr(agent_workflow.AgentWorkflow, '_get_llm_service', lambda *args: llm)
    monkeypatch.setattr(grounded_generation, '_routed_chat_model', lambda *args: object())
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    schema = agent_workflow.search_knowledge_base.args_schema
    count = {'agent': 0}
    async def invoke(model, messages, **kwargs):
        stage = kwargs['stage']
        calls.append((stage, [m.model_dump() for m in messages]))
        if stage == 'intent': return AIMessage(content='qa')
        if stage == 'quality': return AIMessage(content='0.9')
        count['agent'] += 1
        if count['agent'] == 1:
            return AIMessage(content='', tool_calls=[{'name':'search_knowledge_base', 'id':'tool-1', 'args':{'query':'value'}}])
        return AIMessage(content='Value is 42 [1]', response_metadata={'finish_reason':'stop'})
    async def search(args, **kwargs):
        return json.dumps({'retrieval_status':'SUCCESS', 'context':evidence, 'citations':[{'chunk_id':'chunk-1', 'content':evidence, 'fileId':'file-1', 'kb_id':'kb-1'}]})
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', invoke)
    monkeypatch.setattr(agent_workflow, 'search_knowledge_base', SimpleNamespace(args_schema=schema, ainvoke=search))
    with tempfile.TemporaryDirectory(prefix='rag-http-sse-') as temp:
        with local_redis(Path(temp) / 'redis', aof=False) as (url, admin):
            admin.hset(KEY, mapping={'answer_style':'concise'})
            # Keep an unlistened socket bound so this endpoint cannot accidentally
            # connect to a different local service during the failure case.
            unavailable = socket.socket()
            unavailable.bind(('127.0.0.1', 0))
            target = (f"redis://127.0.0.1:{unavailable.getsockname()[1]}/0"
                      if preference_state == 'unavailable' else url)
            db = Redis.from_url(target, decode_responses=True, socket_connect_timeout=0.3, socket_timeout=0.5)
            store = memory.MemoryService(client=db)
            monkeypatch.setattr(memory, 'get_memory_service', lambda: store)
            @asynccontextmanager
            async def lifespan(app):
                yield
                await db.aclose()
            app = FastAPI(lifespan=lifespan)
            app.include_router(router, prefix='/api/v1')
            app.dependency_overrides[get_session] = lambda: FakeSession()
            with socket.socket() as sock:
                sock.bind(('127.0.0.1', 0))
                port = sock.getsockname()[1]
                server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False))
                thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
                thread.start()
                try:
                    deadline = time.monotonic() + 10
                    while not server.started and thread.is_alive() and time.monotonic() < deadline: time.sleep(0.03)
                    assert server.started
                    question = '这次详细解释数值是什么？' if preference_state == 'override' else '数值是什么？'
                    response = httpx.post(f'http://127.0.0.1:{port}/api/v1/chat/completions', json={'messages':[{'role':'user','content':question}], 'kb_ids':['kb-1'], 'mode':mode, 'use_rewrite':False}, timeout=20, trust_env=False)
                    events = parse_sse(response)
                    assert events[-1][0] == 'done' and not any(k == 'error' for k, _ in events), events
                    citations = [c for kind, data in events if kind == 'rag_result' for c in data['citations']]
                    assert any(c['chunk_id'] == 'chunk-1' and c['content'] == evidence and c['fileId'] == 'file-1' for c in citations)
                    assert 'Value is 42 [1]' in ''.join(data.get('content','') for kind, data in events if kind == 'answer_chunk')
                    final = [messages for stage, messages in calls if stage == ('final' if mode == 'chat' else 'agent')][-1]
                    body = str(final)
                    assert evidence in body
                    if preference_state == 'unavailable':
                        assert '回答表达偏好' not in body
                        before = len(calls)
                        failed = parse_sse(httpx.post(f'http://127.0.0.1:{port}/api/v1/chat/completions', json={'messages':[{'role':'user','content':'以后回答都简洁一点。'}], 'kb_ids':[], 'mode':mode}, timeout=20, trust_env=False))
                        assert failed[-1][0] == 'error' and failed[-1][1]['code'] == 'MEMORY_UNAVAILABLE'
                        assert not any(kind == 'done' for kind, _ in failed) and len(calls) == before
                    else:
                        assert ('详细解释' if preference_state == 'override' else '回答简洁') in body
                        if preference_state == 'override': assert '回答简洁' not in body
                    assert admin.hget(KEY, 'answer_style') == 'concise'
                finally:
                    server.should_exit = True
                    thread.join(timeout=10)
                    unavailable.close()
                    assert not thread.is_alive()

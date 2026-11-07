"""Offline product/SSE integration; every external boundary is replaced."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas import ChatRequest, Message, MessageRole
from app.services import chat, memory, web_search, grounded_generation


def service(monkeypatch):
    prefs = memory.PreferencesSnapshot(values=memory.PreferenceValues(),
        sources={key: 'default' for key in memory.FIELDS})
    monkeypatch.setattr(memory, 'get_memory_service', lambda:
                        SimpleNamespace(get_preferences=AsyncMock(return_value=prefs)))
    instance = chat.ChatService.__new__(chat.ChatService)
    instance.llm_service = SimpleNamespace(model='offline-model')
    return instance


def collect(instance, messages, **options):
    async def consume():
        return [event async for event in instance.chat_stream(messages, kb_ids=[], **options)]
    return asyncio.run(consume())


def messages():
    return [Message(role=MessageRole.USER, content='私有历史问题'),
            Message(role=MessageRole.ASSISTANT, content='私有知识库片段'),
            Message(role=MessageRole.USER, content='请根据来源解释公开产品')]


@pytest.mark.parametrize('mode', ['chat', 'agent'])
def test_opt_in_chat_and_agent_use_web_branch_before_kb_preflight(monkeypatch, mode):
    instance = service(monkeypatch)
    evidence = {'source_type': 'web', 'url': 'https://example.org/doc', 'title': '官网',
                'fileName': '官网', 'kbName': '互联网', 'location': 'https://example.org/doc',
                'content': '实际网页证据', 'score': 0.9}
    search = AsyncMock(return_value=web_search.WebSearchResult('SUCCESS', messages()[-1].content,
        [evidence], 'search-id', {'credits': 1}))
    generate = AsyncMock(return_value=('根据网页的回答。[1]', 'stop', {'total_tokens': 14}))
    monkeypatch.setattr(web_search, 'search_web', search)
    monkeypatch.setattr(grounded_generation, 'generate_grounded_candidate', generate)
    monkeypatch.setattr(chat, '_scope_preflight', lambda *a: pytest.fail('must not preflight KB'))
    monkeypatch.setattr(instance, '_chat_stream_langgraph', lambda *a, **k: pytest.fail('separate web mode'))
    events = collect(instance, messages(), mode=mode, web_search_enabled=True)
    search.assert_awaited_once_with('请根据来源解释公开产品')
    generate.assert_awaited_once()
    model_messages = generate.await_args.args[1]
    assert '私有' not in str(model_messages)
    assert generate.await_args.args[2] == [evidence]
    result = next(data for kind, data in events if kind == 'rag_result')
    assert result['citations'] == [evidence]
    assert result['web_search']['request_id'] == 'search-id'
    assert events[0][1]['requested_mode'] == mode
    assert events[-1][1]['answer_mode'] == 'WEB_SEARCH_GROUNDED'
    assert events[-1][1]['generation_status'] == 'COMPLETED'
    assert events[-1][1]['validation_status'] == 'NOT_RUN'
    assert any(kind == 'answer_chunk' and data['content'] == '根据网页的回答。[1]' for kind, data in events)


@pytest.mark.parametrize('state', ['NO_RESULTS', 'FAILED', 'UNKNOWN', 'NOT_CONFIGURED'])
def test_unavailable_search_has_no_model_call_or_hidden_kb_fallback(monkeypatch, state):
    instance = service(monkeypatch)
    search = AsyncMock(return_value=web_search.WebSearchResult(state, '当前问题'))
    generate = AsyncMock(side_effect=AssertionError('no model call'))
    monkeypatch.setattr(web_search, 'search_web', search)
    monkeypatch.setattr(grounded_generation, 'generate_grounded_candidate', generate)
    events = collect(instance, messages(), web_search_enabled=True)
    assert events[-1][1]['web_search_status'] == state
    if state == 'NO_RESULTS':
        assert events[-1][0] == 'done' and events[-1][1]['generation_status'] == 'NOT_RUN'
    else:
        assert events[-1][0] == 'error'
        assert events[-1][1]['code'] == 'WEB_SEARCH_' + state
        assert events[-1][1]['retryable'] is False
        assert not any(kind == 'done' for kind, _ in events)
    assert next(data for kind, data in events if kind == 'rag_result')['citations'] == []
    generate.assert_not_awaited()


def test_default_false_keeps_existing_chat_path(monkeypatch):
    instance = service(monkeypatch)
    async def ordinary(**kwargs):
        yield ('done', {'existing_path': True})
    monkeypatch.setattr(instance, '_chat_stream_normal', ordinary)
    search = AsyncMock(side_effect=AssertionError('opt-in required'))
    monkeypatch.setattr(web_search, 'search_web', search)
    events = collect(instance, [Message(role=MessageRole.USER, content='你好')])
    assert events == [('done', {'existing_path': True})]
    assert ChatRequest(messages=[], kb_ids=[]).web_search_enabled is False
    search.assert_not_awaited()


def test_model_unknown_is_terminal_without_replay(monkeypatch):
    instance = service(monkeypatch)
    monkeypatch.setattr(web_search, 'search_web', AsyncMock(return_value=web_search.WebSearchResult(
        'SUCCESS', 'query', [{'content': 'web evidence'}])))
    generate = AsyncMock(side_effect=TimeoutError('uncertain'))
    monkeypatch.setattr(grounded_generation, 'generate_grounded_candidate', generate)
    events = collect(instance, messages(), web_search_enabled=True)
    assert events[-1][0] == 'error' and events[-1][1]['status'] == 'UNKNOWN'
    generate.assert_awaited_once()
    assert not any(kind == 'done' for kind, _ in events)


def test_api_forwards_opt_in_and_serializes_web_sse(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import chat as chat_api
    app = FastAPI()
    app.include_router(chat_api.router)
    app.dependency_overrides[chat_api.get_session] = lambda: SimpleNamespace()
    app.dependency_overrides[chat_api.get_preference_scope] = lambda: None
    captured = []
    class FakeChat:
        async def chat_stream(self, **kwargs):
            captured.append(kwargs)
            yield ('web_search_status', {'status': 'NO_RESULTS', 'execution_mode': 'WEB_SEARCH_GROUNDED'})
            yield ('done', {'web_search_status': 'NO_RESULTS'})
    monkeypatch.setattr(chat_api, 'ChatService', FakeChat)
    with TestClient(app) as client:
        response = client.post('/chat/completions', json={'messages': [{'role': 'user', 'content': '公开问题'}],
                                                        'kb_ids': [], 'web_search_enabled': True})
    assert response.status_code == 200
    assert captured[0]['web_search_enabled'] is True
    assert 'event: web_search_status' in response.text
    payloads = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert payloads[-1]['web_search_status'] == 'NO_RESULTS'

"""Offline web snapshot identity, actual provider payload and SSE contracts."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import chat, web_search
from app.services.conversation_context import delivery_capture_scope
from app.services.web_source_registry import build_web_registry, validate_web_snapshot


def fresh(url='https://example.org/a', content='公开产品 安装 说明'):
    return {'source_type': 'web', 'url': url, 'title': '公开产品', 'content': content}


def snapshot(**changes):
    row = build_web_registry([fresh()], [], '').entries[0]
    return {**row, 'source_id': 99, 'source_alias': 's99', **changes}


def test_snapshot_validation_and_request_local_dedup():
    old = snapshot()
    same_url_new_body = snapshot(content='新正文')
    assert validate_web_snapshot(same_url_new_body) is None
    assert validate_web_snapshot(snapshot(url='file:///secret')) is None
    assert validate_web_snapshot(snapshot(web_snapshot_status='UNKNOWN')) is None
    assert validate_web_snapshot(snapshot(source_type='kb')) is None
    assert 'source_id' not in validate_web_snapshot(old)
    reg = build_web_registry([fresh(), fresh()], [old], '公开产品 安装')
    assert len(reg.entries) == 1
    assert reg.entries[0]['source_alias'] == 's1'
    assert reg.entries[0]['origin'] == 'CURRENT_WEB_SEARCH'
    assert build_web_registry([], [old], '完全不同的问题').entries == []
    reused = build_web_registry([], [old], '公开产品 安装').entries[0]
    assert reused['source_id'] == 1 and reused['source_alias'] == 's1'
    assert reused['origin'] == 'HISTORY_WEB_SNAPSHOT'
    assert reused['fetched_at'] == old['fetched_at']


def test_same_url_different_body_is_a_distinct_snapshot():
    old = snapshot()
    reg = build_web_registry([fresh(content='公开产品 安装 新版')], [old], '公开产品 安装')
    assert [c['source_id'] for c in reg.entries] == [1, 2]
    assert [c['content'] for c in reg.entries] == ['公开产品 安装 新版', old['content']]


def test_explicit_followup_uses_latest_turn_but_ordinary_query_requires_relevance():
    older = snapshot(history_turn_id='old')
    latest = snapshot(history_turn_id='new', url='https://example.org/new')
    assert build_web_registry([], [older, latest], '无关的新主题').entries == []
    selected = build_web_registry([], [older, latest], '请展开上一条的说明').entries
    assert len(selected) == 1 and selected[0]['url'] == latest['url']
    assert selected[0]['source_alias'] == 's1'
    for question in ('它具体是什么？', 'What about its limitations?'):
        selected = build_web_registry([], [older, latest], question).entries
        assert len(selected) == 1 and selected[0]['url'] == latest['url']
        assert selected[0]['selection_reason'] == 'LATEST_TURN_PRONOUN_HEURISTIC'
    assert build_web_registry([], [older, latest], '它和火星探测有何关系？').entries == []


@pytest.mark.parametrize('state', ['SUCCESS', 'NO_RESULTS'])
def test_actual_provider_messages_and_history_alias_translation(monkeypatch, state):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    captured = []
    async def create(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(id='offline', choices=[SimpleNamespace(
            message=SimpleNamespace(content='公开产品说明 [s1]，未知 [s99]'), finish_reason='stop')], usage=None)
    llm = SimpleNamespace(model='offline', _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    instance = chat.ChatService.__new__(chat.ChatService)
    instance._history_evidence = [snapshot()]
    monkeypatch.setattr(instance, '_resolved_llm', lambda _: llm)
    monkeypatch.setattr(web_search, 'search_web', AsyncMock(return_value=web_search.WebSearchResult(
        state, '公开产品 安装', [fresh()] if state == 'SUCCESS' else [])))
    capture = {}
    async def consume():
        with delivery_capture_scope(capture):
            return [event async for event in instance._chat_stream_web('公开产品 安装')]
    events = asyncio.run(consume())
    assert len(captured) == 1
    payload = str(captured[0]['messages'])
    assert '[s1] 来源' in payload and '公开产品 安装 说明' in payload
    assert '[s99] 来源' not in payload
    assert 'SNAPSHOT_ONLY' in payload and '不代表本轮访问或实时验证' in payload
    answer = next(data['content'] for name, data in events if name == 'answer_chunk')
    assert answer == '公开产品说明 [1]，未知 [s99]'
    assert capture['provider_requests'][0]['evidence'][0]['url'] == fresh()['url']
    assert events[-1][1]['web_search_status'] == state
    assert events[-1][1]['validation_status'] == 'NOT_RUN'
    assert events[-1][1]['source_alias_translation']['restored_alias_count'] == 1
    assert events[-1][1]['source_alias_translation']['unknown_alias_count'] == 1


@pytest.mark.parametrize('state', ['FAILED', 'UNKNOWN', 'NOT_CONFIGURED'])
def test_failed_search_not_hidden_by_history(monkeypatch, state):
    instance = chat.ChatService.__new__(chat.ChatService)
    instance._history_evidence = [snapshot()]
    monkeypatch.setattr(instance, '_resolved_llm', lambda _: pytest.fail('must not generate'))
    monkeypatch.setattr(web_search, 'search_web', AsyncMock(return_value=web_search.WebSearchResult(
        state, '公开产品 安装')))
    async def consume():
        return [event async for event in instance._chat_stream_web('公开产品 安装')]
    events = asyncio.run(consume())
    assert events[-1][0] == 'error'
    assert events[-1][1]['web_search_status'] == state
    assert next(data for name, data in events if name == 'rag_result')['citations'] == []


def test_invalid_success_results_do_not_generate_without_evidence(monkeypatch):
    instance = chat.ChatService.__new__(chat.ChatService)
    monkeypatch.setattr(instance, '_resolved_llm', lambda _: pytest.fail('no evidence'))
    monkeypatch.setattr(web_search, 'search_web', AsyncMock(return_value=web_search.WebSearchResult(
        'SUCCESS', 'query', [{'content': 'missing URL/title'}])))
    async def consume():
        return [event async for event in instance._chat_stream_web('query')]
    events = asyncio.run(consume())
    assert events[-1][1]['web_search_status'] == 'SUCCESS'
    assert events[-1][1]['retrieval_status'] == 'NO_RESULTS'
    assert events[-1][1]['generation_status'] == 'NOT_RUN'

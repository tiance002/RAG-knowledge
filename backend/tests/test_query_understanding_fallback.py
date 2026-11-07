import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app.services import grounded_generation as gg
from app.services import agent_workflow as aw
from app.services import chat
from app.schemas import Message, MessageRole


@pytest.mark.parametrize('strategy,finish,content,expected_finish', [
    ('fixed', None, '', None), ('model', 'stop', '模型答复', 'stop'),
    ('model', 'length', '截断答复', 'length'), ('model', 'stop', '', 'empty'),
])
def test_configured_fallback(strategy, finish, content, expected_finish, monkeypatch):
    config = SimpleNamespace(FALLBACK_STRATEGY=strategy, FALLBACK_RESPONSE='没有资料',
        FALLBACK_PROMPT='自定义提示', LLM_ANSWER_MAX_TOKENS=4096)
    monkeypatch.setattr(gg, 'get_settings', lambda: config)
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish, message=SimpleNamespace(content=content))], usage=None))
    llm = SimpleNamespace(model='test', _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    text, actual, status, _ = asyncio.run(gg.generate_no_results_fallback(llm, '问题'))
    assert actual == strategy and status == expected_finish
    if strategy == 'fixed':
        assert text == '没有资料'
        create.assert_not_awaited()
    elif finish == 'stop' and content:
        assert '模型答复' in text and '未在知识库中找到' in text
        assert '自定义提示' in create.await_args.kwargs['messages'][0]['content']
    else:
        assert text == '没有资料'


def test_agent_defaults_to_retrieval_without_classification_call():
    workflow = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    workflow._get_llm_service = MagicMock(side_effect=AssertionError('unexpected model call'))
    result = asyncio.run(workflow.analyze_intent({'messages': [HumanMessage(content='你好')], 'kb_ids': ['kb']}, {}))
    assert result['intent'] == 'qa'
    workflow._get_llm_service.assert_not_called()


def test_agent_confirmed_empty_uses_fallback_after_retries(monkeypatch):
    helper = AsyncMock(return_value=('没有资料', 'fixed', None, None))
    monkeypatch.setattr(gg, 'generate_no_results_fallback', helper)
    workflow = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    workflow._get_llm_service = lambda state: object()
    data = {'retrieval_status': 'NO_RESULTS', 'citations': [], 'degraded': False}
    state = {'messages': [HumanMessage(content='question'), ToolMessage(
        name='search_knowledge_base', tool_call_id='x', content=json.dumps(data))],
        'original_query': 'question', 'kb_ids': ['kb'], 'retrieval_status': 'NO_RESULTS',
        'retry_count': 3, 'retrieval_retry_scheduled': False, 'citations': [], 'source_registry': []}
    result = asyncio.run(workflow.agent_node(state, {}))
    assert result['messages'][0].content == '没有资料'
    assert result['generation_status'] == 'NOT_RUN'
    helper.assert_awaited_once()


def test_agent_degraded_empty_is_unknown_not_absence():
    raw = ToolMessage(name='search_knowledge_base', tool_call_id='x', content=json.dumps({
        'retrieval_status': 'NO_RESULTS', 'citations': [], 'degraded': True}))
    response = AIMessage(content='unverified')
    aw._guard_knowledge_answer({'messages': [raw], 'retrieval_status': 'UNKNOWN'}, response)
    assert '状态不确定' in response.content


@pytest.mark.parametrize('status,expected,should_fallback', [
    ('NO_RESULTS', 'configured fallback', True),
    ('RETRIEVAL_ERROR', '检索发生错误', False),
    ('UNKNOWN', '状态不确定', False),
])
def test_normal_chat_no_results_does_not_hide_retrieval_failure(monkeypatch, status, expected, should_fallback):
    class Session:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(chat, 'Session', Session)
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'false')
    helper = AsyncMock(return_value=('configured fallback', 'fixed', None, None))
    monkeypatch.setattr(gg, 'generate_no_results_fallback', helper)
    service = chat.ChatService.__new__(chat.ChatService)
    service._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=5)
    service._resolved_llm = lambda model_id: SimpleNamespace(model='test')
    service._retrieve_with_new_service = AsyncMock(return_value=([], {'retrieval_status': status}))
    async def collect():
        return [e async for e in service._chat_stream_normal(
            [Message(role=MessageRole.USER, content='问题')], ['kb'], 5, 0.1,
            None, False, 0.0, None)]
    events = asyncio.run(collect())
    assert expected in next(item['content'] for kind, item in events if kind == 'answer_chunk')
    assert events[-1][1]['retrieval_status'] == status
    assert helper.await_count == int(should_fallback)
    service._retrieve_with_new_service.assert_awaited_once()


def test_normal_chat_uses_query_understanding_for_greeting():
    async def generate_stream(**kwargs):
        yield '你好'
    understand = AsyncMock(return_value={'intent': 'greeting', 'rewrite_query': '你好', 'image_description': ''})
    service = chat.ChatService.__new__(chat.ChatService)
    service._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=5)
    service._resolved_llm = lambda model_id: SimpleNamespace(
        model='test', understand_query=understand, generate_stream=generate_stream)
    service._retrieve_with_new_service = AsyncMock()
    async def collect():
        return [e async for e in service._chat_stream_normal(
            [Message(role=MessageRole.USER, content='你好')], ['kb'], 5, 0.1,
            None, False, 0.0, None, use_rewrite=True)]
    events = asyncio.run(collect())
    assert any(kind == 'answer_chunk' and item['content'] == '你好' for kind, item in events)
    understand.assert_awaited_once()
    service._retrieve_with_new_service.assert_not_awaited()

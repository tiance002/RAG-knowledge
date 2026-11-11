import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.schemas import Message, MessageRole
from app.services import chat, memory, agent_workflow


@pytest.mark.parametrize('history,expected', [
    ([(MessageRole.ASSISTANT, 'prior answer')], True),
    ([(MessageRole.USER, 'prior question'), (MessageRole.ASSISTANT, 'prior answer')], True),
    ([], False),
])
def test_chat_stream_passes_untrimmed_history_indicator(monkeypatch, history, expected):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    snapshot = SimpleNamespace(max_chat_history_rounds=0, revision=1, degraded=False)
    monkeypatch.setattr(chat, 'get_runtime_settings', lambda: snapshot)
    monkeypatch.setattr(memory, 'get_memory_service', lambda: SimpleNamespace(
        get_preferences=AsyncMock(return_value=memory.PreferencesSnapshot())))
    service = chat.ChatService.__new__(chat.ChatService)
    received = []
    async def stub(**kwargs):
        received.append(kwargs)
        yield 'done', {'generation_status': 'NOT_RUN'}
    service._chat_stream_langgraph = stub
    messages = [Message(role=role, content=content) for role, content in history]
    messages.append(Message(role=MessageRole.USER, content='follow-up'))
    async def run():
        return [item async for item in service.chat_stream(messages=messages, kb_ids=['kb'], mode='agent')]
    assert asyncio.run(run())[-1][0] == 'done'
    assert received[0]['has_prior_conversation'] is expected
    assert len(received[0]['messages']) == 1


def test_direct_method_derives_indicator_if_not_supplied(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    states = []
    class Graph:
        async def astream_events(self, state, version):
            states.append(state)
            if False:
                yield None
    monkeypatch.setattr(agent_workflow, 'AgentWorkflow', lambda: SimpleNamespace(app=Graph()))
    service = chat.ChatService.__new__(chat.ChatService)
    service._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=0)
    service._resolved_llm = lambda _: SimpleNamespace(base_url='https://fixture.invalid', api_key='fake', model='fake')
    messages = [Message(role=MessageRole.ASSISTANT, content='prior'), Message(role=MessageRole.USER, content='follow-up')]
    async def run():
        return [item async for item in service._chat_stream_langgraph(messages, ['kb'], 5, 0, None)]
    assert asyncio.run(run())[-1][0] == 'done'
    assert states[0]['has_prior_conversation'] is True
    assert len(states[0]['messages']) == 1

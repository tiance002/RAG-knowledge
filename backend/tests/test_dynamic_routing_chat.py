"""ChatService -> real routed generator -> fake provider; no paid calls."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.schemas import Message, MessageRole
from app.services import chat, grounded_generation


@pytest.mark.parametrize('mode,evidence,cheap_answer,expected', [
    ('OFF', True, '收入100万元。[1]', (1, 0, 0)),
    ('CHEAP_ONLY', True, '收入100万元。[1]', (0, 1, 0)),
    ('DYNAMIC', True, '收入100万元。[2]', (0, 1, 1)),
    ('DYNAMIC', False, '收入100万元。[2]', (0, 0, 0)),
])
def test_chat_stream_real_routing_boundary(monkeypatch, mode, evidence, cheap_answer, expected):
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', mode)
    monkeypatch.setenv('RAG_MODEL_ROUTER_UNKNOWN_POLICY', 'CHEAP')
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', 'REFERENCE_RANGE')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    monkeypatch.delenv('RAG_MODEL_ROUTER_OFF_FILE', raising=False)
    class Session:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(chat, 'Session', Session)
    def fake_service(model, answer):
        response = SimpleNamespace(id='request', model=model, usage=None, choices=[
            SimpleNamespace(message=SimpleNamespace(content=answer), finish_reason='stop')])
        call = AsyncMock(return_value=response)
        return SimpleNamespace(model=model, _get_client=lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=call)))), call
    baseline, base_call = fake_service('baseline', '收入100万元。[1]')
    cheap, cheap_call = fake_service('Qwen/Qwen3.5-4B', cheap_answer)
    expensive, exp_call = fake_service('zai-org/GLM-5.3-Flash', '收入100万元。[1]')
    monkeypatch.setattr(grounded_generation, '_role_service', lambda role, expected: cheap if role == 'CHEAP' else expensive)
    observed = []
    monkeypatch.setattr(grounded_generation, 'event', lambda name, **kwargs: observed.append((name, kwargs)))
    service = chat.ChatService.__new__(chat.ChatService)
    service.llm_service = baseline
    rows = [{'id': 'c1', 'file_id': 'f1', 'file_name': 'report', 'content': '收入100万元。', 'score': .9}] if evidence else []
    service._retrieve_with_new_service = AsyncMock(return_value=(rows, {'retrieval_status': 'SUCCESS' if evidence else 'NO_RESULTS'}))
    async def collect():
        return [item async for item in service._chat_stream_normal(
            [Message(role=MessageRole.USER, content='根据知识库回答收入')], ['kb'], 5, 0, None, False, 0, None)]
    events = asyncio.run(collect())
    assert tuple(call.await_count for call in (base_call, cheap_call, exp_call)) == expected
    assert events[-1][0] == 'done'
    if mode == 'DYNAMIC' and evidence:
        assert cheap_call.await_args.kwargs['messages'] == exp_call.await_args.kwargs['messages']
        outcome = next(data for name, data in observed if name == 'router.outcome')
        assert outcome['status'] == 'COMPLETED' and outcome['retry_count'] == 1
        assert outcome['cost']['status'] == 'UNKNOWN'
    if mode == 'OFF':
        assert not any(name.startswith('router.') for name, _ in observed)

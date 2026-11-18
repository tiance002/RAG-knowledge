"""Bounded direct history routing contracts; provider streams are test doubles."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import grounded_generation as generation
from app.services.llm import LLMService


HISTORY = [{'role': 'user', 'content': 'Give two amounts.'},
           {'role': 'assistant', 'content': 'The amounts are 20 and 8.'},
           {'role': 'user', 'content': 'Calculate their difference.'}]


@pytest.fixture
def router(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setenv('RAG_MODEL_ROUTER_SELECTION_POLICY', 'QUALITY_FIRST_V3')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    monkeypatch.delenv('RAG_MODEL_ROUTER_OFF_FILE', raising=False)
    baseline = SimpleNamespace(model='baseline')
    selected = LLMService(api_key='test', model='zai-org/GLM-5.3-Flash')
    selected._router_extra_body = {'enable_thinking': False}
    bindings = []
    monkeypatch.setattr(generation, '_role_service', lambda role, target: (bindings.append((role, target)) or selected))
    return baseline, selected, bindings


def test_arithmetic_actual_history_routes_without_retrieval_claim(router, monkeypatch):
    baseline, selected, bindings = router
    events = []
    monkeypatch.setattr(generation, 'event', lambda name, **kw: events.append((name, kw)))
    assert generation.route_direct_history_generation(baseline, None, HISTORY, HISTORY[-1]['content']) == (selected, selected.model)
    assert bindings == [('EXPENSIVE', selected.model)]
    event = events[-1][1]
    assert event['evidence_scope'] == 'HISTORY_ONLY' and event['evidence_count'] == 0
    assert 'retrieval_status' not in event


@pytest.mark.parametrize('mode,policy,question,history', [
    ('OFF', 'QUALITY_FIRST_V3', 'Calculate their difference.', HISTORY),
    ('DYNAMIC', 'WEIGHTED_V1', 'Calculate their difference.', HISTORY),
    ('DYNAMIC', 'AFFINITY_V2', 'Calculate their difference.', HISTORY),
    ('CHEAP_ONLY', 'QUALITY_FIRST_V3', 'Calculate their difference.', HISTORY),
    ('EXPENSIVE_ONLY', 'QUALITY_FIRST_V3', 'Calculate their difference.', HISTORY),
    ('DYNAMIC', 'QUALITY_FIRST_V3', 'Hello!', HISTORY),
    ('DYNAMIC', 'QUALITY_FIRST_V3', 'Summarize the discussion.', HISTORY),
    ('DYNAMIC', 'QUALITY_FIRST_V3', 'Calculate their difference.', HISTORY[-1:]),
    ('DYNAMIC', 'QUALITY_FIRST_V3', 'Calculate their difference.', [{'role': 'assistant', 'content': '  '}, HISTORY[-1]]),
])
def test_other_direct_paths_preserve_baseline(router, monkeypatch, mode, policy, question, history):
    baseline, _, bindings = router
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', mode)
    monkeypatch.setenv('RAG_MODEL_ROUTER_SELECTION_POLICY', policy)
    assert generation.route_direct_history_generation(baseline, 'explicit-model', history, question) == (baseline, 'explicit-model')
    assert bindings == []


def test_eligible_unauthorized_is_blocked_before_binding(router, monkeypatch):
    baseline, _, bindings = router
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'false')
    with pytest.raises(generation.RoutingBlocked):
        generation.route_direct_history_generation(baseline, None, HISTORY, HISTORY[-1]['content'])
    assert bindings == []


def test_eligible_oversize_history_blocks_before_binding(router, monkeypatch):
    baseline, _, bindings = router
    monkeypatch.setattr(generation.get_settings(), 'GROUNDED_REQUEST_MAX_CHARS', 100)
    messages = [dict(m) for m in HISTORY]
    messages[1]['content'] = 'history ' * 100
    with pytest.raises(ValueError, match='FINAL_REQUEST_OVER_CHAR_BUDGET'):
        generation.route_direct_history_generation(baseline, None, messages, HISTORY[-1]['content'])
    assert bindings == []


@pytest.mark.parametrize('actual,status', [('zai-org/GLM-5.3-Flash', 'MATCH'), ('other', 'MISMATCH'), (None, 'UNKNOWN')])
def test_single_stream_transfers_thinking_and_reports_real_identity(router, monkeypatch, actual, status):
    from app.services import llm as llm_module
    baseline, selected, _ = router
    events = []
    monkeypatch.setattr(llm_module, 'event', lambda name, **kw: events.append((name, kw)))
    class Stream:
        def __init__(self):
            self.sent = False
        def __aiter__(self):
            return self
        async def __anext__(self):
            if self.sent:
                raise StopAsyncIteration
            self.sent = True
            return SimpleNamespace(model=actual, choices=[SimpleNamespace(delta=SimpleNamespace(content='12'), finish_reason='stop')], usage=None)
        async def close(self):
            pass
    create = AsyncMock(return_value=Stream())
    selected._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    final = {}
    async def run():
        service, target = generation.route_direct_history_generation(baseline, None, HISTORY, HISTORY[-1]['content'])
        return [part async for part in service.generate_stream(HISTORY, model_id=target, on_final=final.update)]
    assert asyncio.run(run()) == ['12']
    assert create.await_count == 1
    assert create.await_args.kwargs['extra_body'] == {'enable_thinking': False}
    assert final['model'] == actual
    identity = next(data for name, data in events if name == 'router.provider_identity')
    assert identity['status'] == status and identity['evidence_scope'] == 'HISTORY_ONLY'
    assert selected.provider_request_parameters(extra_body={}) == {}


@pytest.mark.parametrize('stream_error', [False, True])
def test_direct_chat_selects_once_and_closes_only_routed_service(router, monkeypatch, stream_error):
    from app.services import chat
    from app.schemas import Message
    baseline, selected, bindings = router
    baseline.understand_query = AsyncMock(return_value={'intent': 'follow_up'})
    baseline.aclose = AsyncMock()
    selected.aclose = AsyncMock()
    calls = []
    async def stream(**kwargs):
        calls.append(kwargs)
        if stream_error:
            raise RuntimeError('synthetic stream failure')
        kwargs['on_final']({'finish_reason': 'stop', 'model': selected.model})
        yield '12'
    selected.generate_stream = stream
    svc = chat.ChatService.__new__(chat.ChatService)
    svc._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=20)
    svc._resolved_llm = lambda _: baseline
    svc._retrieve_with_new_service = AsyncMock(side_effect=AssertionError('direct must not retrieve'))
    async def run():
        return [event async for event in svc._chat_stream_normal(
            [Message(**m) for m in HISTORY], [], 5, 0, None, False, 0, None)]
    events = asyncio.run(run())
    assert len(calls) == len(bindings) == 1
    assert calls[0]['model_id'] == selected.model
    assert calls[0]['messages'] == HISTORY
    assert calls[0]['max_tokens'] == generation.get_settings().LLM_ANSWER_MAX_TOKENS
    selected.aclose.assert_awaited_once()
    baseline.aclose.assert_not_awaited()
    svc._retrieve_with_new_service.assert_not_awaited()
    assert ('error' if stream_error else 'done') in [name for name, _ in events]

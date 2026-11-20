"""Opt-in transport/packing tests with fake providers, no quality claims."""
import asyncio
import copy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from app.services import cheap_grounding as cheap
from app.services import grounded_generation as gg
from app.services.grounded_request_budget import PreparedGroundedRequest, request_chars


def service(model):
    create = AsyncMock(return_value=SimpleNamespace(model=model, usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='answer [1]'), finish_reason='stop')]))
    async def stream(**kwargs):
        create.stream_input = kwargs['messages']
        yield 'answer [1]'
        kwargs['on_final']({'finish_reason': 'stop'})
    return SimpleNamespace(model=model, _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=create))), generate_stream=stream), create


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv('RAG_CHEAP_GROUNDING_ENABLED', 'true')
    monkeypatch.setattr(gg, 'event', lambda *a, **k: None)
    monkeypatch.setattr(gg.get_settings(), 'GROUNDED_REQUEST_MAX_CHARS', 32000)
    monkeypatch.setattr(gg.get_settings(), 'LLM_ANSWER_MAX_TOKENS', 4096)
    llm, call = service('Qwen/Qwen3.5-4B')
    route = SimpleNamespace(selected_role='CHEAP', mode='CHEAP_ONLY')
    resolver = Mock(return_value=(llm, llm.model, route, object()))
    monkeypatch.setattr(gg, '_route_generation', resolver)
    monkeypatch.setattr(gg, '_router_outcome', lambda *a, **k: None)
    messages = [dict(role='user', content='question')]
    citations = [dict(content='fact', fileName='f', location='p', chunk_id='c')]
    return llm, call, resolver, messages, citations


def test_off_is_original_and_defers_route(setup, monkeypatch):
    llm, call, resolver, messages, citations = setup
    monkeypatch.delenv('RAG_CHEAP_GROUNDING_ENABLED')
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    assert cheap.CONTRACT_TEXT not in prepared.messages[0]['content']
    assert prepared.route_resolution is None
    resolver.assert_not_called()


def test_opt_in_prepares_actual_budget_and_caches_route_for_both_paths(setup):
    llm, call, resolver, messages, citations = setup
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    assert resolver.call_count == 1
    assert resolver.call_args.args[2] == prepared.routing_messages
    assert cheap.CONTRACT_TEXT not in prepared.routing_messages[0]['content']
    assert prepared.summary['request_chars'] == request_chars(prepared.messages)
    assert prepared.summary['cheap_grounding_status'] == 'CONTRACT_APPLIED'
    assert prepared.messages[0]['content'] == prepared.original_messages[0]['content'] + '\n\n' + cheap.CONTRACT_TEXT
    import app.services.model_router as router
    # A completed cheap-only request requires no escalation.
    from unittest.mock import patch
    with patch.object(router, 'decide_escalation', return_value=SimpleNamespace(should_retry=False, reason='TEST')):
        asyncio.run(gg.generate_grounded_candidate(llm, messages, citations, prepared_request=prepared))
    async def collect():
        return [x async for x in gg.stream_grounded_candidate(llm, messages, citations, prepared_request=prepared)]
    asyncio.run(collect())
    assert resolver.call_count == 1
    assert call.await_args.kwargs['messages'] == call.stream_input == prepared.messages


def test_expensive_and_history_only_unchanged(setup):
    llm, call, resolver, messages, citations = setup
    expensive, _ = service('expensive')
    resolver.return_value = (expensive, expensive.model,
                            SimpleNamespace(selected_role='EXPENSIVE'), object())
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    assert cheap.CONTRACT_TEXT not in prepared.messages[0]['content']
    assert prepared.messages == prepared.original_messages
    resolver.return_value = (llm, llm.model, SimpleNamespace(selected_role='CHEAP'), object())
    history_only = gg.prepare_grounded_request(llm, messages, [])
    assert cheap.CONTRACT_TEXT not in history_only.messages[0]['content']
    assert history_only.summary['cheap_grounding_reason'] == 'NO_GROUNDED_EVIDENCE'
    resolver.return_value = (llm, 'expensive', SimpleNamespace(selected_role='EXPENSIVE'), object())
    expensive = gg.prepare_grounded_request(llm, messages, citations)
    assert expensive.messages == expensive.original_messages
    resolver.return_value = (llm, llm.model, SimpleNamespace(selected_role='CHEAP'), object())
    history = gg.prepare_grounded_request(llm, messages, [])
    assert history.messages == history.original_messages


def test_contract_cannot_remove_any_citation(setup, monkeypatch):
    llm, call, resolver, messages, citations = setup
    citations.append(dict(content='x' * 1000, fileName='b', location='p'))
    baseline = gg._build_grounded_request(llm, messages, citations, register=False)[1]
    monkeypatch.setattr(gg.get_settings(), 'GROUNDED_REQUEST_MAX_CHARS', request_chars(baseline) + 120)
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    assert prepared.summary['cheap_grounding_status'] == 'CONTRACT_SKIPPED_WOULD_DROP_EVIDENCE'
    assert prepared.messages == baseline and prepared.citations == citations


def test_router_off_baseline_requires_positive_configured_binding(setup, monkeypatch):
    llm, call, resolver, messages, citations = setup
    resolver.return_value = (llm, llm.model, None, None)
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.delenv('RAG_ROUTER_CHEAP_MODEL_ID', raising=False)
    unknown = gg.prepare_grounded_request(llm, messages, citations)
    assert unknown.summary['cheap_grounding_reason'] == 'CHEAP_BINDING_NOT_CONFIGURED'
    assert cheap.CONTRACT_TEXT not in unknown.messages[0]['content']
    monkeypatch.setenv('RAG_ROUTER_CHEAP_MODEL_ID', 'configured-id')
    llm.base_url = 'https://provider.invalid'
    llm.api_key = 'offline-test-credential'
    binding = SimpleNamespace(model=llm.model, base_url=llm.base_url, api_key=llm.api_key)
    monkeypatch.setattr(gg, '_role_service', lambda *a: binding)
    prepared = gg.prepare_grounded_request(llm, messages, citations)
    assert prepared.summary['cheap_grounding_status'] == 'CONTRACT_APPLIED'
    async def collect():
        return [x async for x in gg.stream_grounded_candidate(llm, messages, citations, prepared_request=prepared)]
    asyncio.run(collect())
    assert call.stream_input == prepared.messages
    assert request_chars(call.stream_input) == prepared.summary['request_chars']
    binding.api_key = 'other-tenant'
    ambiguous = gg.prepare_grounded_request(llm, messages, citations)
    assert ambiguous.summary['cheap_grounding_reason'] == 'BASELINE_CHEAP_BINDING_AMBIGUOUS'
    assert cheap.CONTRACT_TEXT not in ambiguous.messages[0]['content']


def test_exact_requested_binding_id_requires_resolved_target(setup, monkeypatch):
    llm, _, _, _, _ = setup
    monkeypatch.setenv('RAG_ROUTER_CHEAP_MODEL_ID', 'configured-id')
    monkeypatch.setattr(gg, '_role_service', lambda *a: llm)
    assert gg._explicit_cheap_binding(llm, llm.model, None, 'configured-id', llm.model)[0]
    assert not gg._explicit_cheap_binding(llm, 'other', None, 'configured-id', llm.model)[0]


@pytest.mark.parametrize('streamed', [False, True])
def test_chat_sse_reports_actual_contract_budget_and_citations(setup, monkeypatch, streamed):
    from app.services import chat
    from app.schemas import Message
    llm, call, resolver, _, _ = setup
    resolver.return_value = (llm, llm.model, None, None)
    monkeypatch.setenv('RAG_ROUTER_CHEAP_MODEL_ID', 'configured-id')
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    monkeypatch.setenv('RAG_GROUNDED_STREAM_ENABLED', 'true' if streamed else 'false')
    llm.base_url = 'https://offline.invalid'
    llm.api_key = 'offline-test-value'
    monkeypatch.setattr(gg, '_role_service', lambda *a: llm)
    class Session:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(chat, 'Session', Session)
    svc = chat.ChatService.__new__(chat.ChatService)
    svc.llm_service = llm
    svc._resolved_llm = lambda model: llm
    svc._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=20)
    svc._retrieve_with_new_service = AsyncMock(return_value=([
        dict(id='c', file_id='f', file_name='report', content='answer fact', score=1)],
        {'retrieval_status': 'SUCCESS'}))
    async def collect():
        return [e async for e in svc._chat_stream_normal(
            [Message(role='user', content='question')], ['kb'], 5, 0, None, False, 0, None)]
    events = asyncio.run(collect())
    assert not any(k == 'error' for k, v in events)
    final = next(v for k, v in reversed(events) if k == 'rag_result')
    sent = call.stream_input if streamed else call.await_args.kwargs['messages']
    assert cheap.CONTRACT_TEXT in sent[0]['content']
    assert final['request_budget']['request_chars'] == request_chars(sent)
    assert final['request_budget']['cheap_grounding_status'] == 'CONTRACT_APPLIED'
    assert sum(kind == 'rag_result' and bool(payload.get('request_budget'))
               for kind, payload in events) == 1
    assert all(c['content'] in sent[0]['content'] for c in final['citations'])
    assert resolver.call_count == 1


def test_quality_escalation_restores_original_history_and_prompt(setup, monkeypatch):
    llm, call, resolver, messages, citations = setup
    expensive, expensive_call = service('expensive')
    resolver.return_value[2].mode = 'DYNAMIC'
    monkeypatch.setattr(gg, '_role_service', lambda *a: expensive)
    import app.services.model_router as router
    monkeypatch.setattr(router, 'decide_escalation', lambda *a, **k:
        SimpleNamespace(should_retry=True, selected_model='expensive', reason='TEST'))
    proof = SimpleNamespace(citation_index=1, proof=dict(title='protected workbook title',
        headers=[(0, 0, 'column')], label='row', value=12, notes=['proof note'],
        source='workbook source', footnotes={'a': {'text': 'proof footnote'}}))
    prepared = gg.prepare_grounded_request(llm, messages, citations,
        preference_policy='preference marker', spreadsheet_context=proof)
    result = asyncio.run(gg.generate_grounded_candidate(llm, messages, citations,
        prepared_request=prepared, quality_check=lambda answer: 'FAIL'))
    assert cheap.CONTRACT_TEXT in call.await_args.kwargs['messages'][0]['content']
    assert expensive_call.await_args.kwargs['messages'] == prepared.original_messages
    assert cheap.CONTRACT_TEXT not in expensive_call.await_args.kwargs['messages'][0]['content']
    assert 'preference marker' in expensive_call.await_args.kwargs['messages'][0]['content']
    assert 'protected workbook title' in expensive_call.await_args.kwargs['messages'][0]['content']
    assert len(result) == 3 and result[0] == 'answer [1]'
    assert result.final_request_budget['request_chars'] == request_chars(prepared.original_messages)
    assert result.final_request_budget['cheap_grounding_status'] == 'EXPENSIVE_RETRY_ORIGINAL_REQUEST'


def test_buffered_fallback_sse_keeps_initial_receipt_then_updates_before_answer(setup, monkeypatch):
    from app.services import chat, model_router
    from app.schemas import Message
    llm, cheap_call, resolver, _, _ = setup
    resolver.return_value[2].mode = 'DYNAMIC'
    cheap_call.return_value.choices[0].message.content = 'unsupported [9]'
    expensive, expensive_call = service('expensive')
    monkeypatch.setattr(gg, '_role_service', lambda *a: expensive)
    monkeypatch.setattr(model_router, 'decide_escalation', lambda *a, **k:
        SimpleNamespace(should_retry=True, selected_model='expensive', reason='TEST'))
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', 'REFERENCE_RANGE')
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    monkeypatch.setenv('RAG_GROUNDED_STREAM_ENABLED', 'false')
    class Session:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(chat, 'Session', Session)
    chat_service = chat.ChatService.__new__(chat.ChatService)
    chat_service.llm_service = llm
    chat_service._resolved_llm = lambda model: llm
    chat_service._runtime_snapshot = lambda: SimpleNamespace(max_chat_history_rounds=20)
    chat_service._retrieve_with_new_service = AsyncMock(return_value=([
        dict(id='c', file_id='f', file_name='report', content='fact', score=1)],
        {'retrieval_status': 'SUCCESS'}))
    history = [Message(role='user', content='earlier ' + 'a' * 600),
               Message(role='assistant', content='history ' + 'b' * 600),
               Message(role='user', content='question')]
    baseline = gg._build_grounded_request(llm,
        [{'role': m.role.value, 'content': m.content} for m in history],
        [dict(content='fact', fileName='report', location='未知位置')], register=False)[1]
    monkeypatch.setattr(gg.get_settings(), 'GROUNDED_REQUEST_MAX_CHARS', request_chars(baseline) + 150)
    async def collect():
        return [event async for event in chat_service._chat_stream_normal(
            history, ['kb'], 5, 0, None, False, 0, None)]
    events = asyncio.run(collect())
    assert not any(kind == 'error' for kind, payload in events)
    receipts = [(i, payload) for i, (kind, payload) in enumerate(events)
                if kind == 'rag_result' and payload.get('request_budget')]
    assert len(receipts) == 2
    first_index, first = receipts[0]
    final_index, final = receipts[1]
    assert first['request_budget']['cheap_grounding_status'] == 'CONTRACT_APPLIED'
    assert first['request_budget']['omitted_history_turns'] == 1
    assert not first.get('request_budget_update')
    assert final['request_budget_update'] is True
    assert final['request_budget']['cheap_grounding_status'] == 'EXPENSIVE_RETRY_ORIGINAL_REQUEST'
    assert final['request_budget']['provider_model'] == 'expensive'
    assert final['request_budget']['omitted_history_turns'] == 0
    assert final['request_budget']['omitted_history_message_indices'] == []
    assert len(expensive_call.await_args.kwargs['messages']) > len(cheap_call.await_args.kwargs['messages'])
    assert first['request_budget']['request_chars'] == request_chars(cheap_call.await_args.kwargs['messages'])
    assert final['request_budget']['request_chars'] == request_chars(expensive_call.await_args.kwargs['messages'])
    assert first['citations'] == final['citations']
    assert first_index < final_index < next(i for i, (kind, _) in enumerate(events) if kind == 'answer_chunk')
    assert resolver.call_count == 1 and cheap_call.await_count == expensive_call.await_count == 1


def test_app_builder_and_resolved_runtime_path_match_all_frozen500_inputs(setup):
    llm, call, resolver, _, _ = setup
    llm._router_extra_body = {'enable_thinking': False}
    root = Path(__file__).resolve().parents[2]
    ledger_path = root / 'var/router-risk-v9-20261117/preparation-ledger500.json'
    if not ledger_path.exists():
        pytest.skip('local frozen500 artifacts unavailable; no parity claim')
    ledger = json.loads(ledger_path.read_bytes())
    fields = dict(model='Qwen/Qwen3.5-4B', max_tokens=4096, temperature=0.7,
                  stream=False, extra_body={'enable_thinking': False})
    assert len(ledger) == 500
    for row in ledger:
        source = json.loads(Path(row['source']['path']).read_bytes())
        snapshot = copy.deepcopy(source)
        actual, citations, audit = cheap.prepare_cheap_request(
            source['messages'], source['citations'], provider_fields=fields)
        expected = json.loads(Path(row['candidate_input']['path']).read_bytes())
        assert actual == expected['messages'], row['global_index']
        assert citations == expected['citations'], row['global_index']
        assert source == snapshot
        runtime = gg._resolve_cheap_grounding(PreparedGroundedRequest(
            source['messages'], source['citations'],
            {'request_chars': request_chars(source['messages'])}, source['messages']),
            llm, None, 'SUCCESS')
        assert runtime.messages == expected['messages'], row['global_index']
        assert runtime.citations == expected['citations'], row['global_index']
        assert runtime.routing_messages == source['messages']
    assert cheap.CONTRACT_SHA256 == '527db41ffeaafdf3e2936916f7c5186a0d5b13611e8231eb949cda11c78bc004'

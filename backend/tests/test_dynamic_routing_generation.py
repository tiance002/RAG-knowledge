import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services import grounded_generation as generation


def service(model='baseline', *, error=None):
    response = SimpleNamespace(id='request', model=model, usage=None,
        choices=[SimpleNamespace(message=SimpleNamespace(content='answer'), finish_reason='stop')])
    call = AsyncMock(side_effect=error, return_value=response)
    return SimpleNamespace(model=model, _get_client=lambda: SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=call)))), call


def invoke(llm, model_id=None):
    return asyncio.run(generation.generate_grounded_candidate(
        llm, [{'role': 'user', 'content': 'question'}],
        [{'chunk_id': 'c1', 'content': 'evidence'}], model_id=model_id))


def fake_router(monkeypatch, role='CHEAP'):
    decision = SimpleNamespace(mode='DYNAMIC', selected_role=role, selected_model='test-model',
                               to_payload=lambda: {'selected_role': role})
    fake = SimpleNamespace(RouterConfig=SimpleNamespace(from_env=lambda: object()),
                           decide_route=lambda *a, **kw: decision,
                           decide_escalation=lambda *a, **kw: SimpleNamespace(
                               should_retry=kw['quality_status'] == 'FAIL',
                               selected_model='expensive', reason='TEST'))
    monkeypatch.setitem(sys.modules, 'app.services.model_router', fake)
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')


def test_default_off_preserves_explicit_model_and_single_call(monkeypatch):
    monkeypatch.delenv('RAG_MODEL_ROUTER_MODE', raising=False)
    llm, call = service()
    assert invoke(llm, 'explicit-model')[0] == 'answer'
    assert call.await_count == 1
    assert call.await_args.kwargs['model'] == 'explicit-model'


def test_actual_router_cheap_only_selects_role_without_retry(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'CHEAP_ONLY')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    baseline, baseline_call = service()
    selected, selected_call = service('test-model')
    calls = []
    monkeypatch.setattr(generation, '_role_service', lambda role, expected: (calls.append((role, expected)) or selected))
    assert invoke(baseline)[0] == 'answer'
    assert calls == [('CHEAP', 'Qwen/Qwen3.5-4B')]
    baseline_call.assert_not_awaited()
    assert selected_call.await_count == 1


def test_actual_dynamic_router_quality_failure_escalates_once(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setenv('RAG_MODEL_ROUTER_UNKNOWN_POLICY', 'CHEAP')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    baseline, _ = service()
    cheap, cheap_call = service('Qwen/Qwen3.5-4B')
    expensive, expensive_call = service('zai-org/GLM-5.3-Flash')
    monkeypatch.setattr(generation, '_role_service', lambda role, expected: cheap if role == 'CHEAP' else expensive)
    asyncio.run(generation.generate_grounded_candidate(
        baseline, [{'role': 'user', 'content': 'question'}],
        [{'chunk_id': 'c1', 'content': 'evidence'}], quality_check=lambda answer: 'FAIL'))
    assert cheap_call.await_count == expensive_call.await_count == 1
    assert cheap_call.await_args.kwargs['messages'] == expensive_call.await_args.kwargs['messages']


@pytest.mark.parametrize('setting,answer,expected', [
    ('NONE', 'Out of range [2]', 1),
    ('REFERENCE_RANGE', 'Valid [1]', 1),
    ('REFERENCE_RANGE', 'No citation', 1),
    ('REFERENCE_RANGE', 'Out of range [2]', 2),
    ('REFERENCE_RANGE', 'Zero [0]', 2),
])
def test_opt_in_reference_range_escalates_only_invalid_numbers(monkeypatch, setting, answer, expected):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setenv('RAG_MODEL_ROUTER_UNKNOWN_POLICY', 'CHEAP')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', setting)
    baseline, _ = service()
    cheap, cheap_call = service('Qwen/Qwen3.5-4B')
    expensive, expensive_call = service('zai-org/GLM-5.3-Flash')
    cheap_call.return_value.choices[0].message.content = answer
    expensive_call.return_value.choices[0].message.content = 'Still out of range [9]'
    monkeypatch.setattr(generation, '_role_service', lambda role, model: cheap if role == 'CHEAP' else expensive)
    invoke(baseline)
    assert cheap_call.await_count == 1
    assert expensive_call.await_count == expected - 1
    if expected == 2:
        assert cheap_call.await_args.kwargs['messages'] == expensive_call.await_args.kwargs['messages']


def test_opt_in_reference_range_does_not_retry_expensive_route(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'EXPENSIVE_ONLY')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    monkeypatch.setenv('RAG_ROUTER_QUALITY_CHECK', 'REFERENCE_RANGE')
    baseline, _ = service()
    expensive, call = service('zai-org/GLM-5.3-Flash')
    call.return_value.choices[0].message.content = 'Out of range [2]'
    monkeypatch.setattr(generation, '_role_service', lambda role, model: expensive)
    invoke(baseline)
    assert call.await_count == 1


def test_enabled_without_server_authorization_blocks_before_call(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.delenv('RAG_ROUTER_ALLOW_MODEL_CALLS', raising=False)
    llm, call = service()
    with pytest.raises(generation.RoutingBlocked):
        invoke(llm)
    call.assert_not_awaited()


def test_rollback_marker_restores_baseline_without_authorizing_roles(monkeypatch, tmp_path):
    marker = tmp_path / 'router.off'
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setenv('RAG_MODEL_ROUTER_OFF_FILE', str(marker))
    marker.touch()
    monkeypatch.delenv('RAG_ROUTER_ALLOW_MODEL_CALLS', raising=False)
    baseline, call = service()
    assert invoke(baseline)[0] == 'answer'
    assert call.await_count == 1


@pytest.mark.parametrize('flag', ['TRUNCATED', 'PROVIDER_FALLBACK', 'UNSUPPORTED'])
def test_unimplemented_retry_policies_fail_explicitly_before_sending(monkeypatch, flag):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'DYNAMIC')
    monkeypatch.setenv('RAG_ROUTER_ALLOW_MODEL_CALLS', 'true')
    monkeypatch.setenv('RAG_MODEL_ROUTER_ESCALATE_ON_' + flag, 'true')
    llm, call = service()
    with pytest.raises(generation.RoutingBlocked, match='does not implement'):
        invoke(llm)
    call.assert_not_awaited()


def test_observation_keeps_feature_provenance_and_role_readable():
    from app.services.model_router import RouterConfig, decide_route
    from app.services.observability import sanitize
    payload = decide_route('question', [{'file_id': 'f', 'chunk_id': 'c', 'content': 'text'}],
                           messages=[{'role': 'user', 'content': 'question'}],
                           config=RouterConfig(mode='CHEAP_ONLY')).to_payload()
    masked = sanitize(payload)
    assert masked['role'] == 'CHEAP'
    assert masked['features']['L']['status'] == 'ESTIMATED'
    assert isinstance(masked['features']['L']['method'], str)
    assert masked['measurements']['weights']['L'] == .1


def test_enabled_with_no_retrieval_does_not_call_provider(monkeypatch):
    fake_router(monkeypatch)
    llm, call = service()
    with pytest.raises(generation.RoutingBlocked):
        asyncio.run(generation.generate_grounded_candidate(
            llm, [{'role': 'user', 'content': 'question'}], [], retrieval_status='NO_RESULTS'))
    call.assert_not_awaited()


def test_selected_binding_reuses_exact_evidence_and_does_not_retry(monkeypatch):
    fake_router(monkeypatch)
    baseline, baseline_call = service()
    selected, selected_call = service('test-model')
    monkeypatch.setattr(generation, '_role_service', lambda role, expected: selected)
    assert invoke(baseline)[0] == 'answer'
    baseline_call.assert_not_awaited()
    assert selected_call.await_count == 1
    request = selected_call.await_args.kwargs
    assert request['model'] == 'test-model'
    assert request['messages'][1] == {'role': 'user', 'content': 'question'}
    assert 'evidence' in request['messages'][0]['content']


def test_provider_timeout_is_not_replayed(monkeypatch):
    fake_router(monkeypatch)
    baseline, baseline_call = service()
    selected, selected_call = service('test-model', error=TimeoutError('uncertain'))
    monkeypatch.setattr(generation, '_role_service', lambda role, expected: selected)
    with pytest.raises(TimeoutError):
        invoke(baseline)
    assert selected_call.await_count == 1
    baseline_call.assert_not_awaited()


def test_explicit_quality_failure_retries_once_with_same_prompt(monkeypatch):
    fake_router(monkeypatch)
    baseline, _ = service()
    cheap, cheap_call = service('test-model')
    expensive, expensive_call = service('expensive')
    monkeypatch.setattr(generation, '_role_service', lambda role, expected: cheap if role == 'CHEAP' else expensive)
    result = asyncio.run(generation.generate_grounded_candidate(
        baseline, [{'role': 'user', 'content': 'question'}],
        [{'chunk_id': 'c1', 'content': 'evidence'}], quality_check=lambda answer: 'FAIL'))
    assert result[0] == 'answer'
    assert cheap_call.await_count == expensive_call.await_count == 1
    assert cheap_call.await_args.kwargs['messages'] == expensive_call.await_args.kwargs['messages']


@pytest.mark.parametrize('quality', ['UNKNOWN', 'PASS', 'invalid'])
def test_no_retry_without_explicit_quality_failure(monkeypatch, quality):
    fake_router(monkeypatch)
    baseline, _ = service()
    cheap, cheap_call = service('test-model')
    monkeypatch.setattr(generation, '_role_service', lambda role, expected: cheap)
    asyncio.run(generation.generate_grounded_candidate(
        baseline, [{'role': 'user', 'content': 'question'}],
        [{'chunk_id': 'c1', 'content': 'evidence'}], quality_check=lambda answer: quality))
    assert cheap_call.await_count == 1


@pytest.mark.parametrize('change', [
    {'is_active': False}, {'model_type': 'embedding'}, {'model_name': 'wrong'},
    {'base_url': ''}, {'base_url': 'https://user:pass@example.invalid'}, {'api_key': ''},
])
def test_invalid_server_binding_is_blocked(monkeypatch, change):
    from sqlmodel import Session
    from app.models import ModelType
    from app.services import llm as llm_module

    values = dict(is_active=True, model_type=ModelType.LLM, model_name='test-model',
                  base_url='https://example.invalid/v1', api_key='secret')
    values.update(change)
    class FakeSession:
        def __init__(self, engine): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, model, key): return SimpleNamespace(**values)
    monkeypatch.setenv('RAG_ROUTER_CHEAP_MODEL_ID', 'server-binding')
    monkeypatch.setattr('sqlmodel.Session', FakeSession)
    monkeypatch.setattr(llm_module, 'LLMService', lambda **kwargs: pytest.fail('must not create client'))
    with pytest.raises(generation.RoutingBlocked):
        generation._role_service('CHEAP', 'test-model')


@pytest.mark.parametrize('finish,actual', [('length', 'test-model'), (None, 'test-model'), ('stop', None), ('stop', 'different')])
def test_unknown_truncated_or_mismatched_completion_never_escalates(monkeypatch, finish, actual):
    fake_router(monkeypatch)
    baseline, _ = service()
    cheap, call = service('test-model')
    call.return_value.model = actual
    call.return_value.choices[0].finish_reason = finish
    monkeypatch.setattr(generation, '_role_service', lambda *args: cheap)
    check = AsyncMock()  # A synchronous validator must not even run for these outcomes.
    asyncio.run(generation.generate_grounded_candidate(
        baseline, [{'role': 'user', 'content': 'question'}], [{'content': 'evidence'}], quality_check=check))
    assert call.await_count == 1
    check.assert_not_called()


def test_routed_thinking_parameters_reach_provider_and_generation_trace(monkeypatch):
    from contextlib import contextmanager
    fake_router(monkeypatch)
    baseline, _ = service()
    cheap, call = service('test-model')
    cheap._router_extra_body = {'enable_thinking': False}
    monkeypatch.setattr(generation, '_role_service', lambda *args: cheap)
    traces = []
    @contextmanager
    def traced(*args, **kwargs):
        traces.append(kwargs)
        yield {}, None
    monkeypatch.setattr(generation, 'provider_generation', traced)
    invoke(baseline)
    assert call.await_args.kwargs['extra_body'] == {'enable_thinking': False}
    assert traces[0]['extra_body'] == {'enable_thinking': False}


@pytest.mark.parametrize('setting,expected', [(None, None), ('', None), ('  ', None), ('false', False), ('true', True), ('bad', 'BLOCK')])
def test_valid_role_binding_applies_only_explicit_thinking_policy(monkeypatch, setting, expected):
    from app.models import ModelType
    from app.services import llm as llm_module
    binding = SimpleNamespace(is_active=True, model_type=ModelType.LLM, model_name='test-model',
                              base_url='https://example.invalid/v1', api_key='secret')
    class FakeSession:
        def __init__(self, engine): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def get(self, *args): return binding
    monkeypatch.setenv('RAG_ROUTER_CHEAP_MODEL_ID', 'binding')
    monkeypatch.delenv('RAG_ROUTER_CHEAP_ENABLE_THINKING', raising=False)
    if setting is not None:
        monkeypatch.setenv('RAG_ROUTER_CHEAP_ENABLE_THINKING', setting)
    monkeypatch.setattr('sqlmodel.Session', FakeSession)
    monkeypatch.setattr(llm_module, 'LLMService', lambda **kwargs: SimpleNamespace(**kwargs))
    if expected == 'BLOCK':
        with pytest.raises(generation.RoutingBlocked):
            generation._role_service('CHEAP', 'test-model')
    else:
        result = generation._role_service('CHEAP', 'test-model')
        assert getattr(result, '_router_extra_body', None) == (None if expected is None else {'enable_thinking': expected})


def test_actual_provider_identity_is_not_requested_alias(monkeypatch):
    events = []
    monkeypatch.setattr(generation, 'event', lambda name, **kwargs: events.append((name, kwargs)))
    response = SimpleNamespace(response_metadata={'model_name': 'actual-model'})
    assert generation._routed_response_identity(response, 'requested-alias', stage='agent') == 'MISMATCH'
    assert events[0][1]['provider_model_id'] == 'actual-model'
    assert events[0][1]['requested_model_id'] == 'requested-alias'


@pytest.mark.parametrize('finish,answer,error,status', [
    ('stop', 'answer', None, 'COMPLETED'), ('stop', '', None, 'EMPTY'),
    ('length', 'partial', None, 'TRUNCATED'), (None, 'answer', None, 'UNKNOWN'),
    ('stop', 'answer', TimeoutError('unknown'), 'UNKNOWN'),
    ('stop', 'answer', ValueError('provider failure'), 'PROVIDER_FAILURE'),
])
def test_router_outcome_distinguishes_completed_from_transport_return(monkeypatch, finish, answer, error, status):
    fake_router(monkeypatch)
    baseline, _ = service()
    selected, call = service('test-model', error=error)
    call.return_value.choices[0].finish_reason = finish
    call.return_value.choices[0].message.content = answer
    monkeypatch.setattr(generation, '_role_service', lambda *args: selected)
    events = []
    monkeypatch.setattr(generation, 'event', lambda name, **kwargs: events.append((name, kwargs)))
    if error is not None:
        with pytest.raises(type(error)) as raised:
            invoke(baseline)
        assert raised.value is error
    else:
        invoke(baseline)
    outcomes = [data for name, data in events if name == 'router.outcome']
    assert len(outcomes) == 1
    assert outcomes[0]['status'] == status
    assert outcomes[0]['cost'] == {'status': 'UNKNOWN'}
    assert outcomes[0]['retry_count'] == 0
    assert call.await_count == 1


@pytest.mark.parametrize('content', ['', '  ', None])
def test_enabled_empty_evidence_is_blocked_before_provider(monkeypatch, content):
    fake_router(monkeypatch)
    baseline, call = service()
    with pytest.raises(generation.RoutingBlocked):
        asyncio.run(generation.generate_grounded_candidate(
            baseline, [{'role': 'user', 'content': 'q'}], [{'content': content}]))
    call.assert_not_awaited()


@pytest.mark.parametrize('error,status', [
    (asyncio.CancelledError(), 'CANCELLED'),
    (httpx.ReadTimeout('uncertain'), 'UNKNOWN'),
    (httpx.ConnectError('failed before connect'), 'PROVIDER_FAILURE'),
])
def test_router_exception_outcome_reuses_provider_classification(monkeypatch, error, status):
    fake_router(monkeypatch)
    baseline, _ = service()
    selected, call = service('test-model', error=error)
    monkeypatch.setattr(generation, '_role_service', lambda *args: selected)
    events = []
    monkeypatch.setattr(generation, 'event', lambda name, **kwargs: events.append((name, kwargs)))
    with pytest.raises(type(error)) as caught:
        invoke(baseline)
    assert caught.value is error
    assert next(data['status'] for name, data in events if name == 'router.outcome') == status
    assert call.await_count == 1


@pytest.mark.parametrize('invalid', [True, -1, None])
def test_escalation_usage_does_not_sum_invalid_token_counters(monkeypatch, invalid):
    fake_router(monkeypatch)
    baseline, _ = service()
    cheap, cheap_call = service('test-model')
    expensive, expensive_call = service('expensive')
    cheap_call.return_value.usage = {'prompt_tokens': invalid, 'completion_tokens': 2, 'total_tokens': invalid}
    expensive_call.return_value.usage = {'prompt_tokens': 10, 'completion_tokens': 3, 'total_tokens': 13}
    monkeypatch.setattr(generation, '_role_service', lambda role, expected: cheap if role == 'CHEAP' else expensive)
    result = asyncio.run(generation.generate_grounded_candidate(
        baseline, [{'role': 'user', 'content': 'q'}], [{'content': 'evidence'}], quality_check=lambda answer: 'FAIL'))
    assert result[2] == {'completion_tokens': 5}

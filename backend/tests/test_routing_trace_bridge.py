"""Synthetic only: no SDK, credentials, provider calls, or cloud exports."""
import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge():
    path = Path(__file__).resolve().parents[2] / 'scripts/routing_trace_bridge.py'
    spec = importlib.util.spec_from_file_location('routing_trace_bridge_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake(bridge, monkeypatch):
    calls = []

    @contextmanager
    def manager(name, **kwargs):
        calls.append((name, kwargs))
        try:
            yield SimpleNamespace(update=lambda **kw: calls.append(('update', kw)))
        finally:
            calls.append(('exit', name))

    obs = SimpleNamespace(
        native_enabled=lambda: True,
        span=manager, generation=manager,
        capture=lambda value, **kw: {'content_sha256': bridge._hash(value), **kw},
        observation_io=lambda **kw: calls.append(('io', kw)),
        event=lambda name, **kw: calls.append((name, kw)),
        flush_observations=lambda: calls.append(('flush', {})),
    )
    monkeypatch.setattr(bridge, '_load_observability', lambda: obs)
    monkeypatch.setattr(bridge, '_load_capture_scope', lambda: lambda **kw: manager('scope', **kw))
    return obs, calls


def test_one_generation_basic_capture_and_no_raw_content(bridge, fake):
    _, calls = fake
    result = {'text': 'private answer', 'usage': {'prompt_tokens': 12, 'completion_tokens': 3},
              'finish_reason': 'stop', 'provider_request_id': 'provider-id', 'latency_ms': 123}
    original = dict(result)
    with bridge.trace_call({'qid': 'q1', 'question': 'private question'}, 'cheap', 'cheap-model',
                           {'router_mode': 'CHEAP_ONLY', 'messages': [{'role': 'user', 'content': 'private prompt'}],
                            'features': {'chars': 16, 'unsafe': 'private feature'}, 'api_key': 'never forward'}) as trace:
        trace.finish(result)
        trace.finish(result)
    assert result == original
    assert len([c for c in calls if c[0] == 'routing.provider_generation']) == 1
    assert len([c for c in calls if c[0] == 'update']) == 1
    assert next(c for c in calls if c[0] == 'scope')[1]['capture_mode'] == 'BASIC'
    update = next(c for c in calls if c[0] == 'update')[1]
    assert update['usage'] == {'input': 12, 'output': 3}
    assert update['cost'] is None and update['cost_status'] == 'UNKNOWN'
    assert update['provider_request_id'] == 'provider-id'
    assert 'private' not in repr(calls) and 'never forward' not in repr(calls)
    assert [c[1] for c in calls if c[0] == 'exit'] == [
        'routing.provider_generation', 'routing.provider_call', 'scope']
    bridge.flush()
    assert calls[-1][0] == 'flush'


def test_business_exception_is_never_suppressed(bridge, fake):
    _, calls = fake
    with pytest.raises(ValueError, match='provider failure'):
        with bridge.trace_call({'qid': 'q1'}, 'cheap', 'm'):
            raise ValueError('provider failure')
    update = next(c for c in calls if c[0] == 'update')[1]
    assert update['execution_status'] == 'FAILED'
    assert update['error_type'] == 'ValueError'


@pytest.mark.parametrize('failure', ['import', 'native_enabled', 'scope', 'span', 'capture', 'event', 'generation', 'io', 'update', 'exit', 'metadata', 'flush'])
def test_observation_failures_do_not_block_business(bridge, fake, monkeypatch, failure):
    obs, _ = fake

    def broken(*args, **kwargs):
        raise RuntimeError('telemetry unavailable')

    metadata = {'features': {'chars': 1}}
    if failure == 'import':
        monkeypatch.setattr(bridge, '_load_observability', broken)
    elif failure == 'scope':
        monkeypatch.setattr(bridge, '_load_capture_scope', broken)
    elif failure == 'metadata':
        metadata = object()
    elif failure == 'exit':
        @contextmanager
        def bad_exit(*args, **kwargs):
            yield None
            broken()
        obs.span = bad_exit
    elif failure == 'update':
        @contextmanager
        def bad_update(*args, **kwargs):
            yield SimpleNamespace(update=broken)
        obs.generation = bad_update
    else:
        setattr(obs, {'io': 'observation_io', 'flush': 'flush_observations'}.get(failure, failure), broken)
    visited = []
    with bridge.trace_call({'qid': 'q1'}, 'cheap', 'm', metadata) as collector:
        visited.append('provider would run')
        collector.finish({'text': 'ok'})
    bridge.flush()
    assert visited == ['provider would run']


def test_disabled_bridge_still_yields_collector(bridge, fake):
    obs, calls = fake
    obs.native_enabled = lambda: False
    with bridge.trace_call(None, 'cheap', 'm', object()) as collector:
        collector.finish(None)
    assert calls == []


@pytest.mark.parametrize('state,execution', [('TRUNCATED', 'TRUNCATED'), ('UNKNOWN', 'UNKNOWN'),
                                           ('EMPTY', 'RETURNED'), ('COMPLETED', 'RETURNED')])
def test_explicit_result_status_and_actual_model(bridge, fake, state, execution):
    obs, calls = fake

    @contextmanager
    def span(*args, **kwargs):
        yield {'trace_id': 'actual-sdk-trace-id'}
    obs.span = span
    with bridge.trace_call({'qid': 'q1'}, 'cheap', 'requested-model') as trace:
        assert trace.trace_id == 'actual-sdk-trace-id'
        trace.finish({'state': state, 'model': 'provider-model'})
    update = next(c for c in calls if c[0] == 'update')[1]
    assert update['execution_status'] == execution
    assert update['generation_status'] == state
    assert update['models']['expected']['model'] == 'requested-model'
    assert update['models']['actual']['model'] == 'provider-model'


def test_error_type_without_error_is_failed(bridge, fake):
    _, calls = fake
    with bridge.trace_call({'qid': 'q1'}, 'cheap', 'm') as trace:
        trace.finish({'error_type': 'TimeoutError'})
    assert next(c for c in calls if c[0] == 'update')[1]['execution_status'] == 'FAILED'


@pytest.mark.parametrize('reported,expected', [(None, None), ({}, None),
    ({'prompt_tokens': 10}, {'input': 10}), ({'completion_tokens': 0}, {'output': 0}),
    ({'prompt_tokens': True, 'total_tokens': -1}, None),
    ({'input_tokens': 2, 'output_tokens': 3, 'total_tokens': 5}, {'input': 2, 'output': 3, 'total': 5})])
def test_usage_never_invents_missing_zero(bridge, reported, expected):
    assert bridge._usage(reported) == expected

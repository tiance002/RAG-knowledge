import asyncio
from contextlib import asynccontextmanager
import json
import time

import pytest
from app.services import observability as obs


def _consume(stream):
    async def run():
        values = []
        terminal_at = None
        start = time.perf_counter()
        async for kind, payload in stream:
            values.append((kind, payload))
            if kind == 'done':
                terminal_at = time.perf_counter()
        return values, terminal_at - start, time.perf_counter() - terminal_at
    return asyncio.run(run())


def test_slow_request_flush_is_deferred_with_identical_events(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events.jsonl'))
    monkeypatch.setattr(obs, '_sdk', lambda: None)
    calls = []
    class SlowClient:
        def flush(self):
            calls.append('flush')
            time.sleep(.12)
    monkeypatch.setattr(obs, '_client', SlowClient())

    @obs.observe('qa.request')
    async def stream():
        with obs.span('synthetic.child'):
            yield 'answer_chunk', {'content': 'same answer'}
        yield 'done', {'generation_status': 'COMPLETED'}

    monkeypatch.setenv('RAG_OBSERVABILITY_FLUSH_POLICY', 'REQUEST')
    request_values, _, request_tail = _consume(stream())
    monkeypatch.setenv('RAG_OBSERVABILITY_FLUSH_POLICY', 'LIFESPAN')
    lifespan_values, _, deferred_tail = _consume(stream())
    assert request_values == lifespan_values
    assert request_tail >= .10 and request_tail - deferred_tail >= .08
    assert calls == ['flush']
    rows = [json.loads(line) for line in (tmp_path / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    roots = [row for row in rows if row.get('stage') == 'qa.request' and row.get('event') == 'end']
    children = [row for row in rows if row.get('stage') == 'synthetic.child' and row.get('event') == 'end']
    assert len(roots) == len(children) == 2
    assert obs.flush_policy() == 'LIFESPAN'
    obs.flush_observations()
    assert calls == ['flush', 'flush']


def test_default_invalid_and_failure_are_fail_open(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events'))
    monkeypatch.setattr(obs, '_sdk', lambda: None)
    calls = []
    class BrokenClient:
        def flush(self):
            calls.append(1)
            raise RuntimeError('synthetic export error')
    monkeypatch.setattr(obs, '_client', BrokenClient())
    monkeypatch.delenv('RAG_OBSERVABILITY_FLUSH_POLICY', raising=False)
    assert obs.flush_policy() == 'REQUEST'
    monkeypatch.setenv('RAG_OBSERVABILITY_FLUSH_POLICY', 'invalid')
    assert obs.flush_policy() == 'REQUEST'
    @obs.observe('qa.request')
    async def stream():
        yield 'answer_chunk', {'content': 'answer'}
        yield 'done', {'generation_status': 'COMPLETED'}
    result, _, _ = _consume(stream())
    assert result[-1][0] == 'done' and calls == [1]


def test_lifespan_flush_runs_after_root_close_and_even_on_error(monkeypatch, tmp_path):
    from app import main
    import app.services.provider_http as provider_http
    import app.services.bounded_chroma_executor as bounded_chroma
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVABILITY_FLUSH_POLICY', 'LIFESPAN')
    log = tmp_path / 'events'
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(log))
    monkeypatch.setattr(obs, '_sdk', lambda: None)
    monkeypatch.setattr(main, 'ensure_directories', lambda: None)
    monkeypatch.setattr(main, 'init_db', lambda: None)
    monkeypatch.setattr(main, 'load_persisted_settings', lambda session: None)
    order = []
    @asynccontextmanager
    async def fake_manager(**kwargs):
        try:
            yield
        finally:
            order.append('provider_closed')
    @asynccontextmanager
    async def fake_chroma():
        try:
            yield
        finally:
            order.append('chroma_closed')
    monkeypatch.setattr(provider_http, 'managed_provider_http', fake_manager)
    monkeypatch.setattr(bounded_chroma, 'managed_chroma_executor', fake_chroma)
    called = []
    class Client:
        def flush(self):
            rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
            assert any(row.get('stage') == 'qa.request' and row.get('event') == 'end' for row in rows)
            called.append(True)
            order.append('flush')
    monkeypatch.setattr(obs, '_client', Client())
    @obs.observe('qa.request')
    async def stream():
        yield 'answer_chunk', {'content': 'answer'}
        yield 'done', {'generation_status': 'COMPLETED'}
    async def run():
        with pytest.raises(ValueError):
            async with main.lifespan(main.app):
                assert [item async for item in stream()][-1][0] == 'done'
                assert not called
                raise ValueError('synthetic application error')
    asyncio.run(run())
    assert called == [True]
    assert order == ['chroma_closed', 'provider_closed', 'flush']


def test_real_sdk_in_memory_exports_same_span_tree_after_final_flush(monkeypatch, tmp_path):
    import uuid
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path / 'events'))
    observed = []
    for policy in ('REQUEST', 'LIFESPAN'):
        monkeypatch.setenv('RAG_OBSERVABILITY_FLUSH_POLICY', policy)
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        client = Langfuse(public_key='pk-test-' + uuid.uuid4().hex, secret_key='synthetic',
                          tracer_provider=provider, span_exporter=exporter,
                          flush_at=50, flush_interval=60)
        monkeypatch.setattr(obs, '_client', client)
        monkeypatch.setattr(obs, '_sdk', lambda: client)
        @obs.observe('qa.request')
        async def stream():
            with obs.span('retrieval.search'):
                obs.event('retrieval.metrics', top_k=3)
            with obs.generation('answer.generate', model='synthetic'):
                yield 'answer_chunk', {'content': 'same answer'}
            yield 'done', {'generation_status': 'COMPLETED'}
        values, _, _ = _consume(stream())
        assert values[-1][0] == 'done'
        obs.flush_observations(emit_timing=False)
        spans = exporter.get_finished_spans()
        relevant = {span.name: span for span in spans if span.name in ('qa.request', 'retrieval.search', 'answer.generate')}
        assert set(relevant) == {'qa.request', 'retrieval.search', 'answer.generate'}
        assert relevant['retrieval.search'].parent.span_id == relevant['qa.request'].context.span_id
        assert relevant['answer.generate'].parent.span_id == relevant['qa.request'].context.span_id
        observed.append(sorted(relevant))
        before = len(spans)
        client.flush()
        assert len(exporter.get_finished_spans()) == before
        client.shutdown()
        provider.shutdown()
    assert observed[0] == observed[1]


def test_request_lifespan_final_flush_does_not_create_orphan_trace(monkeypatch, tmp_path):
    import uuid
    from types import SimpleNamespace
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from app import main
    import app.services.provider_http as provider_http
    import app.services.bounded_chroma_executor as chroma

    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVABILITY_FLUSH_POLICY', 'REQUEST')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e11')
    log = tmp_path / 'events'
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(log))
    monkeypatch.setattr(main, 'ensure_directories', lambda: None)
    monkeypatch.setattr(main, 'init_db', lambda: None)
    monkeypatch.setattr(main, 'load_persisted_settings', lambda session: None)
    class FakeSession:
        def __init__(self, *args): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(main, 'Session', FakeSession)
    @asynccontextmanager
    async def no_resource(**kwargs):
        yield
    monkeypatch.setattr(provider_http, 'managed_provider_http', no_resource)
    monkeypatch.setattr(chroma, 'managed_chroma_executor', no_resource)
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    client = Langfuse(public_key='pk-test-' + uuid.uuid4().hex, secret_key='synthetic',
                      tracer_provider=provider, span_exporter=exporter,
                      flush_at=50, flush_interval=60)
    monkeypatch.setattr(obs, '_client', client)
    monkeypatch.setattr(obs, '_sdk', lambda: client)
    @obs.observe('qa.request')
    async def stream():
        with obs.span('retrieval.search'):
            pass
        yield 'done', {'generation_status': 'COMPLETED'}
    async def run():
        async with main.lifespan(SimpleNamespace(routes=[])):
            assert [item async for item in stream()][-1][0] == 'done'
    try:
        asyncio.run(run())
        spans = exporter.get_finished_spans()
        roots = [span for span in spans if span.parent is None]
        assert len(roots) == 1 and roots[0].name == 'qa.request'
        assert all(span.context.trace_id == roots[0].context.trace_id for span in spans)
        before = len(spans)
        client.flush()
        assert len(exporter.get_finished_spans()) == before
        rows = [json.loads(line) for line in log.read_text(encoding='utf8').splitlines()]
        timing = [row for row in rows if row.get('stage') == 'performance.stage']
        assert any(row.get('parent_observation_id') for row in timing)
        assert any(not row.get('parent_observation_id') for row in timing)
    finally:
        client.shutdown()
        provider.shutdown()

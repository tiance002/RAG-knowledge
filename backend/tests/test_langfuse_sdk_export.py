"""Actual SDK/OTel serialization with an in-memory exporter, no network."""
from types import SimpleNamespace
import json
import pytest
Langfuse = pytest.importorskip('langfuse').Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.services import observability as obs


def test_sdk_exports_linked_redacted_usage(monkeypatch, tmp_path):
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-p0-in-memory-test', secret_key='test-only',
                      tracer_provider=TracerProvider(), span_exporter=exporter,
                      mask=lambda **kw: obs.sanitize(kw.get('data')))
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(tmp_path/'events'))
    monkeypatch.setattr(obs, '_client', client)
    monkeypatch.setattr(obs, '_attempted', True)
    try:
        with obs.span('qa.sdk.test', qid='public-id') as root:
            obs.event('query', query='private question', api_key='credential-value')
            obs.event('retrieval.outcome', outcome='NO_RESULTS')
            obs.candidates('retrieval.raw', [{'id': 'chunk-1', 'content': 'private document'}])
            obs.model_response(SimpleNamespace(model='test-model', usage={'prompt_tokens': 12, 'completion_tokens': 3},
                                                choices=[SimpleNamespace(finish_reason='length')]))
        client.flush()
        spans = exporter.get_finished_spans()
        assert len(spans) == 5
        assert len({x.context.trace_id for x in spans}) == 1
        serialized = str([dict(x.attributes) for x in spans])
        assert 'NO_RESULTS' in serialized
        assert 'private question' not in serialized and 'private document' not in serialized and 'credential-value' not in serialized
        generation = next(x for x in spans if x.name == 'llm.response')
        assert generation.parent is not None
        assert root['trace_id'] == format(generation.context.trace_id, '032x')
        rows = [json.loads(line) for line in (tmp_path/'events').read_text(encoding='utf-8').splitlines()]
        emitted = [row for row in rows if row['event'] in ('start', 'stage')]
        assert len({row['event_id'] for row in emitted}) == len(emitted) == 5
        for row in emitted:
            matching = [span for span in spans if span.name == row['stage']]
            assert len(matching) == 1
            assert row['event_id'] in str(dict(matching[0].attributes))
    finally:
        client.shutdown()

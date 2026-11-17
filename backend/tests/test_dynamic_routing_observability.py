"""Real SDK export; synthetic provider responses only, no external requests."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased

from app.services import grounded_generation as grounded
from app.services import observability as obs
from app.services.trace_schema import capture_scope


def test_dynamic_escalation_real_sdk_exports_two_generations_without_changing_answer(monkeypatch, tmp_path):
    for key, value in {
        'RAG_OBSERVABILITY_ENABLED': 'true', 'RAG_TRACE_SCHEMA': 'e12',
        'RAG_MODEL_ROUTER_MODE': 'DYNAMIC', 'RAG_MODEL_ROUTER_UNKNOWN_POLICY': 'CHEAP',
        'RAG_MODEL_ROUTER_CHEAP_MODEL': 'cheap-model',
        'RAG_MODEL_ROUTER_EXPENSIVE_MODEL': 'expensive-model',
        'RAG_ROUTER_ALLOW_MODEL_CALLS': 'true', 'RAG_E12_CONTENT_POLICY': 'LOCAL_ONLY',
        'RAG_OBSERVATION_LOG': str(tmp_path / 'events.jsonl'),
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('RAG_MODEL_ROUTER_OFF_FILE', raising=False)
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-dynamic-in-memory', secret_key='synthetic',
        tracer_provider=TracerProvider(sampler=TraceIdRatioBased(1.0)),
        span_exporter=exporter, mask_otel_spans=obs._native_mask_spans)
    monkeypatch.setattr(obs, '_client', client)
    monkeypatch.setattr(obs, '_attempted', True)

    def service(model, answer):
        response = SimpleNamespace(model=model, _request_id='request-' + model,
            usage={'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15},
            choices=[SimpleNamespace(message=SimpleNamespace(content=answer), finish_reason='stop')])
        call = AsyncMock(return_value=response)
        return SimpleNamespace(model=model, _get_client=lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=call)))), call

    cheap, cheap_call = service('cheap-model', 'private first candidate')
    expensive, expensive_call = service('expensive-model', 'private final answer')
    monkeypatch.setattr(grounded, '_role_service', lambda role, expected:
                        cheap if role == 'CHEAP' else expensive)
    messages = [{'role': 'user', 'content': 'private question marker'}]
    citations = [{'chunk_id': 'chunk-1', 'content': 'private evidence marker'}]

    def invoke():
        return asyncio.run(grounded.generate_grounded_candidate(
            cheap, messages, citations, quality_check=lambda answer: 'FAIL'))

    try:
        with capture_scope(qid='routing-q1', capture_mode='BASIC'):
            with obs.span('qa.request'):
                observed = invoke()
        client.flush()
        spans = exporter.get_finished_spans()
        root = next(s for s in spans if s.name == 'qa.request')
        generations = [s for s in spans if s.attributes.get('langfuse.observation.type') == 'generation']
        assert len(generations) == 2
        assert cheap_call.await_count == expensive_call.await_count == 1
        assert all(s.parent.span_id == root.context.span_id for s in generations)
        assert len({s.context.trace_id for s in spans}) == 1
        stages = []
        for span in spans:
            attrs = dict(span.attributes)
            stages.extend(json.loads(attrs.get('langfuse.observation.metadata.stages', '[]')))
        decision = next(e['metadata'] for e in stages if e['stage'] == 'router.decision')
        assert decision['role'] == 'CHEAP'
        assert decision['reason'].startswith(('INTERVAL_', 'UNCERTAIN_'))
        assert decision['features'] and decision['measurements']['weights']
        assert decision['measurements']['model_roles'] == {
            'cheap': {'model': 'cheap-model'}, 'expensive': {'model': 'expensive-model'}}
        outcome = next(e['metadata'] for e in stages if e['stage'] == 'router.outcome')
        assert (outcome['status'], outcome['role'], outcome['retry_count']) == ('COMPLETED', 'EXPENSIVE', 1)
        assert outcome['cost']['status'] == 'UNKNOWN'
        identities = [e['metadata'] for e in stages if e['stage'] == 'router.provider_identity']
        assert {e['provider_model_id'] for e in identities} == {'cheap-model', 'expensive-model'}
        assert all(e['status'] == 'MATCH' for e in identities)
        for span in generations:
            attrs = dict(span.attributes)
            assert json.loads(attrs['langfuse.observation.usage_details']) == {'input': 12, 'output': 3, 'total': 15}
            # SDK/OTel context can expose primitive metadata either directly
            # or as its JSON representation; both mean the same finish reason.
            finish = attrs['langfuse.observation.metadata.finish_reason']
            assert (finish if finish == 'stop' else json.loads(finish)) == 'stop'
        serialized = json.dumps([dict(s.attributes) for s in spans])
        assert 'routing-q1' in serialized
        for private in ('private first candidate', 'private final answer', 'private question marker', 'private evidence marker'):
            assert private not in serialized
        before = len(spans)
        monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
        unobserved = invoke()
        client.flush()
        assert observed == unobserved
        assert cheap_call.await_count == expensive_call.await_count == 2
        assert len(exporter.get_finished_spans()) == before
    finally:
        client.shutdown()

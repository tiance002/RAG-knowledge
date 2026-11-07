"""Actual SDK/ChatOpenAI responses; synthetic transport, zero provider calls."""
import asyncio
from copy import deepcopy
import json
import socket
from types import SimpleNamespace
import uuid

import httpx
import pytest
from langchain_openai import ChatOpenAI
from langfuse import Langfuse
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app.services import llm, observability as obs, metrics
from app.services.trace_schema import _forbidden


@pytest.mark.parametrize('kind', ['cumulative', 'terminal_only', 'nonstream'])
def test_actual_chatopenai_trace_requires_unaggregated_usage(monkeypatch, tmp_path, kind):
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex
    def forbidden(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return connect(sock, address, *args, **kwargs)
        raise AssertionError('This test must never connect outside loopback')
    def forbidden_ex(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return connect_ex(sock, address, *args, **kwargs)
        raise AssertionError('This test must never connect outside loopback')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket.socket, 'connect_ex', forbidden_ex)
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    monkeypatch.setenv('RAG_TRACE_SCHEMA', 'e12')
    monkeypatch.setenv('RAG_E12_CONTENT_POLICY', 'LOCAL_ONLY')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')
    log = tmp_path / 'observations.jsonl'
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(log))
    exporter = InMemorySpanExporter()
    client = Langfuse(public_key='pk-usage-' + uuid.uuid4().hex, secret_key='synthetic-only',
        tracer_provider=TracerProvider(), span_exporter=exporter,
        mask=lambda **kw: obs.sanitize(kw.get('data'), final_export=True),
        mask_otel_spans=obs._native_mask_spans, should_export_span=obs._native_should_export)
    monkeypatch.setattr(obs, '_sdk', lambda: client)
    recorded_metrics = []
    monkeypatch.setattr(metrics, 'record_provider_usage', lambda stage, value: recorded_metrics.append(deepcopy(value)))
    monkeypatch.setattr(obs, 'record_provider_usage', lambda stage, value: recorded_metrics.append(deepcopy(value)))
    requests = []
    usage = {'prompt_tokens': 7, 'completion_tokens': 4, 'total_tokens': 11,
             'completion_tokens_details': {'reasoning_tokens': 2}}

    async def provider(request):
        payload = json.loads(request.content)
        requests.append(payload)
        base = {'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': 'synthetic'}
        if not payload.get('stream'):
            return httpx.Response(200, request=request, json={**base, 'usage': usage,
                'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': '0.9'}, 'finish_reason': 'stop'}]})
        chunks = []
        for text, count, finish in [('0', 2, None), ('.', 3, None), ('9', 4, 'stop')]:
            chunks.append({**base, 'object': 'chat.completion.chunk',
                'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': finish}],
                'usage': {**usage, 'completion_tokens': count, 'total_tokens': 7 + count} if kind == 'cumulative' else None})
        chunks.append({**base, 'object': 'chat.completion.chunk', 'choices': [], 'usage': usage})
        return httpx.Response(200, request=request, headers={'content-type': 'text/event-stream'},
            content=''.join('data: ' + json.dumps(row) + '\n\n' for row in chunks) + 'data: [DONE]\n\n')

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as transport:
            model = ChatOpenAI(model='synthetic', api_key='synthetic-only', base_url='https://example.invalid/v1',
                http_async_client=transport, streaming=kind != 'nonstream', max_retries=0)
            with obs.span('usage.test.root'):
                response = await llm.invoke_provider_model(model, ['PRIVATE_QUESTION'], config={}, stage='quality', model_name='synthetic')
                before = response.model_dump()
                obs.model_response(response, role='quality', model='synthetic')
                assert response.model_dump() == before
                return response

    try:
        response = asyncio.run(run())
        assert response.content == '0.9' and len(requests) == 1
        expected_raw = (28, 13) if kind == 'cumulative' else (7, 4)
        assert (response.usage_metadata['input_tokens'], response.usage_metadata['output_tokens']) == expected_raw
        assert all(value == response.usage_metadata for value in recorded_metrics)
        client.flush()
        spans = exporter.get_finished_spans()
        generation = next(span for span in spans if span.name == 'agent.quality')
        attrs = dict(generation.attributes)
        native_usage = json.loads(attrs.get('langfuse.observation.usage_details', '{}'))
        if kind == 'nonstream':
            assert native_usage == {'input': 7, 'output': 4, 'total': 11}
        else:
            # Even terminal-only streams are conservative UNKNOWN: the final
            # aggregate alone does not prove the provider's stream semantics.
            assert not native_usage
            assert 'UNKNOWN' in str(attrs)
        root = next(span for span in spans if span.name == 'usage.test.root')
        assert root is not None  # numeric reasoning count must not reject root
        rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        event = next(row for row in rows if row['stage'] == 'llm.response')
        assert (event['metadata']['usage'] is None) == (kind != 'nonstream')
        exported = str([dict(span.attributes) for span in spans])
        assert 'PRIVATE_QUESTION' not in exported and 'synthetic-only' not in exported
    finally:
        client.shutdown()


@pytest.mark.parametrize('value', [True, -1, 1.5, 'hidden private reasoning', {'text': 'hidden private reasoning'}])
def test_numeric_counter_normalization_never_whitelists_hidden_reasoning(monkeypatch, value):
    payload = {'output_token_details': {'reasoning': value}}
    original = deepcopy(payload)
    normalized = obs.trace_response_usage(SimpleNamespace(usage=payload))[0]
    assert payload == original
    assert _forbidden(normalized)


def test_numeric_counter_trace_copy_preserves_original_and_hidden_content_gate():
    payload = {'output_token_details': {'reasoning': 2}, 'completion_tokens': 4}
    original = deepcopy(payload)
    normalized, status = obs.trace_response_usage(SimpleNamespace(usage=payload))
    assert payload == original and status == 'PROVIDER_REPORTED'
    assert normalized['output_token_details'] == {'reasoning_tokens': 2}
    assert not _forbidden(normalized)
    assert _forbidden({'reasoning': 'still private'})


@pytest.mark.parametrize('existing', [True, '1', 2])
def test_conflicting_counter_keeps_forbidden_key(existing):
    payload = {'output_token_details': {'reasoning': 1, 'reasoning_tokens': existing}}
    original = deepcopy(payload)
    normalized, _ = obs.trace_response_usage(SimpleNamespace(usage=payload))
    assert payload == original and normalized == original
    assert _forbidden(normalized)

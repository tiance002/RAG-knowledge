"""Capture actual SDK request JSON without credentials or external network calls."""
import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from app.services import grounded_generation, llm as llm_module


MODEL = 'Qwen/Qwen3.5-4B'
URL = 'https://api.siliconflow.cn/v1'
MESSAGES = [{'role': 'user', 'content': 'Synthetic transport check.'}]


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    monkeypatch.setenv('RAG_MODEL_ROUTER_MODE', 'OFF')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_SNAPSHOTS_ENABLED', 'false')
    monkeypatch.delenv('OPENAI_PROXY', raising=False)
    monkeypatch.setattr(llm_module.settings, 'LLM_ANSWER_MAX_TOKENS', 4096)
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 11, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(llm_module, 'datetime', FixedDatetime)


def record_transport(monkeypatch, records, failure=None):
    def handle(request):
        payload = json.loads(request.content)
        records.append(payload)
        if failure == 'timeout':
            raise httpx.ReadTimeout('Synthetic timeout', request=request)
        if failure == 'http500':
            return httpx.Response(500, json={'error': {'message': 'Synthetic error', 'type': 'test'}})
        if payload.get('stream'):
            chunk = {'id': 'synthetic', 'object': 'chat.completion.chunk', 'created': 1,
                     'model': MODEL, 'choices': [{'index': 0, 'delta': {'content': 'synthetic answer'},
                                                 'finish_reason': 'stop'}]}
            return httpx.Response(200, headers={'content-type': 'text/event-stream'},
                                  content='data: ' + json.dumps(chunk) + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json={
            'id': 'synthetic', 'object': 'chat.completion', 'created': 1, 'model': MODEL,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'synthetic answer'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5},
        })

    # Keep the real factory, SDK, URL predicate, serialization and retry policy.
    # Intercept only the HTTP transport; no authentication headers are recorded.
    monkeypatch.setattr(llm_module, 'ProviderBorrowingTransport',
                        lambda **kwargs: httpx.MockTransport(handle))


async def run_operation(service, operation, cap=None):
    service.history_rounds = 20  # Isolate transport checks from persisted settings/DB.
    if operation == 'rewrite':
        return await service.rewrite_query('Synthetic question')
    if operation == 'answer':
        return await service.generate(MESSAGES)
    if operation == 'stream':
        return [part async for part in service.generate_stream(MESSAGES, max_tokens=cap)]
    if operation == 'grounded':
        return await grounded_generation.generate_grounded_candidate(
            service, MESSAGES, [{'chunk_id': 'synthetic-c1', 'file_id': 'synthetic-f1',
                                 'content': 'Synthetic evidence.'}])
    if operation == 'fallback':
        return await grounded_generation.generate_no_results_fallback(service, 'Synthetic question')
    model = (grounded_generation._routed_chat_model(service, []) if operation == 'agent_routed'
             else service.get_chat_model(streaming=False, max_tokens=cap))
    try:
        return await model.ainvoke(MESSAGES)
    finally:
        while hasattr(model, 'bound'):
            model = model.bound
        await model.root_async_client.close()
        model.root_client.close()


async def close_service(service):
    if service._client is not None:
        await service._client.close()


@pytest.mark.parametrize('operation,cap', [
    ('rewrite', None), ('answer', None), ('stream', None), ('stream', 321),
    ('grounded', None), ('fallback', None), ('agent', None), ('agent', 321),
    ('agent_routed', None),
])
def test_wire_change_is_only_thinking_flag(monkeypatch, operation, cap):
    monkeypatch.setattr(grounded_generation.get_settings(), 'FALLBACK_STRATEGY', 'model')
    records = []
    record_transport(monkeypatch, records)

    async def run():
        baseline = llm_module.LLMService(base_url=URL, api_key='synthetic-not-a-key', model=MODEL)
        # Pre-fix request construction had no provider defaults.
        baseline.provider_request_parameters = lambda *args, **kwargs: {}
        patched = llm_module.LLMService(base_url=URL, api_key='synthetic-not-a-key', model=MODEL)
        try:
            await run_operation(baseline, operation, cap)
            await run_operation(patched, operation, cap)
        finally:
            await close_service(baseline)
            await close_service(patched)

    asyncio.run(run())
    assert len(records) == 2
    before, after = records
    assert after.pop('enable_thinking') is False
    assert 'extra_body' not in after
    assert after == before  # Prompts, limits, stream mode and all other JSON stay identical.
    if operation == 'rewrite':
        assert before['max_tokens'] == 200
    elif operation in ('answer', 'grounded', 'fallback'):
        assert before['max_tokens'] == 4096
    elif cap is None:
        assert 'max_tokens' not in before and 'max_completion_tokens' not in before
    elif operation == 'stream':
        assert before['max_tokens'] == cap
    else:
        # Preserve the SDK's existing alias; do not alter token policy in this fix.
        assert before.get('max_tokens', before.get('max_completion_tokens')) == cap


@pytest.mark.parametrize('url,model', [
    ('https://api.openai.com/v1', MODEL), ('http://localhost:9999/v1', MODEL),
    (URL, 'XingChenAGI/Xing4.0-29B'), (URL, 'Qwen/Qwen3-8B'),
])
@pytest.mark.parametrize('operation', ['rewrite', 'agent'])
def test_other_models_and_providers_receive_no_new_flag(monkeypatch, url, model, operation):
    records = []
    record_transport(monkeypatch, records)
    service = llm_module.LLMService(base_url=url, api_key='synthetic-not-a-key', model=model)

    async def run():
        try:
            await run_operation(service, operation)
        finally:
            await close_service(service)

    asyncio.run(run())
    assert len(records) == 1
    assert 'enable_thinking' not in records[0]


def test_explicit_role_options_win_without_mutation(monkeypatch):
    records = []
    record_transport(monkeypatch, records)
    service = llm_module.LLMService(base_url=URL, api_key='synthetic-not-a-key', model=MODEL)
    options = {'enable_thinking': True, 'thinking_budget': 512}
    service._router_extra_body = options

    async def run():
        try:
            await run_operation(service, 'agent_routed')
        finally:
            await close_service(service)

    asyncio.run(run())
    assert records[0]['enable_thinking'] is True and records[0]['thinking_budget'] == 512
    assert options == {'enable_thinking': True, 'thinking_budget': 512}
    assert service.provider_request_parameters(extra_body={'enable_thinking': False,
                                                         'thinking_budget': 512})['extra_body'] == {
        'enable_thinking': False, 'thinking_budget': 512}


@pytest.mark.parametrize('operation', ['rewrite', 'agent'])
@pytest.mark.parametrize('failure,expected_status', [('timeout', 'UNKNOWN'), ('http500', 'FAILED')])
def test_sdk_does_not_retry_unknown_or_known_error(monkeypatch, operation, failure, expected_status):
    records = []
    record_transport(monkeypatch, records, failure=failure)
    service = llm_module.LLMService(base_url=URL, api_key='synthetic-not-a-key', model=MODEL)

    async def run():
        try:
            with pytest.raises(Exception) as exc:
                await run_operation(service, operation)
            assert llm_module._provider_status(exc.value) == expected_status
        finally:
            await close_service(service)

    asyncio.run(run())
    assert len(records) == 1
    assert records[0]['enable_thinking'] is False

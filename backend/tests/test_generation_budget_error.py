"""Budget rejection classification without contacting any provider."""
import json
from types import SimpleNamespace

import pytest

from app.services import grounded_generation as gg
from app.services import llm


def serialized_chars(messages, tools=None):
    return len(json.dumps({'messages': messages, 'tools': tools or []}, ensure_ascii=False, default=str))


def test_exact_budget_remains_admissible(monkeypatch):
    messages = [{'role': 'user', 'content': '中文 evidence'}]
    tools = [{'name': 'lookup'}]
    count = serialized_chars(messages, tools)
    monkeypatch.setattr(gg, 'get_settings', lambda: SimpleNamespace(GROUNDED_REQUEST_MAX_CHARS=count))
    assert gg.enforce_request_budget(messages, tools) == count


def test_over_budget_reports_local_rejection_without_content_or_provider_id(monkeypatch):
    messages = [{'role': 'user', 'content': 'private synthetic content 中文'}]
    count = serialized_chars(messages)
    monkeypatch.setattr(gg, 'get_settings', lambda: SimpleNamespace(GROUNDED_REQUEST_MAX_CHARS=count - 1))
    with pytest.raises(llm.GenerationInputBudgetExceeded, match='FINAL_REQUEST_OVER_CHAR_BUDGET') as caught:
        gg.enforce_request_budget(messages)
    assert isinstance(caught.value, ValueError)
    assert caught.value.request_chars == count and caught.value.limit_chars == count - 1
    payload = llm.provider_error_payload(caught.value, stage='agent_workflow')
    assert payload['code'] == 'FINAL_REQUEST_OVER_CHAR_BUDGET'
    assert payload['status'] == 'FAILED' and payload['stage'] == 'generation_input_budget'
    assert payload['generation_request_sent'] is False and payload['retryable'] is False
    assert payload['request_chars'] == count and payload['limit_chars'] == count - 1
    assert 'provider_request_id' not in payload and 'local_request_id' not in payload
    assert 'private synthetic content' not in json.dumps(payload, ensure_ascii=False)
    assert '\u672a\u53d1\u9001' in payload['error']


def test_timeout_remains_unknown_and_not_budget_rejection():
    payload = llm.provider_error_payload(TimeoutError('private'), stage='agent_workflow')
    assert payload['status'] == 'UNKNOWN'
    assert payload.get('code') != 'FINAL_REQUEST_OVER_CHAR_BUDGET'
    assert 'generation_request_sent' not in payload
    assert 'private' not in json.dumps(payload)


def test_agent_stream_exposes_specific_budget_failure(monkeypatch):
    import asyncio
    from app.services import agent_workflow
    from app.services.chat import ChatService
    from app.schemas import Message, MessageRole

    monkeypatch.setattr(gg, 'get_settings', lambda: SimpleNamespace(GROUNDED_REQUEST_MAX_CHARS=1))
    async def failed_events(*args, **kwargs):
        gg.enforce_request_budget([{'role': 'user', 'content': 'private synthetic content'}])
        yield {}
    monkeypatch.setattr(agent_workflow, 'AgentWorkflow',
                        lambda: SimpleNamespace(app=SimpleNamespace(astream_events=failed_events)))
    service = ChatService.__new__(ChatService)
    service._request_settings = SimpleNamespace(max_chat_history_rounds=20)
    service._request_llm = SimpleNamespace(base_url='https://example.invalid/v1', api_key='fake', model='fake')
    async def run():
        return [item async for item in service._chat_stream_langgraph(
            messages=[Message(role=MessageRole.USER, content='question')],
            kb_ids=['kb'], top_k=1, score_threshold=0, model_id=None, use_rewrite=False)]
    events = asyncio.run(run())
    assert events[-1][0] == 'error'
    payload = events[-1][1]
    assert payload['code'] == 'FINAL_REQUEST_OVER_CHAR_BUDGET'
    assert payload['stage'] == 'generation_input_budget'
    assert payload['generation_request_sent'] is False and payload['retryable'] is False
    assert 'provider_request_id' not in payload and 'private synthetic content' not in str(payload)
    assert not any(event == 'done' for event, _ in events)

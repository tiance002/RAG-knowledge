import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.llm import LLMService


@pytest.mark.parametrize('query,output,expected', [
    ('What is the plan for the next quarter?', '{"rewrite_query":"What is the next-quarter plan?","intent":"kb_search"}', 'What is the next-quarter plan?'),
    ('下一季度的工作计划是什么？', '{"rewrite_query":"下一季度的工作计划是什么？","intent":"kb_search"}', '下一季度的工作计划是什么？'),
    ('What is Redis eviction?', 'What is Redis 内存淘汰策略?', 'What is Redis 内存淘汰策略?'),
    ('Translate this question to Chinese.', '{"rewrite_query":"请把这个问题翻译成中文。","intent":"chitchat"}', '请把这个问题翻译成中文。'),
    ('它的 Redis 策略是什么？', '{"rewrite_query":"Redis 的缓存淘汰策略是什么？","intent":"kb_search"}', 'Redis 的缓存淘汰策略是什么？'),
    ('What is Redis?', '{invalid', ''),
])
def test_rewrite_structured_result_and_request_limit(query, output, expected):
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=output))], usage=None))
    service = LLMService(api_key='test', model='test')
    service.history_rounds = 2
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert asyncio.run(service.rewrite_query(query)) == expected
    assert create.await_args.kwargs['max_tokens'] == 200


def test_understand_query_returns_three_fields_with_image_in_single_call():
    output = '{"rewrite_query":"What is this?","intent":"image_only","image_description":"a blue square"}'
    create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
        finish_reason='stop', message=SimpleNamespace(content=output))], usage=None))
    service = LLMService(api_key='test', model='vision-test')
    service.history_rounds = 1
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    result = asyncio.run(service.understand_query('What is this?', images=['data:image/png;base64,c3ludGhldGlj']))
    assert result == {'rewrite_query': 'What is this?', 'intent': 'image_only', 'image_description': 'a blue square'}
    create.assert_awaited_once()
    request = create.await_args.kwargs
    assert request['max_tokens'] == 2048
    assert request['messages'][1]['content'][1]['type'] == 'image_url'


@pytest.mark.parametrize('requested,expected', [(200, 200), (None, 4096), (5000, 4096)])
def test_proxy_preserves_rewrite_limit(tmp_path, monkeypatch, requested, expected):
    import http.client
    from scripts import rsi_quality_worker as worker

    sent = []
    def post(client, url, *args, **kwargs):
        sent.append(json.loads(kwargs['content']))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': 'ok'}}]}, request=httpx.Request('POST', url))

    monkeypatch.setattr(httpx.Client, 'post', post)
    proxy = worker.RecordingProxy(tmp_path, 'offline-key', 1)
    proxy.context = {'case_id': 'limit', 'repeat_id': 1}
    proxy.deadline = time.monotonic() + 30
    port = proxy.start()
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    try:
        payload = {'messages': [{'role': 'user', 'content': 'public'}]}
        if requested is not None:
            payload['max_tokens'] = requested
        connection.request('POST', '/v1/chat/completions', body=json.dumps(payload),
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        assert response.status == 200
        response.read()
        assert sent[0]['max_tokens'] == expected
    finally:
        connection.close()
        proxy.stop()

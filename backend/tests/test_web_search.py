"""Offline HTTP boundary tests; no Tavily/model request is sent."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import Settings
from app.services import web_search


def settings(**overrides):
    return SimpleNamespace(TAVILY_API_KEY=overrides.get('key', 'offline-test-key'),
                           TAVILY_API_KEY_FILE=overrides.get('key_file', ''),
                           TAVILY_TIMEOUT_SECONDS=overrides.get('timeout', 20),
                           TAVILY_MAX_RESULTS=overrides.get('limit', 5))


def invoke(monkeypatch, handler, config=None, query='公开问题'):
    original_client = httpx.AsyncClient
    calls = []
    def capture(request):
        calls.append(request)
        return handler(request)
    monkeypatch.setattr(web_search, 'get_settings', lambda: config or settings())
    monkeypatch.setattr(web_search.httpx, 'AsyncClient', lambda **kw:
                        original_client(transport=httpx.MockTransport(capture), **kw))
    result = asyncio.run(web_search.search_web(query))
    return result, calls


def test_search_preserves_sources_and_uses_single_bounded_basic_request(monkeypatch):
    def handler(request):
        assert request.url == 'https://api.tavily.com/search'
        assert request.headers['authorization'] == 'Bearer offline-test-key'
        body = json.loads(request.content)
        assert body['query'] == '公开问题'
        assert body['search_depth'] == 'basic' and body['auto_parameters'] is False
        assert body['max_results'] == 5 and body['include_raw_content'] is False
        assert body['include_answer'] is False and body['include_usage'] is True
        assert 'history' not in body and 'messages' not in body and 'context' not in body
        assert request.extensions['timeout']['read'] == 20
        return httpx.Response(200, json={'query': '公开问题', 'request_id': 'req-1',
            'usage': {'credits': 1}, 'results': [{'title': '原始标题', 'url': 'https://example.org/source',
                                               'content': '原始证据内容', 'score': 0.91}]})
    result, calls = invoke(monkeypatch, handler)
    assert len(calls) == 1 and result.status == 'SUCCESS'
    assert result.query == result.provider_query == '公开问题'
    assert result.request_id == 'req-1' and result.usage == {'credits': 1}
    assert result.results[0]['content'] == '原始证据内容'
    assert result.results[0]['title'] == '原始标题'
    assert result.results[0]['source_type'] == 'web'
    assert 'chunk_id' not in result.results[0] and 'file_id' not in result.results[0]


@pytest.mark.parametrize('code,status', [(401, 'FAILED'), (429, 'FAILED'), (432, 'FAILED'),
                                          (500, 'UNKNOWN'), (503, 'UNKNOWN')])
def test_http_failures_do_not_retry_or_expose_response(monkeypatch, code, status):
    result, calls = invoke(monkeypatch, lambda request:
                           httpx.Response(code, text='secret-provider-diagnostics', headers={'x-request-id': 'error-id'}))
    assert result.status == status and len(calls) == 1
    assert result.request_id == 'error-id'
    assert 'secret-provider-diagnostics' not in str(result.metadata())
    assert 'offline-test-key' not in str(result)


@pytest.mark.parametrize('error', [httpx.ReadTimeout('secret-token'), httpx.ConnectError('secret-token')])
def test_unknown_transport_outcome_has_no_retry(monkeypatch, error):
    def handler(request):
        raise error
    result, calls = invoke(monkeypatch, handler)
    assert result.status == 'UNKNOWN' and len(calls) == 1
    assert 'secret-token' not in str(result)


def test_total_deadline_cancels_one_pending_attempt(monkeypatch):
    cancelled = []
    async def handler(request):
        try:
            await asyncio.sleep(2)
            return httpx.Response(200, json={'results': []})
        except asyncio.CancelledError:
            cancelled.append(True)
            raise
    result, calls = invoke(monkeypatch, handler, settings(timeout=1))
    assert result.status == 'UNKNOWN' and len(calls) == 1 and cancelled == [True]


@pytest.mark.parametrize('payload', [{}, {'results': None}, ['invalid']])
def test_invalid_success_response_is_unknown(monkeypatch, payload):
    result, calls = invoke(monkeypatch, lambda request: httpx.Response(200, json=payload))
    assert result.status == 'UNKNOWN' and len(calls) == 1


def test_no_results_and_unsafe_urls_are_not_usable_evidence(monkeypatch):
    rows = [{'url': 'javascript:alert(1)', 'content': 'bad'},
            {'url': 'https://user:password@example.org', 'content': 'bad'},
            {'url': 'https://example.org', 'content': ''}]
    result, _ = invoke(monkeypatch, lambda r: httpx.Response(200, json={'results': rows}))
    assert result.status == 'NO_RESULTS' and result.results == []


def test_not_configured_makes_no_request(monkeypatch, tmp_path):
    result, calls = invoke(monkeypatch, lambda r: pytest.fail('network forbidden'),
                           settings(key='', key_file=str(tmp_path / 'missing-secret')))
    assert result.status == 'NOT_CONFIGURED' and not calls
    assert str(tmp_path) not in result.message


def test_key_file_and_environment_precedence(monkeypatch, tmp_path):
    keyfile = tmp_path / 'secret'
    keyfile.write_text('\ufefffile-test-key\n', encoding='utf-8')
    result, calls = invoke(monkeypatch, lambda r: httpx.Response(200, json={'results': []}),
                           settings(key='', key_file=str(keyfile)))
    assert result.status == 'NO_RESULTS'
    assert calls[0].headers['authorization'] == 'Bearer file-test-key'
    assert web_search._read_api_key(settings(key='env-test-key', key_file=str(keyfile))) == 'env-test-key'


def test_query_length_rejected_without_silent_truncation(monkeypatch):
    result, calls = invoke(monkeypatch, lambda r: pytest.fail('network forbidden'), query='x' * 1501)
    assert result.status == 'FAILED' and not calls


def test_runtime_dotenv_precedence(monkeypatch, tmp_path):
    legacy = tmp_path / 'backend.env'
    unified = tmp_path / 'runtime-secrets.env'
    legacy.write_text('TAVILY_API_KEY=legacy-test-key\n', encoding='utf-8')
    unified.write_text('TAVILY_API_KEY=unified-test-key\n', encoding='utf-8')
    monkeypatch.delenv('TAVILY_API_KEY', raising=False)
    assert Settings(_env_file=(legacy, unified)).TAVILY_API_KEY == 'unified-test-key'
    monkeypatch.setenv('TAVILY_API_KEY', 'process-test-key')
    assert Settings(_env_file=(legacy, unified)).TAVILY_API_KEY == 'process-test-key'

"""One real Tavily query through the product SSE route; model generation is SYNTHETIC.

Run with --live to opt in. Without it, this module imports no product code and sends
zero requests. Evidence directories are exclusive; failed/UNKNOWN runs are not retried.
"""
import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch
import uuid


ROOT = Path(__file__).resolve().parents[1]
QUERY = 'Python official documentation len() built-in function'
SYNTHETIC_TEXT = ('SYNTHETIC 模型占位输出：本次仅验证真实 Tavily 网页证据被送达模型请求边界；'
                  '未调用业务模型。[1]')


def save_json(path, payload):
    with path.open('x', encoding='utf-8') as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write('\n')


async def run_live(output):
    # Set before importing the application. This process cannot export synthetic metrics.
    for name in ('RAG_OBSERVABILITY_ENABLED', 'RAG_PROMETHEUS_ENABLED', 'LANGFUSE_ENABLED',
                 'LANGCHAIN_TRACING_V2', 'LANGCHAIN_TRACING'):
        os.environ[name] = 'false'
    os.environ['OTEL_SDK_DISABLED'] = 'true'
    os.environ['RAG_MODEL_ROUTER_MODE'] = 'OFF'
    # redis-py rejects this scheme without opening a socket. The real MemoryService
    # handles the configuration error and returns its documented default snapshot.
    os.environ['MEMORY_REDIS_URL'] = 'memory://tavily-smoke-disabled'
    sys.path.insert(0, str(ROOT / 'backend'))

    import httpx
    import requests
    from app.core.config import get_settings
    from app.main import app
    from app.api import chat as chat_api
    from app.services.chat import ChatService
    from app.services.llm import LLMService
    from app.services.web_search import _read_api_key

    secret = _read_api_key(get_settings())
    manifest = {
        'schema': 'tavily-product-smoke-v1',
        'command_argv': [sys.executable, *sys.argv],
        'started_at': datetime.now(timezone.utc).isoformat(),
        'query': QUERY, 'route': '/api/v1/chat/completions',
        'tavily': {'status': 'NOT_RUN', 'attempts': 0},
        'provider_generation': 'SYNTHETIC', 'real_model_calls': 0,
        'isolation': {
            'asgi_route': 'REAL', 'chat_stream': 'REAL', 'web_search': 'REAL',
            'grounded_prompt_construction': 'REAL',
            'lifespan': 'NOT_RUN: avoids DB initialization/persisted settings loading',
            'unused_retrieval_initialization': 'OMITTED in smoke-only ChatService subclass',
            'preferences': 'REAL MemoryService defaults after unsupported memory:// configuration; no Redis socket',
            'model_network': 'LOCAL SYNTHETIC replacement of LLMService._get_client only',
            'observations_and_cloud_export': 'DISABLED',
        },
        'deployed_startup_validation': 'NOT_RUN',
        'answer_quality_validation': 'NOT_RUN: synthetic provider response',
        'kb_ids': [], 'database_writes': 0, 'index_writes': 0,
    }
    if not secret:
        manifest['tavily']['status'] = 'NOT_CONFIGURED'
        manifest['exit_code'] = 2
        save_json(output / 'manifest.json', manifest)
        return 2

    model_requests = []
    tavily_attempts = []
    original_send = httpx.AsyncClient.send

    def redact(text):
        return text.replace(secret, '[REDACTED]') if secret else text

    async def guarded_send(client, request, **kwargs):
        if request.url.host == 'inprocess-tavily-smoke' and isinstance(client._transport, httpx.ASGITransport):
            return await original_send(client, request, **kwargs)
        if str(request.url) != 'https://api.tavily.com/search' or request.method != 'POST':
            raise RuntimeError('SMOKE_NON_TAVILY_NETWORK_BLOCKED')
        if tavily_attempts:
            raise RuntimeError('SMOKE_SECOND_TAVILY_ATTEMPT_BLOCKED')
        body = json.loads(request.content)
        if body.get('query') != QUERY:
            raise RuntimeError('SMOKE_QUERY_CHANGED')
        tavily_attempts.append({'query': body['query'], 'status': 'UNKNOWN'})
        # Never persist request headers, bearer tokens, or client configuration.
        save_json(output / 'tavily-request.json', body)
        response = await original_send(client, request, **kwargs)
        raw = await response.aread()
        public_body = redact(raw.decode('utf-8', errors='replace'))
        with (output / 'tavily-response.json').open('x', encoding='utf-8') as handle:
            handle.write(public_body)
        tavily_attempts[0].update(status='HTTP_RESPONSE_RECEIVED', http_status=response.status_code,
                                  body_sha256=hashlib.sha256(public_body.encode()).hexdigest())
        return response

    async def synthetic_completion(**kwargs):
        if model_requests:
            raise RuntimeError('SMOKE_SECOND_MODEL_ATTEMPT_BLOCKED')
        model_requests.append(kwargs)
        save_json(output / 'model-request-SYNTHETIC.json',
                  {'provider_generation': 'SYNTHETIC', 'network_sent': False, 'request': kwargs})
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=SYNTHETIC_TEXT),
                                                        finish_reason='stop')],
                               model=kwargs.get('model'), usage=None, _request_id='synthetic-not-provider')

    synthetic_client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=synthetic_completion)))

    class IsolatedWebChat(ChatService):
        def __init__(self):
            # All exercised methods stay inherited. The web branch never uses retrieval.
            self.llm_service = LLMService()

    def blocked_requests(*args, **kwargs):
        raise RuntimeError('SMOKE_REQUESTS_NETWORK_BLOCKED')

    try:
        with patch.object(chat_api, 'ChatService', IsolatedWebChat), \
                patch.object(LLMService, '_get_client', lambda self: synthetic_client), \
                patch.object(httpx.AsyncClient, 'send', guarded_send), \
                patch.object(requests.sessions.Session, 'request', blocked_requests):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url='http://inprocess-tavily-smoke') as client:
                response = await client.post('/api/v1/chat/completions', json={
                    'messages': [{'role': 'user', 'content': QUERY}], 'kb_ids': [],
                    'mode': 'agent', 'web_search_enabled': True, 'stream': True,
                })
        sse = redact(response.text)
        with (output / 'response.sse').open('x', encoding='utf-8') as handle:
            handle.write(sse)
        events = []
        kind = None
        for line in sse.splitlines():
            if line.startswith('event: '):
                kind = line[7:]
            elif line.startswith('data: '):
                events.append({'event': kind, 'data': json.loads(line[6:])})
        save_json(output / 'sse-events.json', events)
        search = next((item['data'] for item in reversed(events)
                       if item['event'] == 'web_search_status'), {})
        citations = next((item['data'].get('citations', []) for item in events
                          if item['event'] == 'rag_result'), [])
        done = next((item['data'] for item in events if item['event'] == 'done'), {})
        prompt = '\n\n'.join(message.get('content', '') for message in
                             (model_requests[0].get('messages', []) if model_requests else []))
        checks = {
            'http_200': response.status_code == 200,
            'exactly_one_real_tavily_attempt': len(tavily_attempts) == 1,
            'search_success': search.get('status') == 'SUCCESS',
            'web_citations_present': bool(citations) and all(c.get('source_type') == 'web' for c in citations),
            'exactly_one_synthetic_model_request': len(model_requests) == 1,
            'all_source_urls_in_actual_model_request': bool(citations) and all(c['url'] in prompt for c in citations),
            'all_source_contents_in_actual_model_request': bool(citations) and all(c['content'] in prompt for c in citations),
            'explicit_execution_mode': done.get('execution_mode') == 'WEB_SEARCH_GROUNDED',
            'synthetic_answer_in_sse': SYNTHETIC_TEXT in sse,
            'no_sse_error': not any(item['event'] == 'error' for item in events),
        }
        manifest.update(http_status=response.status_code, checks=checks,
                        citations=[{'title': c.get('title'), 'url': c.get('url')} for c in citations],
                        provider_model_request_count=len(model_requests),
                        sse_event_types=[item['event'] for item in events])
        manifest['tavily'].update(status=search.get('status', 'UNKNOWN'), attempts=len(tavily_attempts),
                                 request_id=search.get('request_id'), usage=search.get('usage'),
                                 transport=tavily_attempts)
        exit_code = 0 if all(checks.values()) else 1
    except Exception as exc:
        # Save the error class only: exception messages can contain URLs or credentials.
        manifest['error_type'] = type(exc).__name__
        manifest['tavily'].update(status='UNKNOWN' if tavily_attempts else 'NOT_RUN',
                                 attempts=len(tavily_attempts), transport=tavily_attempts)
        exit_code = 1
    manifest['finished_at'] = datetime.now(timezone.utc).isoformat()
    manifest['exit_code'] = exit_code
    save_json(output / 'manifest.json', manifest)
    print(json.dumps({'output': str(output), 'exit_code': exit_code,
                      'tavily_status': manifest['tavily']['status'],
                      'tavily_attempts': len(tavily_attempts), 'provider_generation': 'SYNTHETIC'}))
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Authorize exactly one real Tavily query')
    parser.add_argument('--output', type=Path, help='New exclusive directory under project var/')
    args = parser.parse_args()
    if not args.live:
        print('NOT RUN: --live is required; Tavily calls=0, model calls=0.')
        return 0
    output = (args.output or ROOT / 'var' / 'tavily-rsi-20261105' / ('live-smoke-' + uuid.uuid4().hex)).resolve()
    if not output.is_relative_to((ROOT / 'var').resolve()):
        parser.error('--output must be inside project var/')
    output.mkdir(parents=True, exist_ok=False)
    return asyncio.run(run_live(output))


if __name__ == '__main__':
    raise SystemExit(main())

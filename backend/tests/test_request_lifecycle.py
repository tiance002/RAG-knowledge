import asyncio
import json

import pytest
from app.services.performance_timing import RequestLifecycleMiddleware


def _scope(attempt=b"attempt-1"):
    return {"type": "http", "method": "POST", "path": "/api/v1/chat/completions",
            "headers": [(b"x-rag-run-id", b"run-1"), (b"x-rag-attempt-id", attempt)]}


def _enable(monkeypatch, tmp_path):
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("RAG_PERFORMANCE_IDENTITY_HEADERS", "true")
    monkeypatch.setenv("RAG_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("RAG_OBSERVATION_LOG", str(path))
    return path


def test_records_after_final_body_and_keeps_identity_basic(monkeypatch, tmp_path):
    path = _enable(monkeypatch, tmp_path)
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"secret", "more_body": False})
        await asyncio.sleep(0.03)
    async def send(message): pass
    asyncio.run(RequestLifecycleMiddleware(app)(_scope(), None, send))
    row = json.loads(path.read_text(encoding="utf-8"))
    assert row['attempt_id'] == 'attempt-1' and row['event'] == 'stage'
    assert row['metadata']['post_body_app_cleanup_ms'] >= 20
    assert row['metadata']['asgi_app_ms'] >= row['metadata']['post_body_app_cleanup_ms']
    assert row['metadata']['asgi_app_status'] == 'RETURNED'
    assert 'network_teardown' in row['metadata']['timing_scope']
    assert 'secret' not in path.read_text(encoding='utf-8')


@pytest.mark.parametrize('failure', ['error', 'cancel'])
def test_failed_or_cancelled_has_no_invented_final_body(monkeypatch, tmp_path, failure):
    path = _enable(monkeypatch, tmp_path)
    async def app(scope, receive, send):
        if failure == 'cancel':
            raise asyncio.CancelledError()
        raise ValueError('do not log this')
    async def send(message): pass
    with pytest.raises(asyncio.CancelledError if failure == 'cancel' else ValueError):
        asyncio.run(RequestLifecycleMiddleware(app)(_scope(), None, send))
    row = json.loads(path.read_text(encoding='utf-8'))
    assert row['metadata']['asgi_app_status'] == ('CANCELLED' if failure == 'cancel' else 'FAILED')
    assert row['metadata']['post_body_app_cleanup_ms'] is None
    assert 'do not log this' not in path.read_text(encoding='utf-8')


def test_opt_in_and_valid_identity_required(monkeypatch, tmp_path):
    path = _enable(monkeypatch, tmp_path)
    async def app(scope, receive, send): pass
    async def send(message): pass
    asyncio.run(RequestLifecycleMiddleware(app)(_scope(b'invalid space'), None, send))
    monkeypatch.setenv('RAG_PERFORMANCE_IDENTITY_HEADERS', 'false')
    asyncio.run(RequestLifecycleMiddleware(app)(_scope(), None, send))
    assert not path.exists()

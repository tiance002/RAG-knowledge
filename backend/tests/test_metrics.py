import asyncio
from types import SimpleNamespace

from app.services import metrics


class Metric:
    def __init__(self, name, rows):
        self.name = name
        self.rows = rows

    def labels(self, **labels):
        return BoundMetric(self.name, labels, self.rows)

    def inc(self, value=1):
        self.rows.append((self.name, {}, "inc", value))

    def dec(self, value=1):
        self.rows.append((self.name, {}, "dec", value))


class BoundMetric:
    def __init__(self, name, labels, rows):
        self.name, self.labels, self.rows = name, labels, rows

    def inc(self, value=1):
        self.rows.append((self.name, self.labels, "inc", value))

    def dec(self, value=1):
        self.rows.append((self.name, self.labels, "dec", value))

    def observe(self, value):
        self.rows.append((self.name, self.labels, "observe", value))


def install_fake_metrics(monkeypatch):
    rows = []
    monkeypatch.setattr(metrics, "_enabled", lambda: True)
    monkeypatch.setattr(metrics, "_initialize", lambda: True)
    for name in ("_http_requests", "_http_duration", "_http_in_flight", "_sse_events", "_sse_ttft", "_sse_requests", "_sse_duration", "_provider_attempts", "_provider_duration", "_background_in_flight", "_background_tasks", "_background_duration", "_token_usage", "_agent_retry_decisions"):
        monkeypatch.setattr(metrics, name, Metric(name, rows))
    return rows


def test_asgi_http_metrics_wait_for_full_stream_and_record_terminal(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    route = SimpleNamespace(path="/api/v1/chat/completions")

    async def app(scope, receive, send):
        scope["route"] = route
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        metrics.sse_event("answer_chunk", {"content": "first"})
        metrics.sse_event("done", {})
        await send({"type": "http.response.body", "body": b"data", "more_body": True})
        await send({"type": "http.response.body", "body": b"", "more_body": False})

    sent = []

    async def run():
        async def send(message):
            sent.append(message)
        await metrics.HTTPMetricsMiddleware(app)({"type": "http", "method": "POST", "path": "/api/v1/chat/completions", "headers": []}, lambda: None, send)

    asyncio.run(run())
    http = next(row for row in rows if row[0] == "_http_requests")
    assert http[1] == {"method": "POST", "route": "/api/v1/chat/completions", "status_code": "200", "mode": "streaming", "outcome": "RETURNED"}
    ttft = next(row for row in rows if row[0] == "_sse_ttft")
    assert ttft[1]["response_delivery"] == "unknown"
    assert next(row for row in rows if row[0] == "_sse_requests")[1]["outcome"] == "DONE"
    assert [row[2] for row in rows if row[0] == "_http_in_flight"] == ["inc", "dec"]
    assert len(sent) == 3


def test_sse_without_terminal_or_final_body_is_unknown(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/v1/chat/completions")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        metrics.sse_event("answer_chunk", {"content": "partial"})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})
    async def run():
        async def send(_): pass
        await metrics.HTTPMetricsMiddleware(app)({"type": "http", "method": "POST", "path": "/", "headers": []}, None, send)
    asyncio.run(run())
    assert next(row for row in rows if row[0] == "_http_requests")[1]["outcome"] == "UNKNOWN"
    assert next(row for row in rows if row[0] == "_sse_requests")[1]["outcome"] == "INCOMPLETE"


def test_http_duration_stops_at_final_body_not_background_completion(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/files/parse")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b"{}", "more_body": False})
        await asyncio.sleep(0.06)  # stand-in for Starlette BackgroundTasks run after body send
    async def run():
        async def send(_): pass
        await metrics.HTTPMetricsMiddleware(app)({"type": "http", "method": "POST", "path": "/files/parse", "headers": []}, None, send)
    asyncio.run(run())
    observed = next(row[3] for row in rows if row[0] == "_http_duration")
    assert observed < 0.06


def test_context_reaches_handler_through_starlette_base_http_middleware(monkeypatch):
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import StreamingResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    rows = install_fake_metrics(monkeypatch)
    async def handler(request):
        async def content():
            metrics.sse_event("answer_chunk", {"content": "one"})
            metrics.sse_event("done", {})
            yield "data: one\n\n"
        return StreamingResponse(content(), media_type="text/event-stream")
    async def pass_through(request, call_next):
        return await call_next(request)

    app = Starlette(routes=[Route("/chat", handler, methods=["POST"])])
    client = TestClient(BaseHTTPMiddleware(metrics.HTTPMetricsMiddleware(app), dispatch=pass_through))
    response = client.post("/chat")
    assert response.status_code == 200
    event = next(row for row in rows if row[0] == "_sse_events")
    assert event[1] == {"route": "/chat", "event_type": "answer_chunk"}
    assert next(row for row in rows if row[0] == "_sse_requests")[1]["outcome"] == "DONE"


def test_validated_buffered_candidate_delivery_is_labeled_only_when_explicit(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    async def app(scope, receive, send):
        scope["route"] = SimpleNamespace(path="/api/v1/chat/completions")
        await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/event-stream")]})
        metrics.sse_event("answer_chunk", {"content": "validated"})
        metrics.sse_event("done", {"answer_mode": "GROUNDED_GENERATION"})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    async def run():
        async def send(_): pass
        await metrics.HTTPMetricsMiddleware(app)({"type": "http", "method": "POST", "path": "/", "headers": []}, None, send)
    asyncio.run(run())
    ttft = next(row for row in rows if row[0] == "_sse_ttft")
    assert ttft[1]["response_delivery"] == "buffered_validated"


def test_sse_errors_preserve_unknown_and_truncated_outcomes(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    state = {"route": "/api/v1/chat/completions", "started": 1.0, "ttft": None, "terminal": None}
    token = metrics._request_state.set(state)
    try:
        metrics.sse_event("error", {"status": "UNKNOWN"})
        assert state["terminal"] == "UNKNOWN"
        metrics.sse_event("error", {"status": "TRUNCATED"})
        assert state["terminal"] == "TRUNCATED"
    finally:
        metrics._request_state.reset(token)
    assert len([row for row in rows if row[0] == "_sse_events"]) == 2


def test_provider_metric_labels_are_bounded_and_include_delivery_mode(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    metrics.record_provider_attempt("private-model-name", "private-status", 1.25, streaming=True)
    row = next(row for row in rows if row[0] == "_provider_attempts")
    assert row[1] == {"stage": "other", "mode": "streaming", "outcome": "UNKNOWN"}


def test_known_429_and_timeout_are_safe_metric_categories():
    from app.services.metrics import provider_outcome_category
    from requests import HTTPError, Response
    import httpx
    response = Response()
    response.status_code = 429
    assert provider_outcome_category(HTTPError(response=response), "FAILED") == "RATE_LIMITED"
    assert provider_outcome_category(TimeoutError("private"), "UNKNOWN") == "TIMEOUT_UNKNOWN"
    assert provider_outcome_category(httpx.ReadTimeout("private"), "UNKNOWN") == "TIMEOUT_UNKNOWN"


def test_task_metrics_capture_execution_not_queue_wait(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    @metrics.track_background_task("parse")
    def work():
        return {"status": "pending_confirm"}
    result = work()
    assert result["status"] == "pending_confirm"
    task = next(row for row in rows if row[0] == "_background_tasks")
    assert task[1] == {"task": "parse", "outcome": "COMPLETED"}

    @metrics.track_background_task("index")
    def fail():
        raise RuntimeError("secret")
    try:
        fail()
    except RuntimeError:
        pass
    assert any(row[0] == "_background_tasks" and row[1] == {"task": "index", "outcome": "FAILED"} for row in rows)

    Retry = type("Retry", (Exception,), {"__module__": "celery.exceptions"})
    @metrics.track_background_task("vectorize")
    def retry():
        raise Retry()
    try:
        retry()
    except Retry:
        pass
    assert any(row[0] == "_background_tasks" and row[1] == {"task": "vectorize", "outcome": "RETRYING"} for row in rows)


def test_provider_usage_counts_only_reported_integer_tokens(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    metrics.record_provider_usage("grounded_answer", {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17, "estimated_tokens": 99})
    values = {row[1]["token_type"]: row[3] for row in rows if row[0] == "_token_usage"}
    assert values == {"input": 12, "output": 5, "total": 17}


def test_agent_retry_decisions_are_counted_only_from_bounded_source_events(monkeypatch):
    rows = install_fake_metrics(monkeypatch)
    metrics.record_agent_retry_decision("SCHEDULED", 1)
    metrics.record_agent_retry_decision("untrusted", 100000)
    labels = [row[1] for row in rows if row[0] == "_agent_retry_decisions"]
    assert labels == [{"action": "SCHEDULED", "attempt": "1"}, {"action": "UNKNOWN", "attempt": "0"}]


def test_metrics_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv("RAG_PROMETHEUS_ENABLED", raising=False)
    monkeypatch.setattr(metrics, "_request_state", metrics._request_state)
    called = []

    async def app(scope, receive, send):
        called.append(True)

    async def run():
        await metrics.HTTPMetricsMiddleware(app)({"type": "http", "method": "GET", "path": "/", "headers": []}, None, None)

    asyncio.run(run())
    assert called == [True]

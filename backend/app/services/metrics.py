"""Optional, fail-open Prometheus metrics for request and provider outcomes."""
from __future__ import annotations

import contextvars
import asyncio
import os
import time
from threading import Lock
from typing import Any

try:  # Metrics are optional for local installs.
    from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
except Exception:  # pragma: no cover - exercised when optional extra is absent
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"
    CollectorRegistry = Counter = Gauge = Histogram = generate_latest = None


_registry = None
_http_requests = _http_duration = _http_in_flight = _sse_events = _sse_ttft = None
_sse_requests = _sse_duration = _provider_attempts = _provider_duration = None
_background_in_flight = _background_tasks = _background_duration = _token_usage = _agent_retry_decisions = None
_init_lock = Lock()
_request_state: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar("rag_metrics_request", default=None)
_DURATION_BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60, 120, 300, float('inf'))


def _enabled() -> bool:
    return os.getenv("RAG_PROMETHEUS_ENABLED", "false").lower() == "true"


def _initialize() -> bool:
    global _registry, _http_requests, _http_duration, _http_in_flight, _sse_events, _sse_ttft
    global _sse_requests, _sse_duration, _provider_attempts, _provider_duration
    global _background_in_flight, _background_tasks, _background_duration, _token_usage, _agent_retry_decisions
    with _init_lock:
        if _registry is not None:
            return True
        if Counter is None:
            return False
        try:
            registry = CollectorRegistry()
            _http_requests = Counter("rag_http_requests", "Completed HTTP responses", ["method", "route", "status_code", "mode", "outcome"], registry=registry)
            _http_duration = Histogram("rag_http_request_duration_seconds", "Time until the full HTTP response body is sent", ["method", "route", "mode", "outcome"], buckets=_DURATION_BUCKETS, registry=registry)
            _http_in_flight = Gauge("rag_http_requests_in_flight", "HTTP requests currently executing", registry=registry)
            _sse_events = Counter("rag_sse_events", "SSE events emitted", ["route", "event_type"], registry=registry)
            _sse_ttft = Histogram("rag_sse_time_to_first_token_seconds", "Time until first non-empty answer chunk", ["route", "response_delivery"], buckets=_DURATION_BUCKETS, registry=registry)
            _sse_requests = Counter("rag_sse_requests", "Completed SSE responses by application terminal outcome", ["route", "outcome"], registry=registry)
            _sse_duration = Histogram("rag_sse_request_duration_seconds", "Time until full SSE response body completion", ["route", "outcome"], buckets=_DURATION_BUCKETS, registry=registry)
            _provider_attempts = Counter("rag_provider_attempts", "Provider request attempts", ["stage", "mode", "outcome"], registry=registry)
            _provider_duration = Histogram("rag_provider_attempt_duration_seconds", "Provider request duration", ["stage", "mode", "outcome"], buckets=_DURATION_BUCKETS, registry=registry)
            _background_in_flight = Gauge("rag_background_tasks_in_progress", "Background task executions currently running", ["task"], registry=registry)
            _background_tasks = Counter("rag_background_tasks", "Background task execution outcomes", ["task", "outcome"], registry=registry)
            _background_duration = Histogram("rag_background_task_duration_seconds", "Background task execution duration (excludes queue wait)", ["task", "outcome"], registry=registry)
            _token_usage = Counter("rag_provider_tokens", "Provider-reported token usage only", ["stage", "token_type"], registry=registry)
            _agent_retry_decisions = Counter("rag_agent_retry_decisions", "Agent retry decisions made by the quality node", ["action", "attempt"], registry=registry)
            _registry = registry
            return True
        except Exception:
            return False


def available() -> bool:
    return _enabled() and _initialize()


def enabled() -> bool:
    return _enabled()


def render_metrics() -> tuple[bytes, str] | None:
    if not _enabled() or not _initialize():
        return None
    try:
        return generate_latest(_registry), CONTENT_TYPE_LATEST
    except Exception:
        return None


def _route(scope: dict) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) and path.startswith("/") else "unmatched"


def _limited(value: str, allowed: set[str], default: str) -> str:
    return value if value in allowed else default


def sse_event(event_type: str, payload: Any = None) -> None:
    """Record one application SSE event and request-local terminal/TTFT state."""
    if not _enabled() or not _initialize():
        return
    state = _request_state.get()
    if state is None:
        return
    try:
        name = _limited(str(event_type), {"answer_chunk", "agent_thought", "rag_result", "retrieval_status", "error", "done"}, "other")
        _sse_events.labels(route=state["route"], event_type=name).inc()
        if name == "answer_chunk" and state["ttft"] is None:
            content = payload.get("content") if isinstance(payload, dict) else None
            if isinstance(content, str) and content:
                state["ttft"] = time.monotonic() - state["started"]
        if name in ("error", "done"):
            if name == "done":
                state["terminal"] = "DONE"
                answer_mode = payload.get("answer_mode") if isinstance(payload, dict) else None
                explicit_delivery = payload.get("response_delivery") if isinstance(payload, dict) else None
                state["response_delivery"] = "buffered_validated" if answer_mode == "GROUNDED_GENERATION" else _limited(str(explicit_delivery), {"streaming", "buffered_validated"}, "unknown")
            else:
                status = payload.get("status") if isinstance(payload, dict) else None
                state["terminal"] = _limited(str(status), {"FAILED", "UNKNOWN", "TRUNCATED", "PRE_CONNECT_FAILED", "CANCELLED"}, "FAILED")
    except Exception:
        pass


def record_provider_attempt(stage: str, outcome: str, elapsed: float, *, streaming: bool = False, error: BaseException | None = None) -> None:
    if not _enabled() or not _initialize():
        return
    try:
        stage = _limited(str(stage), {"answer_stream", "answer", "rewrite", "intent", "agent", "agent_chat", "quality", "retrieval", "embedding", "rerank", "rerank_chat", "rerank_connection_test", "grounded_answer", "vlm"}, "other")
        outcome = provider_outcome_category(error, outcome) if error is not None else _limited(str(outcome), {"RETURNED", "FAILED", "UNKNOWN", "PRE_CONNECT_FAILED", "TRUNCATED", "CANCELLED", "RATE_LIMITED", "TIMEOUT_UNKNOWN"}, "UNKNOWN")
        mode = "streaming" if streaming else "buffered"
        _provider_attempts.labels(stage=stage, mode=mode, outcome=outcome).inc()
        _provider_duration.labels(stage=stage, mode=mode, outcome=outcome).observe(max(0.0, elapsed))
    except Exception:
        pass


def provider_outcome_category(exc: BaseException, status: str) -> str:
    """Map known transport facts to bounded categories without changing business status."""
    if status == "CANCELLED":
        return "CANCELLED"
    code = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    code = code or getattr(response, "status_code", None)
    if code == 429:
        return "RATE_LIMITED"
    if isinstance(exc, (TimeoutError,)) or "Timeout" in type(exc).__name__:
        return "TIMEOUT_UNKNOWN"
    return _limited(status, {"RETURNED", "FAILED", "UNKNOWN", "PRE_CONNECT_FAILED", "TRUNCATED", "CANCELLED", "RATE_LIMITED", "TIMEOUT_UNKNOWN"}, "UNKNOWN")


def record_provider_usage(stage: str, usage: Any) -> None:
    """Count provider-reported token numbers; missing/estimated usage is ignored."""
    if not _enabled() or not _initialize():
        return
    try:
        if hasattr(usage, "model_dump"):
            usage = usage.model_dump()
        if not isinstance(usage, dict):
            return
        stage = _limited(str(stage), {"generation", "rewrite", "grounded_answer", "answer_stream", "answer", "intent", "agent", "agent_chat", "quality", "embedding", "rerank", "rerank_chat", "rerank_connection_test", "vlm"}, "other")
        aliases = {"prompt_tokens": "input", "input_tokens": "input", "completion_tokens": "output", "output_tokens": "output", "total_tokens": "total"}
        for key, token_type in aliases.items():
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                _token_usage.labels(stage=stage, token_type=token_type).inc(value)
    except Exception:
        pass


def record_agent_retry_decision(action: str, attempt: Any) -> None:
    if not _enabled() or not _initialize():
        return
    try:
        action = _limited(str(action), {"SCHEDULED", "EXHAUSTED"}, "UNKNOWN")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or not 0 <= attempt <= 3:
            attempt = 0
        _agent_retry_decisions.labels(action=action, attempt=str(attempt)).inc()
    except Exception:
        pass


def track_background_task(task: str):
    """Measure execution after the background function starts; queue wait is not measured."""
    task = _limited(str(task), {"parse", "index", "vectorize"}, "other")

    def decorate(fn):
        import functools
        import inspect

        def begin():
            if not _enabled() or not _initialize():
                return None
            try:
                _background_in_flight.labels(task=task).inc()
                return time.monotonic()
            except Exception:
                return None

        def finish(started, outcome):
            if started is None:
                return
            try:
                _background_in_flight.labels(task=task).dec()
                _background_tasks.labels(task=task, outcome=outcome).inc()
                _background_duration.labels(task=task, outcome=outcome).observe(max(0.0, time.monotonic() - started))
            except Exception:
                pass

        @functools.wraps(fn)
        def sync_call(*args, **kwargs):
            started = begin()
            try:
                result = fn(*args, **kwargs)
                outcome = "FAILED" if isinstance(result, dict) and (result.get("error") or result.get("status") == "failed") else "COMPLETED"
                finish(started, outcome)
                return result
            except BaseException as exc:
                finish(started, _task_exception_outcome(exc))
                raise

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_call(*args, **kwargs):
                started = begin()
                try:
                    result = await fn(*args, **kwargs)
                    outcome = "FAILED" if isinstance(result, dict) and (result.get("error") or result.get("status") == "failed") else "COMPLETED"
                    finish(started, outcome)
                    return result
                except BaseException as exc:
                    finish(started, _task_exception_outcome(exc))
                    raise
            return async_call
        return sync_call
    return decorate


def _task_exception_outcome(exc: BaseException) -> str:
    if type(exc).__name__ == "Retry" and type(exc).__module__.startswith("celery"):
        return "RETRYING"
    return "FAILED"


class HTTPMetricsMiddleware:
    """Pure ASGI middleware; observes actual body completion, including streams."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("path") == "/metrics" or not _enabled() or not _initialize():
            await self.app(scope, receive, send)
            return
        try:
            _http_in_flight.inc()
        except Exception:
            pass
        route = _route(scope)
        mode = "buffered"
        state = {"route": route, "started": time.monotonic(), "terminal": None, "ttft": None, "response_delivery": "unknown", "status": 500, "mode": mode}
        token = _request_state.set(state)
        completed = False
        finalized = False

        def finalize(http_outcome: str, *, sse_outcome: str | None = None) -> None:
            nonlocal finalized
            if finalized:
                return
            finalized = True
            finished_at = time.monotonic()
            route = state["route"]
            try:
                if state["ttft"] is not None:
                    _sse_ttft.labels(route=route, response_delivery=state["response_delivery"]).observe(state["ttft"])
                if state["mode"] == "streaming":
                    sse_outcome = sse_outcome or state["terminal"] or "UNKNOWN"
                    sse_outcome = _limited(sse_outcome, {"DONE", "FAILED", "UNKNOWN", "TRUNCATED", "PRE_CONNECT_FAILED", "CANCELLED", "INCOMPLETE"}, "UNKNOWN")
                    _sse_requests.labels(route=route, outcome=sse_outcome).inc()
                    _sse_duration.labels(route=route, outcome=sse_outcome).observe(max(0.0, finished_at - state["started"]))
                method = _limited(str(scope.get("method", "OTHER")).upper(), {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}, "OTHER")
                status_code = str(state["status"]) if 100 <= state["status"] < 600 else "500"
                _http_requests.labels(method=method, route=route, status_code=status_code, mode=state["mode"], outcome=http_outcome).inc()
                _http_duration.labels(method=method, route=route, mode=state["mode"], outcome=http_outcome).observe(max(0.0, finished_at - state["started"]))
            except Exception:
                pass
            try:
                _http_in_flight.dec()
            except Exception:
                pass

        async def send_tracked(message):
            nonlocal completed
            if message["type"] == "http.response.start":
                state["route"] = _route(scope)
                state["status"] = int(message.get("status", 500))
                headers = {k.lower(): v.lower() for k, v in message.get("headers", [])}
                state["mode"] = "streaming" if b"text/event-stream" in headers.get(b"content-type", b"") else "buffered"
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                completed = True
                finalize("RETURNED" if state["status"] < 500 else "FAILED")

        try:
            await self.app(scope, receive, send_tracked)
            if not completed:
                finalize("UNKNOWN", sse_outcome="INCOMPLETE")
        except BaseException as exc:
            cancelled = isinstance(exc, (asyncio.CancelledError, GeneratorExit, KeyboardInterrupt, SystemExit))
            state["terminal"] = "CANCELLED" if cancelled else state["terminal"]
            finalize("CANCELLED" if cancelled else "FAILED", sse_outcome="CANCELLED" if cancelled else "INCOMPLETE")
            raise
        finally:
            if not finalized:
                state["route"] = _route(scope)
                finalize("UNKNOWN", sse_outcome="INCOMPLETE")
            _request_state.reset(token)

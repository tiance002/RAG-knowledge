"""Content-free timings for existing synchronous boundaries; no scheduling changes."""
import functools
import time


def timed_stage(stage_name):
    def decorate(fn):
        @functools.wraps(fn)
        def wrapped(*args, **kwargs):
            started = time.perf_counter()
            outcome = 'RETURNED'
            try:
                return fn(*args, **kwargs)
            except BaseException:
                outcome = 'FAILED'
                raise
            finally:
                # Telemetry must not replace the original result or exception.
                elapsed = (time.perf_counter() - started) * 1000
                try:
                    from app.services.observability import event
                    event('performance.stage', stage_name=stage_name,
                          duration_ms=elapsed, execution_status=outcome,
                          timing_scope='synchronous_function', includes_children=True)
                except Exception:
                    pass
        return wrapped
    return decorate


"""Opt-in, BASIC-only ASGI lifecycle records for performance measurements."""
import asyncio
import functools
import os
import re
import time

_FIELDS = (("run_id", b"x-rag-run-id"), ("attempt_id", b"x-rag-attempt-id"),
           ("qid", b"x-rag-qid"), ("dataset_version", b"x-rag-dataset-version"))
_VALID = re.compile(r"[A-Za-z0-9_.:/-]{1,160}\Z")


def performance_identity(scope):
    if os.getenv("RAG_PERFORMANCE_IDENTITY_HEADERS", "false").lower() != "true":
        return {}
    headers = scope.get("headers", [])
    result = {}
    for field, name in _FIELDS:
        values = [value for key, value in headers if key.lower() == name]
        if len(values) != 1:
            continue
        try:
            value = values[0].decode("ascii")
        except (UnicodeDecodeError, AttributeError):
            continue
        if _VALID.fullmatch(value):
            result[field] = value
    return result


class RequestLifecycleMiddleware:
    """Observe ASGI app return, not network teardown or downstream receipt."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (scope.get("type") != "http" or scope.get("method") != "POST"
                or scope.get("path") != "/api/v1/chat/completions"
                or os.getenv("RAG_OBSERVABILITY_ENABLED", "false").lower() != "true"):
            return await self.app(scope, receive, send)
        from app.services.trace_schema import capture_identity
        if any(capture_identity().values()):
            return await self.app(scope, receive, send)
        identity = performance_identity(scope)
        if not identity.get("run_id") or not identity.get("attempt_id"):
            return await self.app(scope, receive, send)
        started = time.perf_counter()
        final_body = None
        status_code = None
        outcome = "RETURNED"

        async def tracked(message):
            nonlocal final_body, status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status")
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                final_body = time.perf_counter()

        try:
            await self.app(scope, receive, tracked)
        except BaseException as exc:
            outcome = "CANCELLED" if isinstance(exc, (asyncio.CancelledError, GeneratorExit)) else "FAILED"
            raise
        finally:
            ended = time.perf_counter()
            try:
                from app.services.observability import _record
                _record({**identity, "event": "stage", "stage": "qa.asgi_lifecycle", "time": time.time(),
                         "metadata": {"asgi_app_ms": (ended - started) * 1000,
                                      "final_body_ms": (final_body - started) * 1000 if final_body is not None else None,
                                      "post_body_app_cleanup_ms": (ended - final_body) * 1000 if final_body is not None else None,
                                      "asgi_app_status": outcome, "http_status": status_code,
                                      "timing_scope": "asgi_app_return_excluding_record_and_network_teardown"}})
            except Exception:
                pass

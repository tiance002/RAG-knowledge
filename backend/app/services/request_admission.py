"""Per-process, per-event-loop admission for chat work (not provider slots)."""
import asyncio
import os
import math
import logging
import time
import weakref
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

logger = logging.getLogger(__name__)


def _number(name, default, cast, minimum):
    try:
        value = cast(os.getenv(name, str(default)))
        return value if value >= minimum and math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


class AdmissionLease:
    def __init__(self, gate):
        self.gate = gate
        self.references = 1

    def hold_until(self, task):
        if task.done():
            return
        self.references += 1
        task.add_done_callback(lambda _: self.release())

    def release(self):
        if self.references <= 0:
            return
        self.references -= 1
        if self.references == 0:
            self.gate.slots.release()


class AdmissionGate:
    def __init__(self, active, queued, timeout):
        self.slots = asyncio.Semaphore(active)
        self.queued = queued
        self.waiters = 0
        self.timeout = timeout

    async def acquire(self):
        # No await occurs between capacity check and increment: atomic on this loop.
        if self.slots.locked() and self.waiters >= self.queued:
            raise _busy()
        if not self.slots.locked():
            await self.slots.acquire()
            return AdmissionLease(self)
        self.waiters += 1
        try:
            await asyncio.wait_for(self.slots.acquire(), timeout=self.timeout)
            return AdmissionLease(self)
        except asyncio.TimeoutError as exc:
            raise _busy() from exc
        finally:
            self.waiters -= 1


def _busy():
    return HTTPException(503, detail={"code": "CHAT_BUSY", "message": "当前请求较多，请稍后重试。"}, headers={"Retry-After": "5"})


_gates = weakref.WeakKeyDictionary()


def get_gate():
    loop = asyncio.get_running_loop()
    gate = _gates.get(loop)
    if gate is None:
        gate = AdmissionGate(_number('RAG_CHAT_MAX_ACTIVE', 2, int, 1),
                             _number('RAG_CHAT_MAX_QUEUED', 4, int, 0),
                             _number('RAG_CHAT_QUEUE_TIMEOUT', 5.0, float, 0.01))
        _gates[loop] = gate
    return gate


async def acquire_request(request=None):
    started = time.monotonic()
    task = asyncio.create_task(get_gate().acquire())
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=0.1)
            if not task.done() and request is not None and await request.is_disconnected():
                raise HTTPException(499, detail={"code": "CLIENT_DISCONNECTED", "message": "客户端已断开"})
        lease = task.result()
        lease.wait_seconds = time.monotonic() - started
        return lease
    except BaseException:
        task.cancel()
        try:
            lease = await task
        except BaseException:
            pass
        else:
            lease.release()
        raise


class AdmittedStreamingResponse(StreamingResponse):
    def __init__(self, *args, lease, on_unstarted=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lease = lease
        self.on_unstarted = on_unstarted

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            async def close_iterator():
                close = getattr(self.body_iterator, "aclose", None)
                if close is None:
                    return
                while True:
                    # A different task may still own an async generator. Do not
                    # release admission merely because aclose cannot run yet.
                    if getattr(self.body_iterator, "ag_running", False):
                        await asyncio.sleep(0.05)
                        continue
                    try:
                        await close()
                        return
                    except RuntimeError as exc:
                        if "already running" in str(exc):
                            await asyncio.sleep(0.05)
                            continue
                        logger.exception("Unable to confirm stream closure")
                    except Exception:
                        logger.exception("Unable to confirm stream closure")
                    # A failed close is not evidence that provider work ended.
                    # Keep capacity quarantined rather than over-admit.
                    await asyncio.Event().wait()

            task = asyncio.create_task(close_iterator())
            if self.lease:
                self.lease.hold_until(task)
            try:
                # A disconnected ASGI client must not wait indefinitely for a
                # cancellation-suppressing provider; the task owns the permit.
                await asyncio.wait({task}, timeout=1.0)
            finally:
                try:
                    if self.on_unstarted:
                        self.on_unstarted()
                finally:
                    if self.lease:
                        self.lease.release()

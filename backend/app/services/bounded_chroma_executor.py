"""Opt-in bounded executor for DB-free Chroma reads in one API event loop."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import contextvars
import math
import os
import threading
import time

_manager = None
_read_worker = contextvars.ContextVar("chroma_read_worker", default=False)
_worker_manager = contextvars.ContextVar("chroma_worker_manager", default=None)


class ChromaExecutorCapabilityError(RuntimeError):
    """The explicit candidate cannot safely run under this runtime/configuration."""
    capability_status = "BLOCKED"


def chroma_executor_enabled():
    return os.getenv("RAG_CHROMA_EXECUTOR_ENABLED", "false").strip().lower() == "true"


def in_chroma_read_worker():
    return _read_worker.get()


def chroma_http_timeout_seconds():
    try:
        value = float(os.getenv("RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS", "30"))
    except ValueError as exc:
        raise ChromaExecutorCapabilityError("INVALID_CHROMA_QUERY_HTTP_TIMEOUT") from exc
    if not math.isfinite(value) or value <= 0:
        raise ChromaExecutorCapabilityError("INVALID_CHROMA_QUERY_HTTP_TIMEOUT")
    return value


def _workers():
    try:
        value = int(os.getenv("RAG_CHROMA_QUERY_WORKERS", "2"))
    except ValueError as exc:
        raise ChromaExecutorCapabilityError("INVALID_CHROMA_QUERY_WORKERS") from exc
    if not 1 <= value <= 16:
        raise ChromaExecutorCapabilityError("INVALID_CHROMA_QUERY_WORKERS")
    return value


def _event(name, **values):
    try:
        if name == "chroma.executor_wait":
            print(f"CHROMA_TIMING stage=admission wait_ms={values['duration_ms']:.3f} max_workers={values['max_workers']}", flush=True)
        from app.services.observability import event
        event(name, **values)
    except Exception:
        pass


def _consume_exception(future):
    if not future.cancelled():
        future.exception()


class BoundedChromaExecutor:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.pid = os.getpid()
        self.max_workers = _workers()
        self.pool = ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="rag-chroma-read")
        self.slots = asyncio.Semaphore(self.max_workers)
        self.jobs = set()
        self.closed = False
        self._cleanup_lock = threading.Lock()
        self._cleanup_callbacks = {}

    def register_cleanup(self, owner, callback):
        with self._cleanup_lock:
            self._cleanup_callbacks[id(owner)] = callback

    def _cleanup_owned(self):
        with self._cleanup_lock:
            callbacks, self._cleanup_callbacks = list(self._cleanup_callbacks.values()), {}
        failed = False
        for callback in callbacks:
            try:
                callback()
            except Exception:
                failed = True
        if failed:
            raise ChromaExecutorCapabilityError("CHROMA_RESOURCE_CLEANUP_FAILED")

    def _completed(self, future):
        self.jobs.discard(future)
        self.slots.release()

    async def run(self, operation, *args, **kwargs):
        if self.closed or self.pid != os.getpid() or self.loop is not asyncio.get_running_loop():
            raise ChromaExecutorCapabilityError("CHROMA_EXECUTOR_UNAVAILABLE")
        started = time.perf_counter()
        await self.slots.acquire()
        if self.closed:
            self.slots.release()
            raise ChromaExecutorCapabilityError("CHROMA_EXECUTOR_CLOSED")
        _event("chroma.executor_wait", duration_ms=(time.perf_counter() - started) * 1000,
               max_workers=self.max_workers)
        context = contextvars.copy_context()
        def execute():
            token = _read_worker.set(True)
            manager_token = _worker_manager.set(self)
            try:
                return operation(*args, **kwargs)
            finally:
                _worker_manager.reset(manager_token)
                _read_worker.reset(token)
        try:
            future = self.pool.submit(context.run, execute)
        except BaseException:
            self.slots.release()
            raise
        self.jobs.add(future)
        # The concurrent future, not the cancelled caller, owns capacity.
        future.add_done_callback(lambda done: self.loop.call_soon_threadsafe(self._completed, done))
        result = asyncio.wrap_future(future, loop=self.loop)
        result.add_done_callback(_consume_exception)
        return await asyncio.shield(result)

    async def close(self):
        self.closed = True
        # shutdown(wait=True) joins actual workers; move the join off the API loop.
        def drain():
            self.pool.shutdown(wait=True, cancel_futures=False)
            self._cleanup_owned()
        join = asyncio.create_task(asyncio.to_thread(drain))
        try:
            await asyncio.shield(join)
        except asyncio.CancelledError:
            await asyncio.shield(join)
            raise


@asynccontextmanager
async def managed_chroma_executor():
    global _manager
    if not chroma_executor_enabled():
        yield None
        return
    manager = BoundedChromaExecutor()
    previous, _manager = _manager, manager
    try:
        yield manager
    finally:
        if _manager is manager:
            _manager = previous
        await manager.close()


async def run_chroma_query(operation, *args, **kwargs):
    manager = _manager
    if manager is None:
        raise ChromaExecutorCapabilityError("CHROMA_EXECUTOR_NOT_MANAGED")
    return await manager.run(operation, *args, **kwargs)


def register_chroma_cleanup(owner, callback):
    manager = _worker_manager.get()
    if manager is None or not in_chroma_read_worker():
        raise ChromaExecutorCapabilityError("CHROMA_CLEANUP_OWNER_UNAVAILABLE")
    manager.register_cleanup(owner, callback)

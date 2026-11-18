"""Lifespan-owned, bounded SQL read workers. Cancelled callers do not free capacity."""
import asyncio
import contextvars
import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

_manager = None


class BoundedLexicalExecutor:
    def __init__(self, workers=2):
        self.loop = asyncio.get_running_loop()
        self.pid = os.getpid()
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rag-bm25")
        self.slots = asyncio.Semaphore(workers)
        self.closed = False
        self.read_engines = {}

    def reader_engine(self, source):
        # Never compete with caller-held request connections. Each worker owns
        # a short-lived SQLite read connection, bounded by the worker slots.
        from sqlalchemy import create_engine, event
        from sqlalchemy.pool import NullPool
        if source not in self.read_engines:
            engine = create_engine(source.url, poolclass=NullPool,
                connect_args={"check_same_thread": False, "timeout": 30})
            @event.listens_for(engine, 'connect')
            def read_only(connection, _):
                cursor = connection.cursor()
                try:
                    cursor.execute('PRAGMA query_only=ON')
                finally:
                    cursor.close()
            self.read_engines[source] = engine
        return self.read_engines[source]

    async def run(self, operation):
        if self.closed or self.loop is not asyncio.get_running_loop() or self.pid != os.getpid():
            raise RuntimeError("LEXICAL_EXECUTOR_UNAVAILABLE")
        await self.slots.acquire()
        if self.closed:
            self.slots.release()
            raise RuntimeError("LEXICAL_EXECUTOR_CLOSED")
        try:
            future = self.pool.submit(contextvars.copy_context().run, operation)
        except BaseException:
            self.slots.release()
            raise
        future.add_done_callback(lambda _: self.loop.call_soon_threadsafe(self.slots.release))
        result = asyncio.wrap_future(future, loop=self.loop)
        result.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        return await asyncio.shield(result)

    async def close(self):
        self.closed = True
        def drain():
            self.pool.shutdown(wait=True, cancel_futures=False)
            for engine in self.read_engines.values():
                engine.dispose()
            self.read_engines.clear()
        join = asyncio.create_task(asyncio.to_thread(drain))
        try:
            await asyncio.shield(join)
        except asyncio.CancelledError:
            await asyncio.shield(join)
            raise


def lexical_executor_available():
    return (_manager is not None and not _manager.closed
            and _manager.pid == os.getpid() and _manager.loop is asyncio.get_running_loop())


def lexical_reader_engine(source):
    if not lexical_executor_available():
        raise RuntimeError("LEXICAL_EXECUTOR_NOT_MANAGED")
    return _manager.reader_engine(source)


async def run_lexical_read(operation):
    if not lexical_executor_available():
        raise RuntimeError("LEXICAL_EXECUTOR_NOT_MANAGED")
    return await _manager.run(operation)


@asynccontextmanager
async def managed_lexical_executor():
    global _manager
    previous, manager = _manager, BoundedLexicalExecutor()
    _manager = manager
    try:
        yield manager
    finally:
        if _manager is manager:
            _manager = previous
        await manager.close()

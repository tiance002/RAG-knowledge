"""Real blocking worker jobs with synthetic inference; no model downloads/GPU needed."""
import asyncio
from contextvars import ContextVar
import threading
import time
from types import SimpleNamespace

import pytest

from app.services import rerank


MODEL = SimpleNamespace(base_url="synthetic-local-model", context_length=3072)


async def wait_event(event):
    async with asyncio.timeout(1):
        while not event.is_set():
            await asyncio.sleep(.001)


def test_blocking_local_predict_runs_off_loop_with_heartbeat_context_and_exact_arguments(monkeypatch):
    started, release = threading.Event(), threading.Event()
    observed = []
    context = ContextVar("local_rerank_test", default=None)
    expected = [{"index": 1, "score": .9, "text": "second"}, {"index": 0, "score": .3, "text": "first"}]

    class Loader:
        def predict(self, **kwargs):
            observed.append((threading.get_ident(), context.get(), kwargs))
            started.set()
            assert release.wait(2), "test must release blocked inference"
            return expected

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)

    async def run():
        token = context.set("copied-to-worker")
        task = asyncio.create_task(rerank.RerankService(None).arerank("q", ["first", "second"], MODEL))
        try:
            begin = time.monotonic()
            await wait_event(started)
            beats = []
            for _ in range(4):
                await asyncio.sleep(.005)
                beats.append(time.monotonic())
            assert len(beats) == 4 and time.monotonic() - begin < .5
            assert not task.done()
            assert observed == [(observed[0][0], "copied-to-worker", {
                "query": "q", "passages": ["first", "second"], "model_path": MODEL.base_url, "max_len": MODEL.context_length})]
            assert observed[0][0] != threading.get_ident()
            release.set()
            assert await asyncio.wait_for(task, 1) is expected
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            context.reset(token)
    asyncio.run(run())


@pytest.mark.parametrize("worker_fails", [False, True])
def test_repeated_cancellation_holds_capacity_until_thread_finishes(monkeypatch, worker_fails):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Loader:
        def predict(self, **kwargs):
            calls.append(kwargs["query"])
            if kwargs["query"] == "first":
                started.set()
                assert release.wait(2)
                if worker_fails:
                    raise ValueError("synthetic inference failure after cancellation")
            return [{"index": 0, "score": .8, "text": kwargs["passages"][0]}]

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)

    async def run():
        service = rerank.RerankService(None)
        first = asyncio.create_task(service.arerank("first", ["one"], MODEL))
        second = None
        try:
            await wait_event(started)
            for _ in range(2):
                first.cancel()
                await asyncio.sleep(0)
                assert not first.done()
            second = asyncio.create_task(service.arerank("second", ["two"], MODEL))
            await asyncio.sleep(.03)
            assert calls == ["first"] and not second.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(first, 1)
            assert await asyncio.wait_for(second, 1) == [{"index": 0, "score": .8, "text": "two"}]
            assert calls == ["first", "second"]
        finally:
            release.set()
            await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
    asyncio.run(run())


def test_cancelled_waiter_submits_no_local_prediction(monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Loader:
        def predict(self, **kwargs):
            calls.append(kwargs["query"])
            started.set()
            assert release.wait(2)
            return []

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)

    async def run():
        service = rerank.RerankService(None)
        first = asyncio.create_task(service.arerank("first", ["one"], MODEL))
        queued = None
        try:
            await wait_event(started)
            queued = asyncio.create_task(service.arerank("queued", ["two"], MODEL))
            await asyncio.sleep(.01)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            assert calls == ["first"]
            release.set()
            assert await asyncio.wait_for(first, 1) == []
            assert calls == ["first"]
        finally:
            release.set()
            await asyncio.gather(*[task for task in (first, queued) if task is not None], return_exceptions=True)
    asyncio.run(run())


def test_sync_and_async_local_calls_serialize_mutable_loader_state(monkeypatch):
    started, release, sync_entered = threading.Event(), threading.Event(), threading.Event()
    calls, active, maximum = [], 0, 0
    guard = threading.Lock()

    class Loader:
        def predict(self, **kwargs):
            nonlocal active, maximum
            with guard:
                calls.append(kwargs["query"])
                active += 1
                maximum = max(maximum, active)
            try:
                if kwargs["query"] == "async":
                    started.set()
                    assert release.wait(2)
                else:
                    sync_entered.set()
                return [{"index": 0, "score": .8, "text": kwargs["passages"][0]}]
            finally:
                with guard:
                    active -= 1

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)

    async def run():
        service = rerank.RerankService(None)
        asynchronous = asyncio.create_task(service.arerank("async", ["one"], MODEL))
        synchronous = None
        try:
            await wait_event(started)
            synchronous = asyncio.create_task(asyncio.to_thread(service.rerank, "sync", ["two"], MODEL))
            await asyncio.sleep(.03)
            assert not sync_entered.is_set() and calls == ["async"]
            release.set()
            results = await asyncio.wait_for(asyncio.gather(asynchronous, synchronous), 1)
            assert [result[0]["text"] for result in results] == ["one", "two"]
            assert maximum == 1 and calls == ["async", "sync"]
        finally:
            release.set()
            await asyncio.gather(*[task for task in (asynchronous, synchronous) if task is not None], return_exceptions=True)
    asyncio.run(run())


def test_known_local_failure_keeps_existing_empty_fallback_and_releases_admission(monkeypatch):
    class Loader:
        def predict(self, **kwargs):
            if kwargs["query"] == "fail":
                raise ValueError("synthetic local model load failure")
            return [{"index": 0, "score": .6, "text": kwargs["passages"][0]}]

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)

    async def run():
        service = rerank.RerankService(None)
        assert await service.arerank("fail", ["one"], MODEL) == []
        assert await asyncio.wait_for(service.arerank("ok", ["two"], MODEL), 1) == [{"index": 0, "score": .6, "text": "two"}]
    asyncio.run(run())

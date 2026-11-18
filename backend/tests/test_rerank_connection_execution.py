"""Real settings HTTP route + isolated SQLite + blocking synthetic loader/provider."""
import asyncio
import threading
import time
from types import SimpleNamespace

from fastapi import FastAPI
import httpx
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine

from app.api import settings as settings_api
from app.models import CustomModel, ModelType
from app.services import llm, rerank


@pytest.fixture
def api():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            CustomModel(id="local", name="local", model_type=ModelType.RERANK,
                        base_url="synthetic-local", model_name="test", context_length=2048),
            CustomModel(id="remote", name="remote", model_type=ModelType.RERANK,
                        base_url="https://synthetic.invalid/v1", model_name="test", api_key="synthetic-key"),
        ])
        session.commit()
    app = FastAPI()
    app.include_router(settings_api.router)

    async def memory_session():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[settings_api.get_session] = memory_session
    yield app
    engine.dispose()


async def wait_event(event):
    async with asyncio.timeout(1):
        while not event.is_set():
            await asyncio.sleep(.001)


@pytest.mark.parametrize("connection_first", [False, True])
def test_settings_route_and_live_local_inference_are_serial_and_responsive(api, monkeypatch, connection_first):
    started, release = threading.Event(), threading.Event()
    calls = []
    expected_first = "load" if connection_first else "live"

    class Loader:
        def load_model(self, path, max_len):
            calls.append(("load", threading.get_ident(), path, max_len))
            if connection_first:
                started.set()
                assert release.wait(2)

        def predict(self, query, passages, model_path, max_len):
            calls.append((query, threading.get_ident(), model_path, max_len))
            if not connection_first and query == "live":
                started.set()
                assert release.wait(2)
            return [{"index": 0, "score": .8, "text": passages[0]}]

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)
    model = SimpleNamespace(base_url="live-local-model", context_length=3072)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            connection = lambda: asyncio.create_task(client.post("/settings/models/local/test"))
            inference = lambda: asyncio.create_task(rerank.RerankService(None).arerank("live", ["evidence"], model))
            first = connection() if connection_first else inference()
            second = None
            try:
                begin = time.monotonic()
                await wait_event(started)
                second = inference() if connection_first else connection()
                for _ in range(4):
                    await asyncio.sleep(.005)
                assert time.monotonic() - begin < .5
                assert len(calls) == 1 and calls[0][0] == expected_first
                assert calls[0][1] != threading.get_ident()
                assert not first.done() and not second.done()
                release.set()
                a, b = await asyncio.wait_for(asyncio.gather(first, second), 1)
                response, result = (a, b) if connection_first else (b, a)
                assert response.status_code == 200 and response.json()["message"] == "连接测试成功"
                assert result == [{"index": 0, "score": .8, "text": "evidence"}]
                assert [row[0] for row in calls] == (["load", "测试", "live"] if connection_first else ["live", "load", "测试"])
                load = next(row for row in calls if row[0] == "load")
                assert load[2:] == ("synthetic-local", 2048)
            finally:
                release.set()
                await asyncio.gather(*[task for task in (first, second) if task is not None], return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("remote", [False, True])
def test_cancelled_settings_request_drains_connection_work_before_releasing_shared_admission(api, monkeypatch, remote):
    started, release = threading.Event(), threading.Event()
    calls = []

    class Loader:
        def load_model(self, path, max_len):
            calls.append("load")
            started.set()
            assert release.wait(2)

        def predict(self, query, passages, model_path, max_len):
            calls.append(query)
            return [{"index": 0, "score": .8, "text": passages[0]}]

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)
    if remote:
        def factory(**kwargs):
            def create(**kwargs):
                calls.append("remote")
                started.set()
                assert release.wait(2)
                return SimpleNamespace(_request_id="offline-provider-id")
            return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                                   close=lambda: calls.append("close"))
        monkeypatch.setattr(rerank, "OpenAI", factory)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            request = asyncio.create_task(client.post("/settings/models/" + ("remote" if remote else "local") + "/test"))
            inference = None
            try:
                await wait_event(started)
                for _ in range(2):
                    request.cancel()
                    await asyncio.sleep(0)
                    assert not request.done()
                if remote:
                    inference = asyncio.create_task(rerank.async_test_rerank_connection({
                        "base_url": "https://synthetic.invalid/v1", "model_name": "test", "api_key": "synthetic-key"}))
                else:
                    inference = asyncio.create_task(rerank.RerankService(None).arerank(
                        "live", ["evidence"], SimpleNamespace(base_url="local-live", context_length=1024)))
                await asyncio.sleep(.03)
                assert calls == (["remote"] if remote else ["load"]) and not inference.done()
                release.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(request, 1)
                expected = {"success": True, "message": "API 连接成功"} if remote else [{"index": 0, "score": .8, "text": "evidence"}]
                assert await asyncio.wait_for(inference, 1) == expected
                assert calls == (["remote", "close", "remote", "close"] if remote else ["load", "测试", "live"])
            finally:
                release.set()
                await asyncio.gather(*[task for task in (request, inference) if task is not None], return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("failure", [False, True])
def test_remote_settings_test_is_off_loop_closes_sdk_and_keeps_error_contract(api, monkeypatch, failure):
    started, release = threading.Event(), threading.Event()
    calls, closed, payloads, results = [], [], [], []
    original = rerank.async_test_rerank_connection

    async def capture(model):
        result = await original(model)
        results.append(result)
        return result

    monkeypatch.setattr(rerank, "async_test_rerank_connection", capture)
    monkeypatch.setattr(llm, "event", lambda *args, **kwargs: None)

    def factory(**kwargs):
        assert kwargs == {"base_url": "https://synthetic.invalid/v1", "api_key": "synthetic-key", "max_retries": 0}

        def create(**kwargs):
            calls.append(threading.get_ident())
            payloads.append(kwargs)
            started.set()
            assert release.wait(2)
            if failure:
                raise TimeoutError("synthetic-private-body")
            return SimpleNamespace(_request_id="offline-provider-id")

        def close():
            closed.append("sdk")
            raise OSError("synthetic close error must not replace outcome")

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), close=close)

    monkeypatch.setattr(rerank, "OpenAI", factory)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            task = asyncio.create_task(client.post("/settings/models/remote/test"))
            try:
                begin = time.monotonic()
                await wait_event(started)
                for _ in range(4):
                    await asyncio.sleep(.005)
                assert time.monotonic() - begin < .5 and not task.done()
                assert calls == [calls[0]] and calls[0] != threading.get_ident()
                release.set()
                response = await asyncio.wait_for(task, 1)
                assert response.status_code == (400 if failure else 200)
                assert "synthetic-private-body" not in response.text
                if failure:
                    assert results[0]["status"] == "UNKNOWN" and results[0]["local_request_id"]
                    assert response.json()["detail"]["message"] == "连接测试失败: API 连接失败"
                else:
                    assert results[0] == {"success": True, "message": "API 连接成功"}
                assert closed == ["sdk"] and len(payloads) == 1
                assert payloads[0] == {"model": "test", "messages": [{"role": "user", "content": "test"}], "max_tokens": 10}
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


def test_sync_connection_helper_retains_local_success_failure_and_invalid_path_contract(monkeypatch):
    calls = []

    class Loader:
        def load_model(self, path, max_len):
            calls.append((path, max_len))
            if path == "bad":
                raise ValueError("synthetic missing local model")

        def predict(self, query, passages, model_path, max_len):
            assert query == "测试" and len(passages) == 2
            return [{"index": 0, "score": .7, "text": passages[0]}]

    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)
    assert rerank.test_rerank_connection({}) == {"success": False, "message": "模型路径/地址不能为空"}
    assert rerank.test_rerank_connection({"base_url": "ok", "context_length": 8192})["success"] is True
    failed = rerank.test_rerank_connection({"base_url": "bad"})
    assert failed == {"success": False, "message": "本地模型加载失败: synthetic missing local model", "error": "synthetic missing local model"}
    assert calls == [("ok", 8192), ("bad", 4096)]


def test_stalled_remote_connection_route_does_not_reserve_local_inference_capacity(api, monkeypatch):
    started, release = threading.Event(), threading.Event()
    calls, closed = [], []

    def factory(**kwargs):
        assert kwargs["max_retries"] == 0

        def create(**kwargs):
            calls.append(("remote", threading.get_ident()))
            started.set()
            assert release.wait(2)
            return SimpleNamespace(_request_id="offline-provider-id")

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
                               close=lambda: closed.append("sdk"))

    class Loader:
        def predict(self, **kwargs):
            calls.append(("local", threading.get_ident()))
            return [{"index": 0, "score": .8, "text": kwargs["passages"][0]}]

    monkeypatch.setattr(rerank, "OpenAI", factory)
    monkeypatch.setattr(rerank, "RerankModelLoader", Loader)

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            remote = asyncio.create_task(client.post("/settings/models/remote/test"))
            try:
                await wait_event(started)
                result = await asyncio.wait_for(rerank.RerankService(None).arerank(
                    "live", ["evidence"], SimpleNamespace(base_url="local-live", context_length=1024)), .5)
                assert result == [{"index": 0, "score": .8, "text": "evidence"}]
                assert not remote.done() and not closed
                assert [kind for kind, _ in calls] == ["remote", "local"]
                assert all(worker != threading.get_ident() for _, worker in calls)
                release.set()
                response = await asyncio.wait_for(remote, 1)
                assert response.status_code == 200 and closed == ["sdk"]
            finally:
                release.set()
                await asyncio.gather(remote, return_exceptions=True)
    asyncio.run(run())

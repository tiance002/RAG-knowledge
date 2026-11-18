"""Offline lifecycle regressions using real HTTPX wrappers and controlled streams."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest

from app.services import llm, provider_http


def test_simultaneous_event_loops_retain_their_own_manager(monkeypatch):
    monkeypatch.setattr(provider_http, "_managers", {})
    barrier = Barrier(2)

    def worker():
        async def run():
            async with provider_http.managed_provider_http() as manager:
                await asyncio.to_thread(barrier.wait, 3)
                assert provider_http._current_manager() is manager
                async with provider_http.provider_request(
                        base_url="https://synthetic.invalid", api_key="test", stage="test"):
                    assert len(manager._leases) == 1
                assert not manager._leases
        asyncio.run(run())

    with ThreadPoolExecutor(max_workers=2) as executor:
        tasks = [executor.submit(worker) for _ in range(2)]
        for task in tasks:
            task.result(timeout=10)
    assert not provider_http._managers


def test_request_timeout_and_environment_partition_without_retaining_secrets(monkeypatch):
    async def run():
        async with provider_http.managed_provider_http() as manager:
            async with provider_http.provider_request(
                    base_url="https://synthetic.invalid", api_key="key", stage="test", timeout=7.0) as first:
                assert first.timeout.read == 7.0
            async with provider_http.provider_request(
                    base_url="https://synthetic.invalid", api_key="key", stage="test",
                    timeout={phase: 7.0 for phase in ("connect", "read", "write", "pool")}) as repeated:
                assert repeated is first
            async with provider_http.provider_request(
                    base_url="https://synthetic.invalid", api_key="key", stage="test", timeout=None) as disabled:
                assert disabled is not first
                assert disabled.timeout.read is None
            monkeypatch.setenv("HTTP_PROXY", "http://user:first-secret@127.0.0.1:9991")
            async with provider_http.provider_request(
                    base_url="https://synthetic.invalid", api_key="key", stage="test", trust_env=True) as env_first:
                pass
            monkeypatch.setenv("HTTP_PROXY", "http://user:second-secret@127.0.0.1:9992")
            async with provider_http.provider_request(
                    base_url="https://synthetic.invalid", api_key="key", stage="test", trust_env=True) as env_second:
                assert env_second is not env_first
            assert len(manager.clients) == 4
            assert "first-secret" not in repr(manager.clients)
            assert "second-secret" not in repr(manager.clients)
    asyncio.run(run())


def test_shutdown_closes_active_response_and_rejects_queued_request(monkeypatch):
    class Stream(httpx.AsyncByteStream):
        closed = 0

        async def __aiter__(self):
            yield b"first"

        async def aclose(self):
            self.closed += 1

    async def run():
        monkeypatch.setenv("RAG_PROVIDER_MAX_IN_FLIGHT", "1")
        stream = Stream()
        calls = []

        def handle(request):
            calls.append(request)
            return httpx.Response(200, stream=stream)

        async with provider_http.managed_provider_http() as manager:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            monkeypatch.setattr(manager, "_new_client", lambda *args: client)
            async with httpx.AsyncClient(transport=provider_http.ProviderBorrowingTransport(
                    base_url="https://synthetic.invalid", api_key="key", stage="test")) as proxy:
                response = await proxy.send(proxy.build_request("GET", "https://synthetic.invalid"), stream=True)
                waiter = asyncio.create_task(proxy.get("https://synthetic.invalid"))
                await asyncio.sleep(0)
                assert not waiter.done()
                await manager.close()
                with pytest.raises(RuntimeError, match="manager is closed"):
                    await asyncio.wait_for(waiter, 1)
                await response.aclose()
                assert stream.closed == 1
                assert client.is_closed
                assert len(calls) == 1
                assert not manager._leases and not manager._streams
                assert manager.slots._value == 1
    asyncio.run(run())


def test_response_close_finishes_despite_repeated_cancellation(monkeypatch):
    async def run():
        close_started, release_close = asyncio.Event(), asyncio.Event()
        close_count = 0

        class Stream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b"first"

            async def aclose(self):
                nonlocal close_count
                close_count += 1
                close_started.set()
                await release_close.wait()

        async with provider_http.managed_provider_http() as manager:
            client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=Stream())))
            monkeypatch.setattr(manager, "_new_client", lambda *args: client)
            async with httpx.AsyncClient(transport=provider_http.ProviderBorrowingTransport(
                    base_url="https://synthetic.invalid", api_key="key", stage="test")) as proxy:
                response = await proxy.send(proxy.build_request("GET", "https://synthetic.invalid"), stream=True)
                closing = asyncio.create_task(response.aclose())
                await close_started.wait()
                for _ in range(2):
                    closing.cancel()
                    await asyncio.sleep(0)
                    assert not closing.done()
                release_close.set()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(closing, 1)
                await response.aclose()
                assert close_count == 1
                assert not manager._leases and not manager._streams
                assert manager.slots._value == manager.max_in_flight
    asyncio.run(run())


def test_shutdown_cleans_lease_before_headers_and_closes_late_response(monkeypatch):
    async def run():
        sending, release_send = asyncio.Event(), asyncio.Event()
        responses = []

        async def handle(request):
            sending.set()
            await release_send.wait()
            response = httpx.Response(200, content=b"late")
            responses.append(response)
            return response

        async with provider_http.managed_provider_http() as manager:
            client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
            monkeypatch.setattr(manager, "_new_client", lambda *args: client)
            async with httpx.AsyncClient(transport=provider_http.ProviderBorrowingTransport(
                    base_url="https://synthetic.invalid", api_key="key", stage="test")) as proxy:
                request = asyncio.create_task(proxy.get("https://synthetic.invalid"))
                await sending.wait()
                await manager.close()
                assert not manager._leases
                assert manager.slots._value == manager.max_in_flight
                release_send.set()
                with pytest.raises(RuntimeError, match="manager is closed"):
                    await request
                assert len(responses) == 1 and responses[0].is_closed
    asyncio.run(run())


def test_llm_owner_registration_and_repeated_cancel_finish_all_owned_clients():
    async def run():
        owned = []
        token = llm._owned_llm_services.set(owned)
        try:
            service = llm.LLMService(base_url="https://synthetic.invalid", api_key="key", model="test")
        finally:
            llm._owned_llm_services.reset(token)
        assert owned == [service]
        assert llm._owned_llm_services.get() is None
        started, release = asyncio.Event(), asyncio.Event()
        closed = []

        async def close_sdk():
            started.set()
            await release.wait()
            closed.append("sdk")

        async def close_bad():
            closed.append("bad")
            raise OSError("synthetic close error")

        async def close_last():
            closed.append("last")

        service._client = SimpleNamespace(close=close_sdk)
        service._langchain_http_clients = [SimpleNamespace(aclose=close_bad), SimpleNamespace(aclose=close_last)]
        closing = asyncio.create_task(service.aclose())
        await started.wait()
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, 1)
        await service.aclose()
        assert closed == ["sdk", "bad", "last"]
        with pytest.raises(RuntimeError, match="LLMService is closed"):
            service._get_client()
        with pytest.raises(RuntimeError, match="LLMService is closed"):
            service.get_chat_model()
    asyncio.run(run())


def test_close_errors_preserve_stream_output_and_finish_metadata(monkeypatch):
    class Stream:
        closed = 0

        async def __aiter__(self):
            yield SimpleNamespace(choices=[SimpleNamespace(
                delta=SimpleNamespace(content="answer"), finish_reason="stop")])

        async def close(self):
            self.closed += 1
            raise OSError("synthetic close failure")

    async def run():
        stream = Stream()

        async def create(**kwargs):
            return stream

        service = llm.LLMService(base_url="https://synthetic.invalid", api_key="key", model="test")
        monkeypatch.setattr(service, "_get_client", lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        metadata = []
        assert [part async for part in service.generate_stream(
            [{"role": "user", "content": "test"}], on_final=metadata.append)] == ["answer"]
        assert metadata == [{"finish_reason": "stop", "usage": None}]
        assert stream.closed == 1
        await service.aclose()
    asyncio.run(run())


@pytest.mark.parametrize("decorated", [False, True])
def test_stream_generator_close_waits_for_cleanup_under_repeated_cancel(monkeypatch, decorated):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        closed = []

        class Stream:
            async def __aiter__(self):
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="partial"), finish_reason=None)])
                await asyncio.Event().wait()

            async def close(self):
                started.set()
                await release.wait()
                closed.append("stream")

        async def create(**kwargs):
            return Stream()

        service = llm.LLMService(base_url="https://synthetic.invalid", api_key="key", model="test")
        monkeypatch.setattr(service, "_get_client", lambda: SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
        generate = service.generate_stream if decorated else lambda messages: llm.LLMService.generate_stream.__wrapped__(service, messages)
        generator = generate([{"role": "user", "content": "test"}])
        assert await anext(generator) == "partial"
        closing = asyncio.create_task(generator.aclose())
        await started.wait()
        for _ in range(2):
            closing.cancel()
            await asyncio.sleep(0)
            assert not closing.done()
        release.set()
        if decorated:
            # The observation wrapper preserves the original GeneratorExit
            # during aclose; repeated cancellation must still finish cleanup.
            await asyncio.wait_for(closing, 1)
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(closing, 1)
        assert closed == ["stream"]
        await service.aclose()
    asyncio.run(run())

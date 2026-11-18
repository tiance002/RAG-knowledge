import asyncio

from app.services import provider_http


def test_pool_isolation_closure_and_worker_loop():
    async def run():
        async with provider_http.managed_provider_http() as manager:
            async with provider_http.provider_request(base_url="https://one", api_key="a", stage="test") as first:
                pass
            async with provider_http.provider_request(base_url="https://one", api_key="a", stage="test") as again:
                assert again is first
            async with provider_http.provider_request(base_url="https://one", api_key="b", stage="test") as other:
                assert other is not first
            async with provider_http.provider_request(base_url="https://two", api_key="a", stage="test") as other_url:
                assert other_url is not first
            assert len(manager.clients) == 3
        assert first.is_closed and other.is_closed and other_url.is_closed
        async with provider_http.provider_request(base_url="https://one", api_key="a", stage="test") as worker:
            assert worker is not first
        assert worker.is_closed
    asyncio.run(run())
    asyncio.run(run())


def test_cancelled_request_releases_slot(monkeypatch):
    async def run():
        monkeypatch.setattr(provider_http, "_MAX_IN_FLIGHT", 1)
        entered = asyncio.Event()
        async with provider_http.managed_provider_http():
            async def request():
                async with provider_http.provider_request(base_url="https://one", api_key="a", stage="test"):
                    entered.set()
                    await asyncio.Event().wait()
            task = asyncio.create_task(request())
            await entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            async with asyncio.timeout(1):
                async with provider_http.provider_request(base_url="https://one", api_key="a", stage="test"):
                    pass
    asyncio.run(run())


def test_borrowed_transport_keeps_slot_until_response_closed(monkeypatch):
    import httpx

    class BytesStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"ok"

    async def run():
        monkeypatch.setattr(provider_http, "_MAX_IN_FLIGHT", 1)
        async with provider_http.managed_provider_http() as manager:
            mock_client = httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, stream=BytesStream())))
            key = provider_http._pool_key("https://one", "a", 5.0, True, False)
            manager.clients[key] = mock_client
            proxy = httpx.AsyncClient(transport=provider_http.ProviderBorrowingTransport(
                base_url="https://one", api_key="a", stage="llm", trust_env=False))
            first = await proxy.send(proxy.build_request("GET", "https://one"), stream=True)
            blocked = asyncio.create_task(proxy.get("https://one"))
            await asyncio.sleep(0)
            assert not blocked.done()
            await first.aclose()
            response = await asyncio.wait_for(blocked, 1)
            assert response.content == b"ok"
            await proxy.aclose()
            assert not mock_client.is_closed
        assert mock_client.is_closed
    asyncio.run(run())


def test_actual_sdk_and_langchain_share_borrowed_connections(monkeypatch):
    import json
    import httpx
    from app.services.llm import LLMService

    class JSONStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield json.dumps({"id": "chatcmpl-test", "object": "chat.completion", "created": 1,
                              "model": "test", "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant", "content": "hello"}}]}).encode()

    async def run():
        calls = []
        async def handle(request):
            calls.append(request)
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=JSONStream())
        async with provider_http.managed_provider_http() as manager:
            clients = []
            for trust_env, timeout in ((False, 120.0), (True, None)):
                client = httpx.AsyncClient(transport=httpx.MockTransport(handle))
                clients.append(client)
                manager.clients[provider_http._pool_key("https://one/v1", "test-key", timeout, True, trust_env)] = client
            def unexpected_client(*args, **kwargs):
                raise AssertionError("unexpected pool partition must not make a real request")
            monkeypatch.setattr(manager, "_new_client", unexpected_client)
            service = LLMService(base_url="https://one/v1", api_key="test-key", model="test")
            result = await service._get_client().chat.completions.create(model="test", messages=[{"role": "user", "content": "hi"}])
            assert result.choices[0].message.content == "hello"
            model = service.get_chat_model(streaming=False)
            assert model.http_async_client.follow_redirects is True
            assert service._get_client()._client.follow_redirects is False
            assert (await model.ainvoke("hi")).content == "hello"
            assert len(calls) == 2
            assert all(request.headers["authorization"] == "Bearer test-key" for request in calls)
            assert [request.extensions["timeout"]["read"] for request in calls] == [120.0, None]
            await service.aclose()
            assert not any(client.is_closed for client in clients)
        assert all(client.is_closed for client in clients)
    asyncio.run(run())


def test_shutdown_closes_all_clients_and_rejects_waiters(monkeypatch):
    class FakeClient:
        def __init__(self, fail=False):
            self.closed = False
            self.fail = fail
        async def aclose(self):
            self.closed = True
            if self.fail:
                raise RuntimeError("synthetic close error")

    async def run():
        monkeypatch.setenv("RAG_PROVIDER_MAX_IN_FLIGHT", "1")
        manager = provider_http.ProviderHTTPManager()
        first, second = FakeClient(True), FakeClient()
        manager.clients = {"first": first, "second": second}
        monkeypatch.setitem(provider_http._managers, asyncio.get_running_loop(), manager)
        await manager.slots.acquire()
        async def queued():
            async with provider_http.provider_request(base_url="https://one", api_key="a", stage="test"):
                raise AssertionError("closed manager admitted a request")
        waiter = asyncio.create_task(queued())
        await asyncio.sleep(0)
        await manager.close()
        manager.slots.release()
        import pytest
        with pytest.raises(RuntimeError, match="manager is closed"):
            await waiter
        assert first.closed and second.closed and not manager.clients
    asyncio.run(run())


def test_telemetry_failure_does_not_prevent_temporary_cleanup(monkeypatch):
    async def run():
        def broken(*args, **kwargs):
            raise RuntimeError("synthetic telemetry failure")
        monkeypatch.setattr(provider_http, "event", broken)
        async with provider_http.provider_request(base_url="https://one", api_key="a", stage="test") as client:
            assert not client.is_closed
        assert client.is_closed
    asyncio.run(run())


def test_positive_connection_limit_configuration(monkeypatch):
    async def run():
        monkeypatch.setenv("RAG_PROVIDER_MAX_IN_FLIGHT", "2")
        monkeypatch.setenv("RAG_PROVIDER_MAX_CONNECTIONS", "3")
        manager = provider_http.ProviderHTTPManager()
        assert manager.slots._value == 2
        assert manager.limits.max_connections == 3
        assert manager.limits.max_keepalive_connections == 3
        await manager.close()
    asyncio.run(run())


def test_langchain_explicit_proxy_uses_original_client(monkeypatch):
    from app.services.llm import LLMService
    monkeypatch.setenv("OPENAI_PROXY", "http://127.0.0.1:9999")
    model = LLMService(base_url="https://one/v1", api_key="test-key", model="test").get_chat_model()
    assert model.openai_proxy == "http://127.0.0.1:9999"
    assert model.max_retries == 0
    assert not isinstance(model.http_async_client._transport, provider_http.ProviderBorrowingTransport)


def test_startup_reserves_are_unbound_claimed_once_and_closed(monkeypatch):
    import httpx
    async def run():
        calls = []
        async def forbidden_send(*args, **kwargs):
            calls.append(1)
            raise AssertionError("preinitialization must not send")
        monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_send)
        async with provider_http.managed_provider_http(preinitialize=True) as manager:
            assert len(manager.reserves) == 4
            all_reserves = list(manager.reserves.values())
            assert not calls
            reserved = manager.reserves[provider_http._pool_key("", "", 60.0, True, False)[2:]]
            async with provider_http.provider_request(base_url="https://one", api_key="first", stage="test") as first:
                assert first is reserved
                assert "authorization" not in first.headers
            async with provider_http.provider_request(base_url="https://one", api_key="second", stage="test") as second:
                assert second is not first
            async with provider_http.provider_request(base_url="https://one", api_key="first", stage="test") as again:
                assert again is first
            assert len(manager.reserves) == 3
        assert all(client.is_closed for client in all_reserves)
        assert second.is_closed and not calls
    asyncio.run(run())


def test_unmatched_custom_tls_does_not_consume_reserve():
    import ssl
    async def run():
        async with provider_http.managed_provider_http(preinitialize=True) as manager:
            reserve_key = provider_http._pool_key("", "", 60.0, True, False)[2:]
            reserve = manager.reserves[reserve_key]
            context = ssl.create_default_context()
            async with provider_http.provider_request(base_url="https://one", api_key="first", stage="test", verify=context) as client:
                assert client is not reserve
                assert manager.reserves[reserve_key] is reserve
            assert not client.is_closed
        assert client.is_closed and reserve.is_closed
    asyncio.run(run())


def test_failed_startup_closes_partial_reserves_then_request_remains_lazy(monkeypatch):
    import pytest
    async def run():
        made = []
        class Client:
            is_closed = False
            async def aclose(self):
                self.is_closed = True
        def constructor(**kwargs):
            if made:
                raise ValueError("synthetic TLS failure")
            client = Client()
            made.append(client)
            return client
        monkeypatch.setattr(provider_http.httpx, "AsyncClient", constructor)
        async with provider_http.managed_provider_http(preinitialize=True) as manager:
            assert made[0].is_closed and not manager.reserves
            with pytest.raises(ValueError, match="synthetic TLS failure"):
                async with provider_http.provider_request(base_url="https://one", api_key="key", stage="test"):
                    pass
            assert not manager.clients
    asyncio.run(run())


def test_slot_duration_includes_lazy_construction_and_temporary_close(monkeypatch):
    async def run():
        times = iter([0.0, 1.0, 2.0, 3.0, 4.0, 8.0])
        captured = []
        from types import SimpleNamespace
        monkeypatch.setattr(provider_http, "time", SimpleNamespace(monotonic=lambda: next(times)))
        monkeypatch.setattr(provider_http, "_record_timing", lambda name, stage, value: captured.append((name, value)))
        class Client:
            async def aclose(self):
                pass
        monkeypatch.setattr(provider_http.httpx, "AsyncClient", lambda **kwargs: Client())
        async with provider_http.provider_request(base_url="https://one", api_key="key", stage="test"):
            pass
        assert ("provider.client_initialize", 1.0) in captured
        assert ("provider.slot_finished", 6.0) in captured
    asyncio.run(run())

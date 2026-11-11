"""Loop-scoped provider connection pools for the API process.

Workers using asyncio.run get a short-lived client, never a client from a
closed loop. Credentials are used only to partition pools, never as headers.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import logging
import os
import time

import httpx

from app.services.observability import event

_MAX_IN_FLIGHT = 16
_MAX_POOLS = 64
_MAX_CONNECTIONS = 16
_RESERVE_SPECS = ((60.0, True, False), (120.0, True, False), (5.0, True, False), (60.0, True, True))
_manager = None
logger = logging.getLogger(__name__)


def _positive_env(name, default):
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning("Invalid positive integer for %s; using default", name)
    return default


def _limits():
    maximum = _positive_env("RAG_PROVIDER_MAX_CONNECTIONS", _MAX_CONNECTIONS)
    return httpx.Limits(max_connections=maximum, max_keepalive_connections=min(8, maximum), keepalive_expiry=30.0)


def _record_timing(name, stage, duration):
    # Telemetry cannot make a request fail or prevent cleanup.
    try:
        event(name, stage=stage, duration_ms=duration * 1000)
        event("performance.stage", stage=stage, stage_name=name.replace(".", "_"), duration_ms=duration * 1000)
    except Exception:
        logger.warning("Provider timing observation failed")


class ProviderHTTPManager:
    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.pid = os.getpid()
        self.max_in_flight = _positive_env("RAG_PROVIDER_MAX_IN_FLIGHT", _MAX_IN_FLIGHT)
        self.slots = asyncio.Semaphore(self.max_in_flight)
        self.limits = _limits()
        self.clients = {}
        self.reserves = {}
        self.closed = False

    async def preinitialize(self):
        """Prepare unbound clients without sending traffic or selecting credentials."""
        if self.closed:
            raise RuntimeError("Provider HTTP manager is closed")
        if self.reserves:
            return
        try:
            for timeout, verify, trust_env in _RESERVE_SPECS:
                started = time.monotonic()
                try:
                    client = httpx.AsyncClient(timeout=timeout, verify=verify,
                                               trust_env=trust_env, limits=self.limits)
                finally:
                    _record_timing('provider.client_initialize', 'startup', time.monotonic() - started)
                self.reserves[(timeout, verify, trust_env)] = client
        except BaseException as exc:
            reserves, self.reserves = list(self.reserves.values()), {}
            for client in reserves:
                try:
                    await client.aclose()
                except Exception as close_exc:
                    logger.warning("Provider reserve close failed: %s", type(close_exc).__name__)
            if not isinstance(exc, Exception):
                raise
            logger.warning("Provider client preinitialization failed: %s", type(exc).__name__)

    async def close(self):
        self.closed = True
        clients = [*self.clients.values(), *self.reserves.values()]
        self.clients, self.reserves = {}, {}
        for client in clients:
            try:
                await client.aclose()
            except Exception as exc:
                logger.warning("Provider client close failed: %s", type(exc).__name__)


@asynccontextmanager
async def managed_provider_http(*, preinitialize=False):
    global _manager
    manager = ProviderHTTPManager()
    previous = _manager
    _manager = manager
    try:
        if preinitialize:
            await manager.preinitialize()
        yield manager
    finally:
        if _manager is manager:
            _manager = previous
        await manager.close()


@asynccontextmanager
async def provider_request(*, base_url, api_key, stage, timeout=60.0, verify=True, trust_env=False):
    """Acquire one in-flight slot; hold it until the entire request finishes."""
    manager = _manager
    if (manager is None or manager.closed or manager.pid != os.getpid()
            or manager.loop is not asyncio.get_running_loop()):
        manager = None
    started = time.monotonic()
    if manager is not None:
        await manager.slots.acquire()
        if manager.closed:
            manager.slots.release()
            raise RuntimeError("Provider HTTP manager is closed")
    wait = time.monotonic() - started
    slot_started = time.monotonic()
    client = None
    temporary = False
    try:
        # Include TLS configuration in pool isolation without retaining secrets.
        tls_key = verify if isinstance(verify, (bool, str)) else id(verify)
        digest = hashlib.sha256((api_key or '').encode()).digest()
        key = (base_url or '', digest, timeout, tls_key, trust_env)
        client = manager.clients.get(key) if manager is not None else None
        limits = manager.limits if manager is not None else _limits()
        if client is None:
            # Never reserve a custom TLS context or share a claimed client across keys.
            if manager is not None and verify is True:
                client = manager.reserves.pop((timeout, True, trust_env), None)
            if client is None:
                initialized = time.monotonic()
                try:
                    client = httpx.AsyncClient(timeout=timeout, verify=verify, trust_env=trust_env, limits=limits)
                finally:
                    _record_timing('provider.client_initialize', stage, time.monotonic() - initialized)
            if manager is not None and len(manager.clients) < _MAX_POOLS:
                manager.clients[key] = client
            else:
                temporary = True
        _record_timing('provider.local_wait', stage, wait)
        try:
            event('provider.pool_config', stage=stage, managed=manager is not None,
                  max_in_flight=manager.max_in_flight if manager is not None else None,
                  max_connections=limits.max_connections)
        except Exception:
            logger.warning("Provider configuration observation failed")
        yield client
    finally:
        try:
            if temporary and client is not None:
                await client.aclose()
        finally:
            _record_timing('provider.slot_finished', stage, time.monotonic() - slot_started)
            if manager is not None:
                manager.slots.release()


class _BorrowedResponseStream(httpx.AsyncByteStream):
    def __init__(self, response, lease):
        self.response = response
        self.lease = lease
        self.closed = False

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_raw():
                yield chunk
        finally:
            await self.aclose()

    async def aclose(self):
        if not self.closed:
            self.closed = True
            try:
                await self.response.aclose()
            finally:
                await self.lease.__aexit__(None, None, None)


class ProviderBorrowingTransport(httpx.AsyncBaseTransport):
    """Bridge SDK-owned clients to the loop-owned pool, including streams."""
    def __init__(self, *, base_url, api_key, stage, trust_env):
        self.base_url = base_url
        self.api_key = api_key
        self.stage = stage
        self.trust_env = trust_env

    async def handle_async_request(self, request):
        lease = provider_request(base_url=self.base_url, api_key=self.api_key,
                                 stage=self.stage, trust_env=self.trust_env)
        client = await lease.__aenter__()
        try:
            response = await client.send(request, stream=True, follow_redirects=False)
        except BaseException as exc:
            await lease.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return httpx.Response(status_code=response.status_code, headers=response.headers,
                              stream=_BorrowedResponseStream(response, lease),
                              extensions=response.extensions, request=request)

"""Lifespan/loop-owned HTTP pools borrowed by short-lived LLM SDK clients.

This module never supplies provider headers, changes request bodies, or retries.
An unmanaged worker loop gets a temporary client owned by its response lease.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import logging
import os
import time

import httpx

from app.services.observability import event

logger = logging.getLogger(__name__)
_MAX_IN_FLIGHT = 16
_MAX_CONNECTIONS = 16
_MAX_POOLS = 64
_DEFAULT_TIMEOUT = 120.0
_TIMEOUT_PHASES = ("connect", "read", "write", "pool")
_ENV_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "SSL_CERT_FILE", "SSL_CERT_DIR")
_managers = {}


def _positive_env(name, default):
    try:
        value = int(os.getenv(name, str(default)))
        if value > 0:
            return value
    except ValueError:
        pass
    logger.warning("Invalid positive integer for %s; using default", name)
    return default


def _limits():
    maximum = _positive_env("RAG_PROVIDER_MAX_CONNECTIONS", _MAX_CONNECTIONS)
    return httpx.Limits(max_connections=maximum,
                        max_keepalive_connections=min(8, maximum),
                        keepalive_expiry=30.0)


def _timeout_values(timeout):
    # HTTPX passes these four values down to httpcore on each Request. In
    # particular, timeout=None means disabled timeouts, not our LLM default.
    if isinstance(timeout, dict):
        return {phase: timeout.get(phase) for phase in _TIMEOUT_PHASES}
    return httpx.Timeout(timeout).as_dict()


def _environment_key(trust_env):
    if not trust_env:
        return None
    values = tuple((name, os.getenv(name)) for root in _ENV_NAMES
                   for name in (root, root.lower()))
    # Proxy credentials and paths must not appear in retained pool keys/logs.
    return hashlib.sha256(repr(values).encode()).digest()


def _pool_key(base_url, api_key, timeout, verify, trust_env):
    tls_key = verify if isinstance(verify, (bool, str)) else id(verify)
    return (str(base_url or ""), hashlib.sha256((api_key or "").encode()).digest(),
            tuple(_timeout_values(timeout).items()), tls_key,
            bool(trust_env), _environment_key(trust_env))


def _record_timing(name, stage, duration):
    try:
        event(name, stage=stage, duration_ms=duration * 1000)
    except Exception:
        logger.warning("Provider timing observation failed")


async def _await_cleanup(task):
    """Finish releasing sockets/slots even if the borrower is cancelled."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


def _current_manager():
    manager = _managers.get(asyncio.get_running_loop())
    if manager is not None and manager.pid == os.getpid():
        return manager
    return None


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
        self._leases = set()
        self._streams = set()
        self._close_task = None

    def _new_client(self, timeout, verify, trust_env):
        return httpx.AsyncClient(timeout=httpx.Timeout(**_timeout_values(timeout)),
                                 verify=verify, trust_env=trust_env,
                                 limits=self.limits)

    async def preinitialize(self):
        """Prepare one unclaimed LLM client; do not resolve credentials/send."""
        if self.closed:
            raise RuntimeError("Provider HTTP manager is closed")
        key = _pool_key("", "", _DEFAULT_TIMEOUT, True, False)[2:]
        if key not in self.reserves:
            started = time.monotonic()
            try:
                self.reserves[key] = self._new_client(_DEFAULT_TIMEOUT, True, False)
            finally:
                _record_timing("provider.client_initialize", "startup", time.monotonic() - started)

    async def _close(self):
        # Stop admission first. A queued borrower wakes when its prior lease is
        # released, observes closed=True, and cannot start another request.
        self.closed = True
        for stream in list(self._streams):
            try:
                await stream.aclose()
            except Exception as exc:
                logger.warning("Provider response close failed: %s", type(exc).__name__)
        clients = [*self.clients.values(), *self.reserves.values()]
        self.clients, self.reserves = {}, {}
        for client in clients:
            try:
                await client.aclose()
            except Exception as exc:
                logger.warning("Provider client close failed: %s", type(exc).__name__)
        # Also release leases whose send had not returned response headers.
        for lease in list(self._leases):
            try:
                await lease.__aexit__(None, None, None)
            except Exception as exc:
                logger.warning("Provider lease close failed: %s", type(exc).__name__)

    async def close(self):
        if self._close_task is None:
            self.closed = True
            self._close_task = asyncio.create_task(self._close())
        await _await_cleanup(self._close_task)


@asynccontextmanager
async def managed_provider_http(*, preinitialize=False):
    manager = ProviderHTTPManager()
    previous = _managers.get(manager.loop)
    _managers[manager.loop] = manager
    try:
        if preinitialize:
            await manager.preinitialize()
        yield manager
    finally:
        # Keep the closed owner visible while shutdown rejects its waiters.
        try:
            await manager.close()
        finally:
            if _managers.get(manager.loop) is manager:
                if previous is None:
                    _managers.pop(manager.loop, None)
                else:
                    _managers[manager.loop] = previous


class _ProviderLease:
    def __init__(self, *, base_url, api_key, stage, timeout, verify, trust_env):
        self.manager = _current_manager()
        self.key = _pool_key(base_url, api_key, timeout, verify, trust_env)
        self.stage, self.timeout = stage, timeout
        self.verify, self.trust_env = verify, trust_env
        self.client = None
        self.temporary = False
        self.acquired = False
        self.closed = False
        self.started = None
        self._close_task = None

    async def __aenter__(self):
        manager = self.manager
        waiting = time.monotonic()
        if manager is not None:
            await manager.slots.acquire()
            self.acquired = True
            if manager.closed:
                manager.slots.release()
                self.acquired = False
                raise RuntimeError("Provider HTTP manager is closed")
            manager._leases.add(self)
        self.started = time.monotonic()
        try:
            self.client = manager.clients.get(self.key) if manager else None
            if self.client is None:
                if manager is not None:
                    self.client = manager.reserves.pop(self.key[2:], None)
                if self.client is None:
                    initialized = time.monotonic()
                    try:
                        if manager:
                            self.client = manager._new_client(self.timeout, self.verify, self.trust_env)
                        else:
                            self.client = httpx.AsyncClient(
                                timeout=httpx.Timeout(**_timeout_values(self.timeout)),
                                verify=self.verify, trust_env=self.trust_env, limits=_limits())
                    finally:
                        _record_timing("provider.client_initialize", self.stage,
                                       time.monotonic() - initialized)
                if manager is not None and len(manager.clients) < _MAX_POOLS:
                    manager.clients[self.key] = self.client
                else:
                    self.temporary = True
            _record_timing("provider.local_wait", self.stage, self.started - waiting)
            return self.client
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def _close(self):
        self.closed = True
        try:
            if self.temporary and self.client is not None:
                await self.client.aclose()
        finally:
            if self.manager is not None:
                self.manager._leases.discard(self)
                if self.acquired:
                    self.acquired = False
                    self.manager.slots.release()
            if self.started is not None:
                _record_timing("provider.slot_finished", self.stage,
                               time.monotonic() - self.started)

    async def __aexit__(self, exc_type, exc, traceback):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _await_cleanup(self._close_task)


def provider_request(*, base_url, api_key, stage, timeout=_DEFAULT_TIMEOUT,
                     verify=True, trust_env=False):
    return _ProviderLease(base_url=base_url, api_key=api_key, stage=stage,
                          timeout=timeout, verify=verify, trust_env=trust_env)


class _BorrowedResponseStream(httpx.AsyncByteStream):
    def __init__(self, response, lease):
        self.response, self.lease = response, lease
        self._close_task = None
        if lease.manager is not None:
            lease.manager._streams.add(self)

    async def __aiter__(self):
        try:
            async for chunk in self.response.aiter_raw():
                yield chunk
        finally:
            await self.aclose()

    async def _close(self):
        try:
            await self.response.aclose()
        finally:
            try:
                await self.lease.__aexit__(None, None, None)
            finally:
                if self.lease.manager is not None:
                    self.lease.manager._streams.discard(self)

    async def aclose(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await _await_cleanup(self._close_task)


class ProviderBorrowingTransport(httpx.AsyncBaseTransport):
    """SDK-owned facade; the application lifespan owns the borrowed sockets."""
    def __init__(self, *, base_url, api_key, stage, trust_env=False,
                 timeout=_DEFAULT_TIMEOUT, verify=True):
        self.base_url, self.api_key, self.stage = base_url, api_key, stage
        self.trust_env, self.timeout, self.verify = trust_env, timeout, verify

    async def handle_async_request(self, request):
        timeout = request.extensions.get("timeout", self.timeout)
        lease = provider_request(base_url=self.base_url, api_key=self.api_key,
                                 stage=self.stage, timeout=timeout,
                                 trust_env=self.trust_env, verify=self.verify)
        client = await lease.__aenter__()
        try:
            # Redirect/auth policy belongs to the outer SDK's HTTPX client.
            # send forwards the already-built request, including timeout/trace.
            response = await client.send(request, stream=True,
                                         follow_redirects=False, auth=httpx.Auth())
            if lease.closed or (lease.manager is not None and lease.manager.closed):
                await response.aclose()
                raise RuntimeError("Provider HTTP manager is closed")
            return httpx.Response(status_code=response.status_code, headers=response.headers,
                                  stream=_BorrowedResponseStream(response, lease),
                                  extensions=response.extensions, request=request)
        except BaseException as exc:
            await lease.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    async def aclose(self):
        # Closing an SDK wrapper must not close the lifespan's shared pool.
        pass

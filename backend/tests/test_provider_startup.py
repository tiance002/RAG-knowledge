"""Actual API lifespan, real HTTPX clients, stub persistence, and zero sends."""
import asyncio
from types import SimpleNamespace

import httpx
from sqlalchemy.pool import StaticPool
from sqlmodel import create_engine


def test_actual_app_lifespan_preinitializes_unbound_reserves_and_closes_all(monkeypatch):
    # Importing the app registers routes; its database initializer only runs
    # inside lifespan, where all persistence/directory hooks below are stubbed.
    from app import main
    from app.services import observability, provider_http

    memory_engine = create_engine('sqlite://', poolclass=StaticPool,
                                  connect_args={'check_same_thread': False})
    calls = []
    sends = []
    monkeypatch.setattr(main, 'engine', memory_engine)
    monkeypatch.setattr(main, 'ensure_directories', lambda: calls.append('ensure_directories'))
    monkeypatch.setattr(main, 'init_db', lambda: calls.append('init_db'))

    def persisted_settings(session):
        assert session.bind is memory_engine
        calls.append('load_persisted_settings_memory_session')
        return SimpleNamespace(revision=1)

    monkeypatch.setattr(main, 'load_persisted_settings', persisted_settings)
    monkeypatch.setattr(observability, 'flush_observations', lambda **kwargs: calls.append('flush_observations'))
    monkeypatch.setattr(provider_http, '_manager', None)
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('LANGFUSE_ENABLED', 'false')

    async def forbidden_async_send(self, request, *args, **kwargs):
        sends.append(('async', str(request.url)))
        raise AssertionError('HTTP send forbidden in startup-reserve test')

    def forbidden_sync_send(self, request, *args, **kwargs):
        sends.append(('sync', str(request.url)))
        raise AssertionError('HTTP send forbidden in startup-reserve test')

    # Keep AsyncClient/SSL/transport constructors real. No model is selected or
    # endpoint contacted; synthetic API keys only partition unbound pools.
    monkeypatch.setattr(httpx.AsyncClient, 'send', forbidden_async_send)
    monkeypatch.setattr(httpx.Client, 'send', forbidden_sync_send)
    observed = {}

    async def run():
        async with main.app.router.lifespan_context(main.app):
            manager = provider_http._manager
            observed['manager'] = manager
            assert manager is not None and not manager.closed
            assert len(manager.reserves) == 4
            assert not manager.clients
            expected_keys = {(60.0, True, False), (120.0, True, False),
                             (5.0, True, False), (60.0, True, True)}
            assert set(manager.reserves) == expected_keys
            initial = dict(manager.reserves)
            observed['initial_reserves'] = list(initial.values())
            assert all(isinstance(client, httpx.AsyncClient) for client in initial.values())
            assert all(not client.is_closed for client in initial.values())
            assert all(not client.headers.get('Authorization') for client in initial.values())
            assert all(not client.base_url.host for client in initial.values())

            expected_rerank = initial[(120.0, True, False)]
            async with provider_http.provider_request(base_url='https://synthetic.invalid/v1',
                    api_key='synthetic-key-a', stage='rerank', timeout=120.0) as first:
                assert first is expected_rerank
                assert len(manager.reserves) == 3
            async with provider_http.provider_request(base_url='https://synthetic.invalid/v1',
                    api_key='synthetic-key-a', stage='rerank', timeout=120.0) as repeated:
                assert repeated is first
            async with provider_http.provider_request(base_url='https://synthetic.invalid/v1',
                    api_key='synthetic-key-b', stage='rerank', timeout=120.0) as auth_variant:
                assert auth_variant is not first
                assert auth_variant not in initial.values()
                assert not auth_variant.headers.get('Authorization')
            assert len(manager.clients) == 2
            assert len(manager.reserves) == 3
            observed['auth_variant'] = auth_variant
            assert sends == []
        assert manager.closed
        assert not manager.clients and not manager.reserves
        assert provider_http._manager is None

    try:
        asyncio.run(run())
    finally:
        memory_engine.dispose()
    assert calls == ['ensure_directories', 'init_db', 'load_persisted_settings_memory_session',
                     'flush_observations']
    assert sends == []
    assert all(client.is_closed for client in observed['initial_reserves'])
    assert observed['auth_variant'].is_closed
    print('STARTUP_RAW_CHECKS reserves=4 claimed_timeout=120 auth_partition=distinct '
          'http_sends=0 db=memory init/settings/directories=stubbed '
          'claimed_and_unused_clients=closed manager=restored')

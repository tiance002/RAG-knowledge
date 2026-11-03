import asyncio
import json
import socket

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

from app.api.memory import router
from app.services.memory import (
    MemoryService, MemoryUnavailable, PreferenceScope, get_memory_service,
    get_preference_scope,
)


def run_async(test):
    def wrapper():
        asyncio.run(test())
    return wrapper


class FakeRedis:
    """In-memory substitute; eval simulates the Lua contract, not Redis execution."""
    def __init__(self):
        self.data = {}
        self.unavailable = False

    def check(self):
        if self.unavailable:
            raise ConnectionError('offline')

    async def hgetall(self, key):
        self.check()
        return dict(self.data.get(key, {}))

    async def hset(self, key, mapping):
        self.check()
        self.data.setdefault(key, {}).update(mapping)
        return len(mapping)

    async def hdel(self, key, field):
        self.check()
        self.data.get(key, {}).pop(field, None)

    async def delete(self, key):
        self.check()
        self.data.pop(key, None)

    async def eval(self, script, numkeys, key, *args):
        self.check()
        if "redis.call('HDEL'" in script:
            field, timestamp = args
            fields = self.data.get(key, {})
            fields.pop(field, None)
            if not any(name in fields for name in ('answer_style', 'knowledge_level', 'output_preferences')):
                self.data.pop(key, None)
            else:
                fields.update(updated_at=timestamp, schema_version='1')
            return 1
        additions, removals, timestamp = args
        fields = self.data.setdefault(key, {})
        values = json.loads(fields.get('output_preferences', '[]'))
        if not isinstance(values, list) or any(v not in ('step_by_step', 'table', 'code_example', 'principle_first') for v in values):
            raise ValueError('invalid stored output preferences')
        remove = set(json.loads(removals))
        result = [v for v in values if v not in remove]
        for value in json.loads(additions):
            if value not in result:
                result.append(value)
        fields.update(output_preferences=json.dumps(result), schema_version='1', updated_at=timestamp)
        return json.dumps(result)


@run_async
async def test_defaults_partial_update_delete_and_reset():
    client = FakeRedis()
    service = MemoryService(client)
    before = await service.get_preferences()
    assert before.values.model_dump() == {'answer_style': 'normal', 'knowledge_level': 'intermediate', 'output_preferences': []}
    assert set(before.sources.values()) == {'default'}
    current = await service.update_preference({'answer_style': 'concise', 'knowledge_level': 'beginner'})
    assert client.data['rag:memory:preferences:default']['updated_at'].endswith('+00:00')
    assert current.sources == {'answer_style': 'explicit', 'knowledge_level': 'explicit', 'output_preferences': 'default'}
    assert (await service.update_preference({'answer_style': 'detailed'})).values.knowledge_level == 'beginner'
    result = await service.change_output_preferences(add=['table', 'principle_first'])
    assert result.values.output_preferences == ['table', 'principle_first']
    result = await service.change_output_preferences(remove=['table'])
    assert result.values.output_preferences == ['principle_first']
    result = await service.delete_preference('answer_style')
    assert result.values.answer_style == 'normal' and result.sources['answer_style'] == 'default'
    result = await service.reset_preferences()
    assert result == before
    assert client.data == {}


@run_async
async def test_delete_last_field_removes_metadata_and_explicit_empty_survives():
    client = FakeRedis()
    service = MemoryService(client)
    await service.update_preference({'answer_style': 'concise', 'output_preferences': []})
    snapshot = await service.delete_preference('answer_style')
    assert snapshot.sources['output_preferences'] == 'explicit'
    assert snapshot.values.output_preferences == []
    await service.delete_preference('output_preferences')
    assert not client.data


@run_async
async def test_acknowledged_write_failed_reread_is_reported():
    class ReadFailsAfterWrite(FakeRedis):
        async def hset(self, key, mapping):
            await super().hset(key, mapping)
            self.unavailable = True
    service = MemoryService(ReadFailsAfterWrite())
    with pytest.raises(MemoryUnavailable, match='acknowledged') as error:
        await service.update_preference({'answer_style': 'concise'})
    assert error.value.write_acknowledged is True


@run_async
async def test_identity_scoping_and_invalid_persisted_values():
    client = FakeRedis()
    service = MemoryService(client)
    alice = PreferenceScope(tenant_id='org', user_id='alice')
    bob = PreferenceScope(tenant_id='org', user_id='bob')
    await service.update_preference({'answer_style': 'concise'}, alice)
    assert (await service.get_preferences(bob)).sources['answer_style'] == 'default'
    assert (await service.get_preferences(alice)).values.answer_style == 'concise'
    client.data['rag:memory:preferences:org:alice']['knowledge_level'] = 'invalid'
    snapshot = await service.get_preferences(alice)
    assert snapshot.values.knowledge_level == 'intermediate'
    assert snapshot.sources['knowledge_level'] == 'default'
    assert snapshot.values.answer_style == 'concise'


@run_async
async def test_validation_and_failure_are_explicit():
    client = FakeRedis()
    service = MemoryService(client)
    for changes in ({'answer_style': 'wrong'}, {'unknown': 'x'}, {'output_preferences': ['unknown']}, {'answer_style': None}):
        with pytest.raises(ValidationError):
            await service.update_preference(changes)
    with pytest.raises(ValueError):
        await service.delete_preference('schema_version')
    client.unavailable = True
    assert (await service.get_preferences()).redis_fallback is True
    with pytest.raises(MemoryUnavailable) as error:
        await service.update_preference({'answer_style': 'concise'})
    assert error.value.write_acknowledged is False
    with pytest.raises(MemoryUnavailable):
        await service.reset_preferences()


@run_async
async def test_timeout_bounds_read_and_write():
    class Slow(FakeRedis):
        async def hgetall(self, key):
            await asyncio.sleep(1)
        async def hset(self, key, mapping):
            await asyncio.sleep(1)
    service = MemoryService(Slow(), timeout=0.01)
    assert (await service.get_preferences()).redis_fallback
    with pytest.raises(MemoryUnavailable):
        await service.update_preference({'answer_style': 'normal'})


@run_async
async def test_real_redis_client_connection_failure_falls_back():
    # Reserve a local port without listening; the client must fail rather than
    # accidentally connect to any existing Redis instance.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        client = Redis(host='127.0.0.1', port=port, decode_responses=True,
                       socket_connect_timeout=0.1, socket_timeout=0.1,
                       retry=Retry(NoBackoff(), 0))
        try:
            service = MemoryService(client, timeout=0.3)
            assert (await service.get_preferences()).redis_fallback is True
            with pytest.raises(MemoryUnavailable):
                await service.update_preference({'answer_style': 'normal'})
        finally:
            await client.aclose()


@run_async
async def test_api_validation_scope_and_failures():
    client = FakeRedis()
    service = MemoryService(client)
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_memory_service] = lambda: service
    app.dependency_overrides[get_preference_scope] = lambda: PreferenceScope(tenant_id='trusted', user_id='alice')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        endpoint = '/api/memory/preferences'
        assert (await http.get(endpoint)).json()['values']['output_preferences'] == []
        response = await http.put(endpoint, json={'answer_style': 'detailed'})
        assert response.status_code == 200 and response.json()['sources']['answer_style'] == 'explicit'
        for payload in ({'answer_style': 'bad'}, {'tenant_id': 'bob'}, {'knowledge_level': None}, {}):
            assert (await http.put(endpoint, json=payload)).status_code == 422
        assert (await http.delete(endpoint + '/bad')).status_code == 422
        assert (await http.delete(endpoint + '/answer_style')).json()['sources']['answer_style'] == 'default'
        assert (await http.delete(endpoint)).status_code == 200
        client.unavailable = True
        assert (await http.get(endpoint)).json()['redis_fallback'] is True
        assert (await http.put(endpoint, json={'answer_style': 'normal'})).status_code == 503


@run_async
async def test_two_server_authenticated_sessions_cannot_read_or_reset_each_other():
    from fastapi import Header
    from typing import Annotated

    actors = {'session-A': PreferenceScope(tenant_id='org', user_id='alice'),
              'session-B': PreferenceScope(tenant_id='org', user_id='bob')}

    def test_session(authorization: Annotated[str, Header()]):
        return actors[authorization]

    app = FastAPI()
    app.include_router(router)
    service = MemoryService(FakeRedis())
    app.dependency_overrides[get_preference_scope] = test_session
    app.dependency_overrides[get_memory_service] = lambda: service
    api = '/api/memory/preferences'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as http:
        alice = {'Authorization': 'session-A'}
        bob = {'Authorization': 'session-B', 'X-User-Id': 'alice'}
        assert (await http.put(api, json={'knowledge_level': 'expert'}, headers=alice)).status_code == 200
        bob_values = (await http.get(api + '?user_id=alice', headers=bob)).json()
        assert bob_values['values']['knowledge_level'] == 'intermediate'
        assert bob_values['sources']['knowledge_level'] == 'default'
        assert (await http.delete(api + '?user_id=alice', headers=bob)).status_code == 200
        alice_values = (await http.get(api, headers=alice)).json()
        assert alice_values['values']['knowledge_level'] == 'expert'
        assert alice_values['sources']['knowledge_level'] == 'explicit'

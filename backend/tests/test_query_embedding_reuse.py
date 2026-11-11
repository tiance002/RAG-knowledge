"""P6V1 offline wire contracts: real embedding HTTP construction, no provider."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import math
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlmodel import Session, SQLModel, create_engine
from sqlalchemy.pool import StaticPool

from app.models import CustomModel, DocumentChunk, FileDocument, KnowledgeBase, ModelType
from app.services import embedding, llm, retrieval
from app.services.bm25 import BM25Index
from app.services.model_bindings import ModelBinding, ModelBindingError
from app.services.task_effect_context import effect_scope, has_active_effect_scope


VECTOR = [0.25, -0.5, 0.75]
QUERY = 'public question'


@pytest.fixture
def wire(monkeypatch):
    records, events = [], []
    state = SimpleNamespace(responder=None, records=records, events=events)

    async def respond(request):
        records.append({'method': request.method, 'url': str(request.url),
                        'authorization': request.headers['authorization'],
                        'body': json.loads(request.content)})
        if state.responder:
            result = state.responder(request)
            return await result if hasattr(result, '__await__') else result
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': VECTOR}]})

    @asynccontextmanager
    async def request(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['stage'] == 'embedding'
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), trust_env=False) as client:
            yield client

    monkeypatch.setattr(embedding, 'provider_request', request)
    monkeypatch.setattr(llm, 'event', lambda name, **data: events.append((name, data)))
    monkeypatch.setattr(embedding, 'event', lambda name, **data: events.append((name, data)))
    monkeypatch.setattr(retrieval, 'event', lambda name, **data: events.append((name, data)))
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'false')
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.delenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', raising=False)
    return state


@pytest.fixture
def fixture_db():
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    index = BM25Index()
    with Session(engine) as db:
        db.add_all([CustomModel(id=ident, name=ident, model_type=ModelType.EMBEDDING,
            base_url='http://embedding.test/v1', api_key='synthetic-key-UNLOGGED',
            model_name='public-embedding', context_length=4096) for ident in ('m1', 'm2')])
        for kb in ('a', 'b', 'outside'):
            db.add(KnowledgeBase(id=kb, name='KB '+kb, embedding_model='m1', vlm_model=''))
            db.add(FileDocument(id='file-'+kb, kb_id=kb, name=kb+'.txt', local_path='public-fixture', size=0))
            db.add(DocumentChunk(id='chunk-'+kb, file_id='file-'+kb, content='public evidence '+kb))
        db.commit()
        for kb in ('a', 'b', 'outside'):
            index.index_chunk(db, 'chunk-'+kb, 'public evidence '+kb, kb)
        db.commit()
    yield engine, index
    engine.dispose()


class OwnedSession(Session):
    def _assert_owner(self):
        assert threading.get_ident() == self.owner_thread
        task = asyncio.current_task()
        if not hasattr(self, 'owner_task'):
            self.owner_task = task
        assert task is self.owner_task

    def get(self, *args, **kwargs):
        self._assert_owner()
        return super().get(*args, **kwargs)

    def exec(self, *args, **kwargs):
        self._assert_owner()
        return super().exec(*args, **kwargs)


def service_for(fixture_db, *, mutate=False):
    _, index = fixture_db
    calls = []
    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    service.bm25_index = index

    def query(**kwargs):
        calls.append({**kwargs, 'query_vector': list(kwargs['query_vector'])})
        if mutate:
            kwargs['query_vector'][0] = 999
        return [{'id': 'chunk-'+kwargs['kb_id'], 'score': .8}]

    service.vector_store = SimpleNamespace(query=query)
    return service, calls


async def search(fixture_db, service, mode='vector', **kwargs):
    engine, _ = fixture_db
    with OwnedSession(engine) as db:
        db.owner_thread = threading.get_ident()
        return await service.search(db, kwargs.pop('query', QUERY), kwargs.pop('kb_ids', ['a', 'b']),
            search_mode=mode, top_k=2, score_threshold=0, use_cache=False,
            log_retrieval=False, **kwargs)


@pytest.mark.parametrize('mode', ['vector', 'hybrid'])
def test_actual_mocktransport_two_vs_one_posts_preserves_vectors_scopes_evidence_and_usage(wire, fixture_db, monkeypatch, mode, request):
    service, calls = service_for(fixture_db)
    expected, old_metrics = asyncio.run(search(fixture_db, service, mode))
    old_calls = list(calls)
    assert len(wire.records) == 2
    expected_wire = wire.records[0]
    assert wire.records == [expected_wire, expected_wire]
    assert expected_wire['method'] == 'POST'
    assert expected_wire['url'] == 'http://embedding.test/v1/embeddings'
    assert expected_wire['body'] == {'model': 'public-embedding', 'input': [QUERY]}
    wire.records.clear(); wire.events.clear(); calls.clear()
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    actual, new_metrics = asyncio.run(search(fixture_db, service, mode))
    assert wire.records == [expected_wire]
    assert calls == old_calls
    assert [row['kb_id'] for row in calls] == ['a', 'b']
    assert [row['allowed_ids'] for row in calls] == [['chunk-a'], ['chunk-b']]
    assert all(row['query_vector'] == VECTOR and row['allowed_build_ids'] == [] for row in calls)
    assert actual == expected and {row['id'] for row in actual} == {'chunk-a', 'chunk-b'}
    for field in ('vector_count', 'bm25_count', 'merged_count', 'final_count', 'fallback_used', 'degraded', 'retrieval_status'):
        assert new_metrics[field] == old_metrics[field]
    assert sum(name == 'provider.request_started' for name, _ in wire.events) == 1
    assert sum(name == 'embedding.query_reuse' for name, _ in wire.events) == 1
    assert 'synthetic-key-UNLOGGED' not in repr(wire.events) and QUERY not in repr(wire.events)
    request.node.user_properties.append(('p6_wire_ab', json.dumps({
        'mode': mode, 'baseline_post_count': 2, 'candidate_post_count': len(wire.records),
        'same_payload': wire.records == [expected_wire], 'same_kb_calls_and_vectors': calls == old_calls,
        'same_evidence': actual == expected, 'candidate_ids': [row['id'] for row in actual],
        'actual_provider': False, 'tokens': None, 'cost': None})))


def test_each_consumer_gets_an_independent_vector_copy(wire, fixture_db, monkeypatch):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, calls = service_for(fixture_db, mutate=True)
    asyncio.run(search(fixture_db, service))
    assert len(wire.records) == 1
    assert [row['query_vector'] for row in calls] == [VECTOR, VECTOR]


def test_reuse_does_not_duplicate_reported_provider_usage(wire, fixture_db, monkeypatch):
    from app.services import metrics
    usage_calls = []
    monkeypatch.setattr(metrics, 'record_provider_usage',
                        lambda stage, usage: usage_calls.append((stage, usage)))
    wire.responder = lambda request: httpx.Response(200, json={
        'data': [{'index': 0, 'embedding': VECTOR}], 'usage': {'total_tokens': 7}})
    service, _ = service_for(fixture_db)
    asyncio.run(search(fixture_db, service))
    assert len(wire.records) == 2 and usage_calls == [('embedding', {'total_tokens': 7})] * 2
    wire.records.clear(); usage_calls.clear()
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    asyncio.run(search(fixture_db, service))
    assert len(wire.records) == 1 and usage_calls == [('embedding', {'total_tokens': 7})]


@pytest.mark.parametrize('field,value', [
    ('base_url', 'http://second.test/v1'), ('api_key', 'second-synthetic-key'),
    ('model_name', 'different-model'), ('context_length', 8192), ('id', 'different-binding'),
])
def test_complete_binding_identity_does_not_merge_distinct_requests(wire, fixture_db, monkeypatch, field, value):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    engine, _ = fixture_db
    with Session(engine) as db:
        kb = db.get(KnowledgeBase, 'b'); kb.embedding_model = 'm2'; db.add(kb)
        model = db.get(CustomModel, 'm2')
        if field != 'id':
            setattr(model, field, value)
        db.add(model); db.commit()
    service, _ = service_for(fixture_db)
    asyncio.run(search(fixture_db, service))
    assert len(wire.records) == 2


@pytest.mark.parametrize('change', [
    {'base_url': 'http://second.test/v1'}, {'api_key': 'second-synthetic-key'},
    {'model_name': 'different-model'}, {'context_length': 8192},
    {'source': 'deployment'}, {'binding_id': 'different-binding'},
])
def test_each_identity_component_is_distinct_even_with_other_binding_fields_equal(wire, fixture_db, monkeypatch, change):
    from app.services import model_bindings
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    original = model_bindings.resolve_model
    count = 0
    def resolve(*args, **kwargs):
        nonlocal count
        binding = original(*args, **kwargs)
        count += 1
        return replace(binding, **change) if count == 2 else binding
    monkeypatch.setattr(model_bindings, 'resolve_model', resolve)
    service, _ = service_for(fixture_db)
    asyncio.run(search(fixture_db, service))
    assert count == 2 and len(wire.records) == 2
    if 'base_url' in change: assert wire.records[0]['url'] != wire.records[1]['url']
    if 'api_key' in change: assert wire.records[0]['authorization'] != wire.records[1]['authorization']
    if 'model_name' in change: assert wire.records[0]['body']['model'] != wire.records[1]['body']['model']


def test_exact_query_and_binding_resolution_still_apply_before_hit(wire, fixture_db, monkeypatch):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, _ = service_for(fixture_db)

    async def run():
        with OwnedSession(fixture_db[0]) as db:
            db.owner_thread = threading.get_ident(); memo = {}
            await service._embed_query_for_kb(db, 'a', QUERY, query_vectors=memo)
            await service._embed_query_for_kb(db, 'b', QUERY+' ', query_vectors=memo)
            model = db.get(CustomModel, 'm1'); model.is_active = False; db.flush()
            with pytest.raises(ModelBindingError):
                await service._embed_query_for_kb(db, 'b', QUERY, query_vectors=memo)
    asyncio.run(run())
    assert len(wire.records) == 2
    assert wire.records[1]['body']['input'] == [QUERY+' ']


def test_separate_searches_and_concurrent_requests_never_share_memos(wire, fixture_db, monkeypatch):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, calls = service_for(fixture_db)

    async def delay(request):
        await asyncio.sleep(.005)
        return httpx.Response(200, json={'data': [{'index': 0, 'embedding': VECTOR}]})
    wire.responder = delay

    async def run():
        first = await search(fixture_db, service)
        second = await search(fixture_db, service)
        both = await asyncio.gather(search(fixture_db, service), search(fixture_db, service))
        assert first[0] == second[0] == both[0][0] == both[1][0]
    asyncio.run(run())
    assert len(wire.records) == 4 and len(calls) == 8


@pytest.mark.parametrize('mode', ['vector', 'hybrid'])
def test_unknown_never_retries_or_queries_next_kb(wire, fixture_db, monkeypatch, mode):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, calls = service_for(fixture_db)
    def fail(request):
        raise httpx.ReadTimeout('synthetic-uncertain', request=request)
    wire.responder = fail
    with pytest.raises(httpx.ReadTimeout) as failure:
        asyncio.run(search(fixture_db, service, mode))
    assert failure.value.provider_status == 'UNKNOWN'
    assert len(wire.records) == 1 and calls == []


def test_cancelled_inflight_has_no_entry_and_does_not_affect_next_request(wire, fixture_db, monkeypatch):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, calls = service_for(fixture_db)

    async def run():
        entered = asyncio.Event()
        async def hold(request):
            entered.set(); await asyncio.Event().wait()
        wire.responder = hold
        task = asyncio.create_task(search(fixture_db, service))
        await entered.wait(); task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        assert len(wire.records) == 1 and calls == []
        wire.responder = None
        await search(fixture_db, service)
        assert len(wire.records) == 2 and len(calls) == 2
    asyncio.run(run())


@pytest.mark.parametrize('vector', [[], [float('nan')], [float('inf')], [True], ['0.5'], [10**400]])
def test_unsuitable_vectors_are_returned_unchanged_and_not_memoized(wire, fixture_db, monkeypatch, vector):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, _ = service_for(fixture_db)
    wire.responder = lambda request: httpx.Response(200, json={'data': [{'index': 0, 'embedding': vector}]})
    # HTTPX correctly refuses nonfinite JSON; use its content input to represent
    # malformed provider JSON without changing the service's current parsing.
    if any(type(value) is float and not math.isfinite(value) for value in vector):
        wire.responder = lambda request: httpx.Response(200, content=json.dumps({'data': [{'index': 0, 'embedding': vector}]}).encode())
    async def run():
        with OwnedSession(fixture_db[0]) as db:
            db.owner_thread = threading.get_ident(); memo = {}
            first = await service._embed_query_for_kb(db, 'a', QUERY, query_vectors=memo)
            second = await service._embed_query_for_kb(db, 'b', QUERY, query_vectors=memo)
            assert repr(first) == repr(second) == repr(vector) and memo == {}
    asyncio.run(run())
    assert len(wire.records) == 2


@pytest.mark.parametrize('url,model,query', [
    ('http://embedding.test/api/embeddings', 'ordinary', QUERY),
    ('http://embedding.test/api/embed', 'ordinary', QUERY),
    ('http://embedding.test/v1', 'doubao-model', QUERY),
    ('http://ark.volces.com/v1', 'ordinary', QUERY),
    ('http://embedding.test/v1', 'ordinary', '[图片: public.png]'),
    ('http://embedding.test/unknown', 'ordinary', QUERY),
    ('http://embedding.test/v1?api-version=x', 'ordinary', QUERY),
])
def test_unsupported_contracts_bypass_before_any_memo_hit(fixture_db, monkeypatch, url, model, query):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, _ = service_for(fixture_db)
    with Session(fixture_db[0]) as db:
        row = db.get(CustomModel, 'm1'); row.base_url = url; row.model_name = model; db.commit()
    send = AsyncMock(return_value=VECTOR)
    monkeypatch.setattr(embedding.EmbeddingService, 'embed_query', send)
    asyncio.run(search(fixture_db, service, query=query))
    assert send.await_count == 2


def test_active_effect_scope_preserves_every_effect_call(wire, fixture_db, monkeypatch):
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    service, _ = service_for(fixture_db)
    effect_calls = []
    async def runner(kind, identity, callback):
        effect_calls.append(kind); return await callback()
    async def run():
        assert not has_active_effect_scope()
        with effect_scope(runner):
            assert has_active_effect_scope()
            await search(fixture_db, service)
        assert not has_active_effect_scope()
    asyncio.run(run())
    assert effect_calls == ['embedding', 'embedding'] and len(wire.records) == 2


@pytest.mark.parametrize('change', [
    {'source': 'unknown'}, {'model_type': 'unknown'}, {'context_length': 0},
    {'binding_id': ''},
])
def test_unknown_configuration_and_non_none_ambiguous_effect_scope_are_not_eligible(change):
    binding = ModelBinding('m', 'ordinary', 'http://embedding.test/v1', 'synthetic',
                           model_type='embedding', context_length=4096)
    binding = replace(binding, **change)
    service = embedding.EmbeddingService(base_url=binding.base_url, api_key=binding.api_key, model=binding.model_name)
    assert retrieval._query_embedding_reuse_key(service, binding, QUERY) is None
    with effect_scope({}):
        assert has_active_effect_scope() is True
        assert retrieval._query_embedding_reuse_key(service, replace(binding,
            source='custom_model', model_type='embedding', context_length=4096,
            binding_id='m'), QUERY) is None
    assert has_active_effect_scope() is False


def test_flag_off_preserves_positional_embed_call_shape_and_fulltext_has_no_embedding(fixture_db, monkeypatch):
    monkeypatch.delenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', raising=False)
    service, _ = service_for(fixture_db)
    send = AsyncMock(return_value=VECTOR); service._embed_query_for_kb = send
    asyncio.run(search(fixture_db, service))
    assert len(send.call_args_list) == 2 and all(call.kwargs == {} for call in send.call_args_list)
    send.reset_mock()
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    asyncio.run(search(fixture_db, service, 'fulltext'))
    send.assert_not_awaited()

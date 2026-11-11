"""Local ingestion collection reuse; no Provider or external Chroma service."""
from collections import Counter
from copy import deepcopy
import hashlib
import importlib.util
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import chromadb
from chromadb.config import Settings
import pytest

from app.services import observability, vector_store


class Collection:
    def __init__(self, calls, seed=True, dimension=3):
        self.calls, self.dimension = calls, dimension
        self.rows = {'seed': ('public seed', {'file_id': 'seed'}, [1., 0., 0.])} if seed else {}
        self.fail = None

    def count(self):
        self.calls.append(('count', len(self.rows)))
        if self.fail == 'count': raise RuntimeError('count unavailable')
        return len(self.rows)

    def query(self, **kwargs):
        self.calls.append(('query', deepcopy(kwargs)))
        if self.fail == 'query': raise RuntimeError('query unavailable')
        if len(kwargs['query_embeddings'][0]) != self.dimension: raise ValueError('dimension mismatch')
        return {'ids': [['seed']]}

    def add(self, **kwargs):
        self.calls.append(('add', deepcopy(kwargs)))
        if self.fail == 'add': raise RuntimeError('add unavailable')
        if any(len(vector) != self.dimension for vector in kwargs['embeddings']): raise ValueError('dimension mismatch')
        for ident, content, metadata, vector in zip(kwargs['ids'], kwargs['documents'], kwargs['metadatas'], kwargs['embeddings']):
            self.rows[ident] = (content, metadata, vector)

    def get(self, ids, include):
        self.calls.append(('get', {'ids': list(ids), 'include': list(include)}))
        if self.fail == 'get': raise RuntimeError('get unavailable')
        indexes = {'documents': 0, 'metadatas': 1, 'embeddings': 2}
        found = [ident for ident in ids if ident in self.rows]
        return {'ids': found, **{key: [self.rows[ident][indexes[key]] for ident in found] for key in include}}


class Client:
    def __init__(self, collection=True, dimension=3):
        self.calls = []
        self.collection = Collection(self.calls, dimension=dimension) if collection else None

    def get_collection(self, **kwargs):
        self.calls.append(('lookup', kwargs))
        if self.collection is None: raise ValueError('Collection does not exist')
        return self.collection

    def get_or_create_collection(self, **kwargs):
        self.calls.append(('get_or_create', kwargs))
        if self.collection is None: self.collection = Collection(self.calls, seed=False)
        return self.collection

    def get_max_batch_size(self): return 1000


def service(client, cls=vector_store.VectorStoreService):
    result = cls.__new__(cls); result.client = client
    return result


def public_data(size):
    documents = [{'id': 'child-'+str(index), 'content': ('public synthetic child '+str(index)+' ') * 40,
                  'metadata': {'file_id': 'public-file', 'chunk_type': 'child', 'chunk_index': index}}
                 for index in range(size)]
    return documents, [[1., 0., .5] for _ in documents]


def write_and_read(writer, documents, vectors):
    writer.validate_dimension('kb', vectors[0])
    writer.add_documents('kb', documents, vectors)
    return writer.get_documents('kb', [row['id'] for row in documents], include=['documents', 'metadatas', 'embeddings'])


@pytest.fixture
def saved_ecs_class():
    path = Path(__file__).resolve().parents[2] / '.planning/2026-11-12-ingestion-write-optimization/vector-store/vector_store.before.py'
    if not path.is_file():
        pytest.skip('Historical ECS A/B source is not present; core operation/batching/failure/actual-Chroma tests remain enabled')
    assert hashlib.sha256(path.read_bytes()).hexdigest() == '944705f3134c4af5249e9bcd4997e70ab67f53aadca94ee2d9fa54556720561e'
    spec = importlib.util.spec_from_file_location('saved_ecs_vector_store', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module.VectorStoreService


def test_2105_child_vectors_keep_all_batches_and_full_readback_with_one_lookup(request):
    documents, vectors = public_data(2105)
    new_client = Client()
    store = service(new_client)
    with store.collection_operation('kb') as operation:
        actual = write_and_read(operation, documents, vectors)
    assert actual['ids'] == [row['id'] for row in documents]
    assert actual['documents'] == [row['content'] for row in documents]
    assert actual['metadatas'] == [row['metadata'] for row in documents] and actual['embeddings'] == vectors
    new_counts = Counter(name for name, _ in new_client.calls)
    assert new_counts == {'lookup': 1, 'count': 1, 'query': 1, 'add': 3, 'get': 3}
    assert [len(data['ids']) for name, data in new_client.calls if name in ('add', 'get')] == [1000, 1000, 105] * 2
    request.node.user_properties.append(('collection_operation_core', str({
        'vectors': 2105, 'calls': dict(new_counts), 'complete_readback': True})))


def test_public_standalone_api_keeps_saved_ecs_calls_and_results(saved_ecs_class, request):
    # Only this historical comparison needs old source. The independent core
    # 2105-vector test above always runs on a fresh clone without that artifact.
    for size in (3, 2105):
        documents, vectors = public_data(size)
        old_client, standalone_client, operation_client = Client(), Client(), Client()
        expected = write_and_read(service(old_client, saved_ecs_class), documents, vectors)
        assert write_and_read(service(standalone_client), documents, vectors) == expected
        assert old_client.calls == standalone_client.calls
        with service(operation_client).collection_operation('kb') as operation:
            assert write_and_read(operation, documents, vectors) == expected
        if size == 2105:
            old_counts = Counter(name for name, _ in old_client.calls)
            new_counts = Counter(name for name, _ in operation_client.calls)
            assert old_counts == {'lookup': 2, 'get_or_create': 1, 'count': 1, 'query': 1, 'add': 3, 'get': 3}
            assert new_counts == {'lookup': 1, 'count': 1, 'query': 1, 'add': 3, 'get': 3}
            request.node.user_properties.append(('collection_operation_ab', str({
                'vectors': 2105, 'old': dict(old_counts), 'new': dict(new_counts), 'full_readback_equal': True})))


def test_missing_collection_is_not_created_until_add_and_get_does_not_create():
    client = Client(collection=False); store = service(client)
    documents, vectors = public_data(2)
    with store.collection_operation('kb') as operation:
        operation.validate_dimension('kb', vectors[0])
        assert client.collection is None
        with pytest.raises(RuntimeError, match='disappeared'): operation.get_documents('kb', ['child-0'])
        operation.add_documents('kb', documents, vectors)
        assert operation.get_documents('kb', ['child-0'])['documents'] == [documents[0]['content']]
    counts = Counter(name for name, _ in client.calls)
    assert counts == {'lookup': 1, 'get_or_create': 1, 'add': 1, 'get': 1}


def test_each_file_operation_checks_fresh_handle_and_dimension_again():
    client = Client(); store = service(client); documents, vectors = public_data(1)
    with store.collection_operation('kb') as operation: write_and_read(operation, documents, vectors)
    client.collection = Collection(client.calls, dimension=4)
    before = deepcopy(client.collection.rows)
    with store.collection_operation('kb') as operation:
        with pytest.raises(ValueError, match='explicit migration'): operation.validate_dimension('kb', vectors[0])
    assert client.collection.rows == before
    counts = Counter(name for name, _ in client.calls)
    assert counts['lookup'] == counts['count'] == counts['query'] == 2
    assert counts['add'] == counts['get'] == 1


def test_empty_existing_collection_counts_but_does_not_query_and_second_file_does():
    client = Client(); client.collection.rows.clear(); store = service(client)
    documents, vectors = public_data(1)
    with store.collection_operation('kb') as operation: write_and_read(operation, documents, vectors)
    with store.collection_operation('kb') as operation: operation.validate_dimension('kb', vectors[0])
    counts = Counter(name for name, _ in client.calls)
    assert counts['count'] == 2 and counts['query'] == 1 and counts['lookup'] == 2


def test_deleted_handle_is_not_silently_replaced_for_write_or_verification():
    client = Client(); store = service(client); documents, vectors = public_data(1)
    with store.collection_operation('kb') as operation:
        operation.validate_dimension('kb', vectors[0])
        old = client.collection
        client.collection = Collection(client.calls)
        old.fail = 'add'
        with pytest.raises(RuntimeError, match='add unavailable'): operation.add_documents('kb', documents, vectors)
        old.fail = 'get'
        with pytest.raises(RuntimeError, match='get unavailable'): operation.get_documents('kb', ['child-0'])
    assert set(client.collection.rows) == {'seed'}
    assert Counter(name for name, _ in client.calls)['lookup'] == 1
    assert not any(name == 'get_or_create' for name, _ in client.calls)


def test_scope_cannot_escape_cross_kb_thread_exit_or_reenter():
    client = Client(); operation = service(client).collection_operation('kb')
    with pytest.raises(RuntimeError, match='not active'): operation.get_documents('kb', ['child-0'])
    with operation:
        with pytest.raises(ValueError, match='cross knowledge'): operation.get_documents('outside', ['child-0'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(operation.get_documents, 'kb', ['child-0'])
            with pytest.raises(RuntimeError, match='owner thread'): pending.result()
    with pytest.raises(RuntimeError, match='not active'): operation.validate_dimension('kb', [1., 0., .5])
    with pytest.raises(RuntimeError, match='re-entered'):
        with operation: pass
    assert client.calls == [] and operation._collection is None


@pytest.mark.parametrize('failure', ['count', 'query', 'add', 'get'])
def test_storage_failure_is_propagated_once_and_timing_remains_failed(monkeypatch, failure):
    events = []
    monkeypatch.setattr(observability, 'event', lambda name, **data: events.append((name, data)))
    client = Client(); client.collection.fail = failure; store = service(client)
    documents, vectors = public_data(1)
    with pytest.raises(RuntimeError, match=failure+' unavailable'):
        with store.collection_operation('kb') as operation: write_and_read(operation, documents, vectors)
    assert sum(name == failure for name, _ in client.calls) == 1
    stages = [data for name, data in events if name == 'performance.stage']
    assert any(row['execution_status'] == 'FAILED' for row in stages)
    assert all(row['duration_ms'] >= 0 for row in stages)
    assert not any(name == 'get_or_create' for name, _ in client.calls)


@pytest.mark.parametrize('failure', ['add', 'get'])
def test_mid_batch_failure_returns_no_partial_success_or_retry(failure):
    client = Client(); store = service(client); documents, vectors = public_data(2105)
    original = getattr(client.collection, failure)
    invocations = []
    def fail_second(**kwargs):
        invocations.append(list(kwargs['ids']))
        if len(invocations) == 2:
            raise RuntimeError('second batch unavailable')
        return original(**kwargs)
    setattr(client.collection, failure, fail_second)
    with pytest.raises(RuntimeError, match='second batch unavailable'):
        with store.collection_operation('kb') as operation: write_and_read(operation, documents, vectors)
    assert [len(ids) for ids in invocations] == [1000, 1000]
    counts = Counter(name for name, _ in client.calls)
    assert counts['lookup'] == counts['count'] == counts['query'] == 1
    assert counts['get_or_create'] == 0
    if failure == 'add':
        assert len(client.collection.rows) == 1001 and counts['get'] == 0
    else:
        assert len(client.collection.rows) == 2106 and counts['add'] == 3


def test_lookup_error_is_not_reinterpreted_as_missing_or_retried():
    client = Client(); store = service(client)
    def unavailable(**kwargs):
        client.calls.append(('lookup', kwargs))
        raise RuntimeError('metadata unavailable')
    client.get_collection = unavailable
    with pytest.raises(RuntimeError, match='metadata unavailable'):
        with store.collection_operation('kb') as operation: operation.validate_dimension('kb', [1., 0., .5])
    assert Counter(name for name, _ in client.calls) == {'lookup': 1}


def test_timing_stages_and_vector_counts_do_not_emit_content(monkeypatch):
    events = []
    monkeypatch.setattr(observability, 'event', lambda name, **data: events.append((name, data)))
    documents, vectors = public_data(2)
    with service(Client()).collection_operation('kb') as operation: write_and_read(operation, documents, vectors)
    names = {data['stage_name'] for name, data in events if name == 'performance.stage'}
    assert names == {'ingest_chroma_collection_get', 'ingest_chroma_count', 'ingest_chroma_dimension_query', 'ingest_chroma_add', 'ingest_chroma_get'}
    counts = [data for name, data in events if name == 'ingestion.chroma.counts']
    assert {'operation': 'add', 'vector_count': 2, 'document_count': 2} in counts
    assert {'operation': 'get_returned', 'vector_count': 2} in counts
    assert 'public synthetic' not in repr(events) and 'public-file' not in repr(events) and 'child-0' not in repr(events)


def test_actual_chroma_1100_vectors_complete_across_batches_without_embedding_model():
    real = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    kb = 'local_ingest_'+uuid4().hex
    name = vector_store.VectorStoreService._get_collection_name(kb)
    collection = real.get_or_create_collection(name=name)
    collection.add(ids=['seed'], documents=['public seed'], embeddings=[[1., 0., 0.]], metadatas=[{'file_id': 'seed'}])
    calls = Counter()
    class BoundCollection:
        def count(self): calls['count'] += 1; return collection.count()
        def query(self, **kwargs): calls['query'] += 1; return collection.query(**kwargs)
        def add(self, **kwargs): calls['add'] += 1; return collection.add(**kwargs)
        def get(self, **kwargs): calls['get'] += 1; return collection.get(**kwargs)
    class BoundClient:
        def get_collection(self, **kwargs):
            assert kwargs['name'] == name; calls['lookup'] += 1
            assert str(real.get_collection(**kwargs).id) == str(collection.id)
            return BoundCollection()
        def get_or_create_collection(self, **kwargs): raise AssertionError('unexpected repeated metadata/create')
        def get_max_batch_size(self): return real.get_max_batch_size()
    documents, vectors = public_data(1100)
    try:
        with service(BoundClient()).collection_operation(kb) as operation:
            operation.validate_dimension(kb, vectors[0])
            operation.add_documents(kb, documents, vectors)
            fetched = operation.get_documents(kb, [row['id'] for row in documents], include=['documents', 'metadatas', 'embeddings'])
        found = {ident: (content, metadata, list(vector)) for ident, content, metadata, vector in zip(
            fetched['ids'], fetched['documents'], fetched['metadatas'], fetched['embeddings'])}
        assert len(found) == 1100
        assert all(found[row['id']] == (row['content'], row['metadata'], vector) for row, vector in zip(documents, vectors))
        assert calls == {'lookup': 1, 'count': 1, 'query': 1, 'add': 2, 'get': 2}
    finally:
        real.delete_collection(name=name)

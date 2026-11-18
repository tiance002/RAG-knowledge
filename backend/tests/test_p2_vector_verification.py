"""Bounded complete vector verification, using synthetic data only."""
from collections import Counter
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import weakref
from uuid import uuid4

import chromadb
from chromadb.config import Settings
import pytest

from app.services import vector_store


class ReturnedVector(list):
    pass


class Collection:
    def __init__(self, calls, dimension=3):
        self.calls = calls
        self.dimension = dimension
        self.rows = {'seed': ('seed', {'file_id': 'seed'}, [1., 0., 0.])}
        self.alive = True
        self.failure = None
        self.damage = None
        self.reads = 0
        self.require_release = True
        self.last_vectors = []

    def _check(self, operation):
        self.calls.append((operation, None))
        if not self.alive:
            raise RuntimeError('collection unavailable')
        if self.failure == operation:
            raise RuntimeError(operation + ' unavailable')

    def count(self):
        self._check('count')
        return len(self.rows)

    def query(self, query_embeddings, **kwargs):
        self._check('query')
        if len(query_embeddings[0]) != self.dimension:
            raise ValueError('dimension mismatch')
        return {'ids': [['seed']]}

    def add(self, **kwargs):
        self._check('add')
        self.calls[-1] = ('add', list(kwargs['ids']))
        if any(len(vector) != self.dimension for vector in kwargs['embeddings']):
            raise ValueError('dimension mismatch')
        for ident, content, metadata, vector in zip(
                kwargs['ids'], kwargs['documents'], kwargs['metadatas'], kwargs['embeddings']):
            self.rows[ident] = (content, deepcopy(metadata), list(vector))

    def get(self, ids, include):
        self._check('get')
        self.calls[-1] = ('get', list(ids))
        if self.require_release:
            assert all(reference() is None for reference in self.last_vectors), 'prior response embeddings retained'
        self.reads += 1
        keys = {'documents': 0, 'metadatas': 1, 'embeddings': 2}
        # Chroma does not promise response order equals request order.
        found = list(reversed([ident for ident in ids if ident in self.rows]))
        result = {'ids': found, **{key: [deepcopy(self.rows[ident][keys[key]]) for ident in found]
                                  for key in include}}
        if 'embeddings' in include:
            result['embeddings'] = [ReturnedVector(vector) for vector in result['embeddings']]
            self.last_vectors = [weakref.ref(vector) for vector in result['embeddings']]
        if self.reads == 2 and self.damage:
            self.damage(result)
        return result


class Client:
    def __init__(self, maximum=2):
        self.calls = []
        self.collection = Collection(self.calls)
        self.maximum = maximum
        self.lookup_failure = False

    def get_collection(self, **kwargs):
        self.calls.append(('lookup', None))
        if self.lookup_failure:
            raise RuntimeError('lookup unavailable')
        if self.collection is None:
            raise ValueError('Collection does not exist')
        return self.collection

    def get_or_create_collection(self, **kwargs):
        self.calls.append(('create', None))
        if self.collection is None:
            self.collection = Collection(self.calls)
            self.collection.rows.clear()
        return self.collection

    def get_max_batch_size(self):
        return self.maximum


def make_service(client):
    store = vector_store.VectorStoreService.__new__(vector_store.VectorStoreService)
    store.client = client
    return store


def data(size=5):
    documents = [{'id': f'child-{i}', 'content': f'public child {i}',
                  'metadata': {'build_id': 'staging-public', 'file_id': 'public-file', 'chunk_index': i}}
                 for i in range(size)]
    vectors = [[float(i) / 10, 0., 1.] for i in range(size)]
    return documents, vectors, [document['id'] for document in documents]


@pytest.mark.parametrize('size, batches', [(0, []), (1, [1]), (2, [2]), (3, [2, 1]), (4, [2, 2]), (5, [2, 2, 1])])
@pytest.mark.parametrize('scoped', [False, True])
def test_every_expected_field_verified_in_bounded_batches_and_responses_released(size, batches, scoped):
    documents, vectors, ids = data(size)
    client = Client()
    store = make_service(client)
    if scoped:
        with store.collection_operation('kb') as writer:
            writer.validate_dimension('kb', [1., 0., 0.])
            writer.add_documents('kb', documents, vectors)
            assert writer.verify_documents('kb', documents, vectors, list(reversed(ids))) is None
        assert Counter(name for name, _ in client.calls) == Counter({
            'lookup': 1, 'count': 1, 'query': 1, 'add': len(batches), 'get': len(batches)})
    else:
        store.add_documents('kb', documents, vectors)
        assert store.verify_documents('kb', documents, vectors, list(reversed(ids))) is None
    assert [len(ids) for name, ids in client.calls if name == 'add'] == batches
    assert [len(ids) for name, ids in client.calls if name == 'get'] == batches
    assert all(reference() is None for reference in client.collection.last_vectors)
    assert {key: client.collection.rows[key] for key in ids} == {
        row['id']: (row['content'], row['metadata'], vector) for row, vector in zip(documents, vectors)}


def damage_response(kind):
    def damage(result):
        if kind == 'missing':
            for key in result:
                result[key].pop()
        elif kind == 'wrong-id':
            result['ids'][-1] = 'outside-manifest'
        elif kind == 'duplicate-id':
            result['ids'][-1] = result['ids'][0]
        elif kind == 'document':
            result['documents'][-1] += ' corrupted'
        elif kind == 'metadata':
            result['metadatas'][-1]['build_id'] = 'wrong-build'
        elif kind == 'embedding':
            result['embeddings'][-1][0] += 1
        elif kind == 'dimension':
            result['embeddings'][-1].pop()
        elif kind == 'nan':
            result['embeddings'][-1][0] = float('nan')
        elif kind == 'nonnumeric':
            result['embeddings'][-1][0] = 'bad-vector'
        elif kind.startswith('short-'):
            result[kind.removeprefix('short-')].pop()
        elif kind.startswith('null-'):
            result[kind.removeprefix('null-')] = None
        elif kind == 'extra':
            for key in result:
                result[key].append(result[key][-1])
    return damage


@pytest.mark.parametrize('kind', [
    'missing', 'wrong-id', 'duplicate-id', 'document', 'metadata', 'embedding', 'dimension', 'nan',
    'nonnumeric', 'short-documents', 'short-metadatas', 'short-embeddings', 'null-documents',
    'null-metadatas', 'null-embeddings', 'extra'])
def test_second_batch_damage_fails_complete_verification_without_reading_tail(kind):
    client = Client()
    documents, vectors, ids = data()
    store = make_service(client)
    client.collection.damage = damage_response(kind)
    with store.collection_operation('kb') as writer:
        writer.validate_dimension('kb', vectors[0])
        writer.add_documents('kb', documents, vectors)
        with pytest.raises(RuntimeError, match='BUILD_VECTOR_VERIFICATION_FAILED'):
            writer.verify_documents('kb', documents, vectors, ids)
    assert [ids for name, ids in client.calls if name == 'get'] == [ids[:2], ids[2:4]]
    assert Counter(name for name, _ in client.calls)['add'] == 3


@pytest.mark.parametrize('mismatch', ['short-vectors', 'short-ids', 'wrong-id', 'duplicate-ids', 'duplicate-documents'])
def test_manifest_mismatch_fails_before_any_read(mismatch):
    client = Client()
    documents, vectors, ids = data()
    if mismatch == 'short-vectors':
        vectors.pop()
    elif mismatch == 'short-ids':
        ids.pop()
    elif mismatch == 'wrong-id':
        ids[-1] = 'outside-manifest'
    elif mismatch == 'duplicate-ids':
        ids[-1] = ids[0]
    else:
        documents[-1] = documents[0]
        ids[-1] = ids[0]
    with pytest.raises(RuntimeError, match='BUILD_VECTOR_VERIFICATION_FAILED'):
        make_service(client).verify_documents('kb', documents, vectors, ids)
    assert Counter(name for name, _ in client.calls) == {'lookup': 1}


def test_public_get_documents_still_aggregates_all_requested_fields():
    documents, vectors, ids = data()
    client = Client()
    client.collection.require_release = False
    store = make_service(client)
    store.add_documents('kb', documents, vectors)
    actual = store.get_documents('kb', ids, include=['documents', 'metadatas', 'embeddings'])
    observed = {ident: (content, metadata, vector) for ident, content, metadata, vector in zip(
        actual['ids'], actual['documents'], actual['metadatas'], actual['embeddings'])}
    assert observed == {row['id']: (row['content'], row['metadata'], vector)
                        for row, vector in zip(documents, vectors)}
    assert [len(batch) for name, batch in client.calls if name == 'get'] == [2, 2, 1]


@pytest.mark.parametrize('failure', ['lookup', 'count', 'query', 'add', 'get'])
def test_storage_errors_propagate_without_retry_or_successful_verification(failure):
    documents, vectors, ids = data()
    client = Client()
    if failure == 'lookup':
        client.lookup_failure = True
    else:
        client.collection.failure = failure
    with pytest.raises(RuntimeError, match=failure + ' unavailable'):
        with make_service(client).collection_operation('kb') as writer:
            writer.validate_dimension('kb', vectors[0])
            writer.add_documents('kb', documents, vectors)
            writer.verify_documents('kb', documents, vectors, ids)
    assert Counter(name for name, _ in client.calls)[failure] == 1
    assert not any(name == 'create' for name, _ in client.calls)


def test_second_read_storage_error_is_propagated_without_retry():
    documents, vectors, ids = data()
    client = Client()
    store = make_service(client)
    original = client.collection.get
    reads = []

    def failing_get(**kwargs):
        reads.append(list(kwargs['ids']))
        if len(reads) == 2:
            raise RuntimeError('second get unavailable')
        return original(**kwargs)

    client.collection.get = failing_get
    store.add_documents('kb', documents, vectors)
    with pytest.raises(RuntimeError, match='second get unavailable'):
        store.verify_documents('kb', documents, vectors, ids)
    assert reads == [ids[:2], ids[2:4]]


def test_missing_collection_is_not_created_by_verification():
    documents, vectors, ids = data()
    client = Client()
    client.collection = None
    store = make_service(client)
    with pytest.raises(RuntimeError, match='disappeared'):
        store.verify_documents('kb', documents, vectors, ids)
    with store.collection_operation('kb') as writer:
        writer.validate_dimension('kb', vectors[0])
        with pytest.raises(RuntimeError, match='disappeared'):
            writer.verify_documents('kb', documents, vectors, ids)
        writer.add_documents('kb', documents, vectors)
        writer.verify_documents('kb', documents, vectors, ids)
    assert Counter(name for name, _ in client.calls) == {'lookup': 2, 'create': 1, 'add': 3, 'get': 3}


@pytest.mark.parametrize('replacement', [False, True])
def test_deleted_or_recreated_collection_does_not_replace_operation_handle(replacement):
    documents, vectors, ids = data()
    client = Client()
    store = make_service(client)
    with store.collection_operation('kb') as writer:
        writer.validate_dimension('kb', vectors[0])
        writer.add_documents('kb', documents, vectors)
        old = client.collection
        old.alive = False
        client.collection = Collection(client.calls) if replacement else None
        with pytest.raises(RuntimeError, match='collection unavailable'):
            writer.verify_documents('kb', documents, vectors, ids)
        with pytest.raises(RuntimeError, match='collection unavailable'):
            writer.add_documents('kb', documents, vectors)
    assert Counter(name for name, _ in client.calls)['lookup'] == 1
    assert not any(name == 'create' for name, _ in client.calls)
    if replacement:
        assert set(client.collection.rows) == {'seed'}


def test_every_new_operation_resolves_and_validates_again_and_rejects_changed_dimension():
    documents, vectors, ids = data(1)
    client = Client()
    store = make_service(client)
    for _ in range(2):
        with store.collection_operation('kb') as writer:
            writer.validate_dimension('kb', vectors[0])
            writer.add_documents('kb', documents, vectors)
            writer.verify_documents('kb', documents, vectors, ids)
    client.collection = Collection(client.calls, dimension=4)
    with store.collection_operation('kb') as writer:
        with pytest.raises(ValueError, match='explicit migration'):
            writer.validate_dimension('kb', vectors[0])
    counts = Counter(name for name, _ in client.calls)
    assert counts == {'lookup': 3, 'count': 3, 'query': 3, 'add': 2, 'get': 2}


def test_verification_cannot_escape_kb_thread_or_closed_context():
    documents, vectors, ids = data()
    client = Client()
    operation = make_service(client).collection_operation('kb')
    with operation:
        with pytest.raises(ValueError, match='cross knowledge'):
            operation.verify_documents('other-kb', documents, vectors, ids)
        with ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(operation.verify_documents, 'kb', documents, vectors, ids)
            with pytest.raises(RuntimeError, match='owner thread'):
                result.result()
    with pytest.raises(RuntimeError, match='not active'):
        operation.verify_documents('kb', documents, vectors, ids)
    assert client.calls == []


def test_add_preflight_preserves_failure_before_writing_malformed_later_document():
    documents, vectors, _ = data()
    documents[-1].pop('content')
    client = Client()
    with pytest.raises(KeyError, match='content'):
        make_service(client).add_documents('kb', documents, vectors)
    assert not any(name == 'add' for name, _ in client.calls)


def test_embedding_float_tolerance_matches_existing_verifier():
    documents, vectors, ids = data()
    client = Client()
    store = make_service(client)
    store.add_documents('kb', documents, vectors)
    client.collection.rows[ids[-1]][2][0] += 1e-8
    store.verify_documents('kb', documents, vectors, ids)
    client.collection.rows[ids[-1]][2][0] += 1e-3
    with pytest.raises(RuntimeError, match='BUILD_VECTOR_VERIFICATION_FAILED'):
        store.verify_documents('kb', documents, vectors, ids)


def test_real_local_chroma_full_readback_and_collection_recreation():
    # In-memory client, explicit embeddings, unique synthetic collection: no
    # persistent user index, external server, embedding function or model call.
    client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    store = make_service(client)
    store._batch_size = lambda: 2
    kb_id = str(uuid4())
    documents, vectors, ids = data()
    collection_name = store._get_collection_name(kb_id)
    try:
        with store.collection_operation(kb_id) as writer:
            writer.validate_dimension(kb_id, vectors[0])
            writer.add_documents(kb_id, documents, vectors)
            writer.verify_documents(kb_id, documents, vectors, ids)
            client.delete_collection(collection_name)
            client.create_collection(collection_name)
            with pytest.raises(chromadb.errors.NotFoundError):
                writer.verify_documents(kb_id, documents, vectors, ids)
        with store.collection_operation(kb_id) as writer:
            with pytest.raises(RuntimeError, match='BUILD_VECTOR_VERIFICATION_FAILED'):
                writer.verify_documents(kb_id, documents, vectors, ids)
            writer.add_documents(kb_id, documents, vectors)
            writer.verify_documents(kb_id, documents, vectors, ids)
    finally:
        client.delete_collection(collection_name)

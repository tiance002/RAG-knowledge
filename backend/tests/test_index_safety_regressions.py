import importlib
import json
import sys
import types

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import ChunkIndex, DocumentChunk, FileDocument, IndexStats, KnowledgeBase
from app.services.bm25 import BM25Index, tokenize


@pytest.fixture(autouse=True)
def isolated_runtime_and_models(monkeypatch):
    # Provider identities are fixture data, never loaded from a deployment DB.
    from app.services import runtime_settings, model_bindings
    monkeypatch.setattr(runtime_settings, "get_runtime_settings", lambda *a: types.SimpleNamespace(
        chunk_size=500, chunk_overlap=50, default_vlm_model=None))
    monkeypatch.setattr(model_bindings, "resolve_model", lambda session, role, identity, **kw:
        None if role == "vlm" else types.SimpleNamespace(base_url="http://fixture.invalid", api_key="fixture",
            model_name=identity, context_length=8196))


def test_chinese_fallback_and_stats_are_idempotent(monkeypatch):
    monkeypatch.setitem(sys.modules, 'jieba', None)
    assert tokenize('量子计算')
    engine = create_engine('sqlite:///:memory:')
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        index = BM25Index()
        index.index_chunk(session, 'chunk-a', '量子计算', 'kb')
        session.commit()
        index.index_chunk(session, 'chunk-a', '量子计算', 'kb')
        session.commit()
        assert session.get(IndexStats, 'kb').total_docs == 1
        index.delete_chunk_index(session, 'chunk-a', 'kb')
        session.commit()
        index.delete_chunk_index(session, 'chunk-a', 'kb')
        session.commit()
        stats = session.get(IndexStats, 'kb')
        assert stats.total_docs == 0
        assert json.loads(stats.term_doc_freq) == {}
        assert session.query(ChunkIndex).count() == 0


def test_dimension_failure_preserves_other_and_existing_vectors(monkeypatch):
    chroma = types.ModuleType('chromadb')
    chroma.Client = object
    chroma.errors = types.SimpleNamespace(NotFoundError=type('NotFoundError', (Exception,), {}))
    config = types.ModuleType('chromadb.config')
    config.Settings = object
    monkeypatch.setitem(sys.modules, 'chromadb', chroma)
    monkeypatch.setitem(sys.modules, 'chromadb.config', config)
    sys.modules.pop('app.services.vector_store', None)
    module = importlib.import_module('app.services.vector_store')

    class Collection:
        def __init__(self):
            self.rows = {'A': [1., 0.], 'B': [0., 1.]}
        def count(self): return len(self.rows)
        def add(self, ids, embeddings, **kwargs):
            if any(len(v) != 2 for v in embeddings): raise ValueError('dimension mismatch')
            self.rows.update(zip(ids, embeddings))
        def query(self, query_embeddings, **kwargs):
            if len(query_embeddings[0]) != 2: raise ValueError('dimension mismatch')
            return {'ids': [list(self.rows)], 'documents': [['A', 'B']],
                    'metadatas': [[{}, {}]], 'distances': [[0., 1.]]}
    class Client:
        def __init__(self): self.collection = Collection(); self.deleted = False
        def get_collection(self, **kwargs): return self.collection
        def get_or_create_collection(self, **kwargs): return self.collection
        def delete_collection(self, **kwargs): self.deleted = True; self.collection.rows.clear()
    store = module.VectorStoreService.__new__(module.VectorStoreService)
    store.client = Client()
    with pytest.raises(ValueError, match='explicit migration'):
        store.validate_dimension('kb', [1., 2., 3.])
    with pytest.raises(ValueError, match='explicit migration'):
        store.add_documents('kb', [{'id': 'B', 'content': 'new'}], [[1., 2., 3.]])
    assert not store.client.deleted
    assert store.client.collection.rows == {'A': [1., 0.], 'B': [0., 1.]}
    assert [x['id'] for x in store.query('kb', [1., 0.])] == ['A', 'B']
    def broken(**kwargs): raise RuntimeError('storage unavailable')
    store.client.get_collection = broken
    with pytest.raises(RuntimeError, match='storage unavailable'):
        store.query('kb', [1., 0.])


def test_vectorize_failure_is_terminal_and_stale_claim_cannot_write(monkeypatch):
    from app.models import ContentType, FileStatus
    from sqlalchemy.pool import StaticPool
    # This imports the real task with only the missing Chroma package substituted.
    chroma = types.ModuleType('chromadb')
    chroma.Client = object
    chroma.errors = types.SimpleNamespace()
    config = types.ModuleType('chromadb.config')
    config.Settings = object
    monkeypatch.setitem(sys.modules, 'chromadb', chroma)
    monkeypatch.setitem(sys.modules, 'chromadb.config', config)
    sys.modules.pop('app.services.vector_store', None)
    sys.modules.pop('app.tasks.vectorize_tasks', None)
    module = importlib.import_module('app.tasks.vectorize_tasks')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(module, 'engine', engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='model', vlm_model='none'))
        session.add(FileDocument(id='f', kb_id='kb', name='f', local_path='synthetic', size=1, status=FileStatus.EMBEDDING))
        session.add(DocumentChunk(id='c', file_id='f', content='synthetic', content_type=ContentType.TEXT))
        session.commit()
    with Session(engine) as session:
        revision = session.get(FileDocument, 'f').updated_at.isoformat()
    class FailingEmbedding:
        def __init__(self, **kwargs): pass
        async def embed_documents(self, *args, **kwargs): raise RuntimeError('synthetic outage')
    class FakeStore:
        def add_documents(self, **kwargs): pass
        def get_count(self, kb): return 1
    monkeypatch.setattr(module, 'EmbeddingService', FailingEmbedding)
    monkeypatch.setattr(module, 'VectorStoreService', FakeStore)
    task = module.vectorize_file_task
    assert task.max_retries == 0
    with pytest.raises(RuntimeError, match='synthetic outage'):
        task.run('f', expected_revision=revision)
    with Session(engine) as session:
        assert session.get(FileDocument, 'f').status == FileStatus.FAILED
    # A duplicate or stale delivery after terminal failure cannot resume work.
    assert task.run('f') == {'error': '文件状态不正确'}
    with Session(engine) as session:
        f = session.get(FileDocument, 'f'); f.status = FileStatus.READY; session.add(f); session.commit()
    assert task.run('f') == {'error': '文件状态不正确'}
    # New job has the same status but a different generation.
    with Session(engine) as session:
        f = session.get(FileDocument, 'f'); f.status = FileStatus.EMBEDDING
        f.updated_at = __import__('datetime').datetime(2030, 1, 1); session.add(f); session.commit()
    assert task.run('f', expected_revision=revision) == {'error': '任务已过期或缺少版本标识'}
    assert task.run('f') == {'error': '任务已过期或缺少版本标识'}
    with Session(engine) as session:
        assert session.get(FileDocument, 'f').status == FileStatus.EMBEDDING
        current_revision = session.get(FileDocument, 'f').updated_at.isoformat()
    writes = []
    class ReplacedDuringEmbedding:
        def __init__(self, **kwargs): pass
        async def embed_documents(self, *args, **kwargs):
            with Session(engine) as session:
                f = session.get(FileDocument, 'f')
                f.updated_at = __import__('datetime').datetime(2040, 1, 1)
                session.add(f); session.commit()
            return [[1., 0.]]
    class RecordingStore:
        def add_documents(self, **kwargs): writes.append(kwargs)
    monkeypatch.setattr(module, 'EmbeddingService', ReplacedDuringEmbedding)
    monkeypatch.setattr(module, 'VectorStoreService', RecordingStore)
    assert task.run('f', expected_revision=current_revision) == {'error': '任务已过期'}
    assert writes == []
    with Session(engine) as session:
        f = session.get(FileDocument, 'f')
        assert f.status == FileStatus.EMBEDDING
        assert f.updated_at == __import__('datetime').datetime(2040, 1, 1)
    sys.modules.pop('app.tasks.vectorize_tasks', None)
    sys.modules.pop('app.services.vector_store', None)


def test_reparse_and_submit_preflight_preserve_existing_data(monkeypatch):
    from app.models import FileStatus
    from sqlalchemy.pool import StaticPool
    chroma = types.ModuleType('chromadb'); chroma.Client = object; chroma.errors = types.SimpleNamespace()
    config = types.ModuleType('chromadb.config'); config.Settings = object
    monkeypatch.setitem(sys.modules, 'chromadb', chroma)
    monkeypatch.setitem(sys.modules, 'chromadb.config', config)
    sys.modules.pop('app.services.vector_store', None)
    sys.modules.pop('app.tasks.parse_tasks', None)
    module = importlib.import_module('app.tasks.parse_tasks')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(module, 'engine', engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='old', vlm_model='none'))
        for file_id in ('A', 'B'):
            session.add(FileDocument(id=file_id, kb_id='kb', name=file_id, local_path='synthetic', size=1, status=FileStatus.READY))
            session.add(DocumentChunk(id='chunk-'+file_id, file_id=file_id, content='old '+file_id))
        session.commit()
        bm25 = BM25Index()
        for file_id in ('A', 'B'):
            bm25.index_chunk(session, 'chunk-'+file_id, 'old '+file_id, 'kb')
        session.commit()
    vectors = {'A': [1., 0.], 'B': [0., 1.]}
    class Collection:
        def count(self): return len(vectors)
        def get(self, where, limit): return {'ids': [where['file_id']] if where['file_id'] in vectors else []}
    class Store:
        def _existing_collection(self, kb): return Collection()
        def validate_dimension(self, kb, vector):
            if len(vector) != 2: raise ValueError('dimension mismatch')
        def delete_by_file_id(self, kb, file_id): vectors.pop(file_id)
        def delete_ids(self, kb, ids): pass
        def query(self, kb, vector): return list(vectors)
    class WrongDimension:
        def __init__(self, **kwargs): pass
        async def embed_documents(self, *args, **kwargs): return [[1., 2., 3.]]
    monkeypatch.setattr(module, 'VectorStoreService', Store)
    monkeypatch.setattr(module, 'EmbeddingService', WrongDimension)
    with pytest.raises(ValueError, match='单文件入库更换'):
        module.process_submitted_chunks('B', [{'content': 'new B'}], vector_model='different')
    with pytest.raises(ValueError, match='手动重解析'):
        module.process_file_parsing('B', auto_vectorize=False)
    class Parser:
        def __init__(self, **kwargs): pass
        def parse(self, **kwargs):
            from app.services.parser import ParsedChunk
            return [ParsedChunk(content='new B', page_number=None, content_type='text')]
    monkeypatch.setattr(module, 'FileParser', Parser)
    with pytest.raises(ValueError, match='dimension mismatch'):
        module.process_file_parsing('B', auto_vectorize=True)
    with pytest.raises(ValueError, match='dimension mismatch'):
        module.process_submitted_chunks('B', [{'content': 'new B'}])
    class FailingEmbedding:
        def __init__(self, **kwargs): pass
        async def embed_documents(self, *args, **kwargs):
            raise RuntimeError('full embedding failed')
    monkeypatch.setattr(module, 'EmbeddingService', FailingEmbedding)
    with pytest.raises(RuntimeError, match='full embedding failed'):
        module.process_file_parsing('B', auto_vectorize=True)
    with pytest.raises(RuntimeError, match='full embedding failed'):
        module.process_submitted_chunks('B', [{'content': 'new B'}])
    class GoodEmbedding:
        def __init__(self, **kwargs): pass
        async def embed_documents(self, texts, **kwargs): return [[1., 0.] for _ in texts]
    class RejectedWriteStore(Store):
        def add_documents(self, **kwargs): raise RuntimeError('synthetic write rejected')
    class FullParser:
        def __init__(self, **kwargs): pass
        def parse(self, **kwargs):
            return [types.SimpleNamespace(content='new B', page_number=0, content_type='text', image_path='', token_count=2, metadata={})]
    monkeypatch.setattr(module, 'EmbeddingService', GoodEmbedding)
    monkeypatch.setattr(module, 'VectorStoreService', RejectedWriteStore)
    monkeypatch.setattr(module, 'FileParser', FullParser)
    with pytest.raises(RuntimeError, match='synthetic write rejected'):
        module.process_file_parsing('B', auto_vectorize=True)
    with Session(engine) as session:
        assert session.get(FileDocument, 'B').status == FileStatus.READY
        assert session.get(DocumentChunk, 'chunk-B').content == 'old B'
        assert session.get(DocumentChunk, 'chunk-A').content == 'old A'
        assert session.get(KnowledgeBase, 'kb').embedding_model == 'old'
        assert session.get(IndexStats, 'kb').total_docs == 2
    assert RejectedWriteStore().query('kb', [1., 0.]) == ['A', 'B']
    assert vectors == {'A': [1., 0.], 'B': [0., 1.]}
    sys.modules.pop('app.tasks.parse_tasks', None)
    sys.modules.pop('app.services.vector_store', None)


def test_reparse_and_manual_index_preserve_structure_and_bm25(monkeypatch):
    from app.models import FileStatus
    from sqlalchemy.pool import StaticPool
    chroma = types.ModuleType('chromadb'); chroma.Client = object; chroma.errors = types.SimpleNamespace()
    config = types.ModuleType('chromadb.config'); config.Settings = object
    monkeypatch.setitem(sys.modules, 'chromadb', chroma); monkeypatch.setitem(sys.modules, 'chromadb.config', config)
    sys.modules.pop('app.services.vector_store', None); sys.modules.pop('app.tasks.parse_tasks', None)
    module = importlib.import_module('app.tasks.parse_tasks')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine); monkeypatch.setattr(module, 'engine', engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='old', vlm_model='none'))
        session.add(FileDocument(id='f', kb_id='kb', name='f.txt', local_path='synthetic.txt', size=1, status=FileStatus.READY))
        session.add(DocumentChunk(id='old', file_id='f', content='old term')); session.commit()
        BM25Index().index_chunk(session, 'old', 'old term', 'kb'); session.commit()
    staged_documents = []
    class Store:
        def _existing_collection(self, kb): return None
        def validate_dimension(self, kb, vector): pass
        def delete_by_file_id(self, kb, fid): pass
        def add_documents(self, **kwargs): staged_documents.extend(kwargs['documents'])
        def delete_ids(self, kb, ids): pass
    events = []
    monkeypatch.setattr(module, 'enabled', lambda: True)
    monkeypatch.setattr(module, 'event', lambda name, **data: events.append((name, data)))
    lineage = types.ModuleType('app.services.index_lineage')
    lineage.file_lineage = lambda file_id, path, chunks: {'file_id': file_id, 'chunk_count': len(chunks)}
    monkeypatch.setitem(sys.modules, 'app.services.index_lineage', lineage)
    class Embed:
        def __init__(self, **kwargs): pass
        async def embed_documents(self, texts, **kwargs): return [[1., 0.] for _ in texts]
    class Parser:
        def __init__(self, **kwargs): pass
        def parse(self, **kwargs):
            return [types.SimpleNamespace(content='new heading', page_number=0, content_type='text', image_path='', token_count=2,
                                          metadata={'parent_id': 'chunk_1', 'heading_level': 2, 'heading_text': 'Title', 'chunk_type': 'content'})]
    monkeypatch.setattr(module, 'VectorStoreService', Store); monkeypatch.setattr(module, 'EmbeddingService', Embed)
    monkeypatch.setattr(module, 'FileParser', Parser)
    assert module.process_file_parsing('f')['status'] == 'pending_confirm'
    with Session(engine) as session:
        assert session.get(IndexStats, 'kb').total_docs == 0
        assert session.get(ChunkIndex, 'old') is None
        chunk = session.exec(__import__('sqlmodel').select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        expected = (chunk.parent_id, chunk.heading_level, chunk.heading_text, chunk.chunk_type, chunk.token_count)
        data = [{k: getattr(chunk, k) for k in ('content', 'page_number', 'image_path', 'parent_id', 'heading_level', 'heading_text', 'chunk_type', 'token_count')}]
        data[0]['content_type'] = chunk.content_type.value
    assert module.process_submitted_chunks('f', data) is None
    for event_name in ('ingest.file_lineage', 'ingest.index_chunks', 'ingest.bm25_indexed', 'ingest.vector_indexed'):
        assert sum(name == event_name for name, _ in events) == 1
    assert next(v for name, v in events if name == 'ingest.vector_indexed')['model'] == 'old'
    assert staged_documents[-1]['metadata']['heading_text'] == 'Title'
    with Session(engine) as session:
        chunk = session.exec(__import__('sqlmodel').select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        assert (chunk.parent_id, chunk.heading_level, chunk.heading_text, chunk.chunk_type, chunk.token_count) == expected
        assert session.get(IndexStats, 'kb').total_docs == 1
    module.process_submitted_chunks('f', data)
    with Session(engine) as session:
        assert session.get(IndexStats, 'kb').total_docs == 1
        session.add(FileDocument(id='auto', kb_id='kb', name='auto.txt', local_path='synthetic.txt', size=1))
        session.commit()
    assert module.process_file_parsing('auto', auto_vectorize=True)['status'] == 'parsed'
    with Session(engine) as session:
        chunk = session.exec(__import__('sqlmodel').select(DocumentChunk).where(DocumentChunk.file_id == 'auto')).one()
        assert (chunk.parent_id, chunk.heading_level, chunk.heading_text, chunk.chunk_type, chunk.token_count) == expected
        assert session.get(IndexStats, 'kb').total_docs == 2
        session.add(FileDocument(id='retire', kb_id='kb', name='retire.txt', local_path='synthetic.txt', size=1, status=FileStatus.READY))
        session.add(DocumentChunk(id='retire-old', file_id='retire', content='old term'))
        session.commit()
    class RetirementFailure(Store):
        def delete_ids(self, kb, ids):
            if 'retire-old' in ids: raise RuntimeError('retire unavailable')
    monkeypatch.setattr(module, 'VectorStoreService', RetirementFailure)
    with pytest.raises(module.IndexRetirementError, match='retire unavailable'):
        module.process_file_parsing('retire', auto_vectorize=True)
    with Session(engine) as session:
        file_doc = session.get(FileDocument, 'retire')
        assert file_doc.status == FileStatus.PARSED
        assert '旧向量清理失败' in file_doc.error_message
        assert session.get(DocumentChunk, 'retire-old') is None
        session.add(FileDocument(id='rollback', kb_id='kb', name='rollback.txt', local_path='synthetic.txt', size=1, status=FileStatus.READY))
        session.add(DocumentChunk(id='rollback-old', file_id='rollback', content='old term'))
        session.commit()
    cleaned_ids = []
    class RollbackStore(Store):
        def delete_ids(self, kb, ids): cleaned_ids.extend(ids)
    class FailingBM25:
        def delete_chunk_index(self, session, chunk_id, kb_id): pass
        def index_chunk(self, session, chunk_id, content, kb_id): raise RuntimeError('sql indexing failed')
    monkeypatch.setattr(module, 'VectorStoreService', RollbackStore)
    monkeypatch.setattr(module, 'get_bm25_index', lambda: FailingBM25())
    with pytest.raises(RuntimeError, match='sql indexing failed'):
        module.process_submitted_chunks('rollback', data)
    with Session(engine) as session:
        file_doc = session.get(FileDocument, 'rollback')
        assert file_doc.status == FileStatus.READY
        assert '数据库提交失败' in file_doc.error_message
        assert session.get(DocumentChunk, 'rollback-old') is not None
    assert len(cleaned_ids) == 1 and cleaned_ids[0] != 'rollback-old'
    sys.modules.pop('app.tasks.parse_tasks', None); sys.modules.pop('app.services.vector_store', None)

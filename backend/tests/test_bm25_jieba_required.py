"""Real jieba parity and explicit failure instead of analyzer substitution."""
import ast
import asyncio
import builtins
from collections import Counter
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from app.models import KnowledgeBase, FileDocument, FileStatus, DocumentChunk, ChunkIndex, BM25Posting
from app.services import bm25


def block_jieba(monkeypatch):
    original = builtins.__import__
    def missing(name, *args, **kwargs):
        if name == 'jieba':
            raise ModuleNotFoundError("No module named 'jieba'")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', missing)


@pytest.fixture
def indexed(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'isolated.db'))
    SQLModel.metadata.create_all(engine)
    index = bm25.BM25Index(bigram_index_path='')
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb', name='kb', embedding_model='', vlm_model=''))
        s.add(FileDocument(id='file', kb_id='kb', name='file', local_path='unused', size=0, status=FileStatus.READY))
        s.flush()
        for cid, text in [('a', '北京大学研究生命科学 Alpha42'), ('b', '上海交通大学研究工程技术')]:
            s.add(DocumentChunk(id=cid, file_id='file', content=text))
            s.flush()
            index.index_chunk(s, cid, text, 'kb')
        s.commit()
    yield engine, index
    engine.dispose()


def test_real_jieba_used_identically_for_ingestion_and_query(indexed, monkeypatch):
    import jieba
    engine, index = indexed
    calls = []
    actual_cut = jieba.cut
    def record_cut(text):
        calls.append(text)
        return actual_cut(text)
    monkeypatch.setattr(jieba, 'cut', record_cut)
    query = '北京大学 Alpha42'
    query_tokens = bm25.tokenize(query)
    with Session(engine) as s:
        row = s.exec(select(ChunkIndex).where(ChunkIndex.chunk_id == 'a')).one()
        original = s.get(DocumentChunk, 'a').content
        assert json.loads(row.terms) == dict(Counter(bm25.tokenize(original)))
        prepared = index.prepare_file_index([{'id': 'replacement', 'content': original}])
        assert prepared[0]['frequencies'] == json.loads(row.terms)
        calls.clear()
        hits = index.search(s, query, 'kb', 10, live_only=True)
        assert hits and hits[0]['chunk_id'] == 'a'
        assert calls == ['北京大学  ']
        assert 'alpha42' in query_tokens
        assert index.search(s, '上海交通大学', 'kb', 10, live_only=True)[0]['chunk_id'] == 'b'


@pytest.mark.parametrize('text', ['北京大学', 'Alpha42', ''])
def test_missing_dependency_raises_even_for_english_or_empty_input(monkeypatch, text):
    block_jieba(monkeypatch)
    with pytest.raises(bm25.BM25TokenizerDependencyError, match='BM25_JIEBA_REQUIRED') as error:
        bm25.tokenize(text)
    assert isinstance(error.value.__cause__, ModuleNotFoundError)
    with pytest.raises(bm25.BM25TokenizerDependencyError, match='jieba==0.42.1'):
        bm25.BM25Index(bigram_index_path='')


def test_missing_dependency_does_not_change_existing_postings(indexed, monkeypatch):
    engine, index = indexed
    with Session(engine) as s:
        before = [(r.kb_id, r.term, r.index_id, r.tf) for r in s.exec(select(BM25Posting)).all()]
        old_terms = s.exec(select(ChunkIndex.terms).where(ChunkIndex.chunk_id == 'a')).one()
        block_jieba(monkeypatch)
        for operation in [lambda: index.index_chunk(s, 'a', '替换后的文本', 'kb'),
                lambda: index.prepare_file_index([{'id': 'a', 'content': '替换后的文本'}]),
                lambda: index.search(s, '北京大学', 'kb', 50, live_only=True)]:
            with pytest.raises(bm25.BM25TokenizerDependencyError, match='BM25_JIEBA_REQUIRED'):
                operation()
        assert [(r.kb_id, r.term, r.index_id, r.tf) for r in s.exec(select(BM25Posting)).all()] == before
        assert s.exec(select(ChunkIndex.terms).where(ChunkIndex.chunk_id == 'a')).one() == old_terms


def test_default_singleton_ignores_legacy_contrast_setting(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(bm25, 'settings', SimpleNamespace(
        BM25_K1=1.5, BM25_B=0.75, BM25_BIGRAM_INDEX_PATH='legacy-contrast.sqlite'))
    monkeypatch.setattr(bm25, '_bm25_index', None)
    assert bm25.get_bm25_index().bigram_index_path == ''
    block_jieba(monkeypatch)
    with pytest.raises(bm25.BM25TokenizerDependencyError, match='BM25_JIEBA_REQUIRED'):
        bm25.BM25Index()


def test_startup_checks_before_directory_or_database_writes(monkeypatch):
    # Execute the actual lifespan body with isolated globals, avoiding import of
    # the whole API or creation of a real application database in this check.
    path = Path(__file__).parents[1] / 'app/main.py'
    tree = ast.parse(path.read_text(encoding='utf-8'))
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'lifespan')
    writes = []
    scope = {'asynccontextmanager': asynccontextmanager, 'FastAPI': object,
             'ensure_directories': lambda: writes.append('dirs'), 'init_db': lambda: writes.append('db')}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(path), 'exec'), scope)
    block_jieba(monkeypatch)
    async def start():
        async with scope['lifespan'](object()):
            pytest.fail('startup must not finish without jieba')
    with pytest.raises(bm25.BM25TokenizerDependencyError, match='BM25_JIEBA_REQUIRED'):
        asyncio.run(start())
    assert writes == []


def test_alternate_analyzer_entry_stays_rejected():
    with pytest.raises(ValueError, match='BM25_ANALYZER_OVERRIDE_DISABLED'):
        bm25.BM25Index(bigram_index_path='retired-contrast.sqlite')

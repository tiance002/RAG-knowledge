import asyncio
import json
from types import SimpleNamespace

import sys
from unittest.mock import MagicMock
sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from app.services import retrieval, rerank


def test_vector_observation_preserves_results(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    log = tmp_path / 'events.jsonl'
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(log))
    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    service.vector_store = SimpleNamespace(query=lambda **kwargs: [{'id': 'a', 'score': .8, 'content': 'private text'}])
    async def embed(*args):
        return [1.0]
    service._embed_query_for_kb = embed
    service._enrich_results = lambda session, rows: rows
    session = SimpleNamespace(get=lambda *args: SimpleNamespace(name='kb'))
    result, _ = asyncio.run(service.search(session, 'private query', ['kb'], search_mode='vector', use_cache=False, log_retrieval=False))
    assert [r['id'] for r in result] == ['a']
    text = log.read_text(encoding='utf-8')
    assert 'retrieval.vector' in text and 'retrieval.after_dedup' in text
    assert 'private text' not in text and 'private query' not in text


def test_rerank_observes_batches_and_filter(monkeypatch, tmp_path):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'true')
    log = tmp_path / 'events.jsonl'
    monkeypatch.setenv('RAG_OBSERVATION_LOG', str(log))
    service = SimpleNamespace(rerank=lambda query, texts, model=None: [{'index': 0, 'score': .9}])
    rows = [{'id': 'a', 'content': 'secret body', 'score': .3}]
    result = rerank.batch_rerank('secret query', rows, service, model=None, score_threshold=.5)
    assert result[0]['id'] == 'a' and result[0]['rerank_score'] == .9
    text = log.read_text(encoding='utf-8')
    assert 'rerank.batch_input' in text and 'rerank.filtered' in text
    assert 'secret body' not in text and 'secret query' not in text


import asyncio
import sys
from unittest.mock import MagicMock

sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from unittest.mock import AsyncMock, Mock

import pytest

from app.services import observability
from app.services import retrieval


def service():
    svc = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    svc.cache = Mock()
    svc.vector_store = Mock()
    svc.bm25_index = Mock()
    svc._embed_query_for_kb = AsyncMock(return_value=[0.1])
    return svc


def run_search(svc, mode):
    return asyncio.run(svc.search(None, "query", ["kb"], search_mode=mode,
                                  use_cache=False, log_retrieval=False))


@pytest.mark.parametrize("error, expected", [(None, "NO_RESULTS"), (RuntimeError("offline"), "RETRIEVAL_ERROR")])
def test_fulltext_empty_and_failure(monkeypatch, error, expected):
    svc = service()
    svc._bm25_search = AsyncMock(side_effect=error, return_value=([], {}))
    events = []
    monkeypatch.setattr(retrieval, "event", lambda name, **kw: events.append((name, kw)))
    results, metrics = run_search(svc, "fulltext")
    assert results == []
    assert metrics['retrieval_status'] == expected
    assert [kw["outcome"] for name, kw in events if name == "retrieval.outcome"] == [expected]


def test_hybrid_empty_preserves_vector_fallback(monkeypatch):
    svc = service()
    svc.vector_store.query.return_value = []
    svc.bm25_index.search.return_value = []
    svc._vector_search = AsyncMock(return_value=([], {}))
    events = []
    monkeypatch.setattr(retrieval, "event", lambda name, **kw: events.append((name, kw)))
    results, metrics = run_search(svc, "hybrid")
    assert results == [] and metrics["fallback_used"] is True
    svc._vector_search.assert_awaited_once()
    assert [kw["outcome"] for name, kw in events if name == "retrieval.outcome"] == ["NO_RESULTS"]


def test_hybrid_partial_failure_is_unknown(monkeypatch):
    svc = service()
    svc.vector_store.query.side_effect = RuntimeError("vector offline")
    svc.bm25_index.search.return_value = []
    svc._vector_search = AsyncMock(return_value=([], {}))
    events = []
    monkeypatch.setattr(retrieval, "event", lambda name, **kw: events.append((name, kw)))
    results, _ = run_search(svc, "hybrid")
    assert results == []
    assert [kw["outcome"] for name, kw in events if name == "retrieval.outcome"] == ["UNKNOWN"]


def test_unknown_embedding_does_not_trigger_second_request():
    svc = service()
    svc._embed_query_for_kb = AsyncMock(side_effect=TimeoutError('uncertain'))
    svc._vector_search = AsyncMock()
    with pytest.raises(TimeoutError):
        run_search(svc, 'hybrid')
    svc._embed_query_for_kb.assert_awaited_once()
    svc._vector_search.assert_not_awaited()


def test_observability_event_failure_is_fail_open(monkeypatch):
    monkeypatch.setattr(observability, "enabled", lambda: True)
    monkeypatch.setattr(observability, "_record", Mock(side_effect=RuntimeError("trace unavailable")))
    token = observability._current.set({"trace_id": "test", "span_id": "test"})
    try:
        observability.event("retrieval.outcome", outcome="NO_RESULTS")
    finally:
        observability._current.reset(token)

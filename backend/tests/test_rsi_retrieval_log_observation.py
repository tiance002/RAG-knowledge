"""Offline attribution checks; only retrieval_logs is created in in-memory SQLite."""
import asyncio
from copy import deepcopy
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, create_engine, select

sys.modules.setdefault("chromadb", Mock())
sys.modules.setdefault("chromadb.config", Mock())
from app.models import RetrievalLog
from app.services import retrieval
from app.services.rerank import batch_rerank


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    RetrievalLog.__table__.create(engine)
    with Session(engine) as value:
        yield value
    engine.dispose()


@pytest.fixture
def events(monkeypatch):
    captured = []
    monkeypatch.setattr(retrieval, "event", lambda name, **data: captured.append((name, deepcopy(data))))
    return captured


def log_search(session):
    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    service._bm25_search = AsyncMock(side_effect=lambda *args: (
        [{"id": "seed", "content": "source evidence", "score": 0.8}],
        {"bm25_count": 1},
    ))
    return service, asyncio.run(service.search(session, "duplicate query", ["kb"], search_mode="fulltext"))


def test_updates_exact_committed_row_and_preserves_retrieval_summary(session, events, monkeypatch):
    _, (_, older) = log_search(session)
    _, (candidates, metrics) = log_search(session)
    assert older["retrieval_log_id"] != metrics["retrieval_log_id"]
    search_events = [data for name, data in events if name == "retrieval.metrics"]
    assert search_events[-1]["metrics"]["retrieval_log_id"] == metrics["retrieval_log_id"]
    row = session.get(RetrievalLog, metrics["retrieval_log_id"])
    before = (row.final_count, row.results_summary)
    output = [{"id": "seed", "rerank_status": "SCORED", "rerank_score": 0.9}]
    original = Mock(return_value=output)
    clock = iter([10.0, 10.025])
    monkeypatch.setattr(retrieval.time, "perf_counter", lambda: next(clock))

    actual = retrieval.observe_batch_rerank(session, metrics, original,
        query="duplicate query", candidates=candidates, top_k=3)

    assert actual is output
    original.assert_called_once_with(query="duplicate query", candidates=candidates, top_k=3)
    session.expire_all()
    row = session.get(RetrievalLog, metrics["retrieval_log_id"])
    assert row.rerank_enabled and row.rerank_count == 1
    assert row.rerank_latency_ms == pytest.approx(25.0)
    assert (row.final_count, row.results_summary) == before
    untouched = session.get(RetrievalLog, older["retrieval_log_id"])
    assert not untouched.rerank_enabled and untouched.rerank_count == 0
    terminal = events[-1][1]
    assert terminal["retrieval_log_id"] == row.id
    assert terminal["status"] == "RETURNED"
    assert terminal["persistence"]["status"] == "RETURNED"


def test_legitimately_filtered_empty_has_known_zero(session, events):
    _, (candidates, metrics) = log_search(session)
    provider = SimpleNamespace(rerank=Mock(return_value=[{"index": 0, "score": 0.1}]))
    result = retrieval.observe_batch_rerank(session, metrics, batch_rerank,
        query="q", candidates=candidates, rerank_service=provider, model=None,
        top_k=2, score_threshold=0.9)
    assert result == []
    assert metrics["rerank_count"] == 0 and metrics["rerank_status"] == "RETURNED"
    assert events[-1][1]["rerank_count"] == 0
    assert session.get(RetrievalLog, metrics["retrieval_log_id"]).rerank_enabled


@pytest.mark.parametrize("response", [[], [{"index": 50, "score": 0.9}], [{"index": 0, "score": None}]])
def test_unscored_fallback_has_unknown_count(session, events, response):
    _, (candidates, metrics) = log_search(session)
    provider = SimpleNamespace(rerank=Mock(return_value=response))
    result = retrieval.observe_batch_rerank(session, metrics, batch_rerank,
        query="q", candidates=candidates, rerank_service=provider, model=None, top_k=2)
    assert result[0]["rerank_status"] == "UNKNOWN"
    assert metrics["rerank_count"] is None and metrics["rerank_status"] == "UNKNOWN"
    assert events[-1][1]["rerank_count"] is None
    row = session.get(RetrievalLog, metrics["retrieval_log_id"])
    assert row.rerank_enabled
    assert row.rerank_count == 0  # Legacy default is not an observed zero.


def test_failed_provider_fallback_remains_unknown(session, events):
    _, (candidates, metrics) = log_search(session)
    provider = SimpleNamespace(rerank=Mock(side_effect=ValueError("known failure")))
    result = retrieval.observe_batch_rerank(session, metrics, batch_rerank,
        query="q", candidates=candidates, rerank_service=provider, model=None)
    assert result[0]["content"] == candidates[0]["content"]
    assert result[0]["score"] == candidates[0]["score"]
    assert metrics["rerank_status"] == "UNKNOWN" and metrics["rerank_count"] is None


def test_exception_identity_is_preserved_with_failed_trace(session, events):
    _, (_, metrics) = log_search(session)
    error = ValueError("original provider exception")
    with pytest.raises(ValueError) as raised:
        retrieval.observe_batch_rerank(session, metrics, Mock(side_effect=error))
    assert raised.value is error
    assert metrics["rerank_status"] == "FAILED" and metrics["rerank_count"] is None
    assert events[-1][1]["status"] == "FAILED"
    assert session.get(RetrievalLog, metrics["retrieval_log_id"]).rerank_enabled


def test_missing_output_is_unknown_not_zero(session, events):
    metrics = {}
    assert retrieval.observe_batch_rerank(session, metrics, lambda: None) is None
    assert metrics["rerank_status"] == "UNKNOWN" and metrics["rerank_count"] is None


def test_unknown_provider_exception_remains_unknown_and_propagates(session, events):
    import httpx

    _, (_, metrics) = log_search(session)
    error = httpx.ReadTimeout("provider timeout")
    with pytest.raises(httpx.ReadTimeout) as raised:
        retrieval.observe_batch_rerank(session, metrics, Mock(side_effect=error))
    assert raised.value is error
    assert metrics["rerank_status"] == "UNKNOWN" and metrics["rerank_count"] is None
    assert events[-1][1]["status"] == "UNKNOWN"


@pytest.mark.parametrize("log_id", [None, "missing-row"])
def test_absent_or_missing_log_never_invents_a_row(session, events, log_id):
    metrics = {"retrieval_log_id": log_id} if log_id else {}
    result = [{"rerank_status": "SCORED", "rerank_score": 0.5}]
    assert retrieval.observe_batch_rerank(session, metrics, lambda: result) is result
    assert session.exec(select(RetrievalLog)).all() == []
    assert events[-1][1]["retrieval_log_id"] == log_id
    assert events[-1][1]["persistence"]["status"] == ("UNKNOWN" if log_id else "NOT_RUN")


def test_db_failure_is_fail_open_and_retains_source_id(session, events, monkeypatch):
    _, (_, metrics) = log_search(session)
    source_id = metrics["retrieval_log_id"]
    monkeypatch.setattr(session, "execute", Mock(side_effect=RuntimeError("write failed")))
    result = [{"rerank_status": "SCORED", "rerank_score": 0.5}]
    assert retrieval.observe_batch_rerank(session, metrics, lambda: result) is result
    assert metrics["retrieval_log_id"] == source_id
    assert events[-1][1]["persistence"]["status"] == "FAILED"
    assert events[-1][1]["retrieval_log_id"] == source_id


def test_db_failure_does_not_mask_original_exception(session, events, monkeypatch):
    _, (_, metrics) = log_search(session)
    monkeypatch.setattr(session, "execute", Mock(side_effect=RuntimeError("write failed")))
    error = ValueError("original")
    with pytest.raises(ValueError) as raised:
        retrieval.observe_batch_rerank(session, metrics, Mock(side_effect=error))
    assert raised.value is error
    assert events[-1][1]["status"] == "FAILED"
    assert events[-1][1]["persistence"]["status"] == "FAILED"


def test_disabled_logging_and_failed_commit_do_not_expose_id(session, monkeypatch):
    service, _ = log_search(session)
    _, metrics = asyncio.run(service.search(session, "q", ["kb"], search_mode="fulltext", log_retrieval=False))
    assert "retrieval_log_id" not in metrics
    monkeypatch.setattr(session, "commit", Mock(side_effect=RuntimeError("commit failed")))
    _, metrics = asyncio.run(service.search(session, "q", ["kb"], search_mode="fulltext"))
    assert "retrieval_log_id" not in metrics


@pytest.mark.parametrize("consumer", ["chat", "tool", "single_recall", "multi_recall"])
def test_consumers_preserve_monkeypatchable_callable_and_update_source_row(session, events, monkeypatch, consumer):
    from app.models import KnowledgeBase
    from app.schemas import RecallRequest
    from app.services import chat
    from app.tools import retrieval_tool
    from app.api import recall

    _, (candidates, metrics) = log_search(session)
    output = [{**candidates[0], "rerank_status": "SCORED", "rerank_score": 0.9}]
    patched_batch = Mock(return_value=output)
    search = SimpleNamespace(search=AsyncMock(return_value=(candidates, metrics)))
    original_get = session.get
    monkeypatch.setattr(session, "get", lambda model, identity, **kwargs:
        SimpleNamespace(name="Test KB") if model is KnowledgeBase else original_get(model, identity, **kwargs))
    module = chat if consumer == "chat" else retrieval_tool if consumer == "tool" else recall
    monkeypatch.setattr(module, "batch_rerank", patched_batch)
    monkeypatch.setattr(module, "RerankService", lambda session: object())
    monkeypatch.setattr(module, "get_retrieval_service", lambda: search)

    if consumer == "chat":
        service = chat.ChatService.__new__(chat.ChatService)
        service.retrieval_service = search
        result, _ = asyncio.run(service._retrieve_with_new_service(
            session=session, query="q", kb_ids=["kb"], top_k=10, score_threshold=0,
            search_mode="fulltext", vector_weight=0.7, bm25_weight=0.3,
            rerank_enabled=True, rerank_score_threshold=0.2, rerank_model_id=None,
            rerank_top_k=5, context_window=0))
        assert result is output
    elif consumer == "tool":
        monkeypatch.setattr(retrieval_tool, "Session", lambda engine: session)
        asyncio.run(retrieval_tool.search_knowledge_base.coroutine(
            query="q", kb_ids=["kb"], use_rerank=True, context_window=0,
            rerank_top_k=5, rerank_score_threshold=0.2))
    else:
        request = RecallRequest(query="q", rerank_enabled=True, context_window=0,
                                rerank_top_k=5, rerank_score_threshold=0.2)
        if consumer == "single_recall":
            asyncio.run(recall.recall_test("kb", request, session))
        else:
            asyncio.run(recall.recall_test_multi(request, ["kb"], session))

    patched_batch.assert_called_once()
    assert patched_batch.call_args.kwargs["candidates"] is candidates
    assert patched_batch.call_args.kwargs["top_k"] == 5
    assert patched_batch.call_args.kwargs["score_threshold"] == 0.2
    row = session.get(RetrievalLog, metrics["retrieval_log_id"])
    assert row.rerank_enabled and row.rerank_count == 1
    assert row.final_count == 1

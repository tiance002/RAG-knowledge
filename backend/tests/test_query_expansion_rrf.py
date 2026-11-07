"""Focused lexical-expansion/RRF contracts. Provider calls are explicit test doubles."""
import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())

from app.services import query_expansion, retrieval


def service(vector_rows=(), lexical=None):
    svc = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    svc.vector_store = Mock()
    svc.vector_store.query.return_value = list(vector_rows)
    svc._embed_query_for_kb = AsyncMock(return_value=[0.1])
    svc.bm25_index = Mock()
    svc.bm25_index.search.side_effect = lexical or (lambda **kw: [])
    svc._enrich_results = Mock(side_effect=lambda session, rows, allowed: [
        {**row, "file_id": row["id"], "content": "evidence " + row["id"]}
        for row in rows
    ])
    return svc


def search(svc, **kw):
    return asyncio.run(svc.search(None, "rewritten query", kw.pop("kb_ids", ["kb"]),
                                  top_k=kw.pop("top_k", 3),
                                  log_retrieval=False, **kw))


def test_weighted_rrf_uses_ranks_before_normalization_and_preserves_raw_scores():
    vectors = [{"id": "a", "score": .99}, {"id": "b", "score": .5}, {"id": "c", "score": .01}]
    lexical = retrieval.aggregate_bm25_results([[
        {"chunk_id": "c", "bm25_score": 1000.0},
        {"chunk_id": "b", "bm25_score": .001},
    ]])
    rows = retrieval.weighted_rrf(vectors, lexical)
    assert [r["id"] for r in rows] == ["b", "c", "a"]
    by_id = {r["id"]: r for r in rows}
    assert by_id["b"]["rrf_score"] == pytest.approx(.7 / 62 + .3 / 62)
    assert by_id["c"]["rrf_score"] == pytest.approx(.7 / 63 + .3 / 61)
    assert by_id["c"]["vector_score"] == .01
    assert by_id["c"]["bm25_score"] == 1000.0
    assert (by_id["c"]["vector_rank"], by_id["c"]["bm25_rank"]) == (3, 1)
    assert rows[0]["score"] == 1.0 and rows[-1]["score"] == 0.0
    expected = (rows[1]["rrf_score"] - rows[-1]["rrf_score"]) / (rows[0]["rrf_score"] - rows[-1]["rrf_score"])
    assert rows[1]["score"] == pytest.approx(expected)


def test_raw_score_scaling_does_not_change_rrf_and_ties_are_stable():
    first = retrieval.weighted_rrf([{"id": "b", "score": 2}, {"id": "a", "score": 2}], [])
    second = retrieval.weighted_rrf([{"id": "a", "score": 200}, {"id": "b", "score": 200}], [])
    assert [r["id"] for r in first] == ["a", "b"]
    assert [r["rrf_score"] for r in first] == [r["rrf_score"] for r in second]


def test_variant_duplicates_never_add_bm25_route_votes():
    initial = [{"chunk_id": "a", "bm25_score": 3}, {"chunk_id": "b", "bm25_score": 2}]
    once = retrieval.aggregate_bm25_results([initial])
    repeated = retrieval.aggregate_bm25_results([initial] * 6)
    assert [(r["chunk_id"], r["bm25_rank"]) for r in repeated] == [("a", 1), ("b", 2)]
    assert len(repeated[0]["bm25_query_matches"]) == 6
    a = retrieval.weighted_rrf([], once)
    b = retrieval.weighted_rrf([], repeated)
    assert [r["rrf_score"] for r in a] == [r["rrf_score"] for r in b]
    duplicate_vectors = retrieval.weighted_rrf([{"id": "a", "score": .9}] * 5, [])
    assert duplicate_vectors[0]["rrf_score"] == pytest.approx(.7 / 61)


def test_local_expansion_is_bounded_distinct_and_uses_only_existing_terms():
    text = "How does Redis memory caching improve query performance?"
    variants = query_expansion.expand_queries([text, text.upper()], max_queries=100)
    allowed = set(query_expansion.tokenize(text))
    assert 0 < len(variants) <= 5
    assert len({q.casefold() for q in variants}) == len(variants)
    assert all(set(q.split()) <= allowed for q in variants)
    assert all("how" not in q.split() and "does" not in q.split() for q in variants)
    assert query_expansion.expand_queries([text], max_queries=0) == []


def test_sufficient_initial_union_skips_expansion(monkeypatch):
    svc = service([{"id": "vector", "score": .8}],
                  lambda **kw: [{"chunk_id": "lexical", "bm25_score": 4}])
    monkeypatch.setattr(retrieval, "expand_queries", Mock(side_effect=AssertionError("unexpected expansion")))
    rows, metrics = search(svc, top_k=2)
    assert len(rows) == 2
    assert metrics["expansion_triggered"] is False
    svc.bm25_index.search.assert_called_once()
    svc._embed_query_for_kb.assert_awaited_once()


def test_insufficient_union_expands_bm25_only_and_retains_original(monkeypatch):
    def lexical(**kw):
        ids = {"original question": "original", "keyword": "supplement"}
        identifier = ids.get(kw["query"])
        return [{"chunk_id": identifier, "bm25_score": 2}] if identifier else []
    svc = service([{"id": "vector", "score": .8}], lexical)
    monkeypatch.setattr(retrieval, "expand_queries", Mock(return_value=["keyword"]))
    rows, metrics = search(svc, original_query="original question", top_k=4)
    assert {r["id"] for r in rows} == {"vector", "original", "supplement"}
    assert [c.kwargs["query"] for c in svc.bm25_index.search.call_args_list] == [
        "rewritten query", "original question", "keyword"]
    svc._embed_query_for_kb.assert_awaited_once_with(None, "kb", "rewritten query")
    svc.vector_store.query.assert_called_once()
    assert metrics["initial_candidate_count"] == 2
    assert metrics["expansion_query_count"] == 1


def test_each_variant_keeps_kb_scope_and_one_embedding_per_distinct_kb(monkeypatch):
    svc = service()
    monkeypatch.setattr(retrieval, "expand_queries", Mock(return_value=["keyword"]))
    _, metrics = search(svc, kb_ids=["kb-a", "kb-b", "kb-a"], original_query="original")
    assert svc._embed_query_for_kb.await_count == 2
    assert svc.vector_store.query.call_count == 2
    pairs = [(c.kwargs["query"], c.kwargs["kb_id"]) for c in svc.bm25_index.search.call_args_list]
    assert pairs == [(q, kb) for q in ["rewritten query", "original", "keyword"] for kb in ["kb-a", "kb-b"]]
    assert metrics["retrieval_status"] == "NO_RESULTS"


def test_fulltext_uses_shared_expansion_without_any_embedding(monkeypatch):
    svc = service(lexical=lambda **kw: [{"chunk_id": "match", "bm25_score": 2}]
                  if kw["query"] == "keyword" else [])
    monkeypatch.setattr(retrieval, "expand_queries", Mock(return_value=["keyword"]))
    rows, metrics = search(svc, search_mode="fulltext", original_query="original")
    assert [r["id"] for r in rows] == ["match"]
    assert metrics["expansion_query_count"] == 1
    svc._embed_query_for_kb.assert_not_awaited()
    svc.vector_store.query.assert_not_called()


def test_unknown_embedding_outcome_never_retries_or_expands():
    svc = service()
    svc._embed_query_for_kb.side_effect = TimeoutError("uncertain provider outcome")
    with pytest.raises(TimeoutError):
        search(svc, original_query="original")
    svc._embed_query_for_kb.assert_awaited_once()
    svc.bm25_index.search.assert_not_called()
    svc.vector_store.query.assert_not_called()


def test_optional_expansion_failure_keeps_initial_results(monkeypatch):
    def lexical(**kw):
        if kw["query"] == "keyword":
            raise RuntimeError("local lexical index unavailable")
        return [{"chunk_id": "initial", "bm25_score": 4}]
    svc = service(lexical=lexical)
    monkeypatch.setattr(retrieval, "expand_queries", Mock(return_value=["keyword", "unused"]))
    rows, metrics = search(svc)
    assert [r["id"] for r in rows] == ["initial"]
    assert metrics["expansion_failed"] is True and metrics["degraded"] is True
    assert metrics["retrieval_status"] == "SUCCESS"
    assert svc.bm25_index.search.call_count == 2
    svc._embed_query_for_kb.assert_awaited_once()


def test_fusion_events_expose_true_ranks_raw_scores_and_post_rrf_normalization(monkeypatch):
    svc = service([{"id": "a", "score": .9}, {"id": "b", "score": .1}],
                  lambda **kw: [{"chunk_id": "b", "bm25_score": 100}])
    events = []
    monkeypatch.setattr(retrieval, "enabled", lambda: True)
    monkeypatch.setattr(retrieval, "event", lambda name, **kw: events.append((name, kw)))
    rows, _ = search(svc, top_k=2)
    transitions = [data for name, data in events if name == "rag.candidate_transition"]
    fusion = next(t for t in transitions if t["operation"] == "weighted_fusion")
    assert fusion["method"] == "weighted_rrf" and fusion["rrf_k"] == 60
    b = next(r for r in fusion["scores"] if r["candidate_id"] == "b")
    assert (b["vector_rank"], b["bm25_rank"]) == (2, 1)
    assert (b["vector_raw_score"], b["bm25_raw_score"]) == (.1, 100)
    normalizations = [t for t in transitions if t["operation"] == "score_normalization"]
    assert len(normalizations) == 1 and normalizations[0]["input_kind"] == "fusion"
    assert rows[0]["id"] == "b" and rows[0]["score"] == 1.0

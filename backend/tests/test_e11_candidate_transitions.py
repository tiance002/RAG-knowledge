import sys
from unittest.mock import MagicMock
sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from app.services import retrieval, rerank


def test_twenty_to_seven_dedup_observations_do_not_change_results(monkeypatch):
    # Same text in distinct files is retained; later same-source copies are dropped.
    rows = [{"id": f"seed{i}", "file_id": f"file{i}", "kb_id": "kb", "content": "same evidence"} for i in range(7)]
    rows += [{"id": f"copy{i}", "file_id": f"file{i % 7}", "kb_id": "kb", "content": "same evidence"} for i in range(12)]
    rows += [dict(rows[0])]
    calls = []
    monkeypatch.setattr(retrieval, "enabled", lambda: False)
    baseline = retrieval.deduplicate_results(rows)
    monkeypatch.setattr(retrieval, "enabled", lambda: True)
    monkeypatch.setattr(retrieval, "event", lambda name, **kw: calls.append((name, kw)))
    observed = retrieval.deduplicate_results(rows)
    assert observed == baseline == rows[:7]
    transitions = {kw["operation"]: kw for _, kw in calls}
    exact = transitions["dedup_exact_id"]
    similar = transitions["dedup_same_source_similarity"]
    assert len(exact["input_ids"]) == 20 and len(exact["output_ids"]) == 19
    assert len(exact["removed"]) == 1 and exact["removed"][0]["rank_before"] == 20
    assert len(similar["input_ids"]) == 19 and len(similar["output_ids"]) == 7
    assert len(similar["removed"]) == 12
    assert all(x["actual_similarity"] == 1.0 and x["threshold"] == .85 for x in similar["removed"])
    assert all(x["retained_representative_id"] == f"seed{i % 7}" for i, x in enumerate(similar["removed"]))


def test_batch_clipping_is_observed_without_changing_results(monkeypatch):
    class Service:
        def rerank(self, query, candidates, model=None):
            return [{"index": 0, "score": .9}]
    rows = [{"id": "c1", "content": "abcdefghij", "score": .4}]
    monkeypatch.setattr(rerank, "estimate_tokens", lambda text: len(text))
    monkeypatch.setattr(rerank, "enabled", lambda: False)
    baseline = rerank.batch_rerank("q", rows, Service(), None, max_context_tokens=2, top_k=1)
    calls = []
    monkeypatch.setattr(rerank, "enabled", lambda: True)
    monkeypatch.setattr(rerank, "event", lambda name, **kw: calls.append((name, kw)))
    observed = rerank.batch_rerank("q", rows, Service(), None, max_context_tokens=2, top_k=1)
    assert observed == baseline
    clip = next(kw for name, kw in calls if name == "rag.candidate_transition" and kw["operation"] == "truncate")
    assert clip["input_ids"] == ["c1"] and clip["end"] == 4
    assert clip["original_content"]["sha256"] != clip["rerank_sent_content"]["sha256"]


def test_hybrid_to_rerank_transitions_and_observability_equivalence(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    class VectorStore:
        def query(self, **kwargs):
            return [{"id": "a", "score": .9}, {"id": "b", "score": .5}, {"id": "c", "score": .1}]

    class BM25:
        def search(self, **kwargs):
            return [{"chunk_id": "c", "bm25_score": 10.0}, {"chunk_id": "b", "bm25_score": 5.0}, {"chunk_id": "d", "bm25_score": 0.0}]

    class Reranker:
        def rerank(self, query, candidates, model=None):
            return [{"index": 1, "score": .2}, {"index": 2, "score": .9}, {"index": 0, "score": .7}]

    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    service.vector_store = VectorStore()
    service.bm25_index = BM25()
    async def embed(*args):
        return [0.1]
    service._embed_query_for_kb = embed
    service._enrich_results = lambda session, rows: [{**r, "file_id": r["id"], "content": f"evidence {r['id']}"} for r in rows]

    def run():
        rows, metrics = asyncio.run(service.search(SimpleNamespace(), "query", ["kb"], search_mode="hybrid", top_k=3, score_threshold=0.0, vector_weight=.7, bm25_weight=.3, log_retrieval=False))
        result = rerank.batch_rerank("query", rows, Reranker(), None, max_context_tokens=100, top_k=1, score_threshold=.3)
        return rows, result, metrics["retrieval_status"]

    monkeypatch.setattr(retrieval, "enabled", lambda: False)
    monkeypatch.setattr(rerank, "enabled", lambda: False)
    baseline = run()
    events = []
    monkeypatch.setattr(retrieval, "enabled", lambda: True)
    monkeypatch.setattr(rerank, "enabled", lambda: True)
    monkeypatch.setattr(retrieval, "event", lambda name, **kw: events.append((name, kw)))
    monkeypatch.setattr(rerank, "event", lambda name, **kw: events.append((name, kw)))
    observed = run()
    assert observed == baseline
    assert [r["id"] for r in observed[0]] == ["a", "b", "c"]
    assert [r["id"] for r in observed[1]] == ["c"]
    transitions = [data for name, data in events if name == "rag.candidate_transition"]
    fusion = next(e for e in transitions if e["operation"] == "weighted_fusion")
    assert fusion["method"] == "weighted_normalized_sum"
    assert {v["candidate_id"]: v["fusion_score"] for v in fusion["scores"]} == {"a": .7, "b": .5, "c": .3, "d": 0.0}
    fusion_topk = next(e for e in transitions if e.get("rule_version") == "fusion_topk_v1")
    assert fusion_topk["output_ids"] == ["a", "b", "c"]
    assert fusion_topk["removed"][0]["candidate_id"] == "d"
    mapping = next(e for e in transitions if e.get("rule_version") == "batch_rerank_output_v1")
    assert [(m["response_index"], m["candidate_id"]) for m in mapping["response_mapping"]] == [(1, "b"), (2, "c"), (0, "a")]
    sort = next(e for e in transitions if e.get("rule_version") == "rerank_sort_v1")
    assert sort["input_ids"] == ["b", "c", "a"]
    assert sort["output_ids"] == ["c", "a", "b"]
    filtered = next(e for e in transitions if e["operation"] == "threshold_filter")
    assert filtered["output_ids"] == ["c", "a"] and filtered["removed"][0]["candidate_id"] == "b"
    selected = next(e for e in transitions if e.get("rule_version") == "rerank_topk_v1")
    assert selected["output_ids"] == ["c"] and selected["removed"][0]["candidate_id"] == "a"

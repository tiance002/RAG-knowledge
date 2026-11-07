import re
from app.services.observability import native_operation, event, candidates, observe, enabled
from app.services.llm import is_unknown_provider_error
"""
统一检索服务

整合：
- 向量检索
- BM25 全文检索
- 混合检索（Hybrid）
- 分数归一化与融合
- 检索结果去重
"""

import time
import json
from typing import List, Dict, Any, Optional, Literal, Tuple
from datetime import datetime

from sqlmodel import Session, select

from app.models import DocumentChunk, FileDocument, KnowledgeBase, RetrievalLog, generate_uuid, CustomModel
from app.services.vector_store import VectorStoreService
from app.services.embedding import EmbeddingService
from app.services.bm25 import BM25Index, get_bm25_index
from app.services.query_expansion import distinct_queries, expand_queries
from app.core.config import get_settings

settings = get_settings()


class _HybridNoCandidates(Exception):
    """Distinguish an empty hybrid result from a failed retrieval route."""

    def __init__(self, failed_routes: int):
        super().__init__("hybrid returned no candidates")
        self.failed_routes = failed_routes


def normalize_scores(scores: Dict[str, float]) -> Dict[str, float]:
    """
    Min-Max 归一化分数

    将分数映射到 [0, 1] 区间
    """
    if not scores:
        return {}

    min_s = min(scores.values())
    max_s = max(scores.values())

    if max_s == min_s:
        return {k: 1.0 for k in scores}

    return {k: (v - min_s) / (max_s - min_s) for k, v in scores.items()}


def aggregate_bm25_results(result_lists: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """One lexical list: best variant rank, then raw score and stable ID.

    A chunk matching multiple variants receives no sum or vote-count boost.
    Retain every variant's raw score/rank for inspection; only its best-ranked
    occurrence determines its position in the single BM25 route.
    """
    merged = {}
    for query_index, rows in enumerate(result_lists):
        ordered = sorted(rows, key=lambda r: (-r["bm25_score"], r["chunk_id"]))
        seen = set()
        for row in ordered:
            chunk_id = row["chunk_id"]
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank = len(seen)
            key = (rank, -row["bm25_score"], chunk_id)
            match = {"query_index": query_index, "rank": rank, "raw_score": row["bm25_score"]}
            if chunk_id not in merged:
                merged[chunk_id] = {**row, "bm25_query_matches": [], "_variant_key": key}
            candidate = merged[chunk_id]
            candidate["bm25_query_matches"].append(match)
            if key < candidate["_variant_key"]:
                candidate.update(row, _variant_key=key)
    result = sorted(merged.values(), key=lambda r: r["_variant_key"])
    for rank, row in enumerate(result, 1):
        row.pop("_variant_key")
        row["bm25_rank"] = rank
        row["score"] = row["bm25_score"]
    return result


def weighted_rrf(
    vector_results: List[Dict[str, Any]],
    bm25_results: List[Dict[str, Any]],
    vector_weight: float = 0.7,
    bm25_weight: float = 0.3,
) -> List[Dict[str, Any]]:
    """Fuse distinct route ranks with k=60, then normalize the fused scores.

    Vector raw scores order its one route; BM25 already supplies one aggregated
    list. Neither raw route score is normalized before RRF.
    """
    merged = {}
    routes = (
        ("vector", sorted(vector_results, key=lambda r: (-r["score"], r["id"])), vector_weight),
        ("bm25", bm25_results, bm25_weight),
    )
    for route, rows, weight in routes:
        seen = set()
        for row in rows:
            chunk_id = row.get("id") or row.get("chunk_id")
            if not chunk_id or chunk_id in seen:
                continue
            seen.add(chunk_id)
            rank = len(seen)
            candidate = merged.setdefault(chunk_id, {
                "id": chunk_id, "kb_id": row.get("kb_id", ""), "rrf_score": 0.0,
                "vector_rank": None, "bm25_rank": None,
                "vector_score": None, "bm25_score": None,
                "vector_contribution": 0.0, "bm25_contribution": 0.0,
            })
            candidate[f"{route}_rank"] = rank
            candidate[f"{route}_score"] = row["score"] if route == "vector" else row["bm25_score"]
            contribution = weight / (60 + rank)
            candidate[f"{route}_contribution"] = contribution
            candidate["rrf_score"] += contribution
            if route == "bm25":
                candidate["bm25_query_matches"] = row.get("bm25_query_matches", [])
    ordered = sorted(merged.values(), key=lambda r: (-r["rrf_score"], r["id"]))
    normalized = normalize_scores({r["id"]: r["rrf_score"] for r in ordered})
    for row in ordered:
        row["score"] = normalized[row["id"]]
    return ordered


def deduplicate_results(
    results: List[Dict[str, Any]],
    similarity_threshold: float = 0.85
) -> List[Dict[str, Any]]:
    """
    检索结果去重

    策略：
    1. ID 去重
    2. 同一来源内的内容相似度去重（基于 Jaccard 相似度）

    不同文件可能使用同一模板但包含不同事实；即使文本相似，也不能
    用一个文件的引用替代另一个文件。来源未知时保留结果。
    """
    seen_ids = set()
    seen_contents = []
    deduplicated = []
    exact_removed = []
    similar_removed = []

    for rank, r in enumerate(results, 1):
        chunk_id = r.get("id") or r.get("chunk_id")

        # ID 去重
        if chunk_id in seen_ids:
            exact_removed.append({"candidate_id": chunk_id, "reason_code": "duplicate_id", "retained_representative_id": chunk_id, "actual_similarity": None, "threshold": None, "rank_before": rank})
            continue

        content = r.get("content", "")

        # 内容相似度去重
        is_duplicate = False
        file_id = r.get("file_id")
        kb_id = r.get("kb_id")
        for seen_file_id, seen_kb_id, seen, seen_id in seen_contents:
            if not file_id or not seen_file_id or not kb_id or not seen_kb_id:
                continue
            if file_id != seen_file_id or kb_id != seen_kb_id:
                continue
            # Character sets lose order and multiplicity (100 vs 001).
            # Different numeric literals are distinct evidence regardless of
            # approximate text similarity.
            if re.findall(r'[+\-−]?\d+(?:[.,]\d+)*', content) != re.findall(r'[+\-−]?\d+(?:[.,]\d+)*', seen):
                continue
            sim = jaccard_similarity(content, seen)
            if sim > similarity_threshold:
                is_duplicate = True
                similar_removed.append({"candidate_id": chunk_id, "reason_code": "same_source_similarity", "retained_representative_id": seen_id, "actual_similarity": sim, "threshold": similarity_threshold, "rank_before": rank})
                break

        if not is_duplicate:
            seen_ids.add(chunk_id)
            seen_contents.append((file_id, kb_id, content, chunk_id))
            deduplicated.append(r)

    if enabled():
        ids = [r.get("id") or r.get("chunk_id") for r in results]
        # Preserve duplicate occurrence order; an ID is not an occurrence key.
        after_exact = [r.get("id") or r.get("chunk_id") for i, r in enumerate(results, 1)
                       if i not in {x["rank_before"] for x in exact_removed}]
        output = [r.get("id") or r.get("chunk_id") for r in deduplicated]
        for operation, before, after, removed in (
            ("dedup_exact_id", ids, after_exact, exact_removed),
            ("dedup_same_source_similarity", after_exact, output, similar_removed),
        ):
            event("rag.candidate_transition", operation=operation, input_ids=before, output_ids=after,
                  input_candidate_ids=before, output_candidate_ids=after, removed=removed,
                  threshold=similarity_threshold if operation == "dedup_same_source_similarity" else None,
                  rule_version="character_set_jaccard_gt_0.85_v1" if operation == "dedup_same_source_similarity" else "exact_id_v1")
    return deduplicated


def jaccard_similarity(text1: str, text2: str) -> float:
    """计算两个文本的 Jaccard 相似度"""
    if not text1 or not text2:
        return 0.0

    # 简单按字符切分
    set1 = set(text1)
    set2 = set(text2)

    intersection = len(set1 & set2)
    union = len(set1 | set2)

    return intersection / union if union > 0 else 0.0


class RetrievalService:
    """
    统一检索服务

    支持三种模式：
    - vector: 仅向量检索
    - fulltext: 仅 BM25 全文检索
    - hybrid: 混合检索（向量 + BM25）
    """

    def __init__(self):
        self.vector_store = VectorStoreService()
        self.bm25_index = get_bm25_index()

    async def _embed_query_for_kb(self, session: Session, kb_id: str, query: str) -> List[float]:
        from app.services.model_bindings import resolve_model, ModelBindingError
        kb = session.get(KnowledgeBase, kb_id)
        if kb is None:
            raise ModelBindingError("KNOWLEDGE_BASE_NOT_FOUND")
        # A KB binding identifies its existing vector space. Missing/invalid
        # identities must never be replaced by another system embedding model.
        binding = resolve_model(session, "embedding", kb.embedding_model, use_default=False)
        embedding_service = EmbeddingService(base_url=binding.base_url,
            api_key=binding.api_key, model=binding.model_name)
        return await embedding_service.embed_query(query, model_id=binding.model_name)

    @observe("retrieval.search")
    async def search(
        self,
        session: Session,
        query: str,
        kb_ids: List[str],
        search_mode: Literal["vector", "fulltext", "hybrid"] = "hybrid",
        top_k: int = None,
        score_threshold: float = 0.0,
        vector_weight: float = None,
        bm25_weight: float = None,
        use_cache: bool = True,
        log_retrieval: bool = True,
        original_query: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        执行检索

        Args:
            session: 数据库会话
            query: 查询语句
            kb_ids: 知识库 ID 列表
            search_mode: 检索模式
            top_k: 返回数量
            score_threshold: 分数阈值
            vector_weight: 向量权重（hybrid 模式）
            bm25_weight: BM25 权重（hybrid 模式）
            use_cache: 保留的兼容参数；当前因缺少可靠索引版本而跳过缓存
            log_retrieval: 是否记录检索日志
            original_query: 改写前的用户问题，额外参与 BM25，向量仍只查询 query

        Returns:
            (检索结果列表, 检索统计信息)
        """
        top_k = top_k or settings.RETRIEVAL_TOP_K
        vector_weight = vector_weight if vector_weight is not None else settings.RETRIEVAL_VECTOR_WEIGHT
        bm25_weight = bm25_weight if bm25_weight is not None else settings.RETRIEVAL_BM25_WEIGHT

        start_time = time.time()
        metrics = {
            "vector_count": 0,
            "bm25_count": 0,
            "merged_count": 0,
            "vector_latency_ms": 0,
            "bm25_latency_ms": 0,
        }

        # `use_cache` remains accepted for API compatibility; retrieval always
        # executes until a cache with complete provenance is implemented.

        results = []
        fallback_used = False
        failure_outcome = None

        try:
            if search_mode == "vector":
                results, metrics = await self._vector_search(
                    session, query, kb_ids, top_k, score_threshold
                )
            elif search_mode == "fulltext":
                results, metrics = await self._bm25_search(
                    session, query, kb_ids, top_k,
                    **({"original_query": original_query} if original_query else {})
                )
            elif search_mode == "hybrid":
                results, metrics, fallback_used = await self._hybrid_search(
                    session, query, kb_ids, top_k, score_threshold,
                    vector_weight, bm25_weight,
                    **({"original_query": original_query} if original_query else {})
                )
            else:
                raise ValueError(f"未知的检索模式: {search_mode}")

        except Exception as e:
            if is_unknown_provider_error(e):
                event('retrieval.failed', status='UNKNOWN', error_type=type(e).__name__, search_mode=search_mode)
                raise
            if isinstance(e, _HybridNoCandidates):
                failure_outcome = ('RETRIEVAL_ERROR' if e.failed_routes == 2 else
                                   'UNKNOWN' if e.failed_routes else 'NO_RESULTS')
            else:
                failure_outcome = "RETRIEVAL_ERROR"
            print(f"❌ [Retrieval] 检索失败: {type(e).__name__}")
            if failure_outcome == 'NO_RESULTS':
                event('retrieval.no_candidates', status='NO_RESULTS', search_mode=search_mode)
            elif failure_outcome == 'UNKNOWN':
                event('retrieval.incomplete', status='UNKNOWN', search_mode=search_mode)
            else:
                event('retrieval.failed', status='FAILED', error_type=type(e).__name__, search_mode=search_mode)
            # Hybrid has already attempted both routes. Replaying vector
            # here can duplicate a paid embedding and cannot repair a fusion error.

        # 去重
        candidates("retrieval.before_dedup", results)
        results = deduplicate_results(results)
        candidates("retrieval.after_dedup", results)

        if failure_outcome in ("RETRIEVAL_ERROR", "UNKNOWN"):
            outcome = failure_outcome
        else:
            # 单路降级（fallback_used）只要仍有结果就仍然可用：降级事实由
            # metrics["degraded"] 与 fallback_used 记录，不用 UNKNOWN 阻断回答生成。
            outcome = "SUCCESS" if results else (failure_outcome or "NO_RESULTS")
        event("retrieval.outcome", outcome=outcome, search_mode=search_mode,
              fallback_used=fallback_used, result_count=len(results))

        # 计算总耗时
        total_latency = (time.time() - start_time) * 1000
        metrics["total_latency_ms"] = total_latency
        metrics["final_count"] = len(results)
        metrics["fallback_used"] = fallback_used
        metrics["degraded"] = bool(fallback_used and failure_outcome not in ("RETRIEVAL_ERROR", "UNKNOWN"))
        metrics["retrieval_status"] = outcome
        event('retrieval.metrics', metrics=metrics, search_mode=search_mode, top_k=top_k,
              score_threshold=score_threshold, vector_weight=vector_weight, bm25_weight=bm25_weight)

        # 记录检索日志
        if log_retrieval:
            self._log_retrieval(
                session, query, kb_ids, search_mode, top_k, score_threshold,
                vector_weight, bm25_weight, metrics, results
            )

        return results, metrics

    @native_operation("retrieval.vector", as_type="retriever")
    async def _vector_search(
        self,
        session: Session,
        query: str,
        kb_ids: List[str],
        top_k: int,
        score_threshold: float
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """向量检索"""
        start_time = time.time()

        all_results = []
        for kb_id in kb_ids:
            query_vector = await self._embed_query_for_kb(session, kb_id, query)
            results = self.vector_store.query(
                kb_id=kb_id,
                query_vector=query_vector,
                top_k=top_k,
                score_threshold=score_threshold
            )

            # 补充知识库信息
            kb = session.get(KnowledgeBase, kb_id)
            kb_name = kb.name if kb else "未知"

            for r in results:
                r["kb_id"] = kb_id
                r["kb_name"] = kb_name
                all_results.append(r)

        candidates("retrieval.vector", all_results)
        # 补充 chunk 详情
        all_results = self._enrich_results(session, all_results, kb_ids)

        # 按分数排序
        all_results.sort(key=lambda x: x.get("score", 0), reverse=True)
        all_results = all_results[:top_k]

        latency = (time.time() - start_time) * 1000
        metrics = {
            "vector_count": len(all_results),
            "bm25_count": 0,
            "merged_count": len(all_results),
            "vector_latency_ms": latency,
            "bm25_latency_ms": 0,
        }

        return all_results, metrics

    def _bm25_query_results(
        self, session: Session, queries: List[str], kb_ids: List[str], top_k: int,
        query_offset: int = 0,
    ) -> List[List[Dict[str, Any]]]:
        """Query only the caller's KB scope; vector retrieval is never called here."""
        result_lists = []
        for query_index, query in enumerate(queries, query_offset):
            rows = []
            for kb_id in dict.fromkeys(kb_ids):
                for result in self.bm25_index.search(
                    session=session, query=query, kb_id=kb_id, top_k=top_k
                ):
                    rows.append({**result, "id": result["chunk_id"],
                                 "score": result["bm25_score"], "kb_id": kb_id})
            rows.sort(key=lambda r: (-r["bm25_score"], r["chunk_id"]))
            candidates("retrieval.bm25_query", rows, query_index=query_index,
                       score_kind="bm25_raw")
            result_lists.append(rows)
        return result_lists

    def _expanded_bm25_results(
        self, session: Session, query: str, original_query: Optional[str],
        kb_ids: List[str], top_k: int, route_top_k: int,
        other_candidate_ids=None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Supplement an insufficient initial union using at most five local variants.

        Insufficient means fewer than requested top_k distinct initial candidates.
        This is a candidate-count rule, not a change to quality/rerank thresholds.
        """
        base = distinct_queries([query, original_query])
        result_lists = self._bm25_query_results(session, base, kb_ids, route_top_k)
        initial = aggregate_bm25_results(result_lists)
        initial_ids = set(other_candidate_ids or ()) | {r["chunk_id"] for r in initial}
        insufficient = len(initial_ids) < top_k
        variants = expand_queries(base) if insufficient else []
        failed = False
        # Keep successful initial and supplemental results on a local route error.
        # Unknown outcomes remain terminal and must never be retried.
        for variant in variants:
            try:
                result_lists.extend(self._bm25_query_results(
                    session, [variant], kb_ids, route_top_k, len(result_lists)))
            except Exception as exc:
                if is_unknown_provider_error(exc):
                    raise
                failed = True
                event("retrieval.expansion_failed", status="FALLBACK", error_type=type(exc).__name__)
                break
        details = {
            "initial_candidate_count": len(initial_ids),
            "bm25_base_query_count": len(base),
            "bm25_query_count": len(result_lists),
            "expansion_query_count": len(result_lists) - len(base),
            "expansion_triggered": insufficient,
            "expansion_failed": failed,
        }
        event("retrieval.query_expansion", **details,
              max_expansion_queries=5, insufficient_rule="initial_unique_count_lt_top_k",
              rule_version="local_keywords_bm25_only_v1")
        aggregated = aggregate_bm25_results(result_lists)
        if enabled():
            event("rag.candidate_transition", operation="bm25_variant_aggregation",
                  method="best_variant_rank_then_raw_score", input_ids=[r["chunk_id"] for rows in result_lists for r in rows],
                  output_ids=[r["chunk_id"] for r in aggregated],
                  scores=[{"candidate_id": r["chunk_id"], "bm25_rank": r["bm25_rank"],
                           "bm25_raw_score": r["bm25_score"], "query_matches": r["bm25_query_matches"]}
                          for r in aggregated],
                  rule_version="bm25_best_variant_rank_v1", removed=[])
        return aggregated, details

    @native_operation("retrieval.bm25", as_type="retriever")
    async def _bm25_search(
        self, session: Session, query: str, kb_ids: List[str], top_k: int,
        original_query: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """BM25 search shares the same bounded local expansion as hybrid search."""
        start_time = time.time()
        all_results, details = self._expanded_bm25_results(
            session, query, original_query, kb_ids, top_k, top_k)
        candidates("retrieval.bm25", all_results)
        all_results = self._enrich_results(session, all_results, kb_ids)[:top_k]
        metrics = {
            "vector_count": 0, "bm25_count": len(all_results),
            "merged_count": len(all_results), "vector_latency_ms": 0,
            "bm25_latency_ms": (time.time() - start_time) * 1000, **details,
        }
        return all_results, metrics

    async def _hybrid_search(
        self, session: Session, query: str, kb_ids: List[str], top_k: int,
        score_threshold: float, vector_weight: float, bm25_weight: float,
        original_query: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
        """One vector route plus one aggregated lexical route, Weighted RRF then minmax."""
        fallback_used = False
        failed_routes = 0
        vector_start = time.time()
        vector_results = []
        try:
            for kb_id in dict.fromkeys(kb_ids):
                # Rewrites and lexical variants must not multiply paid embeddings.
                query_vector = await self._embed_query_for_kb(session, kb_id, query)
                results = self.vector_store.query(
                    kb_id=kb_id, query_vector=query_vector,
                    top_k=150, score_threshold=score_threshold)
                vector_results.extend({**r, "kb_id": kb_id} for r in results)
        except Exception as e:
            if is_unknown_provider_error(e):
                event('retrieval.vector_failed', status='UNKNOWN', error_type=type(e).__name__)
                raise
            print(f"⚠️ [Retrieval] 向量检索失败: {type(e).__name__}")
            event('retrieval.vector_failed', status='FALLBACK', error_type=type(e).__name__)
            fallback_used = True
            failed_routes += 1
        vector_results.sort(key=lambda r: (-r["score"], r["id"]))
        candidates("retrieval.vector", vector_results)
        vector_latency = (time.time() - vector_start) * 1000

        bm25_start = time.time()
        bm25_results = []
        expansion_metrics = {}
        try:
            bm25_results, expansion_metrics = self._expanded_bm25_results(
                session, query, original_query, kb_ids, top_k, settings.BM25_TOP_K,
                other_candidate_ids=[r["id"] for r in vector_results])
            fallback_used = fallback_used or expansion_metrics["expansion_failed"]
        except Exception as e:
            if is_unknown_provider_error(e):
                event('retrieval.bm25_failed', status='UNKNOWN', error_type=type(e).__name__)
                raise
            print(f"⚠️ [Retrieval] BM25 检索失败: {type(e).__name__}")
            event('retrieval.bm25_failed', status='FALLBACK', error_type=type(e).__name__)
            fallback_used = True
            failed_routes += 1
        candidates("retrieval.bm25", bm25_results)
        bm25_latency = (time.time() - bm25_start) * 1000
        if not vector_results and not bm25_results:
            raise _HybridNoCandidates(failed_routes=failed_routes)

        fused = weighted_rrf(vector_results, bm25_results, vector_weight, bm25_weight)
        selected = fused[:top_k]
        if enabled():
            vector_ids = list(dict.fromkeys(r["id"] for r in vector_results))
            bm25_ids = [r["chunk_id"] for r in bm25_results]
            ids = list(dict.fromkeys(vector_ids + bm25_ids))
            ordered = [r["id"] for r in fused]
            output_ids = [r["id"] for r in selected]
            raw_scores = {r["id"]: r["rrf_score"] for r in fused}
            normalized = {r["id"]: r["score"] for r in fused}
            event("rag.candidate_transition", operation="union_by_chunk_id",
                  input_candidate_ids=vector_ids + bm25_ids, output_candidate_ids=ids,
                  input_ids=vector_ids + bm25_ids, output_ids=ids, removed=[], rule_version="union_by_chunk_id_v1")
            event("rag.candidate_transition", operation="weighted_fusion", method="weighted_rrf",
                  input_ids=ids, output_ids=ids, input_candidate_ids=ids, output_candidate_ids=ids,
                  vector_weight=vector_weight, bm25_weight=bm25_weight, rrf_k=60,
                  scores=[{"candidate_id": r["id"], "vector_rank": r["vector_rank"],
                           "bm25_rank": r["bm25_rank"], "vector_raw_score": r["vector_score"],
                           "bm25_raw_score": r["bm25_score"], "vector_contribution": r["vector_contribution"],
                           "bm25_contribution": r["bm25_contribution"], "fusion_score": r["rrf_score"]}
                          for r in fused], removed=[], rule_version="weighted_rrf_k60_v1")
            event("rag.candidate_transition", operation="score_normalization", input_kind="fusion", output_kind="fusion",
                  input_ids=ordered, output_ids=ordered, input_candidate_ids=ordered, output_candidate_ids=ordered,
                  method="minmax", raw_scores=raw_scores, normalized_scores=normalized,
                  min_score=min(raw_scores.values()), max_score=max(raw_scores.values()),
                  rule_version="minmax_equal_one_v1", removed=[])
            event("rag.candidate_transition", operation="sort", input_ids=ids, output_ids=ordered,
                  input_candidate_ids=ids, output_candidate_ids=ordered, sort_key="rrf_score_desc_then_id",
                  removed=[], rule_version="fusion_sort_v1")
            event("rag.candidate_transition", operation="topk", input_ids=ordered, output_ids=output_ids,
                  input_candidate_ids=ordered, output_candidate_ids=output_ids, top_k=top_k, score_kind="fusion",
                  rule_version="fusion_topk_v1", removed=[{"candidate_id": cid, "reason_code": "outside_topk", "rank_before": i}
                                                          for i, cid in enumerate(ordered, 1) if i > len(selected)])
        candidates("retrieval.fusion", selected,
                   ranks=[{"candidate_id": r["id"], "vector_rank": r["vector_rank"],
                           "bm25_rank": r["bm25_rank"], "rrf_score": r["rrf_score"]} for r in selected],
                   method="weighted_rrf_then_minmax", rrf_k=60)
        merged_results = self._enrich_results(session, selected, kb_ids)
        metrics = {
            "vector_count": len({r["id"] for r in vector_results}), "bm25_count": len(bm25_results),
            "merged_count": len(merged_results), "vector_latency_ms": vector_latency,
            "bm25_latency_ms": bm25_latency, "fusion_method": "weighted_rrf_then_minmax", "rrf_k": 60,
            **expansion_metrics,
        }
        return merged_results, metrics, fallback_used

    def _enrich_results(
        self,
        session: Session,
        results: List[Dict[str, Any]],
        allowed_kb_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        补充检索结果的详细信息

        从数据库获取：
        - chunk 内容
        - 文件名
        - 页码
        - 图片路径
        - 父子关系等
        """
        enriched = []
        lineage_seen = set()
        from app.services.file_versions import inactive_file_ids
        inactive_files = inactive_file_ids(session, allowed_kb_ids) if results else set()

        for r in results:
            chunk_id = r.get("id") or r.get("chunk_id")
            if not chunk_id:
                continue

            # 获取 chunk 详情
            chunk = session.get(DocumentChunk, chunk_id)
            if not chunk or chunk.chunk_type == 'parent' or chunk.file_id in inactive_files:
                continue

            # 获取文件信息
            file_doc = session.get(FileDocument, chunk.file_id)
            # The collection's KB label is not an authority for SQL content.
            # Reject stale/misplaced hits even when expansion is disabled.
            if (not file_doc or (allowed_kb_ids is not None and file_doc.kb_id not in allowed_kb_ids)
                    or (r.get('kb_id') and r['kb_id'] != file_doc.kb_id)):
                event('retrieval.source_rejected', reason='source_scope_mismatch', chunk_id=chunk_id)
                continue
            file_name = file_doc.display_name or file_doc.name
            if enabled() and file_doc and chunk.file_id not in lineage_seen:
                lineage_seen.add(chunk.file_id)
                try:
                    from app.services.index_lineage import file_lineage
                    chunks = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == chunk.file_id)).all()
                    event("retrieval.file_lineage", **file_lineage(chunk.file_id, file_doc.local_path, chunks))
                except Exception:
                    pass

            # 获取知识库名称
            kb_id = file_doc.kb_id
            kb = session.get(KnowledgeBase, kb_id) if kb_id else None
            kb_name = kb.name if kb else "未知知识库"

            # 构建位置信息
            loc_parts = []
            if chunk.page_number:
                loc_parts.append(f"第 {chunk.page_number} 页")
            if chunk.heading_text:
                loc_parts.append(f"章节: {chunk.heading_text}")
            
            location_info = " | ".join(loc_parts) if loc_parts else "未知位置"

            from app.services.pdf_provenance import source_metadata
            provenance = (source_metadata(file_doc.local_path, chunk.content, chunk.page_number,
                                         chunk.content_type) if file_doc else {})

            enriched.append({
                "id": chunk_id,
                "content": chunk.content,
                "score": r.get("score", 0),
                "location_info": location_info,
                "vector_score": r.get("vector_score"),
                "bm25_score": r.get("bm25_score"),
                **{key: r[key] for key in ("vector_rank", "bm25_rank", "rrf_score",
                                          "vector_contribution", "bm25_contribution", "bm25_query_matches")
                   if key in r},
                "kb_id": kb_id,
                "kb_name": kb_name,
                "file_id": chunk.file_id,
                "file_name": file_name,
                "page_number": chunk.page_number,
                "source_metadata": provenance,
                "content_type": chunk.content_type,
                "image_path": chunk.image_path,
                "original_index": chunk.original_index,
                # 新增字段
                "parent_id": chunk.parent_id,
                "heading_level": chunk.heading_level,
                "heading_text": chunk.heading_text,
                "chunk_type": chunk.chunk_type,
                "token_count": getattr(chunk, "token_count", 0),
            })

        return enriched

    def _log_retrieval(
        self,
        session: Session,
        query: str,
        kb_ids: List[str],
        search_mode: str,
        top_k: int,
        score_threshold: float,
        vector_weight: float,
        bm25_weight: float,
        metrics: Dict[str, Any],
        results: List[Dict[str, Any]]
    ) -> None:
        """记录检索日志"""
        try:
            # 生成结果摘要（前 3 条）
            summary = [
                {
                    "content": r.get("content", "")[:100],
                    "score": r.get("score", 0),
                    "file_name": r.get("file_name", ""),
                }
                for r in results[:3]
            ]

            log = RetrievalLog(
                id=generate_uuid(),
                kb_id=",".join(kb_ids),
                query=query[:500],  # 截断长查询
                search_mode=search_mode,
                vector_count=metrics.get("vector_count", 0),
                bm25_count=metrics.get("bm25_count", 0),
                merged_count=metrics.get("merged_count", 0),
                rerank_count=0,  # 后续 rerank 会更新
                final_count=metrics.get("final_count", 0),
                latency_ms=metrics.get("total_latency_ms", 0),
                vector_latency_ms=metrics.get("vector_latency_ms", 0),
                bm25_latency_ms=metrics.get("bm25_latency_ms", 0),
                rerank_latency_ms=0,
                top_k=top_k,
                score_threshold=score_threshold,
                rerank_enabled=False,
                vector_weight=vector_weight,
                bm25_weight=bm25_weight,
                results_summary=json.dumps(summary, ensure_ascii=False),
                created_at=datetime.utcnow()
            )

            session.add(log)
            session.commit()

        except Exception as e:
            print(f"⚠️ [Retrieval] 记录日志失败: {type(e).__name__}")


# 单例
_retrieval_service = None


def get_retrieval_service() -> RetrievalService:
    """获取检索服务单例"""
    global _retrieval_service
    if _retrieval_service is None:
        _retrieval_service = RetrievalService()
    return _retrieval_service

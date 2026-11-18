import re
from app.services.performance_timing import timed_stage
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
import asyncio
import json
import math
import os
import hashlib
from urllib.parse import urlsplit
from typing import List, Dict, Any, Optional, Literal, Tuple
from datetime import datetime

from sqlmodel import Session, select
from sqlalchemy import and_, or_

from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase, RetrievalLog, generate_uuid, CustomModel
from app.services.vector_store import VectorStoreService, VectorPageBudgetExceeded
from app.services.embedding import EmbeddingService
from app.services.bm25 import BM25Index, get_bm25_index
from app.services.query_expansion import distinct_queries, expand_queries
from app.services.retrieval_budget import (
    RetrievalBudget, RetrievalBudgetExpired, RequestQueryVectors, fair_depths,
    active_retrieval_budget as _active_retrieval_budget,
    bounded_sync_vector_query,
)
from app.core.config import get_settings

settings = get_settings()


def _hybrid_stage(stage, started, status):
    # Content-free telemetry must never replace the original result/exception.
    try:
        print("HYBRID_STAGE " + json.dumps(dict(stage=stage,
            wall_ms=(time.perf_counter() - started) * 1000, status=status),
            sort_keys=True), flush=True)
    except Exception:
        pass


def _query_embedding_reuse_enabled():
    return os.getenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'false').lower() == 'true'


def _query_embedding_reuse_key(service, binding, query, authorization_scope=(), target_dimension=None):
    """Private exact identity for the supported single-text OpenAI contract.

    This is deliberately conservative: no URL/text normalization, no inferred
    vector parameters, no legacy/multimodal adapters or effect-scope bypass.
    The returned key contains credentials and must never be emitted or stored.
    """
    from app.services import task_effect_context
    try:
        scope_check = getattr(task_effect_context, 'has_active_effect_scope', None)
        # Older deployed fencing exposes no public inspection capability.
        # Keep the optional optimization disabled rather than inspect internals.
        if scope_check is None:
            return None
        active_scope = scope_check()
    except Exception:
        return None
    if active_scope is not False or type(query) is not str or '[图片:' in query:
        return None
    base, model, credential = service.base_url, binding.model_name, service.api_key
    if (not all(type(value) is str for value in (base, model, credential))
            or not base or not model or model != service.model
            or 'doubao' in model.lower() or 'volces.com' in base.lower()
            or type(binding.binding_id) is not str or not binding.binding_id
            or binding.model_type != 'embedding'
            or binding.source not in ('custom_model', 'deployment')
            or type(binding.context_length) is not int or binding.context_length <= 0):
        return None
    clean = base.rstrip('/')
    endpoint = clean if clean.endswith('/embeddings') else clean + '/embeddings'
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or not parsed.path.endswith('/v1/embeddings')):
            return None
    except ValueError:
        return None
    # There are currently no dimensions/encoding/normalization request options.
    # Version the actual supported contract instead of inventing their values.
    return ('openai-single-text-v1', binding.binding_id, binding.source,
            binding.model_type, binding.context_length, base, endpoint, model,
            credential, query, ('dimensions', None), ('target_dimension', target_dimension),
            tuple(authorization_scope))


def _reusable_query_vector(vector):
    try:
        return (type(vector) is list and bool(vector)
                and all(type(value) in (int, float) and math.isfinite(value) for value in vector))
    except (OverflowError, TypeError, ValueError):
        return False


def _expansion_target_order(targets, query, original_query, capacity=None):
    """Deterministic query-private rotation avoids a permanently favored prefix."""
    targets = sorted(set(targets))
    if len(targets) < 2 or capacity is None or capacity <= 0 or capacity >= len(targets):
        return targets
    private_seed = json.dumps([query, original_query], ensure_ascii=False, separators=(',', ':'))
    private_digest = hashlib.sha256(private_seed.encode('utf-8')).digest()
    offset = int.from_bytes(private_digest[:8], 'big') % len(targets)
    step = int.from_bytes(private_digest[8:16], 'big') % (len(targets) - 1) + 1
    while math.gcd(step, len(targets)) != 1:
        step = step % (len(targets) - 1) + 1
    return [targets[(offset + index * step) % len(targets)] for index in range(len(targets))]


def _verified_build_manifest(build_id, manifest, digest, cache):
    """Cache only integrity-checked publication metadata within one request."""
    key = (build_id, digest)
    if key not in cache:
        parsed = None
        try:
            if hashlib.sha256(manifest.encode('utf-8')).hexdigest() != digest:
                raise ValueError('BUILD_MANIFEST_HASH_MISMATCH')
            payload = json.loads(manifest)
            chunk_ids = payload['chunk_ids']
            if not isinstance(chunk_ids, list) or not all(type(value) is str for value in chunk_ids):
                raise ValueError('BUILD_MANIFEST_CHUNK_IDS_INVALID')
            dimension = payload.get('dimension')
            parsed = {'chunk_ids': frozenset(chunk_ids),
                'dimension': dimension if type(dimension) is int and dimension > 0 else None}
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        cache[key] = parsed
    return cache[key]


def _configure_vector_physical_budget(budget, scopes, depths, query, original_query=None):
    # Conservative500-ID pages use only the frozen SQL scope; no Chroma request
    # or caller Session crosses threads during quota planning. Exact current
    # page size may be larger, leaving headroom rather than a false completion.
    plans = {}
    for kb, (ids, builds) in scopes.items():
        if ids is None and builds is None:
            knn_pages, scope_gets = 1, 0
        else:
            legacy = math.ceil(len(ids or []) / 500)
            knn_pages = legacy + math.ceil(len(builds or []) / 500)
            scope_gets = legacy
        readbacks = math.ceil(depths[kb] / 500) if knn_pages else 0
        plans[kb] = {'ops': knn_pages + scope_gets + readbacks,
                     'rows': knn_pages * depths[kb], 'query_depth': depths[kb]}
    physically_limited = (sum(plan['ops'] for plan in plans.values()) > budget.vector_max_page_ops
        or sum(plan['rows'] for plan in plans.values()) > budget.vector_max_knn_rows)
    order = _expansion_target_order(list(plans), query, original_query,
        len(plans) - 1 if physically_limited and len(plans) > 1 else None)
    budget.configure_vector_targets(plans, order)


class _HybridNoCandidates(Exception):
    """Distinguish an empty hybrid result from a failed retrieval route."""

    def __init__(self, failed_routes: int, metrics=None):
        super().__init__("hybrid returned no candidates")
        self.failed_routes = failed_routes
        self.metrics = metrics or {}


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

    @staticmethod
    def _vector_scope(session, kb_id):
        # Read pointers and legacy chunks together, so one document cannot
        # appear under both its retired IDs and a newly active build.
        from app.services.file_versions import inactive_file_ids
        inactive = inactive_file_ids(session, [kb_id])
        rows = session.exec(select(FileDocument.id, FileDocument.active_build_id, DocumentChunk.id)
            .select_from(FileDocument).outerjoin(DocumentChunk, and_(
                DocumentChunk.file_id == FileDocument.id, FileDocument.active_build_id.is_(None),
                DocumentChunk.chunk_type != 'parent')).join(
                    KnowledgeBase, KnowledgeBase.id == FileDocument.kb_id).where(
            FileDocument.kb_id == kb_id, FileDocument.deleted_at.is_(None),
            KnowledgeBase.is_deleted.is_(False),
        )).all()
        legacy, builds = [], []
        for file_id, build_id, chunk_id in rows:
            if file_id in inactive:
                continue
            if build_id:
                builds.append(build_id)
            elif chunk_id:
                legacy.append(chunk_id)
        return legacy, builds

    def __init__(self):
        from app.services.bounded_chroma_executor import chroma_executor_enabled
        self.vector_store = (VectorStoreService(lazy_retrieval=True)
                             if chroma_executor_enabled() else VectorStoreService())
        self.bm25_index = get_bm25_index()

    async def _embed_query_for_kb(self, session: Session, kb_id: str, query: str,
                                *, query_vectors=None) -> List[float]:
        from app.services.model_bindings import resolve_model, ModelBindingError
        kb = session.get(KnowledgeBase, kb_id)
        if kb is None or kb.is_deleted:
            raise ModelBindingError("KNOWLEDGE_BASE_NOT_FOUND")
        # A KB binding identifies its existing vector space. Missing/invalid
        # identities must never be replaced by another system embedding model.
        binding = resolve_model(session, "embedding", kb.embedding_model, use_default=False)
        embedding_service = EmbeddingService(base_url=binding.base_url,
            api_key=binding.api_key, model=binding.model_name)
        scoped_memo = isinstance(query_vectors, RequestQueryVectors)
        dimension = query_vectors.target_dimensions.get(kb_id) if scoped_memo else None
        key = (_query_embedding_reuse_key(embedding_service, binding, query,
                    getattr(query_vectors, 'authorization_scope', ()), dimension)
               if query_vectors is not None and _query_embedding_reuse_enabled()
                   and (not scoped_memo or dimension is not None) else None)
        if scoped_memo and dimension is None:
            event('embedding.query_reuse', scope='retrieval.search', status='UNKNOWN',
                  reason='target_dimension_unknown')
        if (key is not None and key in query_vectors
                and (dimension is None or len(query_vectors[key]) == dimension)):
            event('embedding.query_reuse', scope='retrieval.search', status='HIT')
            return list(query_vectors[key])
        budget = _active_retrieval_budget.get()
        if budget is not None:
            budget.check()
        request = embedding_service.embed_query(query, model_id=binding.model_name)
        try:
            vector = await budget.wait_for(request) if budget is not None else await request
        except RetrievalBudgetExpired as exc:
            # The provider may have received a cancelled embedding. Never retry
            # or turn this uncertain paid operation into a local fallback.
            exc.provider_status = 'UNKNOWN'
            raise
        if (key is not None and _reusable_query_vector(vector)
                and (dimension is None or len(vector) == dimension)):
            query_vectors[key] = tuple(vector)
        elif scoped_memo and dimension is not None and _reusable_query_vector(vector) and len(vector) != dimension:
            event('embedding.query_reuse', scope='retrieval.search', status='UNKNOWN',
                  reason='target_dimension_mismatch')
        return vector

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
                fallback_used = bool(metrics.get('fallback_used') or metrics.get('vector_work_budget_exhausted'))
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
                metrics.update(e.metrics)
                fallback_used = bool(e.failed_routes or e.metrics.get('initial_scope_coverage_complete') is False)
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
        elif (metrics.get('vector_work_budget_exhausted') or metrics.get('retrieval_deadline_exhausted')
              or metrics.get('initial_scope_coverage_complete') is False):
            outcome = 'PARTIAL' if results else 'UNKNOWN'
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
        metrics['retrieval_incomplete'] = outcome in ('PARTIAL', 'UNKNOWN', 'RETRIEVAL_ERROR')
        event('retrieval.metrics', metrics=metrics, search_mode=search_mode, top_k=top_k,
              score_threshold=score_threshold, vector_weight=vector_weight, bm25_weight=bm25_weight)

        # 记录检索日志
        if log_retrieval:
            self._log_retrieval(
                session, query, kb_ids, search_mode, top_k, score_threshold,
                vector_weight, bm25_weight, metrics, results
            )

        return results, metrics

    async def _query_vector_store(self, **kwargs):
            started = time.perf_counter()
            status = "RETURNED"
            try:
                from app.services.bounded_chroma_executor import chroma_executor_enabled, ChromaExecutorCapabilityError
                if chroma_executor_enabled():
                    try:
                        return await self.vector_store.query_async(**kwargs)
                    except ChromaExecutorCapabilityError as exc:
                        reason = str(exc)
                        if not reason or len(reason) > 96 or not all(c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_' for c in reason):
                            reason = 'CHROMA_CAPABILITY_UNKNOWN'
                        event('chroma.capability', status='BLOCKED', reason_code=reason)
                        raise
                return await bounded_sync_vector_query(lambda: self.vector_store.query(**kwargs))

            except BaseException:
                status = "FAILED"
                raise
            finally:
                _hybrid_stage("chroma_await", started, status)

    async def _timed_embedding(self, session, kb_id, query, **kwargs):
                started = time.perf_counter()
                status = "RETURNED"
                try:
                    return await self._embed_query_for_kb(session, kb_id, query, **kwargs)
                except BaseException:
                    status = "FAILED"
                    raise
                finally:
                    _hybrid_stage("embedding", started, status)

    def _timed_vector_scope(self, session, kb_id):
                started = time.perf_counter()
                status = "RETURNED"
                try:
                    return self._vector_scope(session, kb_id)
                except BaseException:
                    status = "FAILED"
                    raise
                finally:
                    _hybrid_stage("vector_sql_scope", started, status)

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

        budget = RetrievalBudget.from_environment(target_count=len(set(kb_ids)),
            vector_depth_per_target=top_k)
        token = _active_retrieval_budget.set(budget)
        try:
            return await self._vector_search_budgeted(session, query, kb_ids, top_k, score_threshold, budget)
        finally:
            _active_retrieval_budget.reset(token)

    async def _vector_search_budgeted(self, session, query, kb_ids, top_k, score_threshold, budget):
        start_time = time.time()
        targets = list(dict.fromkeys(kb_ids))
        total_requested = top_k * len(targets)
        logical_depth = min(budget.vector_depth, total_requested)
        allocation_order = _expansion_target_order(targets, query, None, logical_depth)
        depths = fair_depths(logical_depth, allocation_order)
        depth_limited = [kb for kb in targets if depths[kb] < top_k]
        omitted = [kb for kb in targets if not depths[kb]]
        scopes = {}
        for kb in targets:
            if depths[kb] and budget.remaining():
                scopes[kb] = self._timed_vector_scope(session, kb)
        _configure_vector_physical_budget(budget, scopes, depths, query)
        all_results = []
        reuse_kwargs = {'query_vectors': RequestQueryVectors(kb_ids)} if _query_embedding_reuse_enabled() else {}
        for kb_id in targets:
            if kb_id not in scopes or not budget.remaining():
                continue
            if reuse_kwargs:
                reuse_kwargs['query_vectors'].target_dimensions[kb_id] = self._known_target_dimension(session, kb_id)
            query_vector = await self._timed_embedding(session, kb_id, query, **reuse_kwargs)
            allowed_ids, allowed_build_ids = scopes[kb_id]
            try:
                results = await budget.wait_for(self._query_vector_store(
                    kb_id=kb_id,
                    query_vector=query_vector,
                    top_k=depths[kb_id],
                    score_threshold=score_threshold,
                    allowed_ids=allowed_ids, allowed_build_ids=allowed_build_ids,
                ))
            except VectorPageBudgetExceeded as exc:
                budget.vector_work_exhausted = True
                results = exc.partial_results
            except RetrievalBudgetExpired:
                break

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
            **budget.metrics(),
            'vector_request_depth': logical_depth,
            'vector_configured_request_depth': budget.vector_depth,
            'vector_requested_depth_total': total_requested,
            'initial_scope_coverage_complete': not depth_limited,
            'initial_target_query_coverage_complete': not omitted,
            'initial_vector_budget_omitted_target_ids': omitted,
            'initial_vector_depth_limited_target_ids': depth_limited,
            'target_allocations': [{'kb_id': kb, 'vector_depth': depths[kb],
                'vector_requested_depth': top_k} for kb in targets],
            'vector_output_limit_per_target': top_k,
            'fallback_used': budget.vector_work_exhausted or bool(depth_limited),

        }

        return all_results, metrics


    @timed_stage('bm25')
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
                    session=session, query=query, kb_id=kb_id, top_k=top_k, live_only=True
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
        other_candidate_ids=None, initial_result_lists=None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """Supplement an insufficient initial union using at most five local variants.

        Insufficient means fewer than requested top_k distinct initial candidates.
        This is a candidate-count rule, not a change to quality/rerank thresholds.
        """
        base = distinct_queries([query, original_query])
        result_lists = (self._bm25_query_results(session, base, kb_ids, route_top_k)
                        if initial_result_lists is None else list(initial_result_lists))
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

    @staticmethod
    def _lexical_execution_scope(session):
        """Use independent committed-index readers for file-SQLite Hybrid.

        Parallelism is unconditional for supported API readers. In-memory or
        shared-connection fixtures retain their same-session compatibility path.
        Pending caller writes are deliberately not part of published-index reads.
        """
        from sqlalchemy.engine import Engine
        from sqlalchemy.pool import StaticPool, SingletonThreadPool
        from app.services.bounded_lexical_executor import lexical_executor_available, lexical_reader_engine
        if not lexical_executor_available():
            return None, "executor_not_managed"
        if not isinstance(session, Session):
            return None, "no_sql_session"
        bind = session.get_bind()
        if not isinstance(bind, Engine) or isinstance(bind.pool, (StaticPool, SingletonThreadPool)):
            return None, "nonindependent_bind"
        if bind.dialect.name == 'sqlite':
            if bind.url.database in (None, '', ':memory:') or bind.url.query.get('mode') == 'memory':
                return None, "in_memory_sqlite"
        else:
            return None, "unsupported_reader_engine"
        return lexical_reader_engine(bind), "managed_independent_reader"

    @staticmethod
    def _parallel_lexical_bind(session):
        return RetrievalService._lexical_execution_scope(session)[0]

    def _known_target_dimension(self, session, kb_id):
        """One declared dimension across the target's live ACTIVE publications.

        A legacy SQL projection, missing build, invalid manifest or mixed space
        is UNKNOWN. It never authorizes cross-target embedding memo reuse.
        """
        from app.services.file_versions import inactive_file_ids
        inactive = inactive_file_ids(session, [kb_id])
        has_chunks = select(DocumentChunk.id).where(
            DocumentChunk.file_id == FileDocument.id, DocumentChunk.chunk_type != 'parent').exists()
        statement = (select(FileDocument.id, FileDocument.active_build_id,
            FileIndexBuild.file_id, FileIndexBuild.kb_id, FileIndexBuild.state,
            FileIndexBuild.manifest, FileIndexBuild.manifest_hash)
            .select_from(FileDocument)
            .join(KnowledgeBase, KnowledgeBase.id == FileDocument.kb_id)
            .outerjoin(FileIndexBuild, FileIndexBuild.build_id == FileDocument.active_build_id)
            .where(FileDocument.kb_id == kb_id, FileDocument.deleted_at.is_(None),
                KnowledgeBase.is_deleted.is_(False),
                or_(FileDocument.active_build_id.is_not(None), has_chunks)))
        budget = _active_retrieval_budget.get()
        cache = budget.authorization_manifests if budget is not None else {}
        dimensions = set()
        for file_id, build_id, owner_file, owner_kb, state, manifest, digest in session.exec(statement):
            if file_id in inactive:
                continue
            if not build_id or (owner_file, owner_kb, state) != (file_id, kb_id, 'ACTIVE'):
                return None
            parsed = _verified_build_manifest(build_id, manifest, digest, cache)
            if parsed is None or parsed['dimension'] is None:
                return None
            dimensions.add(parsed['dimension'])
            if len(dimensions) > 1:
                return None
        return next(iter(dimensions)) if len(dimensions) == 1 else None

    def _authorized_route_rows(self, session, rows, kb_ids):
        """Authorize bounded candidate IDs without reading bodies/provenance.

        SQL source/KB ownership is authoritative. For active publication builds,
        chunk_ids is the exact SQL projection contract written by
        index_builds.publish_index_build/_replace_sql_chunks; vector_ids also
        excludes parents, so it is deliberately not used as the SQL manifest.
        """
        from app.services.file_versions import inactive_file_ids
        ids = list(dict.fromkeys(row.get('id') or row.get('chunk_id') for row in rows))
        ids = [chunk_id for chunk_id in ids if chunk_id]
        if not ids:
            return []
        sources = {}
        for start in range(0, len(ids), 500):
            statement = (select(DocumentChunk.id, DocumentChunk.file_id,
                FileDocument.kb_id, FileDocument.active_build_id)
                .select_from(DocumentChunk)
                .join(FileDocument, FileDocument.id == DocumentChunk.file_id)
                .join(KnowledgeBase, KnowledgeBase.id == FileDocument.kb_id)
                .where(DocumentChunk.id.in_(ids[start:start + 500]),
                    DocumentChunk.chunk_type != 'parent', FileDocument.deleted_at.is_(None),
                    FileDocument.kb_id.in_(kb_ids), KnowledgeBase.is_deleted.is_(False)))
            for chunk_id, file_id, kb_id, build_id in session.exec(statement):
                sources[chunk_id] = (file_id, kb_id, build_id)
        inactive = inactive_file_ids(session, kb_ids,
            candidate_file_ids={source[0] for source in sources.values()})
        sources = {chunk_id: source for chunk_id, source in sources.items() if source[0] not in inactive}
        build_ids = sorted({source[2] for source in sources.values() if source[2]})
        builds = {}
        for start in range(0, len(build_ids), 500):
            statement = select(FileIndexBuild.build_id, FileIndexBuild.file_id,
                FileIndexBuild.kb_id, FileIndexBuild.state, FileIndexBuild.manifest,
                FileIndexBuild.manifest_hash).where(FileIndexBuild.build_id.in_(build_ids[start:start + 500]))
            for build_id, file_id, kb_id, state, manifest, digest in session.exec(statement):
                builds[build_id] = (file_id, kb_id, state, manifest, digest)
        budget = _active_retrieval_budget.get()
        cache = budget.authorization_manifests if budget is not None else {}
        valid = []
        for row in rows:
            chunk_id = row.get('id') or row.get('chunk_id')
            source = sources.get(chunk_id)
            if source is None:
                continue
            file_id, kb_id, build_id = source
            if row.get('kb_id') and row['kb_id'] != kb_id:
                continue
            if build_id:
                build = builds.get(build_id)
                if build is None or build[:3] != (file_id, kb_id, 'ACTIVE'):
                    continue
                _, _, _, manifest, digest = build
                parsed = _verified_build_manifest(build_id, manifest, digest, cache)
                if parsed is None or chunk_id not in parsed['chunk_ids']:
                    continue
                metadata = row.get('metadata') or {}
                if isinstance(metadata, dict) and metadata.get('build_id') not in (None, '', build_id):
                    continue
            valid.append(row)
        return valid

    async def _budgeted_hybrid_routes(self, session, bind, query, original_query,
                                      kb_ids, top_k, score_threshold, budget):
        from app.services.bounded_lexical_executor import run_lexical_read
        from app.services.bounded_chroma_executor import chroma_executor_enabled
        targets = sorted(set(kb_ids))
        base = distinct_queries([query, original_query])
        vector_depths = fair_depths(budget.vector_depth, targets)
        initial_pairs = [(index, kb) for index in range(len(base)) for kb in targets]
        initial_depths = fair_depths(budget.lexical_initial_depth, initial_pairs)
        initial_vector_omitted = [kb for kb in targets if not vector_depths[kb]]
        initial_bm25_fully_omitted = [kb for kb in targets if not any(
            initial_depths[index, kb] > 0 for index in range(len(base)))]
        initial_bm25_omitted_pairs = [{'query_index': index, 'kb_id': kb}
            for (index, kb), depth in initial_depths.items() if depth == 0]
        initial_bm25_omitted = sorted({pair['kb_id'] for pair in initial_bm25_omitted_pairs})
        initial_omitted_pairs = len(initial_bm25_omitted_pairs)
        initial_vector_depth_limited = [kb for kb in targets
            if vector_depths[kb] < budget.vector_baseline_depth_per_target]
        initial_bm25_depth_limited = [{'query_index': index, 'kb_id': kb,
            'allocated_depth': depth, 'baseline_depth': budget.lexical_baseline_depth_per_pair}
            for (index, kb), depth in initial_depths.items()
            if depth < budget.lexical_baseline_depth_per_pair]
        initial_scope_complete = not initial_vector_depth_limited and not initial_bm25_depth_limited
        gate = asyncio.Semaphore(budget.target_inflight)
        durations = {'vector': 0.0, 'bm25': 0.0}
        failures = set()
        lexical_error_count = 0
        pair_count = 0
        started_routes = {}
        ended_routes = {}
        target_timings = []
        target_dimensions = {}

        def record_target(route, kb, queued, acquired, finished, **extra):
            timing = {'route': route, 'kb_id': kb,
                'request_queue_wait_ms': (acquired - queued) * 1000,
                'read_wall_ms': (finished - acquired) * 1000,
                'started_at_ms': (acquired - budget.started) * 1000,
                'finished_at_ms': (finished - budget.started) * 1000, **extra}
            target_timings.append(timing)
            event('retrieval.target_timing', **timing)

        async def bounded_jobs(items, operation, on_schedule=None, on_cancel=None):
            # A sliding window bounds request tasks as well as actual executor
            # submissions. Thread futures retain their process slot on cancel.
            outcomes = []
            for start in range(0, len(items), budget.target_inflight):
                if not budget.remaining():
                    break
                group = items[start:start + budget.target_inflight]
                if on_schedule is not None:
                    for item in group:
                        on_schedule(item)
                tasks = [asyncio.create_task(operation(item)) for item in group]
                try:
                    done, pending = await asyncio.wait(tasks, timeout=budget.remaining())
                    if pending:
                        budget.deadline_exhausted = True
                        for item, task in zip(group, tasks):
                            if task in pending:
                                if on_cancel is not None:
                                    on_cancel(item)
                                task.cancel()
                    # Drain cancelled coroutine wrappers, never the shielded
                    # real executor work. Task order keeps ties deterministic.
                    await asyncio.gather(*tasks, return_exceptions=True)
                    for item, task in zip(group, tasks):
                        if task in done and not task.cancelled():
                            exc = task.exception()
                            if exc is not None:
                                if is_unknown_provider_error(exc):
                                    raise exc
                                outcomes.append((item, exc))
                            else:
                                outcomes.append((item, task.result()))
                except BaseException:
                    for item, task in zip(group, tasks):
                        if not task.done():
                            if on_cancel is not None:
                                on_cancel(item)
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
            return outcomes

        async def lexical_pairs(pairs, depths, queries, *, supplemental=False):
            nonlocal pair_count, lexical_error_count
            started = time.perf_counter()
            status = "RETURNED"
            output = [[] for _ in queries]
            items = [pair for pair in pairs if depths[pair] > 0]
            def read(pair, reader):
                index, kb = pair
                budget.check()
                if supplemental:
                    budget.record_supplement_pair('started', pair)
                try:
                    rows = self._bm25_query_results(reader, [queries[index]], [kb], depths[pair],
                        query_offset=index + (len(base) if supplemental else 0))[0]
                except BaseException:
                    if supplemental:
                        budget.record_supplement_pair('failed', pair)
                    raise
                else:
                    if supplemental:
                        budget.record_supplement_pair('completed', pair)
                    return rows
            async def execute(pair):
                queued = time.perf_counter()
                async with gate:
                    acquired = time.perf_counter()
                    budget.check()
                    worker_started = []
                    def operation():
                        worker_started.append(time.perf_counter())
                        with Session(bind) as reader:
                            return read(pair, reader)
                    try:
                        return await run_lexical_read(operation)
                    except asyncio.CancelledError:
                        if supplemental:
                            budget.record_supplement_pair('cancelled', pair)
                        raise
                    finally:
                        record_target('bm25', pair[1], queued, acquired, time.perf_counter(),
                            query_index=pair[0] + (len(base) if supplemental else 0),
                            phase='supplement' if supplemental else 'initial',
                            executor_queue_wait_ms=((worker_started[0] - acquired) * 1000
                                                    if worker_started else None))
            try:
                if bind is not None:
                    outcomes = await bounded_jobs(items, execute,
                        (lambda pair: budget.record_supplement_pair('scheduled', pair)) if supplemental else None,
                        (lambda pair: budget.record_supplement_pair('cancelled', pair)) if supplemental else None)
                else:
                    outcomes = []
                    for pair in items:
                        if not budget.remaining():
                            break
                        queued = time.perf_counter()
                        if supplemental:
                            budget.record_supplement_pair('scheduled', pair)
                        try:
                            outcomes.append((pair, read(pair, session)))
                        except Exception as exc:
                            if is_unknown_provider_error(exc):
                                raise
                            outcomes.append((pair, exc))
                        finally:
                            # Same-session SQL stays on its owner task and is
                            # non-preemptible. Report a soft deadline honestly.
                            budget.remaining()
                            record_target('bm25', pair[1], queued, queued, time.perf_counter(),
                                query_index=pair[0] + (len(base) if supplemental else 0),
                                phase='supplement' if supplemental else 'initial', executor_queue_wait_ms=0.0)
                for (index, kb), rows in outcomes:
                    pair_count += 1
                    if isinstance(rows, Exception):
                        lexical_error_count += 1
                        failures.add('bm25')
                        event('retrieval.bm25_failed', status='FALLBACK', error_type=type(rows).__name__)
                    else:
                        output[index].extend(rows)
                for rows in output:
                    rows.sort(key=lambda row: (-row['bm25_score'], row['chunk_id']))
                return output
            except BaseException:
                status = "FAILED"
                raise
            finally:
                durations['bm25'] += (time.perf_counter() - started) * 1000
                _hybrid_stage("lexical_supplement" if supplemental else "lexical_initial", started, status)

        async def vector_initial():
            started = time.perf_counter()
            started_routes['vector'] = started
            prepared = []
            output = []
            reuse = ({'query_vectors': RequestQueryVectors(targets)}
                     if _query_embedding_reuse_enabled() else {})
            try:
                # Only this owner task touches the caller Session. Embeddings
                # are resolved sequentially, so memo hits cannot race.
                for kb in targets:
                    if not vector_depths[kb] or not budget.remaining():
                        continue
                    try:
                        if reuse:
                            target_dimensions[kb] = self._known_target_dimension(session, kb)
                            reuse['query_vectors'].target_dimensions[kb] = target_dimensions[kb]
                        vector = await self._timed_embedding(session, kb, query, **reuse)
                        ids, builds = self._timed_vector_scope(session, kb)
                        prepared.append((kb, vector, ids, builds))
                    except Exception as exc:
                        if is_unknown_provider_error(exc):
                            raise
                        failures.add('vector')
                        event('retrieval.vector_failed', status='FALLBACK', error_type=type(exc).__name__)
                _configure_vector_physical_budget(budget,
                    {kb: (ids, builds) for kb, vector, ids, builds in prepared}, vector_depths, query, original_query)
                async def execute(item):
                    kb, vector, ids, builds = item
                    queued = time.perf_counter()
                    async with gate:
                        acquired = time.perf_counter()
                        budget.check()
                        try:
                            try:
                                rows = await self._query_vector_store(kb_id=kb, query_vector=vector,
                                    top_k=vector_depths[kb], score_threshold=score_threshold,
                                    allowed_ids=ids, allowed_build_ids=builds)
                            except VectorPageBudgetExceeded as exc:
                                budget.vector_work_exhausted = True
                                failures.add('vector')
                                rows = exc.partial_results
                            return [{**row, 'kb_id': kb} for row in rows]
                        finally:
                            record_target('vector', kb, queued, acquired, time.perf_counter())
                if bind is not None:
                    outcomes = await bounded_jobs(prepared, execute)
                else:
                    outcomes = []
                    for item in prepared:
                        if not budget.remaining():
                            break
                        try:
                            outcomes.append((item, await budget.wait_for(execute(item))))
                        except Exception as exc:
                            if is_unknown_provider_error(exc):
                                raise
                            outcomes.append((item, exc))
                for _, rows in outcomes:
                    if isinstance(rows, Exception):
                        failures.add('vector')
                        event('retrieval.vector_failed', status='FALLBACK', error_type=type(rows).__name__)
                    else:
                        output.extend(rows)
                return sorted(output, key=lambda row: (-row['score'], row['id']))
            finally:
                ended_routes['vector'] = time.perf_counter()
                durations['vector'] = (ended_routes['vector'] - started) * 1000

        async def lexical_initial():
            started_routes['bm25'] = time.perf_counter()
            try:
                return await lexical_pairs(initial_pairs, initial_depths, base)
            finally:
                ended_routes['bm25'] = time.perf_counter()

        lexical_task = None
        token = _active_retrieval_budget.set(budget)
        try:
            if bind is not None:
                # Independent reader begins while the owner resolves embeddings.
                lexical_task = asyncio.create_task(lexical_initial())
            vector_results = await vector_initial()
            initial_lists = (await lexical_task if lexical_task is not None
                             else await lexical_initial())
            vector_results = self._authorized_route_rows(session, vector_results, targets)
            initial_lists = [self._authorized_route_rows(session, rows, targets) for rows in initial_lists]
            initial_bm25 = aggregate_bm25_results(initial_lists)
            union_ids = {row['id'] for row in vector_results} | {row['chunk_id'] for row in initial_bm25}
            insufficient = len(union_ids) < top_k
            variants = expand_queries(base)[:budget.max_variants] if insufficient and budget.remaining() else []
            expansion_targets = _expansion_target_order(targets, query, original_query,
                min(budget.max_variant_target_pairs, budget.lexical_supplement_depth))
            supplemental_pairs = [(index, kb) for index in range(len(variants)) for kb in expansion_targets]
            eligible_count = len(supplemental_pairs)
            supplemental_pairs = supplemental_pairs[:budget.max_variant_target_pairs]
            supplemental_depths = fair_depths(budget.lexical_supplement_depth, supplemental_pairs)
            # Initial rows are reused verbatim; there is no second base recall.
            supplements = []
            supplement_failed = False
            # A failed optional variant stops later variants. Parallelism is
            # bounded across targets of the same variant, preserving fallback.
            for index in dict.fromkeys(index for index, kb in supplemental_pairs):
                if not budget.remaining():
                    break
                pairs = [pair for pair in supplemental_pairs if pair[0] == index and supplemental_depths[pair]]
                if not pairs:
                    continue
                errors_before = lexical_error_count
                rows = await lexical_pairs(pairs, supplemental_depths, variants, supplemental=True)
                supplements.extend(rows[index:index + 1])
                if lexical_error_count > errors_before:
                    supplement_failed = True
                    break
            supplements = [self._authorized_route_rows(session, rows, targets) for rows in supplements]
            bm25_results = aggregate_bm25_results(initial_lists + supplements)
            overlap = max(0.0, min(ended_routes.values()) - max(started_routes.values())) * 1000
            budget.remaining()
            _, supplement_work = budget.work_snapshot()
            executed_variant_count = len({pair[0] for pair in supplement_work['started']})
            executed_supplement_pairs = len(supplement_work['started'])
            planned_pairs = {pair for pair in supplemental_pairs if supplemental_depths[pair] > 0}
            planned_targets = {kb for pair_index, kb in planned_pairs}
            scheduled_targets = {kb for pair_index, kb in supplement_work['scheduled']}
            completed_targets = {kb for pair_index, kb in supplement_work['completed']}
            details = {
                **budget.metrics(),
                'initial_candidate_count': len(union_ids),
                'initial_scope_coverage_complete': initial_scope_complete,
                'initial_target_query_coverage_complete': not initial_vector_omitted and not initial_omitted_pairs,
                'initial_baseline_depth_complete': initial_scope_complete,
                'initial_vector_depth_limited_target_ids': initial_vector_depth_limited,
                'initial_bm25_depth_limited_pairs': initial_bm25_depth_limited,
                'initial_vector_budget_omitted_target_ids': initial_vector_omitted,
                'initial_bm25_budget_omitted_target_ids': initial_bm25_omitted,
                'initial_bm25_budget_fully_omitted_target_ids': initial_bm25_fully_omitted,
                'initial_bm25_budget_omitted_pairs': initial_bm25_omitted_pairs,
                'initial_budget_omitted_target_ids': sorted(set(initial_vector_omitted + initial_bm25_omitted)),
                'initial_bm25_budget_omitted_pair_count': initial_omitted_pairs,
                'bm25_base_query_count': len(base),
                'bm25_query_count': len(base) + executed_variant_count,
                'expansion_query_count': executed_variant_count,
                'expansion_triggered': insufficient,
                'expansion_failed': supplement_failed,
                'expansion_variant_target_pairs': executed_supplement_pairs,
                'expansion_pairs_capped': eligible_count > len(supplemental_pairs),
                'expansion_target_selection_policy': 'query_rotated_round_robin_v1',
                'expansion_requested_target_count': len(targets) if variants else 0,
                'expansion_planned_target_ids': sorted(planned_targets),
                'expansion_scheduled_target_ids': sorted(scheduled_targets),
                'expansion_uncovered_target_ids': sorted(set(targets) - completed_targets) if variants else [],
                'expansion_budget_uncovered_target_ids': sorted(set(targets) - planned_targets) if variants else [],
                'expansion_completed_target_ids': sorted(completed_targets),
                'expansion_target_coverage_complete': not variants or completed_targets == set(targets),
                'expansion_planned_target_coverage_complete': not variants or planned_targets == set(targets),
                'supplement_pairs_planned': len(planned_pairs),
                'supplement_variants_planned': len({pair[0] for pair in planned_pairs}),
                'expansion_depth_limited': any(depth == 0 for depth in supplemental_depths.values()),
                'serial_sql_deadline_policy': 'SOFT_NONPREEMPTIBLE_SAME_SESSION' if bind is None else 'INDEPENDENT_EXECUTOR_WAIT',
                'retrieval_deadline_overrun_ms': max(0.0, time.perf_counter() - budget.deadline) * 1000,
                'vector_deadline_policy': ('ASYNC_EXECUTOR_WAIT' if chroma_executor_enabled()
                                           else 'SYNC_DRIVER_OFF_LOOP_CANCEL_DRAINS_ACTUAL_WORK'),
                'bm25_target_pair_count': pair_count,
                'initial_routes_overlap_ms': overlap,
                'target_timings': target_timings,
                'target_allocations': [{'kb_id': kb, 'vector_depth': vector_depths[kb],
                    'embedding_dimension': target_dimensions.get(kb),
                    'embedding_dimension_status': ('KNOWN' if target_dimensions.get(kb) is not None else
                        'UNKNOWN' if _query_embedding_reuse_enabled() else 'NOT_REQUESTED'),
                    'bm25_initial_depth': sum(depth for (index, target), depth in initial_depths.items() if target == kb),
                    'bm25_supplement_depth': sum(depth for (index, target), depth in supplemental_depths.items() if target == kb)}
                    for kb in targets],
                'budget_rule_version': 'request_totals_fair_depth_v1',
            }
            event('retrieval.query_expansion', **details,
                  insufficient_rule='valid_initial_unique_count_lt_top_k', rule_version='local_keywords_bm25_only_v2')
            candidates('retrieval.vector', vector_results)
            candidates('retrieval.bm25', bm25_results)
            return (vector_results, bm25_results, details,
                    bool(failures or budget.deadline_exhausted or not initial_scope_complete), len(failures),
                    durations['vector'], durations['bm25'])
        finally:
            _active_retrieval_budget.reset(token)
            if lexical_task is not None:
                if not lexical_task.done():
                    lexical_task.cancel()
                await asyncio.gather(lexical_task, return_exceptions=True)

    async def _parallel_hybrid_routes(self, session, bind, query, original_query, kb_ids, top_k, score_threshold):
        return await self._budgeted_hybrid_routes(session, bind, query, original_query,
            kb_ids, top_k, score_threshold, RetrievalBudget.from_environment(
                target_count=len(set(kb_ids)),
                base_query_count=len(distinct_queries([query, original_query])),
                lexical_depth_per_pair=settings.BM25_TOP_K))

    async def _hybrid_search(
        self, session: Session, query: str, kb_ids: List[str], top_k: int,
        score_threshold: float, vector_weight: float, bm25_weight: float,
        original_query: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any], bool]:
        """One vector route plus one aggregated lexical route, Weighted RRF then minmax."""
        hybrid_started = time.perf_counter()
        bind, lexical_mode = self._lexical_execution_scope(session)
        event("retrieval.hybrid_execution", parallel=bind is not None, reason=lexical_mode)
        budget = RetrievalBudget.from_environment(target_count=len(set(kb_ids)),
            base_query_count=len(distinct_queries([query, original_query])),
            lexical_depth_per_pair=settings.BM25_TOP_K)
        vector_results, bm25_results, expansion_metrics, fallback_used, failed_routes, vector_latency, bm25_latency = await self._budgeted_hybrid_routes(
            session, bind, query, original_query, kb_ids, top_k, score_threshold,
            budget)
        expansion_metrics["hybrid_initial_parallel"] = bind is not None
        expansion_metrics["hybrid_execution_reason"] = lexical_mode
        print(f"HYBRID_TIMING mode={'parallel' if bind is not None else 'serial'} reason={lexical_mode} "
              f"vector_ms={vector_latency:.3f} bm25_ms={bm25_latency:.3f} "
              f"vector_candidates={len(vector_results)} bm25_candidates={len(bm25_results)}")
        _hybrid_stage("routes_wall_including_admission", hybrid_started, "FINISHED")
        if not vector_results and not bm25_results:
            raise _HybridNoCandidates(failed_routes=(failed_routes or
                int(expansion_metrics.get('retrieval_deadline_exhausted', False))),
                metrics={**expansion_metrics, 'vector_latency_ms': vector_latency,
                         'bm25_latency_ms': bm25_latency})

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
        token = _active_retrieval_budget.set(budget)
        try:
            merged_results = self._enrich_results(session, selected, kb_ids)
        finally:
            _active_retrieval_budget.reset(token)
        metrics = {
            "vector_count": len({r["id"] for r in vector_results}), "bm25_count": len(bm25_results),
            "merged_count": len(merged_results), "vector_latency_ms": vector_latency,
            "bm25_latency_ms": bm25_latency, "fusion_method": "weighted_rrf_then_minmax", "rrf_k": 60,
            **expansion_metrics,
        }
        return merged_results, metrics, fallback_used

    @timed_stage('retrieval_enrichment')
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
        # Revalidate final delivery as well as the pre-fusion candidate pool.
        # A live source can change while independent readers are running.
        if allowed_kb_ids is not None:
            results = self._authorized_route_rows(session, results, allowed_kb_ids)
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
            if (not file_doc or file_doc.deleted_at is not None or (allowed_kb_ids is not None and file_doc.kb_id not in allowed_kb_ids)
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
            if chunk.source_metadata_json:
                import json
                provenance = json.loads(chunk.source_metadata_json)

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

"""
向量存储服务 (ChromaDB)
"""

import chromadb
from chromadb.config import Settings as ChromaSettings
from typing import List, Dict, Any, Optional
import functools
import hashlib
import heapq
import json
import importlib.metadata
import math
import threading
import time

import httpx

from app.core.config import get_settings
from app.services.performance_timing import timed_stage

settings = get_settings()


def _chroma_stage(stage, started, status):
    # Content-free telemetry must never replace the original result/exception.
    try:
        print("CHROMA_STAGE " + json.dumps(dict(stage=stage,
            wall_ms=(time.perf_counter() - started) * 1000, status=status),
            sort_keys=True), flush=True)
    except Exception:
        pass


def _ingest_counts(operation, **counts):
    """Content-free companion counters for the existing synchronous timings."""
    try:
        from app.services.observability import event
        event('ingestion.chroma.counts', operation=operation, **counts)
    except Exception:
        pass


class VectorBudgetCapabilityError(RuntimeError):
    """An active request budget cannot enforce physical vector limits."""
    provider_status = 'UNKNOWN'


class VectorPageBudgetExceeded(RuntimeError):
    """Physical vector work cap stopped complete scope coverage."""
    def __init__(self, kb_id, partial_results):
        super().__init__('VECTOR_PAGE_BUDGET_EXCEEDED')
        self.kb_id = kb_id
        self.partial_results = partial_results


class VectorPageReconciliationError(RuntimeError):
    """A selected query-time body could not be verified after bounded retention."""
    provider_status = 'UNKNOWN'


class _CollectionOperation:
    """One explicit ingestion operation; never cache a handle across files."""

    def __init__(self, service, kb_id):
        self._service, self._kb_id = service, kb_id
        self._collection = None
        self._resolved = False
        self._entered = False
        self._closed = False
        self._thread = None

    def __enter__(self):
        if self._entered or self._closed:
            raise RuntimeError('Collection operation cannot be re-entered')
        self._entered = True
        self._thread = threading.get_ident()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._closed = True
        self._collection = None

    def _resolve(self, kb_id, *, create=False):
        if not self._entered or self._closed or threading.get_ident() != self._thread:
            raise RuntimeError('Collection operation is not active on its owner thread')
        if kb_id != self._kb_id:
            raise ValueError('Collection operation cannot cross knowledge bases')
        if not self._resolved:
            self._collection = self._service._ingest_existing_collection(kb_id)
            self._resolved = True
        if create and self._collection is None:
            self._collection = self._service._ingest_get_or_create_collection(kb_id)
        return self._collection

    def validate_dimension(self, kb_id, vector):
        self._service._validate_collection_dimension(self._resolve(kb_id), vector)

    def add_documents(self, kb_id, documents, embeddings):
        self._service._add_documents_to_collection(self._resolve(kb_id, create=True), documents, embeddings)

    def get_documents(self, kb_id, ids, include=None):
        collection = self._resolve(kb_id)
        if collection is None:
            raise RuntimeError('Collection disappeared while verifying vectors')
        return self._service._get_documents_from_collection(collection, ids, include)

    def verify_documents(self, kb_id, documents, embeddings, ids):
        collection = self._resolve(kb_id)
        if collection is None:
            raise RuntimeError('Collection disappeared while verifying vectors')
        return self._service._verify_documents_in_collection(collection, documents, embeddings, ids)


class VectorStoreService:
    """向量存储服务"""

    _client = None
    _client_init_lock = threading.Lock()
    _query_adapter_lock = threading.Lock()

    @classmethod
    def get_client(cls) -> chromadb.Client:
        """获取 ChromaDB 客户端单例"""
        if cls._client is None:
            with cls._client_init_lock:
                if cls._client is None:
                    options = ChromaSettings(anonymized_telemetry=False, allow_reset=True)
                    host = getattr(settings, "CHROMA_HOST", "")
                    if host:
                        cls._client = chromadb.HttpClient(host=host, port=getattr(settings, "CHROMA_PORT", 8000), settings=options)
                    else:
                        cls._client = chromadb.PersistentClient(path=settings.CHROMA_DB_DIR, settings=options)
        return cls._client

    def __init__(self, *, lazy_retrieval=False):
        self.client = None if lazy_retrieval else self.get_client()
        self._lazy_lock = threading.Lock()
        self._owned_http = None

    def create_collection(self, kb_id: str) -> None:
        """创建知识库对应的 Collection"""
        collection_name = self._get_collection_name(kb_id)
        self.client.get_or_create_collection(
            name=collection_name,
            metadata={"kb_id": kb_id},
        )

    def delete_collection(self, kb_id: str) -> None:
        """删除知识库对应的 Collection"""
        collection_name = self._get_collection_name(kb_id)
        try:
            self.client.delete_collection(name=collection_name)
        except Exception:
            pass  # Collection 不存在时忽略

    def collection_operation(self, kb_id: str):
        """Reuse one collection handle for a single file's dimension/add/readback.

        Dimension checks still run afresh in every operation. A deleted/replaced
        handle fails normally; this scope never retries against another UUID.
        """
        return _CollectionOperation(self, kb_id)

    @timed_stage('ingest_chroma_collection_get')
    def _ingest_existing_collection(self, kb_id):
        return self._existing_collection(kb_id)

    @timed_stage('ingest_chroma_collection_get_or_create')
    def _ingest_get_or_create_collection(self, kb_id):
        return self.client.get_or_create_collection(name=self._get_collection_name(kb_id))

    def add_documents(
        self,
        kb_id: str,
        documents: List[Dict[str, Any]],
        embeddings: List[List[float]],
    ) -> None:
        """
        添加文档到向量库

        Args:
            kb_id: 知识库 ID
            documents: 文档列表，每个文档包含 id, content, metadata
            embeddings: 对应的向量列表
        """
        collection_name = self._get_collection_name(kb_id)
        collection = self.client.get_or_create_collection(name=collection_name)
        return self._add_documents_to_collection(collection, documents, embeddings)

    def _add_documents_to_collection(self, collection, documents, embeddings):
        # Preserve the public API's pre-write failure on malformed documents,
        # without retaining complete parallel ID/content/metadata lists.
        for doc in documents:
            _ = doc['id'], doc['content'], doc.get('metadata', {})
        try:
            size = self._batch_size()
            for start in range(0, len(documents), size):
                end = start + size
                batch = documents[start:end]
                self._ingest_add(collection, ids=[doc['id'] for doc in batch],
                                 documents=[doc['content'] for doc in batch],
                                 embeddings=embeddings[start:end],
                                 metadatas=[doc.get('metadata', {}) for doc in batch])
        except Exception as exc:
            if "dimension" in str(exc).lower():
                raise ValueError("Embedding dimension is incompatible with the existing collection; explicit migration is required") from exc
            raise

    def _batch_size(self):
        maximum = getattr(self.client, 'get_max_batch_size', None)
        return max(1, min(1000, int(maximum()))) if callable(maximum) else 500

    def get_documents(self, kb_id, ids, include=None):
        collection = self._existing_collection(kb_id)
        if collection is None:
            raise RuntimeError('Collection disappeared while verifying vectors')
        return self._get_documents_from_collection(collection, ids, include)

    def verify_documents(self, kb_id, documents, embeddings, ids):
        """Read and verify every expected field without retaining all responses."""
        collection = self._existing_collection(kb_id)
        if collection is None:
            raise RuntimeError('Collection disappeared while verifying vectors')
        return self._verify_documents_in_collection(collection, documents, embeddings, ids)

    def _verify_documents_in_collection(self, collection, documents, embeddings, ids):
        # Check the complete manifest first; each expected document is then read
        # once, even when the manifest order differs from the document order.
        expected_ids = {doc['id'] for doc in documents}
        if (len(documents) != len(embeddings) or len(ids) != len(documents) or
                len(expected_ids) != len(documents) or set(ids) != expected_ids):
            raise RuntimeError('BUILD_VECTOR_VERIFICATION_FAILED')
        del expected_ids
        size = self._batch_size()
        for start in range(0, len(documents), size):
            batch = documents[start:start + size]
            page = self._ingest_get(collection, ids=[doc['id'] for doc in batch],
                                    include=['documents', 'metadatas', 'embeddings'])
            self._verify_document_batch(page, batch, embeddings[start:start + size])
            # Chroma returns materialized embeddings. Release this response
            # before requesting the next batch, rather than aggregating them.
            del page

    @staticmethod
    def _verify_document_batch(actual, documents, embeddings):
        try:
            if any(actual.get(key) is None or len(actual[key]) != len(documents)
                   for key in ('ids', 'documents', 'metadatas', 'embeddings')):
                raise RuntimeError('BUILD_VECTOR_VERIFICATION_FAILED')
            observed = {key: (content, metadata, vector) for key, content, metadata, vector in zip(
                actual['ids'], actual['documents'], actual['metadatas'], actual['embeddings'])}
            if len(observed) != len(documents) or any(
                d['id'] not in observed or observed[d['id']][0] != d['content'] or
                observed[d['id']][1] != d['metadata'] or
                len(observed[d['id']][2]) != len(vector) or
                any(not math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-7)
                    for a, b in zip(observed[d['id']][2], vector))
                for d, vector in zip(documents, embeddings)):
                raise RuntimeError('BUILD_VECTOR_VERIFICATION_FAILED')
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RuntimeError('BUILD_VECTOR_VERIFICATION_FAILED') from exc

    def _get_documents_from_collection(self, collection, ids, include=None):
        include = include or ['documents', 'metadatas']
        result = {'ids': [], **{key: [] for key in include}}
        size = self._batch_size()
        for start in range(0, len(ids), size):
            page = self._ingest_get(collection, ids=ids[start:start + size], include=include)
            result['ids'].extend(page['ids'])
            for key in include:
                if page.get(key) is not None:
                    result[key].extend(page[key])
        return result

    def validate_dimension(self, kb_id: str, vector: List[float]) -> None:
        """Check an existing collection without deleting or writing any vectors."""
        collection = self._existing_collection(kb_id)
        return self._validate_collection_dimension(collection, vector)

    def _validate_collection_dimension(self, collection, vector):
        if collection is not None and self._ingest_count(collection):
            try:
                self._ingest_dimension_query(collection, query_embeddings=[vector], n_results=1)
            except Exception as exc:
                if "dimension" in str(exc).lower():
                    raise ValueError("Embedding dimension is incompatible with the existing collection; explicit migration is required") from exc
                raise

    def _existing_collection(self, kb_id: str):
        try:
            return self.client.get_collection(name=self._get_collection_name(kb_id))
        except Exception as exc:
            # Chroma versions expose different names for an absent collection.
            missing_types = tuple(
                cls for cls in (getattr(chromadb.errors, "NotFoundError", None),
                                getattr(chromadb.errors, "InvalidCollectionException", None))
                if isinstance(cls, type)
            )
            if (missing_types and isinstance(exc, missing_types)) or (
                isinstance(exc, ValueError) and "does not exist" in str(exc).lower()
            ):
                return None
            raise

    @timed_stage('ingest_chroma_count')
    def _ingest_count(self, collection):
        count = collection.count()
        _ingest_counts('count', collection_vector_count=count)
        return count

    @timed_stage('ingest_chroma_dimension_query')
    def _ingest_dimension_query(self, collection, **kwargs):
        _ingest_counts('dimension_query', vector_count=len(kwargs['query_embeddings']))
        return collection.query(**kwargs)

    @timed_stage('ingest_chroma_add')
    def _ingest_add(self, collection, **kwargs):
        _ingest_counts('add', vector_count=len(kwargs['embeddings']), document_count=len(kwargs['ids']))
        return collection.add(**kwargs)

    @timed_stage('ingest_chroma_get')
    def _ingest_get(self, collection, **kwargs):
        _ingest_counts('get', requested_vector_count=len(kwargs['ids']))
        result = collection.get(**kwargs)
        _ingest_counts('get_returned', vector_count=len(result['ids']))
        return result

    @timed_stage('chroma')
    def query(
        self,
        kb_id: str,
        query_vector: List[float],
        top_k: int = 5,
        score_threshold: float = 0.0,
        allowed_ids: Optional[List[str]] = None,
        allowed_build_ids: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        向量检索

        Args:
            kb_id: 知识库 ID
            query_vector: 查询向量
            top_k: 返回结果数量
            score_threshold: 相似度阈值

        Returns:
            检索结果列表
        """
        from app.services.retrieval_budget import active_retrieval_budget
        budget = active_retrieval_budget.get()
        if budget is not None and not callable(getattr(budget, 'reserve_vector_page', None)):
            raise VectorBudgetCapabilityError('VECTOR_BUDGET_CAPABILITY_UNAVAILABLE')

        if self.client is None:
            # Preserve the synchronous path if the process-level experiment is
            # disabled after this retrieval object was created.
            with self._lazy_lock:
                if self.client is None:
                    self.client = self.get_client()
        collection_name = self._get_collection_name(kb_id)

        def record_result(count):
            recorder = getattr(budget, 'record_vector_result', None)
            if callable(recorder):
                recorder(count)
        def consistency_failure():
            recorder = getattr(budget, 'record_vector_consistency_unknown', None)
            if callable(recorder):
                recorder()
            return VectorPageReconciliationError('VECTOR_PAGE_RECONCILIATION_FAILED')

        collection = self._existing_collection(kb_id)
        if collection is None:
            record_result(0)
            return []
        work_limited = False
        def reserve(stage, depth):
            nonlocal work_limited
            reservation = getattr(budget, 'reserve_vector_page', None)
            if callable(reservation) and not reservation(kb_id, depth, stage=stage):
                work_limited = True
                return False
            return True
        def record_get(stage, requested, returned):
            recorder = getattr(budget, 'record_vector_get', None)
            if callable(recorder):
                recorder(stage=stage, requested_count=requested, returned_count=returned)
        def scope_get(ids):
            if not reserve('scope_get', len(ids)):
                return None
            present = collection.get(ids=ids, include=[])['ids']
            record_get('scope_get', len(ids), len(present))
            return present
        def pages():
            # Keep just one finite scope page, instead of copying every legacy
            # ID page and its Chroma response before any KNN work starts.
            if allowed_build_ids is None and allowed_ids is None:
                yield {}
                return
            size = self._batch_size()
            # Preserve the legacy first-page existence preflight ordering;
            # later pages stay lazy and never accumulate copied ID lists.
            first_legacy = None
            if allowed_ids and (budget is None or budget.remaining()):
                first_legacy = scope_get(allowed_ids[:size])
                if work_limited:
                    return
            for start in range(0, len(allowed_build_ids or []), size):
                if budget is not None and not budget.remaining():
                    return
                group = allowed_build_ids[start:start + size]
                yield {'where': {'build_id': {'$in': group}}}
            for start in range(0, len(allowed_ids or []), size):
                if budget is not None and not budget.remaining():
                    return
                # An unindexed legacy/preview ID must not break scoped KNN.
                present = (first_legacy if start == 0 else
                    scope_get(allowed_ids[start:start + size]))
                if work_limited:
                    return
                if present:
                    yield {'ids': present}

        # Only retain top-k document bodies. Compact per-ID score and body
        # digests preserve last qualifying occurrence semantics across pages.
        # With arbitrary duplicate downgrades, an evicted winner may need an
        # exact readback after all pages have completed.
        def digest_body(content, metadata):
            raw = json.dumps([content, metadata], sort_keys=True, ensure_ascii=True,
                             separators=(',', ':'), allow_nan=True).encode('utf-8')
            return hashlib.sha256(raw).digest()

        records = {}
        retained = {}
        unbounded = [] if top_k < 0 else None
        for query_args in pages():
            if budget is not None and not budget.remaining():
                break
            if not reserve('knn_query', top_k):
                break
            results = collection.query(query_embeddings=[query_vector], n_results=top_k,
                include=['documents', 'metadatas', 'distances'], **query_args)
            ids = results['ids'][0] if results and results['ids'] and results['ids'][0] else []
            for i, doc_id in enumerate(ids):
                distance = results['distances'][0][i] if results['distances'] else 0
                score = 1 - distance / 2
                # The legacy code filters before deduplication: a later
                # below-threshold row cannot erase a qualifying earlier one.
                if not score >= score_threshold:
                    continue
                row = {'id': doc_id,
                       'content': results['documents'][0][i] if results['documents'] else '',
                       'metadata': results['metadatas'][0][i] if results['metadatas'] else {},
                       'score': score}
                if unbounded is not None:
                    unbounded.append(row)
                    continue
                if not top_k:
                    continue
                records[doc_id] = (score, digest_body(row['content'], row['metadata']))
                if doc_id in retained:
                    retained[doc_id] = row
                elif len(retained) < top_k:
                    retained[doc_id] = row
                else:
                    worst = max(retained, key=lambda key: (-retained[key]['score'], key))
                    if (-score, doc_id) < (-retained[worst]['score'], worst):
                        del retained[worst]
                        retained[doc_id] = row
            recorder = getattr(budget, 'record_vector_page', None)
            if callable(recorder):
                recorder(requested_depth=top_k, returned_count=len(ids),
                         retained_count=len(retained) if unbounded is None else len(unbounded))

        if unbounded is not None:
            deduplicated = {item['id']: item for item in unbounded}
            output = sorted(deduplicated.values(), key=lambda item: (-item['score'], item['id']))[:top_k]
            if work_limited:
                raise VectorPageBudgetExceeded(kb_id, output)
            record_result(len(output))
            return output
        selected = heapq.nsmallest(top_k, records, key=lambda key: (-records[key][0], key))
        missing = [key for key in selected if key not in retained]
        retained = {key: retained[key] for key in selected if key in retained}
        for start in range(0, len(missing), self._batch_size()):
            if budget is not None and not budget.remaining():
                raise consistency_failure()
            batch = missing[start:start + self._batch_size()]
            if not reserve('reconcile_get', len(batch)):
                partial = [retained[key] for key in selected if key in retained]
                raise VectorPageBudgetExceeded(kb_id, partial)
            try:
                page = collection.get(ids=batch, include=['documents', 'metadatas'])
                record_get('reconcile_get', len(batch), len(page['ids']))
                ids = page['ids']
                contents = page['documents']
                metadatas = page['metadatas']
                if (len(ids) != len(batch) or len(set(ids)) != len(ids) or set(ids) != set(batch)
                        or contents is None or metadatas is None
                        or len(contents) != len(ids) or len(metadatas) != len(ids)):
                    raise ValueError('invalid reconciliation result')
                for key, content, metadata in zip(ids, contents, metadatas):
                    score, expected_digest = records[key]
                    if digest_body(content, metadata) != expected_digest:
                        raise ValueError('changed reconciliation result')
                    retained[key] = {'id': key, 'content': content,
                                     'metadata': metadata, 'score': score}
            except Exception as exc:
                raise consistency_failure() from exc
        output = [retained[key] for key in selected]
        if work_limited:
            raise VectorPageBudgetExceeded(kb_id, output)
        record_result(len(output))
        return output

    def _prepare_query_timeout_adapter(self):
        """Bound only worker read requests for the pinned REST adapter.

        Only the query worker uses this adapter. Never patch HTTPX globally,
        replace transports, or change session headers/proxy settings.
        """
        if not getattr(settings, "CHROMA_HOST", ""):
            return  # PersistentClient has no underlying HTTP request timeout.
        from app.services.bounded_chroma_executor import (
            ChromaExecutorCapabilityError, chroma_http_timeout_seconds, in_chroma_read_worker)
        if importlib.metadata.version("chromadb") != "1.3.7":
            raise ChromaExecutorCapabilityError("UNSUPPORTED_CHROMA_HTTP_TIMEOUT_ADAPTER_VERSION")
        session = getattr(getattr(self.client, "_server", None), "_session", None)
        if not isinstance(session, httpx.Client):
            raise ChromaExecutorCapabilityError("UNSUPPORTED_CHROMA_HTTP_SESSION_SHAPE")
        with self._query_adapter_lock:
            if getattr(session, "_rag_chroma_read_timeout_adapter", False):
                return
            original = session.request
            if not callable(original) or getattr(original, "__self__", None) is not session:
                raise ChromaExecutorCapabilityError("UNSUPPORTED_CHROMA_HTTP_REQUEST_SHAPE")
            @functools.wraps(original)
            def request(method, url, *args, **kwargs):
                if in_chroma_read_worker():
                    path = httpx.URL(url).path.rstrip("/")
                    if str(method).upper() not in ("GET", "HEAD") and not (
                            str(method).upper() == "POST" and path.endswith(("/get", "/query"))):
                        raise ChromaExecutorCapabilityError("CHROMA_WORKER_NON_READ_REQUEST")
                    kwargs["timeout"] = chroma_http_timeout_seconds()
                return original(method, url, *args, **kwargs)
            session.request = request
            session._rag_chroma_read_timeout_adapter = True

    def _cleanup_owned_http(self):
        with self._lazy_lock:
            if self._owned_http is not None:
                self._owned_http.close()
                self._owned_http = None
                self.client = None

    def _query_with_bootstrap(self, **kwargs):
        started = time.perf_counter()
        status = "RETURNED"
        try:
            from app.services.bounded_chroma_executor import chroma_http_timeout_seconds, register_chroma_cleanup
            if self.client is None:
                with self._lazy_lock:
                    if self.client is None:
                        host = getattr(settings, "CHROMA_HOST", "")
                        if host:
                            from app.services.bounded_chroma_bootstrap import OwnedChromaHTTP
                            register_chroma_cleanup(self, self._cleanup_owned_http)
                            if self._owned_http is None:
                                self._owned_http = OwnedChromaHTTP(host, getattr(settings, "CHROMA_PORT", 8000), chroma_http_timeout_seconds())
                            self.client = self._owned_http.create()
                        else:
                            # Persistent Chroma systems are shared by path; retain the
                            # existing shared owner rather than closing another user.
                            self.client = self.get_client()
            if getattr(self, '_owned_http', None) is not None:
                register_chroma_cleanup(self, self._cleanup_owned_http)
            self._prepare_query_timeout_adapter()
        except BaseException:
            status = "FAILED"
            raise
        finally:
            _chroma_stage("bootstrap", started, status)
        started = time.perf_counter()
        status = "RETURNED"
        try:
            return self.query(**kwargs)
        except BaseException:
            status = "FAILED"
            raise
        finally:
            _chroma_stage("query", started, status)

    async def query_async(self, **kwargs):
        """Submit bootstrap and the unchanged query as one bounded worker job."""
        from app.services.bounded_chroma_executor import chroma_http_timeout_seconds, run_chroma_query
        chroma_http_timeout_seconds()  # Validate before submitting any work.
        return await run_chroma_query(self._query_with_bootstrap, **kwargs)

    def delete_ids(self, kb_id: str, ids: List[str]) -> None:
        """Delete only the explicitly retired vectors; propagate storage failures."""
        if not ids:
            return
        collection = self._existing_collection(kb_id)
        if collection is None:
            return  # A confirmed missing collection already contains none of these IDs.
        size = self._batch_size()
        for start in range(0, len(ids), size):
            collection.delete(ids=ids[start:start + size])

    def delete_by_file_id(self, kb_id: str, file_id: str) -> None:
        """删除指定文件的所有向量"""
        collection = self._existing_collection(kb_id)
        if collection is not None:
            collection.delete(where={"file_id": file_id})

    def get_count(self, kb_id: str) -> int:
        """获取知识库中的向量数量"""
        collection_name = self._get_collection_name(kb_id)

        try:
            collection = self.client.get_collection(name=collection_name)
            return collection.count()
        except Exception:
            return 0

    @staticmethod
    def _get_collection_name(kb_id: str) -> str:
        """生成 Collection 名称"""
        # ChromaDB collection name 有限制，需要处理
        return f"kb_{kb_id.replace('-', '_')}"

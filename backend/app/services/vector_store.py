"""
向量存储服务 (ChromaDB)
"""

import chromadb
import functools
import importlib.metadata
import threading
import time
import json
import httpx
from chromadb.config import Settings as ChromaSettings
from typing import List, Dict, Any, Optional

from app.core.config import get_settings

settings = get_settings()


def _chroma_stage(stage, started, status):
    # Content-free telemetry must never replace the original result/exception.
    try:
        print("CHROMA_STAGE " + json.dumps(dict(stage=stage,
            wall_ms=(time.perf_counter() - started) * 1000, status=status),
            sort_keys=True), flush=True)
    except Exception:
        pass


class VectorStoreService:
    """向量存储服务"""

    _client = None
    _query_adapter_lock = threading.Lock()

    @classmethod
    def get_client(cls) -> chromadb.Client:
        """获取 ChromaDB 客户端单例"""
        if cls._client is None:
            options = ChromaSettings(
                    anonymized_telemetry=False,
                    allow_reset=True,
                )
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

        ids = [doc["id"] for doc in documents]
        contents = [doc["content"] for doc in documents]
        metadatas = [doc.get("metadata", {}) for doc in documents]

        try:
            size = self._batch_size()
            for start in range(0, len(ids), size):
                end = start + size
                collection.add(ids=ids[start:end], documents=contents[start:end],
                               embeddings=embeddings[start:end], metadatas=metadatas[start:end])
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
        include = include or ['documents', 'metadatas']
        result = {'ids': [], **{key: [] for key in include}}
        size = self._batch_size()
        for start in range(0, len(ids), size):
            page = collection.get(ids=ids[start:start + size], include=include)
            result['ids'].extend(page['ids'])
            for key in include:
                if page.get(key) is not None:
                    result[key].extend(page[key])
        return result

    def validate_dimension(self, kb_id: str, vector: List[float]) -> None:
        """Check an existing collection without deleting or writing any vectors."""
        collection = self._existing_collection(kb_id)
        if collection is not None and collection.count():
            try:
                collection.query(query_embeddings=[vector], n_results=1)
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
        if self.client is None:
            with self._lazy_lock:
                if self.client is None:
                    self.client = self.get_client()
        collection_name = self._get_collection_name(kb_id)

        collection = self._existing_collection(kb_id)
        if collection is None:
            return []

        pages = []
        if allowed_build_ids is None and allowed_ids is None:
            pages.append({})
        else:
            size = self._batch_size()
            for start in range(0, len(allowed_build_ids or []), size):
                group = allowed_build_ids[start:start + size]
                pages.append({'where': {'build_id': {'$in': group}}})
            for start in range(0, len(allowed_ids or []), size):
                # Chroma's ID-constrained KNN can fail when even one requested
                # legacy SQL ID has no vector (e.g. an unindexed preview).
                present = collection.get(ids=allowed_ids[start:start + size], include=[])['ids']
                if present:
                    pages.append({'ids': present})

        # 处理结果
        output = []
        for query_args in pages:
            results = collection.query(query_embeddings=[query_vector], n_results=top_k,
                include=['documents', 'metadatas', 'distances'], **query_args)
            if not results or not results['ids'] or not results['ids'][0]:
                continue
            for i, doc_id in enumerate(results["ids"][0]):
                # ChromaDB 返回的是距离，转换为相似度
                distance = results["distances"][0][i] if results["distances"] else 0
                # 使用余弦距离转相似度: similarity = 1 - distance/2
                score = 1 - distance / 2

                if score >= score_threshold:
                    output.append({
                        "id": doc_id,
                        "content": results["documents"][0][i] if results["documents"] else "",
                        "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                        "score": score,
                    })

        deduplicated = {item['id']: item for item in output}
        return sorted(deduplicated.values(), key=lambda item: (-item['score'], item['id']))[:top_k]

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

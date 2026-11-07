"""Callable isolated Hybrid experiment components; never launches a provider itself.

Caller owns authorization, durable accounting, egress restrictions, process isolation,
preflight, and output lifecycle. Import only after isolated environment configuration.
"""
from contextlib import asynccontextmanager
from pathlib import Path
import os
import sys

ROOT = Path(__file__).resolve().parents[1]
KB_ID = 'm2-ab-kb'
BASE_URL = 'https://api.siliconflow.cn/v1'
EMBEDDING_MODEL = 'BAAI/bge-m3'
RERANK_MODEL = 'BAAI/bge-reranker-v2-m3'
CHAT_MODEL = 'Qwen/Qwen3.5-4B'
FIXED_SEARCH = dict(search_mode='hybrid', top_k=10, score_threshold=0.0,
                    vector_weight=0.7, bm25_weight=0.3, rerank_enabled=True,
                    rerank_score_threshold=0.0, rerank_top_k=10,
                    use_rewrite=False, context_window=0)

class ForbiddenFallback(BaseException):
    """Do not let the application silently score reranking with chat."""


def configure_runtime(output, redis_url, api_key):
    """Configure BEFORE any app import, in a fresh process with a fresh output."""
    output = Path(output).resolve(strict=True)
    if not api_key or any((output / name).exists() for name in ('isolated.db', 'chroma')):
        raise ValueError('Credential or empty isolated runtime required')
    if any(name == 'app' or name.startswith('app.') for name in sys.modules):
        raise RuntimeError('Application already imported; use a fresh process')
    os.environ.update(ENV='test', DATABASE_URL='sqlite:///' + str(output / 'isolated.db'),
        CHROMA_DB_DIR=str(output / 'chroma'), UPLOAD_DIR=str(output / 'uploads'),
        IMAGE_DIR=str(output / 'images'), MEMORY_REDIS_URL=redis_url, REDIS_URL=redis_url,
        LLM_BASE_URL=BASE_URL, LLM_API_KEY=api_key, LLM_MODEL=CHAT_MODEL,
        EMBEDDING_BASE_URL=BASE_URL, EMBEDDING_API_KEY=api_key, EMBEDDING_MODEL=EMBEDDING_MODEL,
        RAG_MODEL_ROUTER_MODE='OFF', RAG_GROUNDED_GENERATION='true', LANGFUSE_ENABLED='false',
        RAG_METRICS_ENABLED='false', RAG_OBSERVABILITY_ENABLED='true',
        RAG_SNAPSHOTS_ENABLED='false', RAG_OBSERVATION_LOG=str(output / 'observations.jsonl'),
        ANONYMIZED_TELEMETRY='False')
    sys.path.insert(0, str(ROOT / 'backend'))


@asynccontextmanager
async def bound_provider(adapter, api_key):
    """Route HTTPX requests through injected adapter and disallow chat rerank fallback.

    The caller must separately block all non-authorized socket egress; this is not
    a budget gate. Context must be set before each sequential request/trial.
    """
    import httpx
    from openai import AsyncOpenAI
    from langchain_openai import ChatOpenAI
    from app.services.llm import LLMService
    from app.services.rerank import RerankService
    from app.models import CustomModel, ModelType
    old_async, old_sync = httpx.AsyncClient.send, httpx.Client.send
    old_client, old_model = LLMService._get_client, LLMService.get_chat_model
    old_init = LLMService.__init__
    old_default, old_fallback = RerankService.get_default_rerank_model, RerankService._rerank_via_chat
    async def send_async(client, request, *args, **kwargs):
        return await adapter.async_send(request)
    def send_sync(client, request, *args, **kwargs):
        return adapter.sync_send(request)
    def fallback(*args, **kwargs):
        raise ForbiddenFallback('Rerank chat fallback forbidden')
    def init(service, *args, **kwargs):
        old_init(service, *args, **kwargs)
        service._router_extra_body = {'enable_thinking': False}
    async_client = httpx.AsyncClient(trust_env=False, follow_redirects=False)
    sync_client = httpx.Client(trust_env=False, follow_redirects=False)
    def get_client(service):
        if service._client is None:
            service._client = AsyncOpenAI(base_url=service.base_url, api_key=service.api_key,
                                          timeout=120, max_retries=0, http_client=async_client)
        return service._client
    def get_model(service, temperature=.5, streaming=True, max_tokens=None):
        return ChatOpenAI(model=service.model, openai_api_key=service.api_key,
            openai_api_base=service.base_url, temperature=temperature, streaming=streaming,
            max_tokens=max_tokens, max_retries=0, extra_body={'enable_thinking': False},
            http_async_client=async_client, http_client=sync_client)
    def default_model(_service):
        return CustomModel(name='isolated rerank', model_type=ModelType.RERANK,
            base_url=BASE_URL, api_key=api_key, model_name=RERANK_MODEL, is_active=True)
    httpx.AsyncClient.send, httpx.Client.send = send_async, send_sync
    LLMService.__init__, LLMService._get_client, LLMService.get_chat_model = init, get_client, get_model
    RerankService.get_default_rerank_model, RerankService._rerank_via_chat = default_model, fallback
    try:
        yield
    finally:
        httpx.AsyncClient.send, httpx.Client.send = old_async, old_sync
        LLMService.__init__, LLMService._get_client, LLMService.get_chat_model = old_init, old_client, old_model
        RerankService.get_default_rerank_model, RerankService._rerank_via_chat = old_default, old_fallback
        await async_client.aclose()
        sync_client.close()


async def build_index(manifest, source_db, adapter, expected_source_sha256=None):
    """Copy only the validated SQLite baseline, then build the missing vector index."""
    import sqlite3
    import hashlib
    from sqlmodel import Session
    from app.core.database import engine
    from app.models import KnowledgeBase
    from app.services.embedding import EmbeddingService
    from app.services.vector_store import VectorStoreService
    chunks = manifest['chunks']
    expected = {c['id']: (c['source_id'], c['content']) for c in chunks}
    if len(chunks) != 80 or len(expected) != 80:
        raise ValueError('Exactly 80 distinct frozen chunks required; caller must verify manifest hash')
    source = Path(source_db).resolve(strict=True)
    destination = Path(os.environ['DATABASE_URL'].removeprefix('sqlite:///')).resolve()
    if source == destination or destination.exists():
        raise ValueError('Fresh isolated destination required')
    def verify_source():
        if expected_source_sha256 is not None:
            wal = Path(str(source) + '-wal')
            if wal.exists() and wal.stat().st_size:
                raise ValueError('Source database has nonempty WAL')
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            if actual != expected_source_sha256:
                raise ValueError('Source database SHA256 differs from frozen plan')
    verify_source()
    with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src:
        rows = list(src.execute('SELECT id, file_id, content FROM document_chunks'))
        if len(rows) != 80 or {r[0]: (r[1], r[2]) for r in rows} != expected:
            raise ValueError('Source database differs from frozen chunks')
        verify_source()
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
        verify_source()
    with Session(engine) as session:
        kb = session.get(KnowledgeBase, KB_ID)
        if kb is None:
            raise ValueError('Baseline KB missing')
        kb.embedding_model = 'sys_embedding'
        session.add(kb)
        session.commit()
    import math
    vectors = []
    service = EmbeddingService()
    for start in range(0, len(chunks), 16):
        adapter.context = f'index:batch-{start // 16 + 1}'
        batch = chunks[start:start + 16]
        result = await service.embed_documents([c['content'] for c in batch])
        if len(result) != len(batch):
            raise ValueError('Embedding count mismatch')
        vectors.extend(result)
    if (not all(isinstance(v, list) and v for v in vectors)
            or len({len(v) for v in vectors}) != 1
            or any(type(n) not in (int, float) or not math.isfinite(n) for v in vectors for n in v)):
        raise ValueError('Invalid or inconsistent embeddings')
    store = VectorStoreService()
    store.add_documents(KB_ID, [{'id': c['id'], 'content': c['content'],
        'metadata': {'file_id': c['source_id'], 'kb_id': KB_ID}} for c in chunks], vectors)
    if store.get_count(KB_ID) != 80:
        raise RuntimeError('Isolated vector count mismatch')
    return {'chunks': 80, 'source_database': str(source), 'embedding_model': EMBEDDING_MODEL,
            'embedding_dimensions': len(vectors[0])}


async def trial(case, mode, arm, scope, adapter, on_event=None):
    """Collect actual app events; evidence and raw answer remain in returned events."""
    from app.services.chat import ChatService
    from app.services.memory import get_memory_service
    from app.schemas.schemas import Message
    if mode not in ('chat', 'agent') or arm not in ('A', 'D'):
        raise ValueError('Only authorized A/D chat/agent modes')
    memory = get_memory_service()
    await memory.reset_preferences(scope=scope)
    if arm == 'D':
        await memory.update_preference({'output_preferences': ['step_by_step']}, scope=scope)
    adapter.context = f"{case['qid']}:{mode}:{arm}"
    events = []
    async for event, data in ChatService().chat_stream(messages=[Message(role='user', content=case['question'])],
            kb_ids=[KB_ID], mode=mode, model_id=None, memory_scope=scope, **FIXED_SEARCH):
        item = {'event': event, 'data': data}
        if on_event is not None:
            on_event(item)
        events.append(item)
    state = 'RETURNED' if any(x['event'] == 'done' for x in events) and not any(x['event'] == 'error' for x in events) else 'UNKNOWN'
    return {'qid': case['qid'], 'mode': mode, 'arm': arm, 'state': state,
            'search': dict(FIXED_SEARCH), 'events': events}

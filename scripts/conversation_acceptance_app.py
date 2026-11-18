"""Isolated acceptance API: real RAG pipeline, synthetic provider HTTP responses.

Run with DATABASE_URL pointing to a NEW acceptance-only SQLite file and
PYTHONPATH=backend;. then uvicorn scripts.conversation_acceptance_app:app.
Never import app.main: production startup, workers and recovery are excluded.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

# Must precede every application import, including package router registration.
ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault('DATABASE_URL', 'sqlite:///' + str(ROOT / 'var/server-chat-history-acceptance-20261114/browser.sqlite').replace('\\', '/'))
os.environ['ENV'] = 'test'
os.environ['RAG_OBSERVABILITY_ENABLED'] = 'false'
os.environ['RAG_MODEL_ROUTER_MODE'] = 'OFF'
os.environ['RAG_GROUNDED_STREAM_ENABLED'] = 'false'
os.environ['RAG_GROUNDED_GENERATION_ENABLED'] = 'true'
os.environ['MEMORY_REDIS_URL'] = 'redis://127.0.0.1:1/0'
os.environ['CHROMA_DB_DIR'] = str(ROOT / 'var/server-chat-history-acceptance-20261114/unused-chroma')
os.environ['CHROMA_HOST'] = ''
os.environ.pop('OPENAI_PROXY', None)

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, SQLModel, create_engine
from app.api import conversations, chat, settings as settings_api, knowledge_base
from app.core.database import get_session
from app.core.settings_model import PersistedSystemSettings
from app.models import KnowledgeBase, FileDocument, FileStatus, DocumentChunk, FileIndexBuild, CustomModel, ModelType
from app.services.bm25 import BM25Index
from app.services.conversation_store import ConversationStore
from app.services.runtime_settings import RuntimeValues, _encode

SOURCE = 'Atlas synthetic launch policy: launch budget is 420 units. Atlas review owner is Mira. Synthetic evidence only.'
KB_ID = 'acceptance-atlas'
MODEL_ID = 'acceptance-model'


def create_app(db_path: Path):
    db_path = Path(db_path).resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine('sqlite:///' + str(db_path).replace('\\', '/'), connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as db:
        if db.get(KnowledgeBase, KB_ID) is None:
            digest = hashlib.sha256(SOURCE.encode()).hexdigest()
            manifest = json.dumps({'chunk_ids': ['atlas-chunk']})
            db.add(KnowledgeBase(id=KB_ID, name='Synthetic Atlas', embedding_model='none', vlm_model='none', chunk_count=1))
            db.add(CustomModel(id=MODEL_ID, name='Synthetic acceptance HTTP', model_type=ModelType.LLM, model_name='synthetic-model', base_url='http://acceptance.invalid/v1', api_key='synthetic-not-a-secret'))
            db.add(FileDocument(id='atlas-file', kb_id=KB_ID, name='synthetic-atlas.txt', local_path='acceptance-only-no-file', size=len(SOURCE), status=FileStatus.READY, active_build_id='atlas-build', sha256=digest))
            db.add(DocumentChunk(id='atlas-chunk', file_id='atlas-file', content=SOURCE))
            db.add(FileIndexBuild(build_id='atlas-build', file_id='atlas-file', kb_id=KB_ID, generation=1, task_id='synthetic', attempt_no=1, state='ACTIVE', manifest=manifest, manifest_hash=hashlib.sha256(manifest.encode()).hexdigest(), chunk_count=1))
            db.flush()
            BM25Index().index_chunk(db, 'atlas-chunk', SOURCE, KB_ID)
            db.add(PersistedSystemSettings(data=_encode(RuntimeValues(default_llm_model=MODEL_ID).model_dump(), 1)))
            db.commit()
    patches = []
    # A test helper import must not activate a Chroma executor for unrelated
    # retrieval tests. Scope this app's lazy-client switch to its own lifetime.
    p = patch.dict(os.environ, {'RAG_CHROMA_EXECUTOR_ENABLED': 'true'})
    p.start()
    patches.append(p)
    from app.core import database
    original_engine = database.engine
    patched_modules = set()
    # Rebind imported references too: dependency override alone misses ChatService.
    for name, module in list(sys.modules.items()):
        if name.startswith('app.') and hasattr(module, 'engine'):
            patched_modules.add(name)
            p = patch.object(module, 'engine', engine)
            p.start()
            patches.append(p)
    calls = []
    async def provider(request):
        assert request.url.host == 'acceptance.invalid', 'Unexpected provider destination blocked'
        body = json.loads(request.content)
        calls.append(body)
        app.state.provider_started = True
        if app.state.provider_delay_seconds:
            await asyncio.sleep(app.state.provider_delay_seconds)
        if app.state.provider_http_status != 200:
            return httpx.Response(app.state.provider_http_status, json={'error': {'message': 'Synthetic rejection', 'type': 'invalid_request_error'}})
        answer = 'Atlas launch budget is 420 units; review owner is Mira [1].'
        combined = json.dumps(body.get('messages', []), ensure_ascii=False)
        if 'rewrite_query' in combined and 'intent' in combined:
            answer = json.dumps({'rewrite_query': 'Atlas launch budget', 'intent': 'kb_search'})
        elif 'binary_score' in combined:
            answer = json.dumps({'binary_score': 'yes'})
        elif '文档相关性评分员' in combined:
            answer = '0.95'
        result = {'id': 'synthetic-response', 'object': 'chat.completion', 'created': 1, 'model': 'synthetic-model', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': answer}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 10, 'completion_tokens': 10, 'total_tokens': 20}}
        result['choices'][0]['finish_reason'] = app.state.provider_finish_reason
        if app.state.provider_empty:
            result['choices'][0]['message']['content'] = ''
        rows = body.get('messages', [])
        last_user = max((i for i, row in enumerate(rows) if row.get('role') == 'user'), default=-1)
        has_tool_result = any(row.get('role') == 'tool' for row in rows[last_user + 1:])
        if body.get('tools') and not has_tool_result:
            result['choices'][0]['message'] = {'role': 'assistant', 'content': None, 'tool_calls': [{'id': 'synthetic-tool', 'type': 'function', 'function': {'name': 'search_knowledge_base', 'arguments': json.dumps({'query': 'Atlas launch budget owner'})}}]}
            result['choices'][0]['finish_reason'] = 'tool_calls'
        if body.get('stream'):
            chunk = {'id': 'synthetic-stream', 'object': 'chat.completion.chunk', 'created': 1, 'model': 'synthetic-model', 'choices': [{'index': 0, 'delta': {'role': 'assistant', 'content': answer}, 'finish_reason': None}]}
            message = result['choices'][0]['message']
            if message.get('tool_calls'):
                chunk['choices'][0]['delta'] = {'role': 'assistant', 'tool_calls': [{'index': 0, **message['tool_calls'][0]}]}
            end = {**chunk, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': result['choices'][0]['finish_reason']}]}
            return httpx.Response(200, headers={'content-type': 'text/event-stream'}, content='data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(end) + '\n\ndata: [DONE]\n\n')
        return httpx.Response(200, json=result)
    # Retain the actual OpenAI SDK and LangChain model factories; replace only transport.
    from app.services import llm
    p = patch.object(llm, 'ProviderBorrowingTransport', lambda **kwargs: httpx.MockTransport(provider))
    p.start()
    patches.append(p)
    app = FastAPI()
    app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
    for router in (conversations.router, chat.router, settings_api.router, settings_api.models_router, knowledge_base.router):
        app.include_router(router, prefix='/api/v1')
    def isolated_session():
        with Session(engine) as db:
            yield db
    store = ConversationStore(engine)
    app.dependency_overrides[get_session] = isolated_session
    app.dependency_overrides[conversations.get_store] = lambda: store
    app.state.acceptance_engine = engine
    app.state.provider_calls = calls
    app.state.provider_started = False
    app.state.provider_delay_seconds = 0
    app.state.provider_http_status = 200
    app.state.provider_finish_reason = 'stop'
    app.state.provider_empty = False
    app.state.acceptance_store = store
    app.state.acceptance_patches = patches
    app.state.acceptance_original_engine = original_engine
    app.state.acceptance_patched_modules = patched_modules
    return app


def close_app(app):
    # Modules imported during a turn also copied the temporary engine binding.
    for name, module in list(sys.modules.items()):
        if (name.startswith('app.') and name not in app.state.acceptance_patched_modules
                and getattr(module, 'engine', None) is app.state.acceptance_engine):
            module.engine = app.state.acceptance_original_engine
    for p in reversed(app.state.acceptance_patches):
        p.stop()
    app.state.acceptance_engine.dispose()


# Explicit opt-in prevents test imports from creating the browser artifact.
if os.environ.get('CONVERSATION_ACCEPTANCE_SERVE') == '1':
    url = os.environ['DATABASE_URL']
    if not url.startswith('sqlite:///') or ':memory:' in url:
        raise RuntimeError('Acceptance server requires its own file SQLite DATABASE_URL')
    app = create_app(Path(url.removeprefix('sqlite:///')))

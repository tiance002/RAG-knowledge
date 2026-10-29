import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import SQLModel, Session, create_engine
from sqlalchemy.pool import StaticPool

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
import importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('chat_route_under_test', Path(__file__).parents[1] / 'app' / 'api' / 'chat.py')
route = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = route
spec.loader.exec_module(route)
router = route.router
from app.core.database import get_session
from app.models import KnowledgeBase, FileDocument, FileStatus, ChunkIndex, DocumentChunk
from app.services import chat


@pytest.fixture
def db(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(chat, 'engine', engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='empty', name='empty', embedding_model='e', vlm_model='v'))
        session.add(KnowledgeBase(id='other', name='other', embedding_model='e', vlm_model='v'))
        session.commit()
    yield engine
    engine.dispose()


@pytest.mark.parametrize('mode', ['chat', 'agent'])
@pytest.mark.parametrize('pending', [False, True])
def test_actual_sse_empty_scope_never_dispatches_providers(db, monkeypatch, mode, pending):
    if pending:
        with Session(db) as session:
            session.add(FileDocument(id='pending', kb_id='empty', name='x.pdf', local_path='x', size=1, status=FileStatus.PENDING))
            session.commit()
    monkeypatch.setattr(chat.ChatService, '__init__', lambda self: None)
    async def forbidden(*args, **kwargs):
        raise AssertionError('provider path invoked')
        yield
    monkeypatch.setattr(chat.ChatService, '_chat_stream_normal', forbidden)
    monkeypatch.setattr(chat.ChatService, '_chat_stream_langgraph', forbidden)
    app = FastAPI()
    app.include_router(router)
    def session_override():
        with Session(db) as session:
            yield session
    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as client:
        response = client.post('/chat/completions', json={
            'messages': [{'role': 'user', 'content': '根据知识库回答问题'}],
            'kb_ids': ['empty'], 'mode': mode, 'use_rewrite': True,
            'rerank_enabled': True, 'search_mode': 'hybrid',
        })
    assert response.status_code == 200
    assert response.text.count('event: answer_chunk') == 1
    assert response.text.count('event: done') == 1
    expected = 'INDEXING_PENDING' if pending else 'NO_RESULTS'
    assert expected in response.text
    assert ('等待索引' if pending else '暂无资料') in response.text
    assert 'event: error' not in response.text


def test_only_selected_scope_and_uncertain_states_are_not_empty(db):
    with Session(db) as session:
        session.add(FileDocument(id='f', kb_id='other', name='x.pdf', local_path='x', size=1, status=FileStatus.PENDING))
        session.commit()
    assert chat._scope_preflight(['empty']) == 'NO_RESULTS'
    assert chat._scope_preflight(['empty', 'other']) == 'INDEXING_PENDING'
    assert chat._scope_preflight(['missing']) is None
    with Session(db) as session:
        session.add(ChunkIndex(id='orphan', chunk_id='unknown', kb_id='empty'))
        session.commit()
    assert chat._scope_preflight(['empty']) is None


@pytest.mark.parametrize('status', list(FileStatus))
def test_any_file_status_including_failure_and_pending_is_not_empty(db, status):
    with Session(db) as session:
        session.add(FileDocument(id='f', kb_id='empty', name='unknown.xyz', local_path='missing', size=1, status=status))
        session.commit()
    expected = 'INDEXING_PENDING' if status in {FileStatus.PENDING, FileStatus.PARSING, FileStatus.EMBEDDING} else None
    assert chat._scope_preflight(['empty']) == expected


def test_database_failure_is_not_empty(monkeypatch):
    def broken(*args):
        raise RuntimeError('database unavailable')
    monkeypatch.setattr(chat, 'Session', broken)
    assert chat._scope_preflight(['empty']) is None


@pytest.mark.parametrize('failure', ['retrieval', 'model_unknown'])
def test_http_sse_fault_is_explicit_and_does_not_retry(db, monkeypatch, failure):
    from app.services import grounded_generation
    with Session(db) as session:
        session.add(FileDocument(id='ready', kb_id='empty', name='source.pdf', local_path='source.pdf', size=1, status=FileStatus.READY))
        session.commit()
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'true')
    monkeypatch.setattr(chat.ChatService, '__init__', lambda self: setattr(self, 'llm_service', MagicMock()))
    evidence = [{'id': 'c1', 'file_id': 'ready', 'file_name': 'source.pdf', 'page_number': 1, 'content': 'Amount: 10 dollars'}]
    retrieve = AsyncMock(side_effect=RuntimeError('controlled fault') if failure == 'retrieval' else None,
                         return_value=(evidence, {'retrieval_status': 'SUCCESS'}))
    generate = AsyncMock(side_effect=TimeoutError('controlled unknown'))
    monkeypatch.setattr(chat.ChatService, '_retrieve_with_new_service', retrieve)
    monkeypatch.setattr(grounded_generation, 'generate_grounded_candidate', generate)
    app = FastAPI()
    app.include_router(router)
    def session_override():
        with Session(db) as session:
            yield session
    app.dependency_overrides[get_session] = session_override
    with TestClient(app) as client:
        response = client.post('/chat/completions', json={
            'messages': [{'role': 'user', 'content': '根据知识库回答问题'}],
            'kb_ids': ['empty'], 'mode': 'chat', 'use_rewrite': False,
            'rerank_enabled': False, 'search_mode': 'fulltext',
        })
    assert response.status_code == 200
    assert response.text.count('event: error') == 1
    assert 'event: done' not in response.text
    assert 'NO_RESULTS' not in response.text
    assert retrieve.await_count == 1
    if failure == 'retrieval':
        assert 'FAILED' in response.text and generate.await_count == 0
    else:
        assert 'UNKNOWN' in response.text and generate.await_count == 1


@pytest.mark.parametrize('other_status,other_chunk,expected', [
    (None, False, 'INDEXING_PENDING'),
    (FileStatus.PENDING, False, 'INDEXING_PENDING'),
    (FileStatus.PENDING, True, None),
    (FileStatus.PENDING_CONFIRM, False, None),
    (FileStatus.READY, False, None),
])
def test_pending_confirmation_with_persisted_chunks(db, other_status, other_chunk, expected):
    with Session(db) as session:
        session.add(FileDocument(id='confirm', kb_id='empty', name='x', local_path='x', size=1, status=FileStatus.PENDING_CONFIRM))
        session.add(DocumentChunk(id='c1', file_id='confirm', content='parsed text'))
        if other_status is not None:
            session.add(FileDocument(id='another', kb_id='empty', name='y', local_path='y', size=1, status=other_status))
            if other_chunk:
                session.add(DocumentChunk(id='c2', file_id='another', content='parsed text'))
        session.commit()
    assert chat._scope_preflight(['empty']) == expected


def test_pending_confirmation_with_index_is_uncertain(db):
    with Session(db) as session:
        session.add(FileDocument(id='confirm', kb_id='empty', name='x', local_path='x', size=1, status=FileStatus.PENDING_CONFIRM))
        session.add(DocumentChunk(id='c1', file_id='confirm', content='parsed text'))
        session.add(ChunkIndex(id='i1', chunk_id='c1', kb_id='empty'))
        session.commit()
    assert chat._scope_preflight(['empty']) is None

\n
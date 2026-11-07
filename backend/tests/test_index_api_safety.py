import importlib
import sys
import os
import pytest
import types

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine


def test_status_api_reports_rejection_without_losing_ready_index(monkeypatch, tmp_path):
    # Reuse installed FastAPI; aiofiles and Chroma are not used in this flow.
    extra = os.environ.get('RAG_TEST_EXTRA_SITE_PACKAGES')
    if extra:
        monkeypatch.setattr(sys, 'path', [*sys.path, extra])
    pytest.importorskip('fastapi')
    chroma = types.ModuleType('chromadb'); chroma.Client = object; chroma.errors = types.SimpleNamespace()
    config = types.ModuleType('chromadb.config'); config.Settings = object
    monkeypatch.setitem(sys.modules, 'chromadb', chroma)
    monkeypatch.setitem(sys.modules, 'chromadb.config', config)
    monkeypatch.setitem(sys.modules, 'aiofiles', types.ModuleType('aiofiles'))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core.database import get_session
    from app.models import KnowledgeBase, FileDocument, DocumentChunk, FileStatus
    parse = importlib.import_module('app.tasks.parse_tasks')
    files = importlib.import_module('app.api.files')
    api_router = importlib.import_module('app.api').api_router
    def leaf_routes(router):
        for route in router.routes:
            nested = getattr(route, 'original_router', None)
            if nested is not None:
                yield from leaf_routes(nested)
            else:
                yield route
    routes = list(leaf_routes(api_router))
    for method, path in [('GET', '/files/{file_id}/chunks'),
                         ('PUT', '/chunks/{chunk_id}'),
                         ('POST', '/files/{file_id}/vectorize')]:
        assert sum(route.path.endswith(path) and method in route.methods for route in routes) == 1
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(parse, 'engine', engine)
    # Exercise HTTP validation + real index operation with a synchronous isolated
    # dispatcher. Celery delivery is covered by test_celery_dispatch separately.
    def isolated_dispatch(file_id, operation, payload):
        try:
            if operation == "index": parse.process_submitted_chunks(file_id, payload["chunks_data"], vector_model=payload.get("vector_model"))
            else: parse.process_file_parsing(file_id, **payload)
        except Exception:
            pass  # receipt failure is exposed through file status in this fixture
        return {"task_id": "fixture", "status": "QUEUED"}
    monkeypatch.setattr(files, "_dispatch", isolated_dispatch)
    class Collection:
        def count(self): return 1
        def get(self, where, limit): return {'ids': ['old'] if where['file_id'] == 'ready' else []}
    class Store:
        fail_delete = False
        def _existing_collection(self, kb): return Collection()
        def delete_by_file_id(self, kb, file_id):
            if self.fail_delete:
                raise OSError('synthetic storage failure')
    monkeypatch.setattr(parse, 'VectorStoreService', Store)
    monkeypatch.setattr(importlib.import_module('app.services.vector_store'), 'VectorStoreService', Store)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='old', vlm_model='none'))
        source = tmp_path / 'synthetic.txt'
        source.write_text('old evidence', encoding='utf-8')
        session.add(FileDocument(id='ready', kb_id='kb', name='ready.txt', local_path=str(source), size=1, status=FileStatus.READY))
        session.add(DocumentChunk(id='old', file_id='ready', content='old evidence', page_number=8, parent_id='chunk_1', heading_level=2, heading_text='Section', chunk_type='content', token_count=12))
        session.add(DocumentChunk(id='foreign', file_id='new', content='foreign', parent_id='foreign-parent'))
        session.add(FileDocument(id='new', kb_id='kb', name='new.txt', local_path='synthetic', size=1, status=FileStatus.PENDING))
        session.commit()
        from app.services.bm25 import BM25Index
        BM25Index().index_chunk(session, 'old', 'old evidence', 'kb')
        session.commit()
    app = FastAPI(); app.include_router(files.router)
    def session_override():
        with Session(engine) as session: yield session
    app.dependency_overrides[get_session] = session_override
    with TestClient(app, raise_server_exceptions=False) as client:
        rejected = client.post('/files/ready/parse', json={'auto_vectorize': False})
        assert rejected.status_code == 409 and '手动重解析' in rejected.json()['detail']
        for fid in ('ready', 'new'):
            response = client.post(f'/files/{fid}/chunks/submit', json={'chunks': [{'content': 'new'}], 'vector_model': 'different'})
            assert response.status_code == 200
            data = client.get(f'/files/{fid}').json()['data']
            assert '单文件入库更换' in data['error_message']
            assert data['status'] == ('ready' if fid == 'ready' else 'failed')
        assert client.get('/files/ready').json()['data']['chunk_count'] == 1
        captured = []
        monkeypatch.setattr(parse, 'process_submitted_chunks', lambda file_id, chunks, **kwargs: captured.append((file_id, chunks)))
        raw_chunk = client.get('/files/ready/chunks').json()['data'][0]
        # Browser mapping retains the ID and edits only the content; spoofed source
        # metadata cannot replace the server-owned fields.
        response = client.post('/files/ready/chunks/submit', json={'chunks': [{
            'id': raw_chunk['id'], 'content': 'edited', 'page_number': 999,
            'parent_id': 'spoofed', 'heading_text': 'spoofed', 'content_type': 'image'
        }]})
        assert response.status_code == 200
        submitted = captured[-1][1][0]
        assert submitted['content'] == 'edited'
        assert (submitted['page_number'], submitted['parent_id'], submitted['heading_level'], submitted['heading_text'], submitted['token_count']) == (8, 'chunk_1', 2, 'Section', 12)
        assert submitted['content_type'] == 'text'
        assert client.post('/files/ready/chunks/submit', json={'chunks': [{'id': 'foreign', 'content': 'spoofed'}]}).status_code == 400
        # Multi-file confirmation uses /vectorize and must carry the same structure.
        assert client.post('/files/ready/vectorize').status_code == 200
        assert captured[-1][1][0]['heading_text'] == 'Section'
        with Session(engine) as session:
            assert session.get(DocumentChunk, 'old').content == 'old evidence'
        Store.fail_delete = True
        assert client.delete('/files/ready').status_code == 500
        assert source.exists() and client.get('/files/ready').json()['data']['chunk_count'] == 1
        Store.fail_delete = False
        assert client.delete('/files/ready').status_code == 200
        assert not source.exists()
    with Session(engine) as session:
        from app.models import IndexStats
        assert session.get(DocumentChunk, 'old') is None
        assert session.get(IndexStats, 'kb').total_docs == 0

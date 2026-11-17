import hashlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient
# The source-read route never queries Chroma; its package-level API imports
# otherwise require an unrelated optional native dependency in this unit test.
sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.api import files
from app.models import FileDocument, KnowledgeBase


def test_pdf_source_pins_version_and_enforces_file_scope(tmp_path):
    path = tmp_path / 'source.pdf'
    payload = b'%PDF-1.7\nfixture original source'
    path.write_bytes(payload)
    doc = SimpleNamespace(kb_id='kb', local_path=str(path), name='report.pdf')
    kb = SimpleNamespace(is_deleted=False)
    class Session:
        def get(self, model, ident):
            if model is FileDocument and ident == 'file': return doc
            if model is KnowledgeBase and ident in ('kb', 'other'): return kb
            return None
    app = FastAPI()
    app.include_router(files.router, prefix='/api/v1')
    app.dependency_overrides[files.get_session] = lambda: Session()
    client = TestClient(app)
    digest = hashlib.sha256(payload).hexdigest()
    url = '/api/v1/files/file/source'
    response = client.get(url, params={'kb_id': 'kb', 'sha256': digest})
    assert response.status_code == 200 and response.content == payload
    assert response.headers['content-type'] == 'application/pdf'
    assert client.get(url, params={'kb_id': 'other', 'sha256': digest}).status_code == 404
    assert client.get(url, params={'kb_id': 'kb', 'sha256': 'bad'}).status_code == 422
    path.write_bytes(payload + b'changed')
    assert client.get(url, params={'kb_id': 'kb', 'sha256': digest}).status_code == 409
    kb.is_deleted = True
    assert client.get(url, params={'kb_id': 'kb', 'sha256': digest}).status_code == 404

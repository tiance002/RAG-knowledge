"""File mutation gates and operator controls through FastAPI and real SQLite."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine

from app.api import file_task_admin, files, knowledge_base
from app.core.database import get_session
from app.models import DocumentChunk, FileDocument, KnowledgeBase
from app.tasks import dispatch


@pytest.fixture
def api(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "api.db"))
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(dispatch, "engine", engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id="kb", name="kb", embedding_model="", vlm_model=""))
        session.add(FileDocument(id="file", kb_id="kb", name="example", size=0,
                                 local_path=str(tmp_path / "source.txt"), desired_generation=1))
        session.add(DocumentChunk(id="chunk", file_id="file", content="old"))
        session.add(dispatch.FileTaskReceipt(task_id="t", file_id="file", active_file="file",
            operation="parse", stage="parse", payload="{}", status="UNKNOWN", target_generation=1))
        session.commit()
    app = FastAPI()
    for router in (files.router, knowledge_base.router, file_task_admin.router):
        app.include_router(router)
    def local_session():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = local_session
    with TestClient(app) as client:
        yield client, engine
    engine.dispose()


def test_busy_edit_delete_kb_and_unknown_audit(api):
    client, engine = api
    view = client.get("/knowledge-bases/kb/tasks").json()["data"]
    assert view["tasks"][0]["task_id"] == "t"
    assert view["queues"]["document.parse"]["unknown"] == 1
    assert client.put("/chunks/chunk", json={"content": "edited"}).status_code == 409
    assert client.delete("/files/file").status_code == 409
    assert client.delete("/knowledge-bases/kb").status_code == 409
    assert client.delete("/knowledge-bases/kb/permanent").status_code == 409
    response = client.post("/files/file/tasks/t/resolve", json={"action": "terminate", "reason": "核对后放弃"})
    assert response.status_code == 200 and response.json()["data"]["status"] == "CANCELLED"
    with Session(engine) as session:
        from sqlmodel import select
        assert session.get(DocumentChunk, "chunk").content == "old"
        assert session.exec(select(dispatch.FileTaskEvent.reason)).one() == "核对后放弃"
    assert client.put("/chunks/chunk", json={"content": "preview edit"}).status_code == 200


def test_cancel_and_delete_preserves_receipt_and_fences_execution(api):
    client, engine = api
    assert client.delete("/files/file?cancel_active=true").status_code == 200
    assert client.get("/files/file/tasks/t").json()["data"]["status"] == "CANCELLED"
    with Session(engine) as session:
        assert session.get(FileDocument, "file") is None
        assert session.get(DocumentChunk, "chunk") is None
    assert dispatch.claim_task("t", "parse") is None


def test_open_source_retry_checks_identity_after_logical_delete(api, tmp_path, monkeypatch):
    from pathlib import Path
    from app.services.file_retirement import clean_source, FileStorageRetirement
    client, engine = api
    path = tmp_path / "source.txt"
    path.write_text("original", encoding="utf-8")
    unlink = Path.unlink
    def busy(self, *args, **kwargs):
        if self == path:
            raise PermissionError("source held by cancelled reader")
        return unlink(self, *args, **kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", busy)
        assert client.delete("/files/file?cancel_active=true").status_code == 200
    with Session(engine) as session:
        assert session.get(FileStorageRetirement, "source:file").error == "PermissionError"
    path.write_text("another file now owns this path", encoding="utf-8")
    clean_source(engine, "source:file")
    assert path.read_text("utf-8") == "another file now owns this path"
    with Session(engine) as session:
        assert session.get(FileStorageRetirement, "source:file").error == "SOURCE_IDENTITY_UNVERIFIED"

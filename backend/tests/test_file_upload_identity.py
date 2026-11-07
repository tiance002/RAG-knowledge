"""Synthetic v1 upload identity contract. Uses temporary SQLite and byte sources."""
import asyncio
import hashlib
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import pytest
from fastapi import HTTPException, UploadFile
from sqlmodel import SQLModel, Session, create_engine, select

from app.models import FileDocument, DocumentChunk, FileStatus, KnowledgeBase
from app.services.file_uploads import register_upload


@pytest.fixture(autouse=True)
def isolate_upload_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_OBSERVABILITY_ENABLED", "false")
    monkeypatch.setenv("RAG_SNAPSHOTS_ENABLED", "false")
    monkeypatch.setenv("RAG_OBSERVATION_LOG", str(tmp_path / "synthetic-upload-events.jsonl"))


@pytest.fixture
def store(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'upload-contract-v1.db'}", connect_args={"check_same_thread": False, "timeout": 30})
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id="kb", name="kb", embedding_model="none", vlm_model="none"))
        session.add(KnowledgeBase(id="other", name="other", embedding_model="none", vlm_model="none"))
        session.commit()
    yield engine, tmp_path
    engine.dispose()


def add(store, identifier, name, data=b"original bytes", kb_id="kb", **options):
    engine, directory = store
    path = directory / f"{identifier}.txt"
    path.write_bytes(data)
    with Session(engine) as session:
        try:
            result = register_upload(session, kb_id=kb_id, file_id=identifier,
                name=name, path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest(), **options)
            if result["duplicate"]:
                path.unlink()
            return result
        except Exception:
            session.rollback()
            path.unlink(missing_ok=True)
            raise


def test_same_name_same_hash_reuses_failed_status_and_index(store):
    first = add(store, "one", "plan.txt")
    with Session(store[0]) as session:
        row = session.get(FileDocument, first["file_id"])
        row.status = FileStatus.FAILED
        session.add(DocumentChunk(id="evidence", file_id=row.id, content="preserved evidence"))
        session.add(row); session.commit()
    again = add(store, "two", "plan.txt")
    assert again["duplicate"] and again["file_id"] == "one" and again["status"] == "failed"
    with Session(store[0]) as session:
        assert len(session.exec(select(FileDocument)).all()) == 1
        assert session.get(DocumentChunk, "evidence").content == "preserved evidence"
    assert (store[1] / "one.txt").read_bytes() == b"original bytes"
    assert not (store[1] / "two.txt").exists()


def test_check_prompts_even_same_hash_and_new_preserves_original_name(store):
    add(store, "one", "方案.pdf")
    with pytest.raises(HTTPException) as caught:
        add(store, "two", "方案.pdf", action="check")
    assert caught.value.status_code == 409
    assert caught.value.detail["same_sha256"] is True
    duplicate = add(store, "two", "方案.pdf", action="new")
    assert duplicate["outcome"] == "created" and duplicate["display_name"] == "方案 (1).pdf"
    assert duplicate["original_name"] == "方案.pdf"
    with Session(store[0]) as session:
        assert session.get(FileDocument, "one").name == session.get(FileDocument, "two").name == "方案.pdf"


def test_same_name_different_bytes_requires_choice_and_safe_replacement(store):
    add(store, "one", "plan.txt")
    with pytest.raises(HTTPException) as caught:
        add(store, "two", "plan.txt", b"new bytes")
    assert caught.value.status_code == 409 and caught.value.detail["same_sha256"] is False
    replacement = add(store, "two", "plan.txt", b"new bytes", action="replace", target_file_id="one")
    assert replacement["supersedes_file_id"] == "one" and replacement["outcome"] == "replacement_staged"
    assert (store[1] / "one.txt").read_bytes() == b"original bytes"
    assert (store[1] / "two.txt").read_bytes() == b"new bytes"
    with Session(store[0]) as session:
        assert len(session.exec(select(FileDocument)).all()) == 2


def test_same_hash_different_names_and_kbs_are_independent(store):
    records = [add(store, "one", "a.txt"), add(store, "two", "b.txt"), add(store, "three", "a.txt", kb_id="other")]
    assert len({row["file_id"] for row in records}) == 3
    assert len({row["sha256"] for row in records}) == 1


def test_concurrent_default_uploads_have_one_record(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda fid: add(store, fid, "race.txt"), ["race-1", "race-2"]))
    assert len({row["file_id"] for row in results}) == 1
    assert sorted(row["duplicate"] for row in results) == [False, True]
    with Session(store[0]) as session:
        assert len(session.exec(select(FileDocument)).all()) == 1


def test_legacy_null_hash_is_not_equal_without_readable_source(store):
    with Session(store[0]) as session:
        session.add(FileDocument(id="legacy", kb_id="kb", name="a.txt", local_path="does-not-exist", size=0))
        session.commit()
    with pytest.raises(HTTPException) as caught:
        add(store, "new", "a.txt")
    assert caught.value.detail["same_sha256"] is False
    with Session(store[0]) as session:
        assert session.get(FileDocument, "legacy").sha256 is None


def test_legacy_readable_source_can_dedup_without_backfill(store):
    add(store, "legacy", "a.txt")
    with Session(store[0]) as session:
        row = session.get(FileDocument, "legacy"); row.sha256 = None; row.original_name = None; session.add(row); session.commit()
    assert add(store, "two", "a.txt")["file_id"] == "legacy"
    with Session(store[0]) as session:
        assert session.get(FileDocument, "legacy").sha256 is None


def test_cross_scope_replacement_and_nonidentical_reuse_rejected(store):
    add(store, "one", "a.txt", kb_id="other")
    for options in [{"action": "replace", "target_file_id": "one"}, {"action": "reuse", "target_file_id": "one"}]:
        with pytest.raises(HTTPException) as caught:
            add(store, "two", "a.txt", **options)
        assert caught.value.status_code == 409
    add(store, "two", "a.txt")
    with pytest.raises(HTTPException):
        add(store, "three", "a.txt", b"different", action="reuse", target_file_id="two")


def test_delete_same_name_keeps_other_record_source_and_chunks(store, monkeypatch):
    import app.api.files as files
    import app.services.vector_store as vectors
    add(store, "one", "a.txt")
    add(store, "two", "a.txt", action="new")
    calls = []
    monkeypatch.setattr(vectors, "VectorStoreService", lambda: type("Store", (), {"delete_by_file_id": lambda self, kb, fid: calls.append((kb, fid))})())
    with Session(store[0]) as session:
        session.add(DocumentChunk(id="c1", file_id="one", content="first"))
        session.add(DocumentChunk(id="c2", file_id="two", content="second"))
        session.commit()
        asyncio.run(files.delete_file("one", session=session))
    with Session(store[0]) as session:
        assert session.get(FileDocument, "one") is None
        assert session.get(FileDocument, "two") is not None
        assert session.get(DocumentChunk, "c2").content == "second"
    assert calls == [("kb", "one")]
    assert not (store[1] / "one.txt").exists() and (store[1] / "two.txt").exists()


def test_upload_streams_hash_and_cleans_rejected_staging(store, monkeypatch):
    import app.api.files as files
    monkeypatch.setattr(files.settings, "UPLOAD_DIR", str(store[1] / "uploads"))
    content = b"a" * (1024 * 1024 + 5)
    class StreamingUpload(UploadFile):
        async def read(self, size=-1):
            assert size == 1024 * 1024
            return await super().read(size)
    with Session(store[0]) as session:
        first = asyncio.run(files.upload_file("kb", StreamingUpload(filename="../plan.txt", file=BytesIO(content)), session=session, action="auto", target_file_id=None)).data
        assert first.sha256 == hashlib.sha256(content).hexdigest()
        second = asyncio.run(files.upload_file("kb", StreamingUpload(filename="../plan.txt", file=BytesIO(content)), session=session, action="auto", target_file_id=None)).data
        assert second.duplicate and second.file_id == first.file_id
    paths = list((store[1] / "uploads" / "kb").iterdir())
    assert len(paths) == 1 and paths[0].read_bytes() == content
    monkeypatch.setattr(files.settings, "MAX_FILE_SIZE", 1)
    with Session(store[0]) as session, pytest.raises(HTTPException):
        asyncio.run(files.upload_file("kb", StreamingUpload(filename="too-big.txt", file=BytesIO(b"ab")), session=session, action="auto", target_file_id=None))
    assert list((store[1] / "uploads" / "kb").iterdir()) == paths


def test_sql_failure_preserves_existing_and_cleans_new_source(store, monkeypatch):
    import app.api.files as files
    add(store, "one", "a.txt")
    monkeypatch.setattr(files.settings, "UPLOAD_DIR", str(store[1] / "failed-uploads"))
    with Session(store[0]) as session:
        def fail_commit():
            raise OSError("synthetic commit rejection")
        monkeypatch.setattr(session, "commit", fail_commit)
        with pytest.raises(OSError, match="synthetic commit rejection"):
            asyncio.run(files.upload_file("kb", UploadFile(filename="a.txt", file=BytesIO(b"new bytes")), session=session, action="new", target_file_id=None))
    assert list((store[1] / "failed-uploads" / "kb").iterdir()) == []
    assert (store[1] / "one.txt").read_bytes() == b"original bytes"
    with Session(store[0]) as session:
        assert [row.id for row in session.exec(select(FileDocument)).all()] == ["one"]


def test_http_contract_exposes_same_name_choices_and_display_identity(store, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import app.api.files as files
    from app.core.database import get_session
    monkeypatch.setattr(files.settings, "UPLOAD_DIR", str(store[1] / "http-uploads"))
    app = FastAPI(); app.include_router(files.router)
    def sessions():
        with Session(store[0]) as session:
            yield session
    app.dependency_overrides[get_session] = sessions
    with TestClient(app) as client:
        first = client.post("/knowledge-bases/kb/upload", files={"file": ("a.txt", b"bytes")})
        assert first.status_code == 202
        fid = first.json()["data"]["file_id"]
        duplicate = client.post("/knowledge-bases/kb/upload", files={"file": ("a.txt", b"bytes")})
        assert duplicate.json()["data"]["duplicate"] is True
        assert duplicate.json()["data"]["file_id"] == fid
        check = client.post("/knowledge-bases/kb/upload?action=check", files={"file": ("a.txt", b"bytes")})
        assert check.status_code == 409 and check.json()["detail"]["actions"] == ["reuse", "new", "replace"]
        created = client.post("/knowledge-bases/kb/upload?action=new", files={"file": ("a.txt", b"different")})
        assert created.status_code == 202
        rows = client.get("/knowledge-bases/kb/files").json()["data"]
        assert len(rows) == 2 and {row["display_name"] for row in rows} == {"a.txt", "a (1).txt"}
        assert all(row["original_name"] == row["name"] == "a.txt" for row in rows)

"""BM25 storage contracts, using real SQL and no network services."""
import json

import pytest
from sqlalchemy import event, inspect
from sqlmodel import Session, SQLModel, create_engine, select

from app.core import database
from app.models import BM25Posting, ChunkIndex, DocumentChunk, FileDocument, FileStatus, IndexStats
from app.services import bm25


def test_legacy_startup_backfills_exactly_once_and_seeks_only_matching_terms(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    SQLModel.metadata.create_all(engine)
    BM25Posting.__table__.drop(engine)
    with Session(engine) as session:
        for cid, kb, terms in [
            ("z-first", "kb", {"apple": 2}), ("a-second", "kb", {"apple": 2}),
            ("third", "kb", {"banana": 1}), ("alien", "other", {"apple": 10}),
        ]:
            session.add(ChunkIndex(chunk_id=cid, kb_id=kb, doc_length=sum(terms.values()),
                                   terms=json.dumps(terms)))
        session.add(IndexStats(kb_id="kb", total_docs=3, avg_doc_length=5 / 3,
                               term_doc_freq='{"apple":2,"banana":1}'))
        session.commit()
    monkeypatch.setattr(database, "engine", engine)
    database.init_db()
    database.init_db()
    queries = []

    def capture(conn, cursor, sql, args, *unused):
        queries.append((sql, args))

    with Session(engine) as session:
        assert len(session.exec(select(BM25Posting)).all()) == 4
        index = bm25.BM25Index()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            hits = index.search(session, "apple apple", "kb", 1)
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        expected = index._calculate_bm25(["apple", "apple"], {"apple": 2}, 2, 3, 5 / 3, {"apple": 2})
        assert hits == [{"chunk_id": "z-first", "bm25_score": expected}]
        assert not any("chunk_index.terms" in sql or "index_stats.term_doc_freq" in sql for sql, _ in queries)
        [(sql, args)] = [(sql, args) for sql, args in queries if "FROM bm25_postings" in sql]
        plan = [row[3] for row in session.connection().exec_driver_sql("EXPLAIN QUERY PLAN " + sql, args)]
        assert any("SEARCH bm25_postings" in step and "term=?" in step for step in plan), plan
        assert all("SCAN " not in step for step in plan), plan
        assert index.search(session, "absentword", "kb") == []


def test_bad_legacy_record_rolls_back_table_creation_and_can_be_retried(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "bad.db"))
    SQLModel.metadata.create_all(engine)
    BM25Posting.__table__.drop(engine)
    with Session(engine) as session:
        session.add(ChunkIndex(id="broken", chunk_id="x", kb_id="kb", terms="broken"))
        session.commit()
    with pytest.raises(ValueError, match="broken"):
        SQLModel.metadata.create_all(engine)
    assert not inspect(engine).has_table("bm25_postings")
    with Session(engine) as session:
        row = session.get(ChunkIndex, "broken")
        assert row.terms == "broken"
        row.terms = '{"fixed": 1}'
        session.commit()
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        assert session.exec(select(BM25Posting.term)).one() == "fixed"


def test_committed_updates_empty_chunks_delete_rollback_and_kb_isolation():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        service = bm25.BM25Index()
        service.index_chunk(session, "a", "alpha shared", "here")
        service.index_chunk(session, "b", "beta shared", "elsewhere")
        session.commit()
        service.index_chunk(session, "a", "gamma", "here")
        session.commit()
        assert not service.search(session, "alpha", "here")
        assert service.search(session, "gamma", "here")[0]["chunk_id"] == "a"
        service.delete_chunk_index(session, "a", "elsewhere")
        service.index_chunk(session, "a", "", "here")
        assert not service.search(session, "gamma", "here")
        session.rollback()
        assert service.search(session, "gamma", "here")
        service.clear_kb_index(session, "here")
        session.commit()
        assert not service.search(session, "gamma", "here")
        assert session.exec(select(BM25Posting.kb_id).distinct()).all() == ["elsewhere"]
        assert service.search(session, "shared", "elsewhere")[0]["chunk_id"] == "b"


def test_failed_rebuild_preserves_published_terms(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(FileDocument(id="file", kb_id="kb", local_path="unused", name="file", size=1, status=FileStatus.READY))
        session.add(DocumentChunk(id="child", file_id="file", content="new term", original_index=0))
        service = bm25.BM25Index()
        service.index_chunk(session, "child", "old term", "kb")
        session.commit()

        def fail(*args):
            raise ValueError("indexing interrupted")

        monkeypatch.setattr(service, "index_chunk", fail)
        with pytest.raises(ValueError, match="interrupted"):
            service.rebuild_kb_index(session, "kb")
        session.commit()
        assert service.search(session, "old", "kb")[0]["chunk_id"] == "child"


def test_bulk_import_matches_normal_writer(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from ecs_benchmark200_helpers import verify_bulk_bm25
    assert verify_bulk_bm25()["status"] == "PASS"

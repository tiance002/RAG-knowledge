"""Migration contracts on disposable synthetic SQLite databases, fixture v1."""
import hashlib
import importlib.util
from pathlib import Path
import sqlite3
import sys

import pytest
from sqlalchemy import create_engine

from app.core.schema_migrations import migrate_schema


SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
sys.path.insert(0, str(SCRIPTS))
from audit_parent_child import audit_database, resolve_source
from migrate_parent_child import migrate, rebuild_manifest


def legacy_db(tmp_path):
    source = tmp_path / "source.txt"
    source.write_bytes(b"frozen source bytes")
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.executescript("""
            CREATE TABLE knowledge_bases (id TEXT PRIMARY KEY);
            CREATE TABLE file_documents (id TEXT PRIMARY KEY,kb_id TEXT,name TEXT,local_path TEXT,status TEXT);
            CREATE TABLE document_chunks (id TEXT PRIMARY KEY,file_id TEXT,chunk_type TEXT,parent_id TEXT,content TEXT);
            CREATE TABLE chunk_index (chunk_id TEXT);
            INSERT INTO knowledge_bases VALUES ('kb');
            INSERT INTO document_chunks VALUES ('child','file','content','chunk_1','legacy body');
            INSERT INTO chunk_index VALUES ('child');
        """)
        connection.execute("INSERT INTO file_documents VALUES ('file','kb','source.txt',?,'READY')", (str(source),))
    return database, source


def test_additive_migration_idempotent_preserves_legacy(tmp_path):
    database, source = legacy_db(tmp_path)
    engine = create_engine("sqlite:///" + str(database))
    first = migrate_schema(engine)
    assert len(first) == 6
    assert migrate_schema(engine) == []
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT id,parent_id,content FROM document_chunks").fetchone() == ("child", "chunk_1", "legacy body")
        assert connection.execute("SELECT sha256,original_name,display_name FROM file_documents").fetchone() == (None, None, None)
        assert connection.execute("SELECT start_offset,end_offset FROM document_chunks").fetchone() == (None, None)
        assert connection.execute("SELECT count(*) FROM chunk_index").fetchone() == (1,)
    engine.dispose()
    assert source.read_bytes() == b"frozen source bytes"


def test_preview_readonly_and_reports_unrepairable_parent(tmp_path):
    database, source = legacy_db(tmp_path)
    raw = database.read_bytes()
    audit = audit_database(database, tmp_path)
    assert audit["counts"]["dangling_parent_refs"] == 1
    assert audit["parent_anomalies"][0]["repair"] == "UNKNOWN_REPARSE_REQUIRED"
    result = migrate(database, tmp_path)
    assert result["failed"] == 0
    assert result["results"][0]["status"] == "WOULD_BACKFILL"
    assert result["results"][0]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert database.read_bytes() == raw
    manifest = rebuild_manifest(result)
    assert manifest["execution"] == "PREVIEW_ONLY"
    assert manifest["files"][0]["previous_file_id"] == "file"
    assert manifest["files"][0]["config"]["quality_evaluated"] is False


def test_backfill_requires_schema_and_journal_then_can_resume(tmp_path):
    database, source = legacy_db(tmp_path)
    with pytest.raises(ValueError, match="journal"):
        migrate(database, tmp_path, apply=True)
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(ValueError, match="schema"):
        migrate(database, tmp_path, apply=True, journal=journal)
    engine = create_engine("sqlite:///" + str(database))
    migrate_schema(engine)
    result = migrate(database, tmp_path, apply=True, journal=journal)
    assert result["failed"] == 0
    assert result["results"][0]["status"] == "APPLIED"
    assert migrate(database, tmp_path, apply=True, journal=journal)["failed"] == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT original_name,display_name,sha256 FROM file_documents").fetchone() == ("source.txt", "source.txt", hashlib.sha256(source.read_bytes()).hexdigest())
        assert connection.execute("SELECT parent_id,content FROM document_chunks").fetchone() == ("chunk_1", "legacy body")
    assert len(journal.read_text(encoding="utf-8").splitlines()) == 2
    assert migrate(database, tmp_path, after_id="file")["processed"] == 0
    engine.dispose()


def test_missing_source_and_hash_mismatch_are_recorded_not_overwritten(tmp_path):
    database, source = legacy_db(tmp_path)
    engine = create_engine("sqlite:///" + str(database))
    migrate_schema(engine)
    journal = tmp_path / "journal.jsonl"
    assert migrate(database, tmp_path, apply=True, journal=journal)["failed"] == 0
    source.write_bytes(b"changed")
    result = migrate(database, tmp_path, apply=True, journal=journal)
    assert result["failed"] == 1
    assert result["results"][0]["reason"] == "EXISTING_HASH_MISMATCH"
    source.unlink()
    result = migrate(database, tmp_path)
    assert result["failed"] == 1
    assert result["results"][0]["reason"] == "SOURCE_MISSING"
    assert rebuild_manifest(result)["files"][0]["eligible"] is False
    engine.dispose()

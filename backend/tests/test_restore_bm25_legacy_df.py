"""Offline rollback utility contracts against disposable SQLite files only."""
import importlib.util
import json
from pathlib import Path
import sqlite3

import pytest


SPEC = importlib.util.spec_from_file_location(
    "restore_bm25_legacy_df", Path(__file__).resolve().parents[2] / "scripts/restore_bm25_legacy_df.py")
restore = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(restore)


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "synthetic.db"
    with sqlite3.connect(path) as connection:
        connection.executescript("""
            CREATE TABLE chunk_index(id TEXT PRIMARY KEY, kb_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
                                     terms TEXT NOT NULL, doc_length INTEGER NOT NULL);
            CREATE TABLE bm25_postings(kb_id TEXT, term TEXT, index_id TEXT, tf INTEGER,
                                       PRIMARY KEY(kb_id,term,index_id));
            CREATE TABLE index_stats(kb_id TEXT PRIMARY KEY,total_docs INTEGER,avg_doc_length REAL,
                                     term_doc_freq TEXT,updated_at TEXT);
            CREATE TABLE untouched(value TEXT);
            INSERT INTO untouched VALUES ('keep');
            INSERT INTO chunk_index VALUES ('a','kb','chunk-a','{"alpha":2,"shared":1}',3);
            INSERT INTO chunk_index VALUES ('b','kb','chunk-b','{"beta":1,"shared":1}',2);
            INSERT INTO chunk_index VALUES ('c','other','chunk-c','{"alpha":1}',1);
            INSERT INTO bm25_postings VALUES ('kb','alpha','a',2),('kb','shared','a',1),
                                             ('kb','beta','b',1),('kb','shared','b',1),('other','alpha','c',1);
            INSERT INTO index_stats VALUES ('kb',2,2.5,'malformed legacy snapshot','unchanged timestamp'),
                                           ('other',1,1.0,'{"stale":99}','another timestamp'),
                                           ('empty',0,0.0,'{"old":1}','empty timestamp');
        """)
    return path


def snapshot(path):
    with sqlite3.connect(path) as connection:
        return {table: connection.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
                for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")}


def test_default_dry_run_does_not_touch_malformed_or_stale_json(database):
    before = snapshot(database)
    result = restore.restore_legacy_df(database)
    assert result["status"] == "DRY_RUN" and result["commit_status"] == "NOT_ATTEMPTED"
    assert (result["stats_rows"], result["chunk_index_rows"], result["posting_rows"], result["df_terms"]) == (3, 3, 5, 4)
    assert result["changed_json_rows"] == 3
    assert snapshot(database) == before
    assert len(list(database.parent.iterdir())) == 1


def test_apply_reconstructs_exact_df_preserves_schema_stats_and_immutable_backup(database, tmp_path):
    before = snapshot(database)
    with sqlite3.connect(database) as connection:
        schema = connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall()
    backup = tmp_path / "before-rollback.db"
    result = restore.restore_legacy_df(database, backup, apply=True, writers_stopped=True)
    assert result["status"] == "APPLIED" and result["commit_status"] == "COMMITTED"
    assert restore._sha256(backup) == result["backup_sha256"]
    assert snapshot(backup) == before
    after = snapshot(database)
    assert all(after[table] == before[table] for table in before if table != "index_stats")
    expected = {"kb": {"alpha": 1, "beta": 1, "shared": 2}, "other": {"alpha": 1}, "empty": {}}
    for previous, current in zip(before["index_stats"], after["index_stats"]):
        assert current[:3] == previous[:3] and current[4:] == previous[4:]
        assert json.loads(current[3]) == expected[current[0]]
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT type,name,sql FROM sqlite_master ORDER BY type,name").fetchall() == schema
    backup_bytes = backup.read_bytes()
    with pytest.raises(restore.RestoreError, match="BACKUP_MUST_BE_NEW"):
        restore.restore_legacy_df(database, backup, apply=True, writers_stopped=True)
    assert backup.read_bytes() == backup_bytes


@pytest.mark.parametrize("apply,ack,backup", [(True, False, True), (True, True, False), (False, False, True)])
def test_explicit_apply_acknowledgment_and_backup_are_required(database, tmp_path, apply, ack, backup):
    before = snapshot(database)
    with pytest.raises(restore.RestoreError):
        restore.restore_legacy_df(database, tmp_path / "new.db" if backup else None, apply, ack)
    assert snapshot(database) == before


@pytest.mark.parametrize("mutation,code", [
    ("DELETE FROM bm25_postings WHERE index_id='a' AND term='alpha'", "POSTING_TERMS_MISMATCH"),
    ("UPDATE bm25_postings SET tf=3 WHERE index_id='a' AND term='alpha'", "POSTING_TERMS_MISMATCH"),
    ("INSERT INTO bm25_postings VALUES('kb','orphan','missing',1)", "ORPHAN_POSTING"),
    ("UPDATE bm25_postings SET kb_id='wrong' WHERE index_id='a'", "POSTING_INDEX_KB_MISMATCH"),
    ("UPDATE chunk_index SET terms='broken' WHERE id='a'", "INVALID_CHUNK_TERMS_OR_LENGTH"),
    ("UPDATE chunk_index SET doc_length=9 WHERE id='a'", "INVALID_CHUNK_TERMS_OR_LENGTH"),
    ("UPDATE index_stats SET total_docs=9 WHERE kb_id='kb'", "TOTAL_DOCS_MISMATCH"),
    ("UPDATE index_stats SET avg_doc_length=9 WHERE kb_id='kb'", "AVG_DOC_LENGTH_MISMATCH"),
    ("DELETE FROM index_stats WHERE kb_id='kb'", "MISSING_INDEX_STATS"),
    ("INSERT INTO chunk_index VALUES('dup','kb','chunk-a','{}',0)", "DUPLICATE_LOGICAL_CHUNK_INDEX"),
])
def test_invalid_index_refuses_apply_without_backup_or_other_repairs(database, tmp_path, mutation, code):
    with sqlite3.connect(database) as connection:
        connection.execute(mutation)
    before = snapshot(database)
    backup = tmp_path / "refused.db"
    with pytest.raises(restore.RestoreError, match=code):
        restore.restore_legacy_df(database, backup, True, True)
    assert snapshot(database) == before and not backup.exists()


def test_sql_failure_rolls_back_all_updates_and_preserves_prewrite_backup(database, tmp_path):
    with sqlite3.connect(database) as connection:
        connection.execute("""CREATE TRIGGER fail_late BEFORE UPDATE ON index_stats WHEN NEW.kb_id='other'
                              BEGIN SELECT RAISE(ABORT,'synthetic failure'); END""")
    before = snapshot(database)
    backup = tmp_path / "transaction-before.db"
    with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
        restore.restore_legacy_df(database, backup, True, True)
    assert snapshot(database) == before and snapshot(backup) == before


def test_validation_failure_after_updates_rolls_back(database, tmp_path, monkeypatch):
    before = snapshot(database)
    backup = tmp_path / "validation-before.db"
    def reject(*args):
        raise restore.RestoreError("INJECTED_POST_WRITE_FAILURE")
    monkeypatch.setattr(restore, "_verify_written_df", reject)
    with pytest.raises(restore.RestoreError, match="INJECTED_POST_WRITE_FAILURE"):
        restore.restore_legacy_df(database, backup, True, True)
    assert snapshot(database) == before and snapshot(backup) == before


@pytest.mark.parametrize("commit_before_error", [False, True])
def test_commit_exception_reports_unknown_and_never_retries(database, tmp_path, monkeypatch, commit_before_error):
    before = snapshot(database)
    original_connect = restore._connect
    commits = []
    class FailCommit(sqlite3.Connection):
        def commit(self):
            commits.append("attempt")
            if commit_before_error:
                super().commit()
            raise sqlite3.OperationalError("synthetic ambiguous commit")
    def connect(path, *, writable=False):
        if writable:
            return sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, isolation_level=None, factory=FailCommit)
        return original_connect(path, writable=False)
    monkeypatch.setattr(restore, "_connect", connect)
    backup = tmp_path / "unknown-before.db"
    with pytest.raises(restore.RestoreError, match="COMMIT_OUTCOME_UNKNOWN") as error:
        restore.restore_legacy_df(database, backup, True, True)
    assert error.value.receipt["status"] == "UNKNOWN"
    assert error.value.receipt["commit_status"] == "UNKNOWN" and commits == ["attempt"]
    assert snapshot(backup) == before
    if commit_before_error:
        assert json.loads(dict((row[0], row[3]) for row in snapshot(database)["index_stats"])["kb"]) == {
            "alpha": 1, "beta": 1, "shared": 2}
    else:
        assert snapshot(database) == before


def test_separate_reader_backup_includes_committed_wal_snapshot(database, tmp_path):
    # Keep this connection open so WAL is not checkpointed merely by last-close.
    with sqlite3.connect(database) as keeper:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("INSERT INTO untouched VALUES ('committed in WAL')")
        keeper.commit()
        assert Path(str(database) + "-wal").stat().st_size > 0
        before = snapshot(database)
        backup = tmp_path / "wal-before.db"
        result = restore.restore_legacy_df(database, backup, True, True)
        assert result["status"] == "APPLIED" and snapshot(backup) == before


def test_cli_default_is_dry_run_and_missing_database_is_not_created(database, tmp_path, capsys):
    assert restore.main([str(database)]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "DRY_RUN"
    missing = tmp_path / "missing.db"
    assert restore.main([str(missing)]) == 2 and not missing.exists()

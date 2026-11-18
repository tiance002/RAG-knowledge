"""Offline SQLite rollback preparation for legacy BM25 writers.

Stop every index writer before applying. This tool validates all physical index
terms/postings and aggregate stats, then changes only the legacy DF JSON. It does
not repair malformed index data, change schema, or retry an uncertain commit.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3


class RestoreError(RuntimeError):
    def __init__(self, code, **receipt):
        super().__init__(code)
        self.receipt = {"status": "REFUSED", "error": code, **receipt}


def _connect(path, *, writable=False):
    return sqlite3.connect(path.as_uri() + ("?mode=rw" if writable else "?mode=ro"),
                           uri=True, timeout=5, isolation_level=None)


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validated_df(connection):
    """Compare actual TF rows and Posting DF against independent chunk terms."""
    indices = {}
    logical_ids = set()
    expected_df = defaultdict(Counter)
    lengths = Counter()
    counts = Counter()
    for index_id, kb_id, chunk_id, encoded, length in connection.execute(
            "SELECT id,kb_id,chunk_id,terms,doc_length FROM chunk_index"):
        if (kb_id, chunk_id) in logical_ids:
            raise RestoreError("DUPLICATE_LOGICAL_CHUNK_INDEX", index_id=index_id)
        logical_ids.add((kb_id, chunk_id))
        try:
            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate term key")
                    result[key] = value
                return result
            terms = json.loads(encoded, object_pairs_hook=unique_object)
            if not isinstance(terms, dict) or any(type(tf) is not int or tf < 1 for tf in terms.values()):
                raise ValueError("invalid term frequencies")
            if type(length) is not int or length < 0 or sum(terms.values()) != length:
                raise ValueError("invalid document length")
        except (ValueError, TypeError) as exc:
            raise RestoreError("INVALID_CHUNK_TERMS_OR_LENGTH", index_id=index_id) from exc
        indices[index_id] = (kb_id, terms)
        expected_df[kb_id].update(terms.keys())
        counts[kb_id] += 1
        lengths[kb_id] += length

    actual_terms = defaultdict(dict)
    posting_count = 0
    for kb_id, term, index_id, tf in connection.execute("SELECT kb_id,term,index_id,tf FROM bm25_postings"):
        posting_count += 1
        if index_id not in indices:
            raise RestoreError("ORPHAN_POSTING", index_id=index_id)
        if indices[index_id][0] != kb_id:
            raise RestoreError("POSTING_INDEX_KB_MISMATCH", index_id=index_id)
        if term in actual_terms[index_id] or type(tf) is not int or tf < 1:
            raise RestoreError("INVALID_OR_DUPLICATE_POSTING", index_id=index_id)
        actual_terms[index_id][term] = tf
    for index_id, (_, terms) in indices.items():
        if actual_terms[index_id] != terms:
            raise RestoreError("POSTING_TERMS_MISMATCH", index_id=index_id)

    aggregate = defaultdict(dict)
    for kb_id, term, df in connection.execute(
            "SELECT p.kb_id,p.term,COUNT(DISTINCT p.index_id) "
            "FROM bm25_postings p JOIN chunk_index ci ON ci.id=p.index_id AND ci.kb_id=p.kb_id "
            "GROUP BY p.kb_id,p.term"):
        aggregate[kb_id][term] = df
    for kb_id in set(aggregate) | set(expected_df):
        if aggregate[kb_id] != dict(expected_df[kb_id]):
            raise RestoreError("POSTING_DF_MISMATCH", kb_id=kb_id)

    stats = {}
    for kb_id, total, average, legacy in connection.execute(
            "SELECT kb_id,total_docs,avg_doc_length,term_doc_freq FROM index_stats"):
        if type(total) is not int or total != counts[kb_id]:
            raise RestoreError("TOTAL_DOCS_MISMATCH", kb_id=kb_id)
        expected_average = lengths[kb_id] / total if total else 0.0
        if not isinstance(average, (int, float)) or not math.isfinite(average) or not math.isclose(
                average, expected_average, rel_tol=1e-9, abs_tol=1e-9):
            raise RestoreError("AVG_DOC_LENGTH_MISMATCH", kb_id=kb_id)
        stats[kb_id] = (total, average, legacy)
    if set(counts) - set(stats):
        raise RestoreError("MISSING_INDEX_STATS", kb_ids=sorted(set(counts) - set(stats)))
    encoded = {kb_id: json.dumps(aggregate[kb_id], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
               for kb_id in stats}
    return encoded, stats, len(indices), posting_count


def _backup(database, destination):
    # Reserve exclusively: never overwrite an existing file or symlink.
    descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    # The caller owns BEGIN IMMEDIATE but has made no writes. A separate reader
    # sees the complete committed snapshot without the write-connection backup
    # deadlock. Keep any partial/failed backup for inspection; never reuse it.
    with closing(_connect(database)) as reader, closing(sqlite3.connect(destination)) as target:
        reader.backup(target)
        if target.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RestoreError("BACKUP_INTEGRITY_FAILED", backup=str(destination))
    return _sha256(destination)


def _verify_written_df(connection, expected, original_stats):
    rebuilt, stats, _, _ = _validated_df(connection)
    if rebuilt != expected or set(stats) != set(original_stats):
        raise RestoreError("POST_WRITE_VALIDATION_FAILED")
    for kb_id, (total, average, encoded) in stats.items():
        if (total, average) != original_stats[kb_id][:2] or encoded != expected[kb_id]:
            raise RestoreError("POST_WRITE_VALIDATION_FAILED", kb_id=kb_id)


def restore_legacy_df(database: Path, backup: Path | None = None, apply=False, writers_stopped=False):
    """Dry-run by default; apply atomically after validation and a new backup.

    Writer acknowledgement is an operational prerequisite, not process discovery.
    BEGIN IMMEDIATE also excludes SQLite writers during snapshot/backup/update.
    Any commit exception is reported UNKNOWN and is never automatically retried.
    """
    database = Path(database).resolve(strict=True)
    if not database.is_file():
        raise RestoreError("DATABASE_NOT_A_FILE")
    if apply and (not writers_stopped or backup is None):
        raise RestoreError("APPLY_REQUIRES_STOPPED_WRITERS_AND_NEW_BACKUP")
    if not apply and backup is not None:
        raise RestoreError("BACKUP_ONLY_SUPPORTED_WITH_APPLY")
    backup = Path(backup).absolute() if backup is not None else None
    if backup is not None and (backup.exists() or backup.is_symlink() or backup.resolve() == database):
        raise RestoreError("BACKUP_MUST_BE_NEW", backup=str(backup))
    receipt = {"database": str(database), "status": "DRY_RUN", "apply": bool(apply),
               "writers_stopped_acknowledged": bool(writers_stopped), "commit_status": "NOT_ATTEMPTED"}
    with closing(_connect(database, writable=bool(apply))) as connection:
        commit_attempted = False
        try:
            connection.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
            expected, stats, chunks, postings = _validated_df(connection)
            receipt.update(stats_rows=len(stats), chunk_index_rows=chunks, posting_rows=postings,
                           df_terms=sum(len(json.loads(value)) for value in expected.values()),
                           changed_json_rows=sum(stats[kb_id][2] != value for kb_id, value in expected.items()))
            if not apply:
                connection.rollback()
                return receipt
            receipt["backup"] = str(backup)
            receipt["backup_sha256"] = _backup(database, backup)
            connection.executemany("UPDATE index_stats SET term_doc_freq=? WHERE kb_id=?",
                                   [(value, kb_id) for kb_id, value in sorted(expected.items())])
            _verify_written_df(connection, expected, stats)
            if _sha256(backup) != receipt["backup_sha256"]:
                raise RestoreError("BACKUP_CHANGED_DURING_RESTORE")
            commit_attempted = True
            connection.commit()
            receipt.update(status="APPLIED", commit_status="COMMITTED")
            return receipt
        except BaseException as exc:
            try:
                connection.rollback()
            except sqlite3.Error:
                pass
            if commit_attempted:
                raise RestoreError("COMMIT_OUTCOME_UNKNOWN", **{**receipt, "status": "UNKNOWN", "commit_status": "UNKNOWN"}) from exc
            if isinstance(exc, RestoreError):
                exc.receipt = {**receipt, **exc.receipt}
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--backup", type=Path)
    args = parser.parse_args(argv)
    try:
        result = restore_legacy_df(args.database, args.backup, args.apply, args.writers_stopped)
    except RestoreError as exc:
        print(json.dumps(exc.receipt, sort_keys=True))
        return 3 if exc.receipt["status"] == "UNKNOWN" else 2
    except (OSError, sqlite3.Error) as exc:
        print(json.dumps({"status": "FAILED", "error": type(exc).__name__}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

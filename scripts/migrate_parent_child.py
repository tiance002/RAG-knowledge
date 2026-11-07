"""Preview legacy file hash backfill and emit a non-destructive rebuild manifest.

Only --apply writes nullable identity fields. Never edits chunk relationships,
source bytes, vector/BM25 indexes or file status. A journal is mandatory on apply.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from audit_parent_child import digest, readonly, resolve_source


def append_journal(path, row):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        handle.flush()
        import os
        os.fsync(handle.fileno())


def migrate(database, root, *, apply=False, journal=None, batch_size=100, after_id="", file_ids=None):
    database, root = Path(database).resolve(), Path(root).resolve()
    if apply and not journal:
        raise ValueError("--journal is required with --apply")
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    # Preview does not import settings/models or migrate schema as a side effect.
    with closing(readonly(database)) as connection:
        connection.row_factory = sqlite3.Row
        query = "SELECT * FROM file_documents WHERE id > ?"
        params = [after_id]
        if file_ids:
            query += " AND id IN (" + ",".join("?" for _ in file_ids) + ")"
            params += list(file_ids)
        query += " ORDER BY id LIMIT ?"
        params.append(batch_size)
        files = [dict(row) for row in connection.execute(query, params)]
        columns = {row[1] for row in connection.execute("PRAGMA table_info(file_documents)")}
    if apply and not {"sha256", "original_name", "display_name"} <= columns:
        raise ValueError("Run the reviewed additive schema migration before backfill; no changes made")
    results = []
    for file in files:
        row = {"file_id": file["id"], "kb_id": file["kb_id"], "database": str(database),
               "timestamp": datetime.now(timezone.utc).isoformat()}
        source = resolve_source(file["local_path"], database, root)
        try:
            if source["status"] != "AVAILABLE":
                raise ValueError("SOURCE_" + source["status"])
            path = Path(source["existing"][0])
            before = path.stat()
            sha256 = digest(path)
            after = path.stat()
            stamp = (before.st_size, before.st_mtime_ns)
            if stamp != (after.st_size, after.st_mtime_ns):
                raise ValueError("SOURCE_CHANGED_DURING_HASH")
            if file.get("sha256") and file["sha256"] != sha256:
                raise ValueError("EXISTING_HASH_MISMATCH")
            row.update(sha256=sha256, source_path=str(path), name=file.get("original_name") or file["name"],
                       status="VERIFIED" if file.get("sha256") else "WOULD_BACKFILL")
            if apply:
                with closing(sqlite3.connect(database, timeout=30)) as writer:
                    writer.execute("BEGIN IMMEDIATE")
                    if stamp != (path.stat().st_size, path.stat().st_mtime_ns):
                        raise ValueError("SOURCE_CHANGED_BEFORE_COMMIT")
                    current = writer.execute("SELECT local_path,sha256 FROM file_documents WHERE id=?", (file["id"],)).fetchone()
                    if not current or current[0] != file["local_path"] or current[1] not in (None, "", sha256):
                        raise ValueError("DATABASE_CHANGED_BEFORE_COMMIT")
                    writer.execute("UPDATE file_documents SET sha256=?,original_name=COALESCE(original_name,name),"
                                   "display_name=COALESCE(display_name,name) WHERE id=?", (sha256, file["id"]))
                    writer.commit()
                row["status"] = "APPLIED"
        except Exception as error:
            row.update(status="FAILED", reason=str(error), error_type=type(error).__name__)
        if journal:
            append_journal(Path(journal), row)
        results.append(row)
    return {"schema": "parent-child-backfill-v1", "apply": apply, "database": str(database),
            "processed": len(results), "failed": sum(row["status"] == "FAILED" for row in results),
            "next_after_id": files[-1]["id"] if files else after_id, "results": results,
            "resume": "Retry a failed batch with the same arguments; successful rows are reverified idempotently. Advance --after-id only after reviewing failures."}


def rebuild_manifest(result):
    return {"schema": "parent-child-rebuild-plan-v1", "execution": "PREVIEW_ONLY",
            "contract": "Create an independent new file/version and validate its indexes before selecting it for traffic; retain old file, chunks, citations and indexes.",
            "files": [{"previous_file_id": row["file_id"], "kb_id": row["kb_id"],
                       "source_path": row.get("source_path"), "source_sha256": row.get("sha256"),
                       "eligible": row["status"] != "FAILED", "reason": row.get("reason"),
                       "action": "UPLOAD_AS_NEW_ID_THEN_PARSE_VALIDATE", "original_name": row.get("name"),
                       "config": {"parent_child": True, "parent_chunk_size": 4096, "parent_chunk_overlap": 0,
                                  "child_chunk_size": 384, "child_chunk_overlap": 0, "unit": "characters", "quality_evaluated": False}}
                      for row in result["results"]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--after-id", default="")
    parser.add_argument("--file-id", action="append")
    parser.add_argument("--rebuild-manifest", type=Path)
    args = parser.parse_args()
    result = migrate(args.database, args.root, apply=args.apply, journal=args.journal,
                     batch_size=args.batch_size, after_id=args.after_id, file_ids=args.file_id)
    if args.rebuild_manifest:
        args.rebuild_manifest.parent.mkdir(parents=True, exist_ok=True)
        args.rebuild_manifest.write_text(json.dumps(rebuild_manifest(result), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(1 if result["failed"] else 0)


if __name__ == "__main__":
    main()

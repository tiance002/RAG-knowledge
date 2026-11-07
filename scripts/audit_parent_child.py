"""Read-only, body-free audit of local SQL/source/index lineage (stdlib only)."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def resolve_source(raw_path, database_path, project_root):
    """A relative deployment path can be ambiguous: never silently choose one."""
    raw = Path(raw_path)
    if raw.is_absolute():
        candidates = [raw.resolve()]
    else:
        candidates = list(dict.fromkeys([(Path(database_path).parent / raw).resolve(),
                                         (Path(project_root) / raw).resolve()]))
    existing = [path for path in candidates if path.is_file()]
    return {"recorded_path": raw_path, "candidates": [str(path) for path in candidates],
            "existing": [str(path) for path in existing],
            "status": "AVAILABLE" if len(existing) == 1 else "AMBIGUOUS" if existing else "MISSING"}


def audit_database(path, project_root):
    result = {"path": str(Path(path).resolve()), "exists": Path(path).is_file()}
    if not result["exists"]:
        return result
    with closing(readonly(path)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        result["tables"] = sorted(tables)
        result["system_settings_rows"] = connection.execute("SELECT COUNT(*) FROM system_settings").fetchone()[0] if "system_settings" in tables else None
        required = {"file_documents", "document_chunks", "knowledge_bases"}
        if not required <= tables:
            result["status"] = "UNSUPPORTED_SCHEMA"
            return result
        connection.execute("BEGIN")  # SQL audit counts share one read snapshot.
        files = [dict(row) for row in connection.execute("SELECT * FROM file_documents")]
        chunks = {row["id"]: dict(row) for row in connection.execute(
            "SELECT id,file_id,chunk_type,parent_id FROM document_chunks")}
        file_by_id = {file["id"]: file for file in files}
        counts = Counter({"parent_refs": 0, "valid_parent_refs": 0, "dangling_parent_refs": 0,
                          "cross_file_parent_refs": 0, "wrong_parent_type_refs": 0,
                          "orphan_file_chunks": 0})
        anomalies = []
        for chunk in chunks.values():
            if chunk["file_id"] not in file_by_id:
                counts["orphan_file_chunks"] += 1
            parent_id = chunk.get("parent_id")
            if not parent_id:
                continue
            counts["parent_refs"] += 1
            parent = chunks.get(parent_id)
            reason = ("dangling_parent_refs" if parent is None else
                      "cross_file_parent_refs" if parent["file_id"] != chunk["file_id"] else
                      "wrong_parent_type_refs" if parent["chunk_type"] != "parent" else "valid_parent_refs")
            counts[reason] += 1
            if reason != "valid_parent_refs":
                anomalies.append({"chunk_id": chunk["id"], "file_id": chunk["file_id"],
                                  "parent_id": parent_id, "reason": reason, "repair": "UNKNOWN_REPARSE_REQUIRED"})
        bm25_ids = {row[0] for row in connection.execute("SELECT chunk_id FROM chunk_index")} if "chunk_index" in tables else set()
        result.update(counts=dict(counts), knowledge_bases=connection.execute("SELECT COUNT(*) FROM knowledge_bases").fetchone()[0],
                      files=len(files), chunks=len(chunks), chunk_types=dict(Counter(chunk["chunk_type"] for chunk in chunks.values())),
                      bm25={"rows": connection.execute("SELECT COUNT(*) FROM chunk_index").fetchone()[0] if "chunk_index" in tables else 0,
                            "unique_chunk_ids": len(bm25_ids), "orphan_chunk_ids": sorted(bm25_ids - chunks.keys()),
                            "parent_chunk_ids_indexed": sorted(cid for cid in bm25_ids if cid in chunks and chunks[cid]["chunk_type"] == "parent")},
                      parent_anomalies=anomalies,
                      files_detail=[{"file_id": file["id"], "kb_id": file["kb_id"], "name": file["name"],
                                     "status": file["status"], "sha256_known": bool(file.get("sha256")),
                                     "chunk_ids": sorted(cid for cid, chunk in chunks.items() if chunk["file_id"] == file["id"]),
                                     "source": resolve_source(file["local_path"], path, project_root)} for file in files])
        result["chunk_ids"] = sorted(chunks)
        # Evidence fingerprints expose no source body. Future audits can compare
        # complete rows, including contents, instead of relying only on counts.
        result["logical_fingerprints"] = {}
        for table, order_by in [("file_documents", "id"), ("document_chunks", "id"), ("chunk_index", "id")]:
            if table not in tables:
                continue
            table_columns = {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}
            if order_by not in table_columns:
                order_by = "chunk_id"
            hasher = hashlib.sha256()
            for record in connection.execute(f'SELECT * FROM "{table}" ORDER BY "{order_by}"'):
                hasher.update(json.dumps(dict(record), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
                hasher.update(b"\n")
            result["logical_fingerprints"][table] = hasher.hexdigest()
    return result


def audit_chroma(path, known_chunk_ids):
    result = {"path": str(Path(path).resolve()), "exists": Path(path).is_file()}
    if not result["exists"]:
        return result
    with closing(readonly(path)) as connection:
        connection.row_factory = sqlite3.Row
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"collections", "embeddings"} <= tables:
            return {**result, "status": "UNSUPPORTED_SCHEMA"}
        collections = []
        for row in connection.execute("SELECT * FROM collections"):
            # config_json_str may include embedding credentials; return distance only.
            row = dict(row)
            config = json.loads(row.get("config_json_str") or "{}")
            distance = (config.get("hnsw_configuration", {}).get("space") or
                        config.get("hnsw", {}).get("space"))
            collections.append({"id": row["id"], "name": row["name"],
                                "dimension": row.get("dimension"), "configured_distance": distance or "UNKNOWN"})
        ids = {row[0] for row in connection.execute("SELECT embedding_id FROM embeddings")}
        result.update(collections=collections, unique_embedding_ids=len(ids),
                      missing_sql_chunk_ids=sorted(ids - known_chunk_ids),
                      sql_chunk_ids_without_chroma_record=sorted(known_chunk_ids - ids),
                      note="SQL metadata presence only; vector binary integrity/query availability not exercised")
    return result


def inventory(root):
    paths = [".venv/pyvenv.cfg", "backend/.venv/pyvenv.cfg", "storage/uploads", "backend/storage/uploads",
             "storage/chroma_db", "backend/storage/chroma_db", "storage/pdf_manifests", "backend/storage/pdf_manifests"]
    resources = []
    for raw in paths:
        path = root / raw
        total = sum(item.stat().st_size for item in path.rglob("*") if item.is_file()) if path.is_dir() else path.stat().st_size if path.exists() else 0
        resources.append({"path": str(path), "exists": path.exists(), "bytes": total, "action": "REUSE_READ_ONLY"})
    # Catalog artifact names only, never file contents or credentials.
    artifact_names = []
    for directory in [root / "var", root / "archive", root / "storage"]:
        if directory.exists():
            for path in directory.rglob("*"):
                if path.is_file() and any(word in path.name.lower() for word in ["gold", "qrel", "frozen", "manifest", "dataset"]):
                    artifact_names.append(str(path))
    try:
        run = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}}"], capture_output=True, text=True, timeout=8)
        containers = {"exit_code": run.returncode, "output": run.stdout.strip(), "stderr": run.stderr.strip()}
    except (OSError, subprocess.TimeoutExpired) as error:
        containers = {"status": "UNKNOWN", "reason": type(error).__name__}
    return {"resources": resources, "existing_dataset_artifact_names": sorted(artifact_names), "containers": containers,
            "configuration_note": "Defaults are relative to server cwd; audit covers both root and backend layouts. Does not infer remote deployment state."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    before = shutil.disk_usage(root)._asdict()
    databases = [audit_database(root / name, root) for name in ["local.db", "backend/local.db"]]
    known = set().union(*(set(db.get("chunk_ids", [])) for db in databases))
    result = {"schema": "parent-child-audit-v1", "created_at": datetime.now(timezone.utc).isoformat(),
              "read_only": True, "disk_before": before, "databases": databases,
              "chroma": [audit_chroma(root / name, known) for name in ["storage/chroma_db/chroma.sqlite3", "backend/storage/chroma_db/chroma.sqlite3"]],
              "inventory": inventory(root), "disk_after": shutil.disk_usage(root)._asdict()}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()

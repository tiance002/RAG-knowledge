"""Durable upload intent and read-only recovery observations; never removes sources."""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from sqlmodel import select
from app.models import FileDocument


def _sync_dir(path):
    # Windows cannot open directories this way; file fsync still applies there.
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_journal(root, kb_id, file_id, path):
    directory = Path(root).resolve() / ".upload-recovery"
    directory.mkdir(parents=True, exist_ok=True)
    _sync_dir(directory.parent)
    journal = directory / (file_id + ".json")
    row = {"version": 1, "phase": "STAGING", "file_id": file_id,
           "kb_id": kb_id, "path": str(Path(path).resolve())}
    with journal.open("x", encoding="utf-8") as handle:
        json.dump(row, handle, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    _sync_dir(directory)
    return journal, row


def sync_staged_source(path):
    path = Path(path)
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())
    _sync_dir(path.parent)


def prepare_journal(journal, row, size, sha256):
    # Publish complete metadata atomically; a crash before replace leaves STAGING.
    updated = {**row, "phase": "PREPARED", "size": size, "sha256": sha256}
    temp = journal.with_name(journal.name + "." + uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(updated, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, journal)
        _sync_dir(journal.parent)
    finally:
        temp.unlink(missing_ok=True)


def clear_journal(journal):
    journal.unlink(missing_ok=True)
    _sync_dir(journal.parent)


def inspect_journal(session, root, file_id):
    """Observe current state only. Missing rows do not authorize file deletion."""
    if not isinstance(file_id, str) or not file_id or Path(file_id).name != file_id or file_id in {".", ".."}:
        raise ValueError("INVALID_UPLOAD_JOURNAL_ID")
    root = Path(root).resolve()
    journal = root / ".upload-recovery" / (file_id + ".json")
    if journal.parent.is_symlink() or journal.is_symlink() or not journal.is_file() or journal.stat().st_size > 16384:
        raise ValueError("INVALID_UPLOAD_JOURNAL")
    data = json.loads(journal.read_text(encoding="utf-8"))
    if data.get("version") != 1 or data.get("file_id") != file_id or data.get("phase") not in {"STAGING", "PREPARED"}:
        raise ValueError("INVALID_UPLOAD_JOURNAL")
    kb_id = data.get("kb_id")
    if not isinstance(kb_id, str) or not kb_id or Path(kb_id).name != kb_id or kb_id in {".", ".."}:
        raise ValueError("INVALID_UPLOAD_JOURNAL")
    source = Path(data["path"])
    expected_dir = (root / kb_id).resolve()
    if source.parent.resolve() != expected_dir or not source.name.startswith(file_id + "_source"):
        raise ValueError("INVALID_UPLOAD_JOURNAL_PATH")
    if source.is_symlink():
        raise ValueError("INVALID_UPLOAD_JOURNAL_PATH")
    references = session.exec(select(FileDocument).where(FileDocument.local_path == str(source))).all()
    row = session.get(FileDocument, file_id)
    matches = bool(row and row.kb_id == kb_id and row.local_path == str(source))
    status = "MATCHING_ROW_OBSERVED" if matches else ("CONFLICTING_ROW_OBSERVED" if row or references else "NO_ROW_OBSERVED")
    integrity = "UNKNOWN"
    if data["phase"] == "PREPARED" and source.is_file():
        h = hashlib.sha256()
        with source.open("rb") as handle:
            for part in iter(lambda: handle.read(1024 * 1024), b""):
                h.update(part)
        integrity = "MATCH" if source.stat().st_size == data.get("size") and h.hexdigest() == data.get("sha256") else "MISMATCH"
    elif not source.exists():
        integrity = "MISSING"
    return {"file_id": file_id, "kb_id": kb_id, "phase": data["phase"],
            "row_observation": status, "reference_count": len(references),
            "file_integrity": integrity, "cleanup_authorized": False}

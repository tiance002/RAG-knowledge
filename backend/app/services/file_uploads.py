"""Byte-identity uploads with transaction-scoped collision coordination.

No existing source or index is modified. Explicit replacement stages a new
version identified by supersedes_file_id; activation is a separate operation.
"""
import hashlib
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import text
from sqlmodel import select

from app.models import FileDocument, FileStatus, KnowledgeBase


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(part)
    return digest.hexdigest()


def original_name(row):
    return row.original_name or row.name


def file_view(row, digest=None):
    return {"file_id": row.id, "status": row.status.value,
            "original_name": original_name(row), "display_name": row.display_name or row.name,
            "sha256": digest or row.sha256, "supersedes_file_id": row.supersedes_file_id}


def register_upload(session, *, kb_id, file_id, name, path, size, sha256,
                    action="auto", target_file_id=None):
    """Coordinate check + insert in one DB write transaction, across processes.

    SQLite BEGIN IMMEDIATE serializes writers including absent-name checks.
    Other supported SQL backends lock the existing knowledge-base row. There
    is no unique hash constraint because explicit new identities may share it.
    The caller owns cleanup of its unique staged path on every non-create exit.
    """
    if action not in {"auto", "check", "reuse", "new", "replace"}:
        raise HTTPException(400, detail="未知上传操作")
    # The endpoint uses a dedicated dependency session. Release its preliminary
    # read transaction before claiming the write lock, then revalidate scope.
    session.rollback()
    if session.get_bind().dialect.name == "sqlite":
        session.execute(text("BEGIN IMMEDIATE"))
        kb = session.get(KnowledgeBase, kb_id)
    else:
        kb = session.exec(select(KnowledgeBase).where(KnowledgeBase.id == kb_id).with_for_update()).first()
    if kb is None or kb.is_deleted:
        raise HTTPException(404, detail="知识库不存在")
    rows = session.exec(select(FileDocument).where(FileDocument.kb_id == kb_id)
                        .order_by(FileDocument.created_at, FileDocument.id)).all()
    same_name = [row for row in rows if original_name(row) == name]
    hashes = {}
    for row in same_name:
        hashes[row.id] = row.sha256
        if not row.sha256:
            try:
                hashes[row.id] = digest_file(row.local_path)
            except OSError:
                # Missing sources and null hashes are unknown, never equal.
                pass
    identical = next((row for row in same_name if hashes[row.id] == sha256), None)
    conflict = {"code": "FILE_NAME_CONFLICT", "message": "已有同名文件，请选择复用、另建或替换为新版本",
                "original_name": name, "same_sha256": identical is not None,
                "duplicate_file_id": identical.id if identical else None,
                "existing_files": [file_view(row, hashes[row.id]) for row in same_name],
                "actions": (["reuse"] if identical else []) + ["new", "replace"]}
    if action == "check" and same_name:
        raise HTTPException(409, detail=conflict)
    if action in {"auto", "reuse"} and identical:
        if action == "reuse" and target_file_id:
            identical = next((row for row in same_name if row.id == target_file_id and hashes[row.id] == sha256), None)
            if identical is None:
                raise HTTPException(409, detail="所选文件与上传内容不一致，不能复用")
        result = {**file_view(identical, sha256), "outcome": "duplicate", "duplicate": True,
                  "name_conflict": True, "available_actions": conflict["actions"]}
        session.rollback()
        return result
    if action == "reuse":
        raise HTTPException(409, detail="没有内容相同的同名文件可复用")
    if action == "auto" and same_name:
        raise HTTPException(409, detail=conflict)
    target = None
    if action == "replace":
        target = next((row for row in same_name if row.id == target_file_id), None)
        if target is None:
            raise HTTPException(409, detail="替换目标不存在、不同名或不属于当前知识库")
    names = {row.display_name or row.name for row in rows}
    display = name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 1
    while display in names:
        display = f"{stem} ({counter}){suffix}"
        counter += 1
    row = FileDocument(id=file_id, kb_id=kb_id, name=name, original_name=name,
                       display_name=display, sha256=sha256, local_path=str(path), size=size,
                       supersedes_file_id=target.id if target else None, status=FileStatus.PENDING)
    session.add(row)
    result = {**file_view(row), "outcome": "replacement_staged" if target else "created",
              "duplicate": False, "name_conflict": bool(same_name), "available_actions": []}
    session.commit()
    return result

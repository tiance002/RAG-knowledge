"""Read-time version activation; old sources/indexes remain recoverable."""
from sqlmodel import select
from app.models import FileDocument, FileStatus


def inactive_file_ids(session, allowed_kb_ids=None):
    statement = select(FileDocument).where(
        FileDocument.supersedes_file_id.is_not(None),
        FileDocument.status.in_([FileStatus.PARSED, FileStatus.READY]))
    if allowed_kb_ids is not None:
        statement = statement.where(FileDocument.kb_id.in_(allowed_kb_ids))
    versions = session.exec(statement).all()
    target_ids = {v.supersedes_file_id for v in versions}
    targets = {f.id: f for f in session.exec(select(FileDocument).where(FileDocument.id.in_(target_ids))).all()} if target_ids else {}
    by_target = {}
    for version in versions:
        target = targets.get(version.supersedes_file_id)
        if not target or target.kb_id != version.kb_id:
            continue
        by_target.setdefault(target.id, []).append(version)
    inactive = set()
    for target_id, rows in by_target.items():
        rows.sort(key=lambda r: (r.created_at, r.id), reverse=True)
        inactive.add(target_id)
        inactive.update(r.id for r in rows[1:])
    return inactive

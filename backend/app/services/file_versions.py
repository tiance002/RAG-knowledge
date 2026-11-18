"""Read-time version activation; old sources/indexes remain recoverable."""
from sqlmodel import select
from sqlalchemy import or_
from app.models import FileDocument, FileStatus


def _require_flushed_eligibility_session(session):
    # Refreshing ORM identities must never discard explicitly unflushed edits.
    if not session.autoflush and (session.new or session.dirty or session.deleted):
        raise RuntimeError('ELIGIBILITY_UNFLUSHED_SESSION')


def inactive_file_ids(session, allowed_kb_ids=None, *, candidate_file_ids=None):
    _require_flushed_eligibility_session(session)
    # Keep complete version activation; only derivative reads can be narrowed.
    candidates = set(candidate_file_ids) if candidate_file_ids is not None else None
    if candidates == set():
        return set()
    statement = select(FileDocument).where(
        FileDocument.supersedes_file_id.is_not(None), FileDocument.deleted_at.is_(None),
        or_(FileDocument.active_build_id.is_not(None),
            FileDocument.status.in_([FileStatus.PARSED, FileStatus.READY])))
    if allowed_kb_ids is not None:
        statement = statement.where(FileDocument.kb_id.in_(allowed_kb_ids))
    versions = session.exec(statement.execution_options(populate_existing=True)).all()
    target_ids = {v.supersedes_file_id for v in versions}
    targets = {}
    target_list = sorted(target_ids)
    for start in range(0, len(target_list), 500):
        targets.update({f.id: f for f in session.exec(select(FileDocument).where(
            FileDocument.id.in_(target_list[start:start + 500])).execution_options(populate_existing=True)).all()})
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
    from app.services.image_caption_derivatives import inactive_caption_derivative_ids
    derivatives = inactive_caption_derivative_ids(session, allowed_kb_ids,
        candidate_file_ids=candidates) if candidates is not None else inactive_caption_derivative_ids(session, allowed_kb_ids)
    result = inactive | derivatives
    return result if candidates is None else result & candidates

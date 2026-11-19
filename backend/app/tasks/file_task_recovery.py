"""Standalone durable task scheduler and lease recovery (python -m app.tasks.file_task_recovery)."""
import os
import time
import logging
from datetime import datetime
from sqlalchemy import update
from sqlmodel import Session, select
from app.tasks import dispatch as d
from app.models import FileDocument, KnowledgeBase

RENOTIFY_MS = int(os.getenv('FILE_TASK_RENOTIFY_MS', '60000'))
SCAN_SECONDS = float(os.getenv('FILE_TASK_SCAN_SECONDS', '5'))

def scan_once(at=None, limit=100):
    at = d.now_ms() if at is None else at
    stats = {'expired': 0, 'unknown': 0, 'retried': 0, 'sent': 0, 'send_failed': 0}
    with Session(d.engine) as session:
        expired = session.exec(select(d.FileTaskReceipt).where(d.FileTaskReceipt.status == 'RUNNING',
            d.FileTaskReceipt.lease_until <= at).limit(limit)).all()
        for row in expired:
            pending = session.exec(select(d.FileTaskEffect).where(d.FileTaskEffect.task_id == row.task_id,
                d.FileTaskEffect.stage == row.stage, d.FileTaskEffect.state == 'PENDING')).first()
            state = 'UNKNOWN' if pending else ('FAILED' if row.stage_attempts >= d.MAX_STAGE_ATTEMPTS else 'RETRY_WAIT')
            delay = (10, 30, 90)[min(max(row.stage_attempts - 1, 0), 2)] * 1000
            changed = session.exec(update(d.FileTaskReceipt).where(d.FileTaskReceipt.task_id == row.task_id,
                d.FileTaskReceipt.status == 'RUNNING', d.FileTaskReceipt.owner_token == row.owner_token,
                d.FileTaskReceipt.lease_until <= at).values(status=state, owner_token=None, lease_until=None,
                    active_file=None if state == 'FAILED' else row.active_file,
                    next_run_at=at+delay, error_type='LeaseExpired', last_dispatched_at=None, updated_at=datetime.utcnow()))
            if changed.rowcount:
                session.exec(update(d.FileTaskAttempt).where(d.FileTaskAttempt.task_id == row.task_id,
                    d.FileTaskAttempt.attempt_no == row.attempt_no, d.FileTaskAttempt.outcome == 'RUNNING')
                    .values(outcome=state, finished_at=at, error_code='LeaseExpired'))
                d.project_failure_in_session(session, row, state, 'LeaseExpired')
                stats['expired'] += 1
                if state == 'UNKNOWN':
                    stats['unknown'] += 1
                elif state == 'RETRY_WAIT':
                    stats['retried'] += 1
        session.commit()
    with Session(d.engine) as session:
        receipt = d.FileTaskReceipt
        uncertain = session.exec(select(receipt.task_id, receipt.attempt_no).where(
            receipt.status == 'UNKNOWN', receipt.stage.in_(['parse', 'embed']),
            receipt.next_run_at != d.UNKNOWN_REVIEWED,
        ).order_by(receipt.updated_at, receipt.task_id).limit(limit)).all()
    for task_id, attempt_no in uncertain:
        if d.reconcile_unknown(task_id, attempt_no):
            stats['retried'] += 1
    with Session(d.engine) as session:
        receipt = d.FileTaskReceipt
        runnable = (receipt.status.in_(['QUEUED', 'PUBLISH_UNKNOWN', 'RETRY_WAIT']),
                    receipt.next_run_at <= at,
                    receipt.last_dispatched_at.is_(None) | (receipt.last_dispatched_at <= at - RENOTIFY_MS))
        # Capture immutable values, not ORM instances that expire on the commits below.
        due = session.exec(select(receipt.task_id, receipt.file_id, receipt.stage, receipt.target_generation)
            .where(*runnable).order_by(receipt.last_dispatched_at, receipt.next_run_at, receipt.created_at)
            .limit(limit)).all()
        for task_id, file_id, stage, generation in due:
            doc = session.get(FileDocument, file_id)
            kb = session.get(KnowledgeBase, doc.kb_id) if doc else None
            invalid = (not doc or getattr(doc, 'deleted_at', None) is not None or not kb or kb.is_deleted
                       or doc.desired_generation != generation)
            condition = (receipt.task_id == task_id, receipt.stage == stage, *runnable)
            if invalid:
                session.exec(update(receipt).where(*condition).values(
                    status='CANCELLED', active_file=None, error_type='DocumentUnavailableOrSuperseded',
                    error_message='文件已删除或任务版本已被替代', updated_at=datetime.utcnow()))
                session.commit()
                continue
            changed = session.exec(update(receipt).where(*condition).values(last_dispatched_at=at))
            session.commit()
            if not changed.rowcount:
                continue
            try:
                d.notify_task(task_id, stage)
                stats['sent'] += 1
            except Exception:
                # One failed broker attempt per scan, with this receipt's
                # reservation retained for bounded redelivery/backoff.
                stats['send_failed'] += 1
                logging.warning("File task %s notification failed; scheduler will retry", task_id, exc_info=True)
                break
        session.commit()
    return stats

def main():
    from app.core.database import init_db
    init_db()
    last_sweep = 0
    while True:
        try:
            result = scan_once()
            if not result['send_failed'] and time.monotonic() - last_sweep >= 300:
                d.notify_sweep()
                last_sweep = time.monotonic()
        except Exception:
            logging.exception('file task recovery scan failed')
        time.sleep(SCAN_SECONDS)

if __name__ == '__main__':
    main()

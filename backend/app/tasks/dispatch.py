"""Durable receipts for one Celery execution per file operation.

A duplicate delivery may recover a queued operation, never replay an operation
which could already have issued a provider request.
"""
import json
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Field, Session, select

from app.core.database import engine
from app.tasks.celery_app import celery_app


class FileTaskReceipt(SQLModel, table=True):
    __tablename__ = 'file_task_receipts'
    task_id: str = Field(primary_key=True)
    file_id: str = Field(index=True)
    active_file: Optional[str] = Field(default=None, unique=True)
    operation: str
    status: str = 'QUEUED'
    payload: str
    result: Optional[str] = None
    error_type: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class FileTaskBusy(RuntimeError):
    def __init__(self, task_id=None):
        self.task_id = task_id
        super().__init__('该文件已有排队、运行或结果未知的任务，请先查询状态')


class DispatchUnconfirmed(RuntimeError):
    def __init__(self, task_id):
        self.task_id = task_id
        super().__init__('任务投递未确认，请查询任务状态，不要自动重发')


def receipt_view(row):
    return dict(task_id=row.task_id, file_id=row.file_id, operation=row.operation,
                status=row.status, error_type=row.error_type,
                result=json.loads(row.result) if row.result else None,
                created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())


def dispatch_file_task(file_id, operation, payload):
    if operation not in ('parse', 'index'):
        raise ValueError('TASK_OPERATION_INVALID')
    payload = dict(payload)
    from app.services.runtime_settings import get_runtime_settings
    from app.models import FileDocument, KnowledgeBase
    with Session(engine) as session:
        snapshot = get_runtime_settings(session)
        payload['_settings_revision'] = snapshot.revision
        file_doc = session.get(FileDocument, file_id)
        kb = session.get(KnowledgeBase, file_doc.kb_id) if file_doc else None
        if operation == 'parse':
            for key, value in {'chunk_mode':'custom', 'chunk_size':snapshot.chunk_size,
                               'chunk_overlap':snapshot.chunk_overlap, 'heading_level':2}.items():
                if payload.get(key) is None:
                    payload[key] = value
            payload['vlm_model_id'] = kb.vlm_model if kb and kb.vlm_model else snapshot.default_vlm_model
        if payload.get('vector_model') is None and kb:
            payload['vector_model'] = kb.embedding_model
    task_id = str(uuid4())
    row = FileTaskReceipt(task_id=task_id, file_id=file_id, active_file=file_id,
                          operation=operation, payload=json.dumps(payload, ensure_ascii=False))
    with Session(engine) as session:
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            existing = session.exec(select(FileTaskReceipt).where(FileTaskReceipt.active_file == file_id)).first()
            raise FileTaskBusy(existing.task_id if existing else None) from exc
    try:
        execute_file_operation.apply_async(args=[task_id], task_id=task_id,
            queue=celery_app.conf.task_default_queue, retry=False)
    except Exception as exc:
        # Publish can have succeeded before the connection failed. Keep the same
        # ID and exclusion: a late delivery remains permitted, but no new job.
        with Session(engine) as session:
            session.exec(update(FileTaskReceipt).where(
                FileTaskReceipt.task_id == task_id, FileTaskReceipt.status == 'QUEUED'
            ).values(status='PUBLISH_UNKNOWN', error_type=type(exc).__name__, updated_at=datetime.utcnow()))
            session.commit()
        raise DispatchUnconfirmed(task_id) from exc
    return dict(task_id=task_id, status='QUEUED')


@celery_app.task(bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=0)
def execute_file_operation(self, task_id):
    with Session(engine) as session:
        claimed = session.exec(update(FileTaskReceipt).where(
            FileTaskReceipt.task_id == task_id,
            FileTaskReceipt.status.in_(['QUEUED', 'PUBLISH_UNKNOWN'])
        ).values(status='RUNNING', updated_at=datetime.utcnow()))
        session.commit()
        row = session.get(FileTaskReceipt, task_id)
        if row is None:
            raise ValueError('TASK_RECEIPT_MISSING')
        if claimed.rowcount != 1:
            if row.status == 'RUNNING':
                row.status = 'UNKNOWN'
                row.error_type = 'DuplicateOrInterruptedExecution'
                row.updated_at = datetime.utcnow()
                session.add(row); session.commit()
            return dict(task_id=task_id, status=row.status, duplicate_delivery=True)
        operation, file_id, payload = row.operation, row.file_id, json.loads(row.payload)
    try:
        from app.services.metrics import track_background_task
        from app.tasks.parse_tasks import process_file_parsing, process_submitted_chunks
        settings_revision = payload.pop('_settings_revision', None)
        if operation == 'parse':
            # Pre-migration queued tasks never requested captions. Do not add a
            # new paid VLM operation while consuming those old payloads.
            payload.setdefault('vlm_model_id', None)
            result = track_background_task('parse')(process_file_parsing)(file_id, **payload)
        elif operation == 'index':
            result = track_background_task('index')(process_submitted_chunks)(file_id, **payload)
        else:
            raise ValueError('TASK_OPERATION_INVALID')
        result = {**(result or {}), 'settings_revision': settings_revision}
        final = 'FAILED' if isinstance(result, dict) and result.get('error') else 'COMPLETED'
        with Session(engine) as session:
            row = session.get(FileTaskReceipt, task_id)
            row.status, row.result, row.active_file = final, json.dumps(result, ensure_ascii=False), None
            row.updated_at = datetime.utcnow(); session.add(row); session.commit()
        return dict(task_id=task_id, status=final, result=result)
    except BaseException as exc:
        from app.services.llm import is_unknown_provider_error
        unknown = is_unknown_provider_error(exc) or type(exc).__name__ in ('SoftTimeLimitExceeded', 'TimeLimitExceeded', 'WorkerLostError', 'CancelledError')
        with Session(engine) as session:
            row = session.get(FileTaskReceipt, task_id)
            row.status, row.error_type = ('UNKNOWN' if unknown else 'FAILED'), type(exc).__name__
            if not unknown:
                row.active_file = None
            row.updated_at = datetime.utcnow(); session.add(row); session.commit()
        raise

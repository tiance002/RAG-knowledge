"""Durable, fenced file task ledger. SQLite is the authority, not Celery."""
import hashlib
import json
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Integer, exists, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import SQLModel, Field, Session, select
from app.core.config import get_settings
from app.core.database import engine
from app.tasks.celery_app import celery_app

LEASE_MS = int(os.getenv('FILE_TASK_LEASE_MS', '90000'))
HEARTBEAT_SECONDS = float(os.getenv('FILE_TASK_HEARTBEAT_SECONDS', '20'))
MAX_STAGE_ATTEMPTS = 4
ATTEMPT_LIMIT_MS = get_settings().CELERY_TASK_TIME_LIMIT * 1000

def now_ms():
    return time.time_ns() // 1000000

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
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
    request_key: Optional[str] = None
    input_hash: Optional[str] = None
    target_generation: Optional[int] = None
    stage: Optional[str] = None
    attempt_no: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default='0'))
    stage_attempts: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default='0'))
    next_run_at: int = Field(default=0, sa_column=Column(Integer, nullable=False, server_default='0'))
    owner_token: Optional[str] = None
    heartbeat_at: Optional[int] = None
    lease_until: Optional[int] = None
    last_dispatched_at: Optional[int] = None
    checkpoint_ref: Optional[str] = None

class FileTaskAttempt(SQLModel, table=True):
    __tablename__ = 'file_task_attempts'
    task_id: str = Field(primary_key=True)
    attempt_no: int = Field(primary_key=True)
    stage: str
    owner_token: str = Field(unique=True)
    worker_id: str
    started_at: int
    finished_at: Optional[int] = None
    outcome: str = 'RUNNING'
    error_code: Optional[str] = None
    artifact_ref: Optional[str] = None

class FileTaskEffect(SQLModel, table=True):
    __tablename__ = 'file_task_effects'
    effect_key: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    stage: str
    input_hash: str
    provider_request_id: Optional[str] = None
    state: str = 'PENDING'
    result_ref: Optional[str] = None
    updated_at: int

class FileTaskEvent(SQLModel, table=True):
    __tablename__ = 'file_task_events'
    event_id: str = Field(primary_key=True)
    task_id: str = Field(index=True)
    action: str
    reason: str
    created_at: int

class RetryableStageError(RuntimeError):
    def __init__(self, delay_ms, code):
        self.delay_ms = max(0, int(delay_ms))
        self.code = str(code)
        super().__init__(self.code)

@dataclass(frozen=True)
class Claim:
    task_id: str
    file_id: str
    target_generation: int
    owner_token: str
    attempt_no: int
    stage: str
    payload: dict
    checkpoint_ref: Optional[str]

class FileTaskBusy(RuntimeError):
    def __init__(self, task_id=None):
        self.task_id = task_id
        super().__init__('该文件已有排队、运行或结果未知的任务，请先查询状态')

class FileTaskConflict(RuntimeError):
    pass

class DispatchUnconfirmed(RuntimeError):
    def __init__(self, task_id):
        self.task_id = task_id
        super().__init__('任务投递未确认')

def receipt_view(row):
    return dict(task_id=row.task_id, file_id=row.file_id, operation=row.operation,
                status=row.status, stage=row.stage, attempt_no=row.attempt_no,
                stage_attempts=row.stage_attempts, target_generation=row.target_generation,
                heartbeat_at=row.heartbeat_at, next_run_at=row.next_run_at,
                checkpoint_ref=row.checkpoint_ref, error_type=row.error_type, error_message=row.error_message,
                result=json.loads(row.result) if row.result else None,
                created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat())

def _hash_input(operation, payload):
    return hashlib.sha256(json.dumps([operation, payload], sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()

def lookup_file_request(file_id, operation, request_key, request_input):
    if request_key is None:
        return None
    digest = _hash_input(operation, request_input)
    with Session(engine) as session:
        prior = session.exec(select(FileTaskReceipt).where(FileTaskReceipt.file_id == file_id,
            FileTaskReceipt.operation == operation, FileTaskReceipt.request_key == request_key)).first()
        if not prior:
            return None
        if prior.input_hash != digest:
            raise FileTaskConflict('TASK_REQUEST_KEY_CONFLICT')
        return dict(task_id=prior.task_id, status=prior.status)

def dispatch_file_task(file_id, operation, payload, request_key=None, *, expected_generation=None, request_input=None):
    if operation not in ('parse', 'index'):
        raise ValueError('TASK_OPERATION_INVALID')
    if request_key is not None and (not isinstance(request_key, str) or not request_key.strip() or len(request_key) > 256):
        raise ValueError('TASK_REQUEST_KEY_INVALID')
    payload = dict(payload)
    digest = _hash_input(operation, payload if request_input is None else request_input)
    from app.services.runtime_settings import get_runtime_settings
    from app.models import FileDocument, KnowledgeBase
    if request_key is not None:
        existing = lookup_file_request(file_id, operation, request_key, payload if request_input is None else request_input)
        if existing:
            return existing
    # Freeze potentially expensive file/model inputs before acquiring a write lock.
    from app.tasks.staged_pipeline import freeze_payload
    with Session(engine) as session:
        doc = session.get(FileDocument, file_id)
        if doc is None or getattr(doc, 'deleted_at', None) is not None:
            raise ValueError('FILE_NOT_FOUND')
        kb = session.get(KnowledgeBase, doc.kb_id)
        if kb is None or kb.is_deleted:
            raise ValueError('FILE_NOT_FOUND')
        snapshot = get_runtime_settings(session)
        payload['_settings_revision'] = snapshot.revision
        if operation == 'parse':
            for key, value in {'chunk_mode':'custom', 'chunk_size':snapshot.chunk_size,
                               'chunk_overlap':snapshot.chunk_overlap, 'heading_level':2}.items():
                if payload.get(key) is None:
                    payload[key] = value
            payload['vlm_model_id'] = kb.vlm_model if kb.vlm_model else snapshot.default_vlm_model
        if payload.get('vector_model') is None:
            payload['vector_model'] = kb.embedding_model
        identity = (doc.kb_id, doc.local_path, doc.sha256, doc.desired_generation, doc.updated_at)
        kb_identity = (kb.embedding_model, kb.vlm_model, kb.is_deleted)
        payload = freeze_payload(session, doc, operation, payload)
    task_id = str(uuid4())
    stage = 'parse' if operation == 'parse' else 'embed'
    with Session(engine) as session:
        if engine.dialect.name == 'sqlite':
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        if request_key is not None:
            prior = session.exec(select(FileTaskReceipt).where(FileTaskReceipt.file_id == file_id,
                FileTaskReceipt.operation == operation, FileTaskReceipt.request_key == request_key)).first()
            if prior:
                if prior.input_hash != digest:
                    raise FileTaskConflict('TASK_REQUEST_KEY_CONFLICT')
                return dict(task_id=prior.task_id, status=prior.status)
        doc = session.get(FileDocument, file_id)
        if doc is None or getattr(doc, 'deleted_at', None) is not None:
            raise ValueError('FILE_NOT_FOUND')
        kb = session.get(KnowledgeBase, doc.kb_id)
        if kb is None or kb.is_deleted:
            raise ValueError('FILE_NOT_FOUND')
        if ((doc.kb_id, doc.local_path, doc.sha256, doc.desired_generation, doc.updated_at) != identity or
                (kb.embedding_model, kb.vlm_model, kb.is_deleted) != kb_identity):
            raise FileTaskConflict('TASK_INPUT_CHANGED')
        if get_runtime_settings(session).revision != snapshot.revision:
            raise FileTaskConflict('TASK_SETTINGS_CHANGED')
        if expected_generation is not None and doc.desired_generation != expected_generation:
            raise FileTaskConflict('TASK_GENERATION_CONFLICT')
        busy = session.exec(select(FileTaskReceipt).where(FileTaskReceipt.active_file == file_id)).first()
        if busy:
            raise FileTaskBusy(busy.task_id)
        doc.desired_generation = (doc.desired_generation or 0) + 1
        from app.models import FileStatus
        if not doc.active_build_id and doc.status not in (FileStatus.PARSED, FileStatus.READY):
            doc.status = FileStatus.PARSING if operation == 'parse' else FileStatus.EMBEDDING
            doc.progress = 0
        doc.error_message = None
        row = FileTaskReceipt(task_id=task_id, file_id=file_id, active_file=file_id,
            operation=operation, stage=stage, target_generation=doc.desired_generation,
            request_key=request_key, input_hash=digest, payload=json.dumps(payload, ensure_ascii=False))
        session.add(doc)
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            existing = session.exec(select(FileTaskReceipt).where(FileTaskReceipt.active_file == file_id)).first()
            raise FileTaskBusy(existing.task_id if existing else None) from exc
    # The scanner is the sole broker publisher. HTTP success depends only on
    # the committed receipt and never waits for Redis or a Celery backend.
    return dict(task_id=task_id, status='QUEUED')

def _bounded_publish(task, *, args=None, task_id=None, queue):
    # Redis transport uses socket_* settings independently of connect_timeout.
    with celery_app.connection_for_write(connect_timeout=1, transport_options={
            'socket_connect_timeout': 1, 'socket_timeout': 1, 'retry_on_timeout': False}) as connection:
        task.apply_async(args=args or [], task_id=task_id, queue=queue, retry=False,
                         ignore_result=True, connection=connection)

def notify_task(task_id, stage):
    queue = 'document.parse' if stage in ('parse', 'embed') else 'document.index'
    _bounded_publish(execute_file_operation, args=[task_id, stage], task_id=task_id, queue=queue)

def notify_sweep():
    _bounded_publish(sweep_index_builds, queue='document.index')

def _document_condition(claim):
    from app.models import FileDocument, KnowledgeBase
    conditions = [FileDocument.id == claim.file_id, FileDocument.desired_generation == claim.target_generation,
                  exists(select(KnowledgeBase.id).where(KnowledgeBase.id == FileDocument.kb_id, KnowledgeBase.is_deleted == False))]
    if hasattr(FileDocument, 'deleted_at'):
        conditions.append(FileDocument.deleted_at.is_(None))
    return exists(select(FileDocument.id).where(*conditions))

def _claim_conditions(claim, at):
    return (FileTaskReceipt.task_id == claim.task_id, FileTaskReceipt.file_id == claim.file_id,
            FileTaskReceipt.stage == claim.stage, FileTaskReceipt.status == 'RUNNING',
            FileTaskReceipt.owner_token == claim.owner_token,
            FileTaskReceipt.target_generation == claim.target_generation,
            FileTaskReceipt.lease_until > at, _document_condition(claim))

def guard_claim_in_session(session, claim):
    """First write in a publication transaction fences stale owners and takes SQLite write lock."""
    at = now_ms()
    result = session.exec(update(FileTaskReceipt).where(*_claim_conditions(claim, at))
                          .values(updated_at=datetime.utcnow()))
    return result.rowcount == 1

def claim_task(task_id, expected_stage, worker_id='worker'):
    from app.models import FileDocument, KnowledgeBase
    at = now_ms()
    token = str(uuid4())
    with Session(engine) as session:
        row = session.get(FileTaskReceipt, task_id)
        if not row or not expected_stage or row.stage != expected_stage or row.target_generation is None:
            return None
        conditions = [FileDocument.id == row.file_id, FileDocument.desired_generation == row.target_generation,
                      exists(select(KnowledgeBase.id).where(KnowledgeBase.id == FileDocument.kb_id, KnowledgeBase.is_deleted == False))]
        if hasattr(FileDocument, 'deleted_at'):
            conditions.append(FileDocument.deleted_at.is_(None))
        result = session.exec(update(FileTaskReceipt).where(FileTaskReceipt.task_id == task_id,
            FileTaskReceipt.stage == expected_stage, FileTaskReceipt.status.in_(['QUEUED', 'RETRY_WAIT', 'PUBLISH_UNKNOWN']),
            FileTaskReceipt.next_run_at <= at, FileTaskReceipt.stage_attempts < MAX_STAGE_ATTEMPTS,
            exists(select(FileDocument.id).where(*conditions))).values(status='RUNNING', owner_token=token,
            heartbeat_at=at, lease_until=at + min(LEASE_MS, ATTEMPT_LIMIT_MS), attempt_no=FileTaskReceipt.attempt_no+1,
            stage_attempts=FileTaskReceipt.stage_attempts+1, updated_at=datetime.utcnow()))
        if result.rowcount != 1:
            session.rollback()
            return None
        session.expire_all()
        row = session.get(FileTaskReceipt, task_id)
        session.add(FileTaskAttempt(task_id=task_id, attempt_no=row.attempt_no, stage=row.stage,
                    owner_token=token, worker_id=worker_id, started_at=at))
        claim = Claim(task_id, row.file_id, row.target_generation, token, row.attempt_no,
                      row.stage, json.loads(row.payload), row.checkpoint_ref)
        session.commit()
        return claim

def heartbeat(claim):
    at = now_ms()
    with Session(engine) as session:
        attempt = session.get(FileTaskAttempt, (claim.task_id, claim.attempt_no))
        if not attempt or attempt.owner_token != claim.owner_token:
            return False
        deadline = attempt.started_at + ATTEMPT_LIMIT_MS
        if deadline <= at:
            return False
        result = session.exec(update(FileTaskReceipt).where(*_claim_conditions(claim, at))
            .values(heartbeat_at=at, lease_until=min(at + LEASE_MS, deadline), updated_at=datetime.utcnow()))
        session.commit()
        return result.rowcount == 1

def _finish_attempt(session, claim, outcome, error_code=None, artifact_ref=None):
    session.exec(update(FileTaskAttempt).where(FileTaskAttempt.task_id == claim.task_id,
        FileTaskAttempt.attempt_no == claim.attempt_no, FileTaskAttempt.owner_token == claim.owner_token,
        FileTaskAttempt.outcome == 'RUNNING').values(outcome=outcome, finished_at=now_ms(),
            error_code=error_code, artifact_ref=artifact_ref))

def complete_in_session(session, claim, result):
    at = now_ms()
    changed = session.exec(update(FileTaskReceipt).where(*_claim_conditions(claim, at)).values(
        status='COMPLETED', result=json.dumps(result, ensure_ascii=False), active_file=None,
        owner_token=None, lease_until=None, updated_at=datetime.utcnow()))
    if changed.rowcount == 1:
        _finish_attempt(session, claim, 'COMPLETED')
    return changed.rowcount == 1

def handoff(claim, next_stage, checkpoint_ref):
    if next_stage not in ('parse', 'embed', 'index', 'publish') or not checkpoint_ref:
        raise ValueError('INVALID_CHECKPOINT')
    with Session(engine) as session:
        changed = session.exec(update(FileTaskReceipt).where(*_claim_conditions(claim, now_ms())).values(
            status='QUEUED', stage=next_stage, checkpoint_ref=checkpoint_ref, stage_attempts=0,
            owner_token=None, lease_until=None, next_run_at=0, last_dispatched_at=None, updated_at=datetime.utcnow()))
        if changed.rowcount == 1:
            _finish_attempt(session, claim, 'HANDED_OFF', artifact_ref=checkpoint_ref)
        session.commit()
        return changed.rowcount == 1

def begin_effect(claim, effect_key, input_hash):
    with Session(engine) as session:
        if not guard_claim_in_session(session, claim):
            session.rollback()
            raise RuntimeError('TASK_LEASE_LOST')
        effect = session.get(FileTaskEffect, effect_key)
        if effect:
            if effect.task_id != claim.task_id or effect.stage != claim.stage or effect.input_hash != input_hash:
                raise FileTaskConflict('TASK_EFFECT_CONFLICT')
            state = effect.state
            if state in ('REPLAY_AUTHORIZED', 'RETRYABLE'):
                effect.state = 'PENDING'
                effect.updated_at = now_ms()
                session.add(effect)
                state = 'NEW'
        else:
            session.add(FileTaskEffect(effect_key=effect_key, task_id=claim.task_id, stage=claim.stage,
                input_hash=input_hash, updated_at=now_ms()))
            state = 'NEW'
        session.commit()
        return state

def mark_effect_result(claim, effect_key, result_ref, provider_request_id=None):
    if not result_ref:
        raise ValueError('EFFECT_RESULT_REQUIRED')
    with Session(engine) as session:
        if not guard_claim_in_session(session, claim):
            session.rollback()
            return False
        changed = session.exec(update(FileTaskEffect).where(FileTaskEffect.effect_key == effect_key,
            FileTaskEffect.task_id == claim.task_id, FileTaskEffect.stage == claim.stage,
            FileTaskEffect.state == 'PENDING').values(state='COMPLETED', result_ref=result_ref,
                provider_request_id=provider_request_id, updated_at=now_ms()))
        session.commit()
        return changed.rowcount == 1

def mark_effect_retryable(claim, effect_key, delay_ms, code):
    """Only call on a provider response proving the request was not accepted."""
    with Session(engine) as session:
        if not guard_claim_in_session(session, claim):
            session.rollback()
            return False
        changed = session.exec(update(FileTaskEffect).where(FileTaskEffect.effect_key == effect_key,
            FileTaskEffect.task_id == claim.task_id, FileTaskEffect.stage == claim.stage,
            FileTaskEffect.state == 'PENDING').values(state='RETRYABLE', updated_at=now_ms()))
        session.commit()
        return changed.rowcount == 1

def project_failure_in_session(session, row, state, code):
    from app.models import FileDocument, FileStatus
    doc = session.get(FileDocument, row.file_id)
    if doc and doc.desired_generation == row.target_generation and not getattr(doc, 'deleted_at', None):
        if not doc.active_build_id and doc.status not in (FileStatus.PARSED, FileStatus.READY):
            if state in ('FAILED', 'UNKNOWN', 'CANCELLED'):
                doc.status = FileStatus.FAILED
            elif state in ('RETRY_WAIT', 'QUEUED'):
                doc.status = FileStatus.PARSING if row.stage == 'parse' else FileStatus.EMBEDDING
        doc.error_message = (('任务结果待核对: ' if state == 'UNKNOWN' else '任务执行失败: ') + code
                             if state in ('FAILED', 'UNKNOWN', 'CANCELLED') else None)
        session.add(doc)

def cancel_in_session(session, file_id, reason='cancelled'):
    # The UPDATE is the first ledger access so cancellation and completion are
    # serialized by SQLite's writer lock, never by a stale read snapshot.
    rows = session.exec(update(FileTaskReceipt).where(FileTaskReceipt.active_file == file_id)
        .values(status='CANCELLED', active_file=None, owner_token=None, lease_until=None,
                error_type=reason, updated_at=datetime.utcnow())
        .returning(FileTaskReceipt.task_id)).all()
    for task_id in rows:
        # At most one attempt is RUNNING for a task; this remains in the caller's transaction.
        value = task_id[0]
        session.exec(update(FileTaskAttempt).where(FileTaskAttempt.task_id == value,
            FileTaskAttempt.outcome == 'RUNNING').values(outcome='CANCELLED',
                finished_at=now_ms(), error_code=reason))
    return len(rows)

def resolve_unknown(file_id, task_id, action, reason):
    if action not in ('retry', 'terminate', 'resume') or not isinstance(reason, str) or not reason.strip():
        raise ValueError('INVALID_UNKNOWN_RESOLUTION')
    with Session(engine) as session:
        if engine.dialect.name == 'sqlite': session.connection().exec_driver_sql('BEGIN IMMEDIATE')
        row = session.get(FileTaskReceipt, task_id)
        if not row or row.file_id != file_id or row.status != 'UNKNOWN':
            raise FileTaskConflict('TASK_NOT_UNKNOWN')
        if action != 'terminate' and row.target_generation is None:
            raise FileTaskConflict('LEGACY_TASK_CANNOT_REPLAY')
        if action == 'resume':
            effects = session.exec(select(FileTaskEffect).where(FileTaskEffect.task_id == task_id,
                FileTaskEffect.stage == row.stage)).all()
            from app.tasks.staged_pipeline import recover_materialized_effects_for_resume, verify_checkpoint_for_resume
            if not recover_materialized_effects_for_resume(session, row, effects):
                raise FileTaskConflict('VERIFIED_EFFECT_RESULT_REQUIRED')
            if not verify_checkpoint_for_resume(row, effects):
                raise FileTaskConflict('VERIFIED_CHECKPOINT_REQUIRED')
        if action == 'retry':
            session.exec(update(FileTaskEffect).where(FileTaskEffect.task_id == task_id,
                FileTaskEffect.state == 'PENDING').values(state='REPLAY_AUTHORIZED', updated_at=now_ms()))
        row.status = 'CANCELLED' if action == 'terminate' else 'RETRY_WAIT'
        row.error_type = 'MANUAL_'+action.upper()+': '+reason.strip()
        row.next_run_at = 0
        row.owner_token=None
        row.lease_until=None
        row.last_dispatched_at=None
        if action == 'terminate': row.active_file=None
        else: row.stage_attempts=0
        row.updated_at=datetime.utcnow()
        session.add(row)
        session.add(FileTaskEvent(event_id=str(uuid4()), task_id=task_id, action=action, reason=reason.strip(), created_at=now_ms()))
        project_failure_in_session(session, row, row.status, 'MANUAL_' + action.upper())
        session.commit()
        return receipt_view(row)

def _fail_claim(claim, exc):
    from app.services.llm import is_unknown_provider_error
    with Session(engine) as session:
        if not guard_claim_in_session(session, claim):
            session.rollback()
            return
        pending = session.exec(select(FileTaskEffect).where(FileTaskEffect.task_id == claim.task_id,
            FileTaskEffect.stage == claim.stage, FileTaskEffect.state == 'PENDING')).first()
        unknown = bool(pending) or (is_unknown_provider_error(exc) and not isinstance(exc, RetryableStageError))
        row=session.get(FileTaskReceipt, claim.task_id)
        from sqlalchemy.exc import OperationalError
        retryable = (isinstance(exc, RetryableStageError) or (isinstance(exc, OperationalError) and 'locked' in str(exc).lower())
                     or type(exc).__name__ in ('SoftTimeLimitExceeded','TimeLimitExceeded','WorkerLostError','CancelledError'))
        row.status = 'UNKNOWN' if unknown else ('RETRY_WAIT' if retryable and row.stage_attempts < MAX_STAGE_ATTEMPTS else 'FAILED')
        row.error_type=type(exc).__name__
        code = str(exc)
        # Internal reason codes are safe to expose. Provider payloads and credentials are not.
        if isinstance(exc, RetryableStageError):
            code = exc.code
        elif not re.fullmatch(r"(?:TASK|BUILD|FILE|KNOWLEDGE_BASE|EFFECT|MODEL|SYSTEM_MODEL|EMBEDDING|PREVIEW|PROVENANCE|PARENT|CHILD|CHUNK)_[A-Z0-9_]+(?:: ?[A-Za-z_]+)?", code):
            code = type(exc).__name__
        row.error_message = code
        row.owner_token=None
        row.lease_until=None
        row.updated_at=datetime.utcnow()
        if row.status == 'FAILED': row.active_file=None
        elif row.status == 'RETRY_WAIT':
            row.next_run_at = now_ms() + max((10, 30, 90)[min(max(row.stage_attempts-1, 0), 2)] * 1000, exc.delay_ms if isinstance(exc, RetryableStageError) else 0)
            row.last_dispatched_at = None
        _finish_attempt(session, claim, row.status, row.error_type)
        project_failure_in_session(session, row, row.status, row.error_message)
        session.add(row)
        session.commit()

@celery_app.task(bind=True, acks_late=True, reject_on_worker_lost=True, max_retries=0)
def execute_file_operation(self, task_id, expected_stage=None):
    claim = claim_task(task_id, expected_stage, str(getattr(self.request, 'hostname', None) or 'worker'))
    if claim is None:
        return dict(task_id=task_id, duplicate_delivery=True)
    stop=threading.Event()
    def beat():
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                if not heartbeat(claim): return
            except Exception:
                return
    thread=threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        from app.tasks.staged_pipeline import execute_stage
        execute_stage(claim)
        with Session(engine) as session:
            row=session.get(FileTaskReceipt, task_id)
            if row and row.status == 'RUNNING' and row.owner_token == claim.owner_token:
                raise RuntimeError('TASK_STAGE_DID_NOT_COMMIT')
            return dict(task_id=task_id, status=row.status if row else 'MISSING')
    except BaseException as exc:
        _fail_claim(claim, exc)
        raise
    finally:
        stop.set()
        thread.join(timeout=HEARTBEAT_SECONDS+1)

@celery_app.task(name='app.tasks.dispatch.sweep_index_builds', ignore_result=True)
def sweep_index_builds():
    from app.services.index_builds import sweep_builds
    from app.tasks.staged_pipeline import sweep_task_artifacts
    from app.services.file_retirement import sweep_storage_retirements
    sweep_builds()
    sweep_task_artifacts()
    sweep_storage_retirements()

"""Idempotent migration of receipt ledger; retain ambiguous legacy work."""
from sqlalchemy import inspect

COLUMNS = {
    'error_message':'TEXT', 'request_key':'TEXT', 'input_hash':'TEXT', 'target_generation':'INTEGER', 'stage':'TEXT',
    'attempt_no':'INTEGER NOT NULL DEFAULT 0', 'stage_attempts':'INTEGER NOT NULL DEFAULT 0',
    'next_run_at':'INTEGER NOT NULL DEFAULT 0', 'owner_token':'TEXT', 'heartbeat_at':'INTEGER',
    'lease_until':'INTEGER', 'last_dispatched_at':'INTEGER', 'checkpoint_ref':'TEXT',
}

def migrate_file_tasks(engine):
    from app.tasks.dispatch import FileTaskReceipt, FileTaskAttempt, FileTaskEffect, FileTaskEvent
    if engine.dialect.name != 'sqlite':
        raise RuntimeError('FILE_TASK_MIGRATION_REQUIRES_SQLITE')
    with engine.begin() as conn:
        conn.exec_driver_sql('BEGIN IMMEDIATE')
        FileTaskReceipt.__table__.create(conn, checkfirst=True)
        present = {r[1] for r in conn.exec_driver_sql('PRAGMA table_info(file_task_receipts)')}
        legacy = 'target_generation' not in present
        for name, kind in COLUMNS.items():
            if name not in present:
                conn.exec_driver_sql(f'ALTER TABLE file_task_receipts ADD COLUMN "{name}" {kind}')
        FileTaskAttempt.__table__.create(conn, checkfirst=True)
        FileTaskEffect.__table__.create(conn, checkfirst=True)
        FileTaskEvent.__table__.create(conn, checkfirst=True)
        conn.exec_driver_sql('CREATE UNIQUE INDEX IF NOT EXISTS ix_file_tasks_request_key ON file_task_receipts(file_id, operation, request_key)')
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_file_tasks_due ON file_task_receipts(status, next_run_at)')
        conn.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_file_tasks_lease ON file_task_receipts(status, lease_until)')
        if legacy:
            conn.exec_driver_sql("UPDATE file_task_receipts SET status='UNKNOWN', error_type='LegacyExecutionUnverified', error_message='历史任务缺少可验证的输入快照或执行租约，请核对后终止并重新提交' WHERE status IN ('RUNNING','QUEUED','PUBLISH_UNKNOWN','RETRY_WAIT')")

        # Older rows cannot safely execute without immutable source/model identities.
        # A partial migration or a restart of only one process must also leave
        # live, correctly leased workers untouched.
        conn.exec_driver_sql("""UPDATE file_task_receipts
            SET status='UNKNOWN', error_type='LegacyExecutionUnverified',
                error_message='历史任务缺少可验证的输入快照或执行租约，请核对后终止并重新提交'
            WHERE status IN ('RUNNING','QUEUED','PUBLISH_UNKNOWN','RETRY_WAIT') AND (
                target_generation IS NULL OR stage IS NULL OR NOT json_valid(payload) OR
                CASE WHEN json_valid(payload) THEN
                    json_type(payload, '$._source_sha256') IS NULL OR
                    json_type(payload, '$._source_path') IS NULL OR
                    json_type(payload, '$._model_snapshots') IS NULL
                    ELSE 1 END OR
                (status='RUNNING' AND (owner_token IS NULL OR lease_until IS NULL)))""")

        conn.exec_driver_sql("""UPDATE file_task_receipts
            SET error_type=COALESCE(error_type, 'LegacyExecutionUnverified'),
                error_message=COALESCE(error_message, '历史任务缺少可验证的输入快照，请核对后终止并重新提交')
            WHERE status='UNKNOWN' AND target_generation IS NULL""")

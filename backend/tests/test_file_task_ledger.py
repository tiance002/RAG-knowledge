"""Isolated ledger tests: local SQLite and broker/provider doubles only."""
from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlmodel import SQLModel, Session, create_engine
from app.models import FileDocument, KnowledgeBase
from app.tasks import dispatch as d
from app.tasks.file_task_recovery import scan_once

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'ledger.db'), connect_args={'timeout': 1})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(d, 'engine', engine)
    with Session(engine) as s:
        s.add(KnowledgeBase(id='kb', name='test', embedding_model='embed', vlm_model='vlm'))
        s.add(FileDocument(id='f', kb_id='kb', name='test', local_path='x', size=1, desired_generation=1))
        s.add(d.FileTaskReceipt(task_id='t', file_id='f', active_file='f', operation='parse',
             payload='{}', stage='parse', target_generation=1))
        s.commit()
    yield engine
    engine.dispose()

def test_duplicate_does_not_mutate_running_and_stale_cannot_finish(ledger):
    c = d.claim_task('t', 'parse')
    assert c and d.claim_task('t', 'parse') is None
    with Session(ledger) as s:
        assert s.get(d.FileTaskReceipt, 't').status == 'RUNNING'
        s.get(FileDocument, 'f').desired_generation = 2
        s.commit()
    assert not d.heartbeat(c)
    with Session(ledger) as s:
        assert not d.guard_claim_in_session(s,c)
        assert not d.complete_in_session(s,c, {'ok':True})
        s.rollback()


def test_heartbeat_never_extends_attempt_deadline(ledger, monkeypatch):
    clock = [1000]
    monkeypatch.setattr(d, 'now_ms', lambda: clock[0])
    monkeypatch.setattr(d, 'ATTEMPT_LIMIT_MS', 600)
    monkeypatch.setattr(d, 'LEASE_MS', 400)
    claim = d.claim_task('t', 'parse')
    clock[0] = 1300
    assert d.heartbeat(claim)
    with Session(ledger) as session:
        assert session.get(d.FileTaskReceipt, 't').lease_until == 1600
    clock[0] = 1600
    assert not d.heartbeat(claim)
    with Session(ledger) as session:
        assert not d.complete_in_session(session, claim, {})


def test_scan_batch_cannot_cancel_a_receipt_completed_during_broker_io(ledger, monkeypatch):
    with Session(ledger) as session:
        session.add(FileDocument(id='g', kb_id='kb', name='g', local_path='g', size=1, desired_generation=1))
        session.add(d.FileTaskReceipt(task_id='second', file_id='g', active_file='g',
            operation='parse', payload='{}', stage='parse', target_generation=1))
        session.commit()
    calls = []
    def notify(task_id, stage):
        calls.append(task_id)
        with Session(ledger) as session:
            second = session.get(d.FileTaskReceipt, 'second')
            second.status, second.active_file = 'COMPLETED', None
            session.get(FileDocument, 'g').desired_generation = 2
            session.commit()
    monkeypatch.setattr(d, 'notify_task', notify)
    assert scan_once()['sent'] == 1
    assert calls == ['t']
    with Session(ledger) as session:
        assert session.get(d.FileTaskReceipt, 'second').status == 'COMPLETED'

def test_expired_pending_effect_is_unknown_not_replayed(ledger, monkeypatch):
    monkeypatch.setattr(d, 'now_ms', lambda: 1000)
    c=d.claim_task('t','parse')
    assert d.begin_effect(c, 'effect', 'hash') == 'NEW'
    assert d.begin_effect(c, 'effect', 'hash') == 'PENDING'
    stats=scan_once(at=1000+d.LEASE_MS+1)
    assert stats['unknown'] == 1 and stats['sent'] == 0
    with Session(ledger) as s:
        row=s.get(d.FileTaskReceipt,'t')
        assert row.status == 'FAILED' and row.active_file is None
        assert row.error_message == d.UNCONFIRMED_FAILURE
        assert d.receipt_view(row)['resolution_available'] is True
        assert s.get(d.FileTaskAttempt, ('t',1)).outcome == 'UNKNOWN'
    d.resolve_unknown('f','t','retry','user explicitly accepts possible charge')
    c2=d.claim_task('t','parse')
    assert c2 and c2.owner_token != c.owner_token
    assert d.begin_effect(c2,'effect','hash') == 'NEW'
    assert d.begin_effect(c2,'effect','hash') == 'PENDING'


def test_public_receipt_hides_internal_unknown_and_pending_review_has_no_actions(ledger):
    with Session(ledger) as session:
        row = session.get(d.FileTaskReceipt, 't')
        row.status, row.error_message = 'UNKNOWN', 'provider detail'
        public = d.receipt_view(row)
        assert public['status'] == 'RETRY_WAIT'
        assert public['error_message'] is None
        assert public['resolution_available'] is False
        row.status = 'PUBLISH_UNKNOWN'
        assert d.receipt_view(row)['status'] == 'QUEUED'

def test_expiry_retry_then_old_token_rejected(ledger, monkeypatch):
    clock=[1000]
    monkeypatch.setattr(d,'now_ms',lambda: clock[0])
    c=d.claim_task('t','parse')
    clock[0]+=d.LEASE_MS+1
    assert scan_once(at=clock[0])['retried'] == 1
    assert d.claim_task('t','parse') is None
    clock[0]+=10001
    c2=d.claim_task('t','parse')
    assert c2 and c2.attempt_no == 2
    with Session(ledger) as s:
        assert not d.complete_in_session(s,c, {'stale':True})
        assert d.complete_in_session(s,c2, {'ok':True})
        s.commit()
    with Session(ledger) as s:
        row=s.get(d.FileTaskReceipt,'t')
        assert row.status == 'COMPLETED' and row.active_file is None

def test_stage_handoff_invalidates_old_message(ledger):
    c=d.claim_task('t','parse')
    assert d.handoff(c,'embed','checkpoint')
    assert d.claim_task('t','parse') is None
    c2=d.claim_task('t','embed')
    assert c2 and c2.checkpoint_ref == 'checkpoint' and c2.attempt_no == 2

def test_cancel_fences_claim_and_records_attempt(ledger):
    c=d.claim_task('t','parse')
    with Session(ledger) as s:
        assert d.cancel_in_session(s,'f','user_delete') == 1
        s.commit()
    assert not d.heartbeat(c)
    with Session(ledger) as s:
        assert s.get(d.FileTaskReceipt,'t').status == 'CANCELLED'
        assert s.get(d.FileTaskAttempt,('t',1)).outcome == 'CANCELLED'

def test_manual_resolution_records_audit_event(ledger):
    with Session(ledger) as s:
        row=s.get(d.FileTaskReceipt,'t'); row.status='UNKNOWN'; s.add(row); s.commit()
    d.resolve_unknown('f','t','terminate','reviewed by operator')
    with Session(ledger) as s:
        from sqlmodel import select
        events=s.exec(select(d.FileTaskEvent)).all()
        assert [(x.action,x.reason) for x in events] == [('terminate','reviewed by operator')]
        assert s.get(d.FileTaskReceipt,'t').active_file is None

def test_old_receipt_migration_does_not_replay_unverified_work(tmp_path):
    from app.core.file_task_migration import migrate_file_tasks
    e=create_engine('sqlite:///' + str(tmp_path/'legacy.db'))
    with e.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE file_task_receipts(task_id TEXT PRIMARY KEY, file_id TEXT, active_file TEXT UNIQUE, operation TEXT, status TEXT, payload TEXT, result TEXT, error_type TEXT, created_at DATETIME, updated_at DATETIME)")
        conn.exec_driver_sql("INSERT INTO file_task_receipts VALUES ('old','f','f','parse','RUNNING','{}',NULL,NULL,'2020-01-01','2020-01-01')")
    migrate_file_tasks(e); migrate_file_tasks(e)
    with Session(e) as s:
        row=s.get(d.FileTaskReceipt,'old')
        assert row.status == 'UNKNOWN' and row.target_generation is None and row.active_file == 'f'
    e.dispose()

def test_full_startup_legacy_upgrade_retains_work_and_scanner_is_safe(tmp_path, monkeypatch):
    from app.core import database
    import json
    e=create_engine('sqlite:///' + str(tmp_path/'prior.db'))
    historical = {
        'queued': ('QUEUED', {'chunk_mode':'custom','chunk_size':500,'_settings_revision':1}),
        'ambiguous': ('PUBLISH_UNKNOWN', {'chunks_data':[{'id':'c','content':'old'}], 'vector_model':'m'}),
        'running': ('RUNNING', {'auto_vectorize':True,'chunk_size':500}),
        'unknown': ('UNKNOWN', {'vlm_model_id':'m'}),
        'completed': ('COMPLETED', {'chunks_data':[]}),
    }
    with e.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE file_task_receipts(task_id TEXT PRIMARY KEY, file_id TEXT, active_file TEXT UNIQUE, operation TEXT, status TEXT, payload TEXT, result TEXT, error_type TEXT, created_at DATETIME, updated_at DATETIME)')
        for task_id,(status,payload) in historical.items():
            conn.exec_driver_sql('INSERT INTO file_task_receipts VALUES (?,?,?,?,?,?,?,?,?,?)',
                (task_id,task_id,None if status == 'COMPLETED' else task_id,'parse',status,json.dumps(payload),
                 '{"ok":true}' if status == 'COMPLETED' else None,None,'2020-01-01','2020-01-01'))
    monkeypatch.setattr(database,'engine',e)
    monkeypatch.setattr(d,'engine',e)
    database.init_db()
    database.init_db()
    with Session(e) as s:
        for task_id in ('queued','ambiguous','running','unknown'):
            row=s.get(d.FileTaskReceipt,task_id)
            assert row.status == 'UNKNOWN' and row.active_file == task_id
            assert row.error_message and row.target_generation is None
            assert json.loads(row.payload) == historical[task_id][1]
        assert s.get(d.FileTaskReceipt,'completed').status == 'COMPLETED'
    sends=[]
    monkeypatch.setattr(d,'notify_task',lambda *args:sends.append(args))
    assert scan_once()['sent'] == 0 and not sends
    with pytest.raises(d.FileTaskConflict,match='LEGACY_TASK_CANNOT_REPLAY'):
        d.resolve_unknown('queued','queued','retry','explicitly authorized')
    result=d.resolve_unknown('queued','queued','terminate','verified old task has no usable snapshot')
    assert result['status'] == 'CANCELLED'
    e.dispose()

def test_migration_preserves_live_lease_then_old_worker_cannot_complete_after_recovery(ledger,monkeypatch):
    from app.core.file_task_migration import migrate_file_tasks
    import json
    clock=[1000]
    monkeypatch.setattr(d,'now_ms',lambda:clock[0])
    with Session(ledger) as s:
        r=s.get(d.FileTaskReceipt,'t')
        r.payload=json.dumps({'_source_path':'source','_source_sha256':'hash','_model_snapshots':{}})
        s.add(r);s.commit()
    old=d.claim_task('t','parse')
    migrate_file_tasks(ledger)
    with Session(ledger) as s:
        row=s.get(d.FileTaskReceipt,'t')
        assert row.status == 'RUNNING' and row.owner_token == old.owner_token
    assert scan_once(at=clock[0])['expired'] == 0
    clock[0]+=d.LEASE_MS+1
    assert scan_once(at=clock[0])['retried'] == 1
    clock[0]+=10001
    new=d.claim_task('t','parse')
    assert new and new.owner_token != old.owner_token
    with Session(ledger) as s:
        assert d.cancel_in_session(s,'f','delete') == 1
        doc=s.get(FileDocument,'f');doc.desired_generation+=1;doc.deleted_at=datetime.utcnow();s.add(doc);s.commit()
    with Session(ledger) as s:
        assert not d.complete_in_session(s,old,{'stale':True})
        assert not d.complete_in_session(s,new,{'stale':True})
        s.rollback()

def test_worker_role_requires_isolated_queue_and_remote_chroma(monkeypatch):
    from types import SimpleNamespace
    import importlib
    module = importlib.import_module('app.tasks.celery_app')
    from app.services import runtime_settings
    # celery_app import may resolve to module in the package; isolate each worker configuration.
    monkeypatch.setattr(runtime_settings, 'get_runtime_settings', lambda: SimpleNamespace(max_concurrency=2, revision=1, degraded=False))
    monkeypatch.setattr(module.settings, 'CHROMA_HOST', 'localhost')
    monkeypatch.setenv('FILE_TASK_WORKER_ROLE','index')
    sender=SimpleNamespace(autoscale=None, concurrency=7,
        app=SimpleNamespace(amqp=SimpleNamespace(queues=SimpleNamespace(consume_from={'document.index': object()})),
                            conf=SimpleNamespace(worker_concurrency=7)))
    module.apply_runtime_worker_settings(sender)
    assert sender.concurrency == sender.app.conf.worker_concurrency == 1
    sender.app.amqp.queues.consume_from={'document.parse':object()}
    with pytest.raises(SystemExit): module.apply_runtime_worker_settings(sender)
    sender.app.amqp.queues.consume_from={'document.index':object()}
    monkeypatch.setattr(module.settings,'CHROMA_HOST',None)
    with pytest.raises(SystemExit): module.apply_runtime_worker_settings(sender)

def test_scanner_retires_missing_document_before_notifying_next(ledger,monkeypatch):
    with Session(ledger) as s:
        s.add(d.FileTaskReceipt(task_id='a',file_id='missing',active_file='missing',operation='parse',
            payload='{}',stage='parse',target_generation=1));s.commit()
    sent=[];monkeypatch.setattr(d,'notify_task',lambda task_id,stage:sent.append(task_id))
    # Both invalid and valid work are eventually visited even in tiny batches.
    scan_once(limit=1);scan_once(limit=1);scan_once(limit=1)
    with Session(ledger) as s: assert s.get(d.FileTaskReceipt,'a').status == 'CANCELLED'
    assert sent == ['t']

"""Dispatch contract tests using SQLite and explicit broker/pipeline doubles."""
from types import SimpleNamespace
import pytest
from sqlmodel import SQLModel, Session, create_engine
from app.models import FileDocument, KnowledgeBase
from app.services import runtime_settings
from app.tasks import dispatch, staged_pipeline

@pytest.fixture
def receipts(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'receipts.db'))
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(dispatch, 'engine', engine)
    monkeypatch.setattr(runtime_settings, 'get_runtime_settings', lambda session=None: SimpleNamespace(
        revision=1, chunk_size=500, chunk_overlap=50, max_concurrency=2,
        default_vlm_model=None, degraded=False))
    monkeypatch.setattr(staged_pipeline, 'freeze_payload', lambda session, doc, op, payload: payload)
    sends=[]
    monkeypatch.setattr(dispatch.execute_file_operation, 'apply_async', lambda **kwargs: sends.append(kwargs))
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='test', embedding_model='embedding', vlm_model=''))
        for n in range(1,5):
            session.add(FileDocument(id='file-'+str(n), kb_id='kb', name='test', local_path='unused', size=1))
        session.commit()
    yield engine, sends
    engine.dispose()

def test_idempotent_acceptance_and_generation_conflicts(receipts):
    engine, sends=receipts
    one=dispatch.dispatch_file_task('file-1','index',{'chunks_data':[1]},'key',expected_generation=0,
                                   request_input={'action':'vectorize'})
    two=dispatch.dispatch_file_task('file-1','index',{'chunks_data':[2]},'key',expected_generation=0,
                                   request_input={'action':'vectorize'})
    assert one == two and sends == []
    from app.tasks.file_task_recovery import scan_once
    assert scan_once()['sent'] == 1
    assert sends[0]['args'] == [one['task_id'], 'embed']
    assert dispatch.lookup_file_request('file-1','index','key',{'action':'vectorize'}) == one
    with pytest.raises(dispatch.FileTaskConflict):
        dispatch.lookup_file_request('file-1','index','key',{'action':'other'})
    with pytest.raises(dispatch.FileTaskBusy):
        dispatch.dispatch_file_task('file-1','parse',{})
    with Session(engine) as session:
        assert session.get(FileDocument,'file-1').desired_generation == 1

def test_broker_failure_is_durable_and_recoverable(receipts,monkeypatch):
    def fail(**kwargs): raise TimeoutError('broker offline')
    monkeypatch.setattr(dispatch.execute_file_operation,'apply_async',fail)
    job=dispatch.dispatch_file_task('file-2','index',{})
    with Session(receipts[0]) as session:
        row=session.get(dispatch.FileTaskReceipt,job['task_id'])
        assert row.status == 'QUEUED' and row.active_file == 'file-2'
    from app.tasks.file_task_recovery import scan_once
    first_scan = scan_once()
    assert first_scan['send_failed'] == 1
    monkeypatch.setattr(dispatch.execute_file_operation,'apply_async',lambda **kwargs: receipts[1].append(kwargs))
    from app.tasks.file_task_recovery import RENOTIFY_MS
    assert scan_once(at=dispatch.now_ms()+RENOTIFY_MS+1)['sent'] == 1
    assert receipts[1][0]['args'] == [job['task_id'], 'embed']

def test_duplicate_delivery_does_not_disturb_live_owner(receipts):
    job=dispatch.dispatch_file_task('file-3','parse',{})
    claim=dispatch.claim_task(job['task_id'],'parse')
    result=dispatch.execute_file_operation.run(job['task_id'],'parse')
    assert claim and result['duplicate_delivery']
    with Session(receipts[0]) as session:
        assert session.get(dispatch.FileTaskReceipt,job['task_id']).status == 'RUNNING'

def test_missing_file_cannot_be_accepted(receipts):
    with pytest.raises(ValueError, match='FILE_NOT_FOUND'):
        dispatch.dispatch_file_task('missing','index',{})

def test_legacy_published_file_stays_visible_on_acceptance_and_failure(receipts):
    from app.models import FileStatus
    engine,_=receipts
    with Session(engine) as s:
        doc=s.get(FileDocument,'file-4');doc.status=FileStatus.PARSED;doc.active_build_id=None;s.add(doc);s.commit()
    job=dispatch.dispatch_file_task('file-4','index',{})
    with Session(engine) as s:
        doc=s.get(FileDocument,'file-4');assert doc.status == FileStatus.PARSED
    claim=dispatch.claim_task(job['task_id'],'embed')
    dispatch._fail_claim(claim, ValueError('permanent'))
    with Session(engine) as s:
        doc=s.get(FileDocument,'file-4');assert doc.status == FileStatus.PARSED and doc.error_message

def test_real_http_returns_without_unreachable_broker_then_scanner_recovers(tmp_path, monkeypatch):
    """Real FastAPI, actual refused TCP broker, SQLite; no active Redis is touched."""
    import socket
    import time
    from celery import Celery
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.core.database import get_session
    from app.api.files import router
    from app.tasks.file_task_recovery import scan_once, RENOTIFY_MS
    engine = create_engine('sqlite:///' + str(tmp_path / 'http-receipts.db'), connect_args={'check_same_thread':False})
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(dispatch,'engine',engine)
    monkeypatch.setattr(runtime_settings,'get_runtime_settings',lambda session=None: SimpleNamespace(
        revision=1, chunk_size=500, chunk_overlap=50, default_vlm_model=None))
    monkeypatch.setattr(staged_pipeline,'freeze_payload',lambda session, doc, op, payload: payload)
    with Session(engine) as s:
        s.add(KnowledgeBase(id='k',name='k',embedding_model='m',vlm_model=''))
        s.add(FileDocument(id='f',kb_id='k',name='f',local_path='x',size=1));s.commit()
    listener=socket.socket();listener.bind(('127.0.0.1',0));port=listener.getsockname()[1];listener.close()
    bad_url=f'redis://127.0.0.1:{port}/0'
    isolated_celery=Celery('test-unreachable',broker=bad_url,backend=bad_url)
    monkeypatch.setattr(dispatch,'celery_app',isolated_celery)
    app=FastAPI();app.include_router(router)
    def db():
        with Session(engine) as session: yield session
    app.dependency_overrides[get_session]=db
    with TestClient(app) as client:
        start=time.perf_counter()
        response=client.post('/files/f/parse',json={'auto_vectorize':True},headers={'Idempotency-Key':'stable-key'})
        elapsed=time.perf_counter()-start
        assert response.status_code==200, response.text
        task_id=response.json()['data']['task_id']
        assert elapsed < 3.0
        repeat=client.post('/files/f/parse',json={'auto_vectorize':True},headers={'Idempotency-Key':'stable-key'})
        assert repeat.status_code==200 and repeat.json()['data']['task_id']==task_id
        started=time.perf_counter();result=scan_once();scan_elapsed=time.perf_counter()-started
        assert result['send_failed']==1 and result['sent']==0 and scan_elapsed < 5.0
        sends=[];monkeypatch.setattr(dispatch,'notify_task',lambda task,stage:sends.append((task,stage)))
        assert scan_once(at=dispatch.now_ms()+RENOTIFY_MS+1)['sent']==1
        assert sends==[(task_id,'parse')]
        print(f'HTTP_ACCEPT_SECONDS={elapsed:.3f} REFUSED_BROKER_SCAN_SECONDS={scan_elapsed:.3f} TASK_ID_STABLE=true')
    isolated_celery.close();engine.dispose()

def test_scan_stops_after_one_broker_failure(receipts,monkeypatch):
    from app.tasks.file_task_recovery import scan_once, RENOTIFY_MS
    a=dispatch.dispatch_file_task('file-1','parse',{})
    b=dispatch.dispatch_file_task('file-2','parse',{})
    calls=[]
    def fail(task,stage):
        calls.append(task);raise OSError('unreachable')
    monkeypatch.setattr(dispatch,'notify_task',fail)
    assert scan_once()['send_failed']==1 and len(calls)==1
    with Session(receipts[0]) as s:
        rows=[s.get(dispatch.FileTaskReceipt,x['task_id']) for x in (a,b)]
        assert sum(r.last_dispatched_at is not None for r in rows)==1

def test_concurrent_same_request_key_creates_one_durable_receipt(receipts):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    engine,_=receipts
    start=Barrier(2)
    def submit():
        start.wait()
        return dispatch.dispatch_file_task('file-1','parse',{},'same-key',request_input={'operation':'parse'})
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs=list(pool.map(lambda _: submit(),range(2)))
    assert jobs[0]['task_id']==jobs[1]['task_id']
    with Session(engine) as session:
        from sqlmodel import select
        assert len(session.exec(select(dispatch.FileTaskReceipt)).all())==1

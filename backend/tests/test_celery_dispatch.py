"""Offline coordinator tests; provider and broker sends are replaced."""
import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from app.tasks import dispatch
from app.services import runtime_settings
from types import SimpleNamespace


@pytest.fixture
def receipts(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'receipts.db'))
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(dispatch, 'engine', engine)
    monkeypatch.setattr(runtime_settings, 'get_runtime_settings', lambda session=None: SimpleNamespace(
        revision=1, chunk_size=500, chunk_overlap=50, max_concurrency=1,
        max_chat_history_rounds=20, default_llm_model=None, default_embedding_model=None,
        default_vlm_model=None, degraded=False))
    sends = []
    monkeypatch.setattr(dispatch.execute_file_operation, 'apply_async',
                        lambda **kw: sends.append(kw))
    yield engine, sends
    engine.dispose()


def test_one_file_has_one_active_job_and_persistent_identity(receipts):
    engine, sends = receipts
    first = dispatch.dispatch_file_task('file-1', 'index', {'chunks_data': []})
    with pytest.raises(dispatch.FileTaskBusy) as error:
        dispatch.dispatch_file_task('file-1', 'parse', {})
    assert error.value.task_id == first['task_id']
    assert len(sends) == 1
    with Session(engine) as session:
        row = session.get(dispatch.FileTaskReceipt, first['task_id'])
        assert row.status == 'QUEUED'
        assert row.active_file == 'file-1'
    assert sends[0]['task_id'] == first['task_id']


def test_publish_unknown_is_preserved_not_reissued(receipts, monkeypatch):
    engine, _ = receipts
    def fail(**kwargs):
        raise TimeoutError('publish outcome unknown')
    monkeypatch.setattr(dispatch.execute_file_operation, 'apply_async', fail)
    with pytest.raises(dispatch.DispatchUnconfirmed) as error:
        dispatch.dispatch_file_task('file-2', 'index', {'chunks_data': []})
    with Session(engine) as session:
        row = session.get(dispatch.FileTaskReceipt, error.value.task_id)
        assert row.status == 'PUBLISH_UNKNOWN'
    with pytest.raises(dispatch.FileTaskBusy):
        dispatch.dispatch_file_task('file-2', 'index', {'chunks_data': []})


def test_completed_delivery_does_not_execute_twice(receipts, monkeypatch):
    from app.tasks import parse_tasks
    calls = []
    monkeypatch.setattr(parse_tasks, 'process_submitted_chunks',
                        lambda file_id, **kw: calls.append(file_id) or {'status': 'ready'})
    job = dispatch.dispatch_file_task('file-3', 'index', {'chunks_data': []})
    first = dispatch.execute_file_operation.run(job['task_id'])
    second = dispatch.execute_file_operation.run(job['task_id'])
    assert first['status'] == 'COMPLETED'
    assert second['duplicate_delivery'] is True
    assert calls == ['file-3']


def test_interrupted_delivery_is_unknown_without_provider_replay(receipts, monkeypatch):
    from app.tasks import parse_tasks
    calls = []
    monkeypatch.setattr(parse_tasks, 'process_submitted_chunks', lambda *a, **k: calls.append(1))
    job = dispatch.dispatch_file_task('file-4', 'index', {'chunks_data': []})
    with Session(receipts[0]) as session:
        row = session.get(dispatch.FileTaskReceipt, job['task_id'])
        row.status = 'RUNNING'; session.add(row); session.commit()
    result = dispatch.execute_file_operation.run(job['task_id'])
    assert result['status'] == 'UNKNOWN'
    assert calls == []
    with pytest.raises(dispatch.FileTaskBusy):
        dispatch.dispatch_file_task('file-4', 'index', {'chunks_data': []})


def test_unknown_provider_failure_is_not_retried(receipts, monkeypatch):
    from app.tasks import parse_tasks
    from app.services import llm
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise TimeoutError('provider response unconfirmed')
    monkeypatch.setattr(parse_tasks, 'process_submitted_chunks', fail)
    monkeypatch.setattr(llm, 'is_unknown_provider_error', lambda exc: isinstance(exc, TimeoutError))
    job = dispatch.dispatch_file_task('file-5', 'index', {'chunks_data': []})
    with pytest.raises(TimeoutError):
        dispatch.execute_file_operation.run(job['task_id'])
    result = dispatch.execute_file_operation.run(job['task_id'])
    assert result['status'] == 'UNKNOWN'
    assert calls == [1]

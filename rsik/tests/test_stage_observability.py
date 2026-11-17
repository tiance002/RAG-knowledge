import pytest
from rsik.state import StageJournal
from rsik.observability import Observer
from rsik.analytics.reader import read_event_log
from rsik.analytics.projections import project_sources
from rsik.analytics.metrics import compute_metrics


def test_stage_recovery_does_not_repeat_action(tmp_path):
    observer = Observer(tmp_path/'observations.sqlite', {'evidence_mode':'SYNTHETIC_CONTROL'})
    journal = StageJournal(tmp_path/'stages',observer=observer)
    effects = {}
    def action(key): effects[key]={'ok':True}; return effects[key]
    def reconcile(key):
        return {'status':'MATCHED','output':effects[key]} if key in effects else {'status':'ABSENT','safe_retry':True}
    with pytest.raises(RuntimeError,match='INJECTED_CRASH'):
        journal.execute('prepare',{},action,reconcile,crash_at='after_action')
    assert journal.execute('prepare',{},action,reconcile)[0]=={'ok':True}
    assert journal.execute('prepare',{},action,reconcile)[0]=={'ok':True}
    events=observer.log.read()
    assert sum(e['event_type']=='EXECUTION_STARTED' for e in events)==1
    assert sum(e['event_type']=='STAGE_STARTED' for e in events)==3
    assert sum(e['event_type']=='RECOVERY_COMPLETED' for e in events)==2
    report=compute_metrics(project_sources([read_event_log(observer.path)]))
    metrics={m['metric_id']:m for m in report['groups'][0]['metrics']}
    assert metrics['R2']['denominator']==2
    assert metrics['R2']['numerator']==2


def test_stage_failure_and_cancel_are_not_quality_rejections(tmp_path):
    observer=Observer(tmp_path/'observations.sqlite',{'evidence_mode':'SYNTHETIC_CONTROL'})
    journal=StageJournal(tmp_path/'s',observer=observer)
    def action(_):raise OSError('private body must not be public')
    with pytest.raises(OSError):journal.execute('a',{},action,lambda _: {'status':'ABSENT','safe_retry':True})
    assert observer.log.read()[-1]['payload']['failure_category']=='INFRASTRUCTURE'
    assert 'private body' not in str(observer.log.read())
    def cancel(_):raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):journal.execute('b',{},cancel,lambda _: {'status':'ABSENT','safe_retry':True})
    assert observer.log.read()[-1]['event_type']=='STAGE_CANCELLED'


def test_observation_storage_failure_is_infrastructure(tmp_path, monkeypatch):
    from rsik.errors import StageFailure
    observer=Observer(tmp_path/'observations.sqlite', {'experiment_id':'e','epoch':'1','evaluator_version':'v1'})
    observer.emit('STAGE_STARTED',{'stage_id':'s'})
    assert observer.log.read()[0]['payload']['evaluator_version']=='v1'
    def fail(*args, **kwargs): raise OSError('private disk error')
    monkeypatch.setattr(observer.log,'append',fail)
    with pytest.raises(StageFailure) as error:
        observer.emit('STAGE_STARTED',{'stage_id':'s'})
    assert error.value.category=='STORAGE_ERROR'
    assert error.value.global_stop is True



def test_stage_observation_read_storage_failure_prevents_action(tmp_path, monkeypatch):
    from rsik.errors import StageFailure
    import sqlite3
    observer = Observer(tmp_path / 'observations.sqlite')
    journal = StageJournal(tmp_path / 'stages', observer=observer)
    def fail():
        raise sqlite3.OperationalError('private read failure')
    monkeypatch.setattr(observer.log, 'read', fail)
    with pytest.raises(StageFailure) as caught:
        journal.execute('a', {}, lambda _: pytest.fail('must not execute'),
                        lambda _: {'status': 'ABSENT', 'safe_retry': True})
    assert caught.value.category == 'STORAGE_ERROR' and caught.value.global_stop
    assert not (tmp_path / 'stages').exists()

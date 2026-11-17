"""Synthetic runtime controls; no model calls or production data."""
import json

import pytest

from rsik.lineage import LineagePolicy, ParentPool
from rsik.observability import Observer
from rsik.analytics.reader import read_event_log
from rsik.analytics.projections import project_sources
from rsik.analytics.metrics import compute_metrics


def setup(tmp_path, kind=None):
    policy = LineagePolicy(tmp_path / 'policy')
    pool = ParentPool(tmp_path / 'pool', evidence_mode='SYNTHETIC_CONTROL')
    observer = Observer(tmp_path / 'observations.sqlite',
        {'experiment_id': 'selection-control', 'evidence_mode': 'SYNTHETIC_CONTROL'})
    if kind:
        receipt = tmp_path / 'receipt.json'
        receipt.write_text(json.dumps({'verdict': 'VERIFIED' if kind == 'VERIFIED' else 'VALIDATED'}))
        pool.admit('parent-a', 'lineage', receipt, kind=kind, chars=100, replay_seconds=1)
    return policy, pool, observer


def report(observer):
    records = project_sources([read_event_log(observer.path)])
    metrics = {m['metric_id']: m for m in compute_metrics(records)['groups'][0]['metrics']}
    selections = [r for r in records if r['projection_type'] == 'Selection']
    return records, selections, metrics


def test_normal_selection(tmp_path):
    policy, pool, observer = setup(tmp_path, 'VERIFIED')
    assert pool.select(policy, round_id='r', observer=observer)['candidate_id'] == 'parent-a'
    records, selections, metrics = report(observer)
    assert len(selections) == 1 and selections[0]['payload']['completed'] is True
    assert len([r for r in records if r['projection_type'] == 'Parent' and r['payload'].get('selected') is True]) == 1
    assert metrics['S3']['value'] == metrics['S6']['value'] == 1


def test_empty_pool_is_completed_zero_not_missing(tmp_path):
    policy, pool, observer = setup(tmp_path)
    assert pool.select(policy, observer=observer) is None
    records, selections, metrics = report(observer)
    assert selections[0]['payload']['returned_count'] == 0
    assert selections[0]['payload']['completed'] is True
    assert not [r for r in records if r['payload'].get('selected') is True]
    assert metrics['S3']['value'] == metrics['S6']['value'] == 0
    assert metrics['S3']['denominator'] == metrics['S6']['denominator'] == 1


def test_all_candidates_excluded_is_completed_empty(tmp_path):
    policy, pool, observer = setup(tmp_path, 'EXPLORATION')
    assert pool.select(policy, observer=observer) is None
    _, selections, metrics = report(observer)
    assert selections[0]['payload']['completed'] is True
    assert metrics['S3']['numerator'] == metrics['S6']['numerator'] == 0
    assert any(e['event_type'] == 'PARENT_REJECTED' for e in observer.log.read())


@pytest.mark.parametrize('interrupt_kind', ['PARENT_RANKED', 'PARENT_SELECTED'])
def test_interrupted_selection_remains_unknown(tmp_path, interrupt_kind):
    policy, pool, observer = setup(tmp_path, 'VERIFIED')
    original = observer.emit
    def interrupt(kind, payload, key=None):
        result = original(kind, payload, key)
        if kind == interrupt_kind:
            raise RuntimeError('CONTROL_INTERRUPT')
        return result
    observer.emit = interrupt
    with pytest.raises(RuntimeError, match='CONTROL_INTERRUPT'):
        pool.select(policy, observer=observer)
    _, selections, metrics = report(observer)
    assert selections[0]['payload']['completed'] == 'UNKNOWN'
    assert metrics['S3']['status'] == metrics['S6']['status'] == 'UNKNOWN'
    assert metrics['S3']['unknown_operations'] == 1


def test_retry_after_interruption_has_unique_attempt(tmp_path):
    policy, pool, observer = setup(tmp_path, 'VERIFIED')
    original = observer.emit
    def interrupt(kind, payload, key=None):
        result = original(kind, payload, key)
        if kind == 'PARENT_SELECTED':
            raise RuntimeError('CONTROL_INTERRUPT')
        return result
    observer.emit = interrupt
    with pytest.raises(RuntimeError):
        pool.select(policy, round_id='r', seed=1, observer=observer)
    observer.emit = original
    pool.select(policy, round_id='r', seed=1, observer=observer)
    _, selections, metrics = report(observer)
    assert len({r['payload']['selection_id'] for r in selections}) == 2
    assert {r['payload']['attempt'] for r in selections} == {1, 2}
    assert metrics['S3']['completed_operations'] == 1
    assert metrics['S3']['unknown_operations'] == 1
    assert metrics['S3']['value'] == 1


def test_duplicate_observations_do_not_count_again(tmp_path):
    policy, pool, observer = setup(tmp_path, 'VERIFIED')
    pool.select(policy, observer=observer)
    for event in observer.log.read():
        observer.emit(event['event_type'], event['payload'], key='duplicate-' + event['event_id'])
    records, selections, metrics = report(observer)
    source = read_event_log(observer.path)
    assert records == project_sources([source, source])
    assert len(selections) == 1
    assert metrics['S3']['denominator'] == metrics['S6']['denominator'] == 1


def test_same_selection_id_conflict_is_unknown(tmp_path):
    policy, pool, observer = setup(tmp_path, 'VERIFIED')
    pool.select(policy, observer=observer)
    end = next(e for e in observer.log.read() if e['event_type'] == 'PARENT_SELECTION_COMPLETED')
    observer.emit('PARENT_SELECTION_COMPLETED', {**end['payload'], 'returned_count': 0}, key='conflict')
    _, selections, metrics = report(observer)
    assert selections[0]['payload']['valid'] == 'UNKNOWN'
    assert metrics['S3']['status'] == metrics['S6']['status'] == 'UNKNOWN'


def test_empty_operations_are_in_both_metric_denominators(tmp_path):
    policy, pool, observer = setup(tmp_path, 'EXPLORATION')
    pool.select(policy, exploration=True, observer=observer)
    pool.select(policy, observer=observer)
    _, selections, metrics = report(observer)
    assert len(selections) == 2
    assert metrics['S3']['numerator'] == 1 and metrics['S3']['denominator'] == 2
    assert metrics['S3']['value'] == .5
    assert metrics['S6']['numerator'] == 1 and metrics['S6']['denominator'] == 4
    assert metrics['S6']['value'] == .25

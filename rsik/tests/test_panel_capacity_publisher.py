import json

import pytest

from rsik import panel_capacity_publisher as publisher
from rsik import panel_dispatch


@pytest.fixture
def inputs():
    return dict(panel_sha256='a' * 64, arm_limits={'base': 3, 'prompt': 3, 'params': 3, 'code': 3},
                observed_unix_ns=123456789, disk_total_bytes=10000, disk_free_bytes=6000,
                projected_write_bytes=1000, memory_available_bytes=1400,
                memory_reserve_bytes=200, memory_per_active_qa_bytes=100,
                database_slots=12, provider_slots=12, budget_slots=12, judge_queue_waiting=0)


def allocations(observation):
    return [arm['database_slots'] for arm in observation['arms'].values()]


def test_actual_low_disk_and_projected_write_fail_closed(inputs):
    inputs.update(disk_total_bytes=10000, disk_free_bytes=345, projected_write_bytes=0)
    observation = publisher.build_observation(**inputs)
    assert allocations(observation) == [0, 0, 0, 0]
    assert observation['diagnostics']['limiting_gates'] == ['disk']
    inputs.update(disk_free_bytes=2100, projected_write_bytes=101)
    assert allocations(publisher.build_observation(**inputs)) == [0, 0, 0, 0]
    inputs['projected_write_bytes'] = 100
    assert allocations(publisher.build_observation(**inputs)) == [3, 3, 3, 3]


@pytest.mark.parametrize('slots,expected', [(0, [0, 0, 0, 0]), (1, [1, 0, 0, 0]),
                                           (2, [1, 1, 0, 0]), (3, [1, 1, 1, 0]),
                                           (5, [2, 1, 1, 1]), (12, [3, 3, 3, 3])])
def test_round_robin_preserves_arm_order_and_shared_total(inputs, slots, expected):
    inputs['database_slots'] = slots
    observation = publisher.build_observation(**inputs)
    assert list(observation['arms']) == list(inputs['arm_limits'])
    assert allocations(observation) == expected
    assert sum(allocations(observation)) == slots
    for resource in ('database_slots', 'memory_slots', 'provider_slots', 'budget_slots'):
        assert sum(arm[resource] for arm in observation['arms'].values()) == slots


def test_unequal_limits_do_not_strand_slots(inputs):
    inputs.update(arm_limits={'code': 1, 'base': 3, 'prompt': 2}, budget_slots=5)
    assert allocations(publisher.build_observation(**inputs)) == [1, 2, 2]


@pytest.mark.parametrize('field', ['disk_total_bytes', 'disk_free_bytes', 'projected_write_bytes',
                                  'memory_available_bytes', 'memory_reserve_bytes',
                                  'memory_per_active_qa_bytes', 'database_slots',
                                  'provider_slots', 'budget_slots', 'judge_queue_waiting'])
def test_each_unknown_observation_pauses_all_arms(inputs, field):
    inputs[field] = None
    observation = publisher.build_observation(**inputs)
    assert allocations(observation) == [0, 0, 0, 0]
    assert observation['diagnostics']['unknown_observations'] == [field]


@pytest.mark.parametrize('changes,gate,total', [
    ({'memory_available_bytes': 599}, 'memory', 3),
    ({'memory_available_bytes': 199}, 'memory', 0),
    ({'provider_slots': 2}, 'provider', 2),
    ({'budget_slots': 4}, 'budget', 4),
    ({'judge_queue_waiting': 117}, 'judge_queue', 3),
    ({'judge_queue_waiting': 121}, 'judge_queue', 0),
])
def test_independent_gate_diagnostics(inputs, changes, gate, total):
    inputs.update(changes)
    observation = publisher.build_observation(**inputs)
    assert sum(allocations(observation)) == total
    assert gate in observation['diagnostics']['limiting_gates']
    assert observation['diagnostics']['gate_slots'][gate] == total
    assert observation['diagnostics']['gate_slots']['database'] == 12


@pytest.mark.parametrize('changes', [
    {'panel_sha256': 'invalid'}, {'panel_sha256': None}, {'observed_unix_ns': True},
    {'observed_unix_ns': -1}, {'arm_limits': {}}, {'arm_limits': {'a': 4}},
    {'arm_limits': {'a': True}}, {'arm_limits': {'': 1}}, {'arm_limits': {'a': 0}},
    {'arm_limits': {str(i): 1 for i in range(5)}}, {'arm_limits': [('a', 1)]},
    {'database_slots': True}, {'provider_slots': -1}, {'budget_slots': 1.5},
    {'judge_queue_waiting': '0'}, {'disk_total_bytes': 0},
    {'disk_free_bytes': 10001}, {'memory_per_active_qa_bytes': 0},
    {'projected_write_bytes': -1},
])
def test_invalid_evidence_is_rejected(inputs, changes):
    inputs.update(changes)
    with pytest.raises(ValueError):
        publisher.build_observation(**inputs)


def test_observation_round_trips_through_existing_reader(inputs, monkeypatch, tmp_path):
    inputs['provider_slots'] = 5
    observation = publisher.build_observation(**inputs)
    target = tmp_path / 'capacity.json'
    publisher.publish_observation(target, observation)
    monkeypatch.setattr(panel_dispatch.time, 'time_ns', lambda: inputs['observed_unix_ns'])
    monkeypatch.setattr(panel_dispatch, 'disk_usage', lambda _: (100, 50, 50))
    assert json.loads(target.read_text(encoding='utf-8')) == observation
    assert [panel_dispatch.read_arm_capacity(target, inputs['panel_sha256'], name, tmp_path, limit)
            for name, limit in inputs['arm_limits'].items()] == [2, 1, 1, 1]
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_replace_follows_fsync_in_same_directory(inputs, monkeypatch, tmp_path):
    target = tmp_path / 'capacity.json'
    target.write_text('{"old": true}', encoding='utf-8')
    observation = publisher.build_observation(**inputs)
    real_fsync, real_replace = publisher.os.fsync, publisher.os.replace
    events = []

    def fsync(descriptor):
        events.append('fsync')
        real_fsync(descriptor)

    def replace(source, destination):
        assert source.parent == target.parent
        assert destination == target
        assert json.loads(target.read_text()) == {'old': True}
        assert json.loads(source.read_text(encoding='utf-8')) == observation
        assert events == ['fsync']
        events.append('replace')
        real_replace(source, destination)

    monkeypatch.setattr(publisher.os, 'fsync', fsync)
    monkeypatch.setattr(publisher.os, 'replace', replace)
    publisher.publish_observation(target, observation)
    assert events == ['fsync', 'replace']
    assert json.loads(target.read_text()) == observation
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('failure', ['fsync', 'replace'])
def test_failed_publication_preserves_old_target_and_cleans_temporary(inputs, monkeypatch, tmp_path, failure):
    target = tmp_path / 'capacity.json'
    target.write_bytes(b'{"old":true}')

    def fail(*args):
        raise OSError('injected failure')

    monkeypatch.setattr(publisher.os, failure, fail)
    with pytest.raises(OSError, match='injected failure'):
        publisher.publish_observation(target, publisher.build_observation(**inputs))
    assert target.read_bytes() == b'{"old":true}'
    assert list(tmp_path.iterdir()) == [target]

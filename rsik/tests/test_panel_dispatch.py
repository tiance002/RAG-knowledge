import asyncio
from dataclasses import asdict
import json
import time

import pytest

from rsik import panel_dispatch


@pytest.fixture
def healthy_disk(monkeypatch):
    monkeypatch.setattr(panel_dispatch, 'disk_usage', lambda _: (100, 50, 50))


def test_arm_capacity_pauses_on_disk(monkeypatch, tmp_path):
    capacity = panel_dispatch.PanelCapacity(4, 4, 4, 4, 0)
    monkeypatch.setattr(panel_dispatch, 'disk_usage', lambda _: (100, 81, 19))
    assert capacity.limit(tmp_path, 4) == 0


@pytest.mark.parametrize('constraints,expected', [
    ((2, 4, 4, 4, 0), 2), ((4, 1, 4, 4, 0), 1), ((4, 4, 3, 4, 0), 3),
    ((4, 4, 4, 0, 0), 0), ((4, 4, 4, 4, 120), 0),
])
def test_each_capacity_source_caps_the_arm(healthy_disk, tmp_path, constraints, expected):
    assert panel_dispatch.PanelCapacity(*constraints).limit(tmp_path, 4) == expected


def test_shared_observation_enforces_binding_expiry_and_global_slots(healthy_disk, tmp_path):
    path = tmp_path / 'capacity.json'
    sample = asdict(panel_dispatch.PanelCapacity(4, 4, 4, 4, 0))
    observation = {'observed_unix_ns': time.time_ns(), 'panel_sha256': 'manifest-a',
                   'arms': {arm: sample.copy() for arm in ('base', 'prompt', 'params', 'code')}}

    def read():
        path.write_text(json.dumps(observation), encoding='utf-8')
        return panel_dispatch.read_arm_capacity(path, 'manifest-a', 'code', tmp_path, 4)

    assert read() == 0   # 16 would exceed the panel's 12-slot cap.
    for arm in observation['arms'].values():
        arm['budget_slots'] = 3
    assert read() == 3
    for arm in observation['arms'].values():
        arm['judge_queue_waiting'] = 115
    assert read() == 0   # The panel cannot claim 12 slots with 5 queue entries free.
    for arm in observation['arms'].values():
        arm['judge_queue_waiting'] = 0
    observation['panel_sha256'] = 'other'
    assert read() == 0
    observation.update(panel_sha256='manifest-a', observed_unix_ns=time.time_ns() - 31_000_000_000)
    assert read() == 0


def test_runtime_drop_drains_active_cases_and_preserves_untouched_work():
    async def simulate():
        limit = 2
        started = []
        finished = []
        both_started = asyncio.Event()
        release = asyncio.Event()

        async def run(item):
            started.append(item)
            if len(started) == 2:
                both_started.set()
            await release.wait()
            finished.append(item)
            return item

        work = asyncio.create_task(panel_dispatch.dispatch_cases(list(range(6)), run, lambda: limit))
        await asyncio.wait_for(both_started.wait(), timeout=3)
        limit = 0
        release.set()
        outcomes, pending = await work
        assert started == [0, 1]
        assert sorted(outcomes) == sorted(finished) == [0, 1]
        assert pending == [2, 3, 4, 5]

    asyncio.run(simulate())

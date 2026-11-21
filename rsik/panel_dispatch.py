"""Admission and bounded dispatch for isolated evaluation panels."""

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
from shutil import disk_usage
import time


@dataclass(frozen=True)
class PanelCapacity:
    database_slots: int
    memory_slots: int
    provider_slots: int
    budget_slots: int
    judge_queue_waiting: int

    def limit(self, run_root: Path, arm_limit: int) -> int:
        values = (self.database_slots, self.memory_slots, self.provider_slots,
                  self.budget_slots, self.judge_queue_waiting)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError('Panel capacity requires nonnegative integer observations')
        if type(arm_limit) is not int or not 1 <= arm_limit <= 4:
            raise ValueError('Panel arm limit must be between one and four')
        total, _, free = disk_usage(run_root)
        if total <= 0 or free * 5 < total:
            return 0
        return max(0, min(arm_limit, *values[:4], 120 - self.judge_queue_waiting))


def read_arm_capacity(path, panel_sha256, arm_id, run_root, arm_limit, *, expected_phase=None):
    """Read the controller's latest atomic observation, defaulting to a pause."""
    try:
        observation = json.loads(path.read_bytes())
        observed_ns = observation['observed_unix_ns']
        if (observation['panel_sha256'] != panel_sha256
                or (expected_phase is not None
                    and observation.get('diagnostics', {}).get('phase') != expected_phase)
                or type(observed_ns) is not int
                or not 0 <= time.time_ns() - observed_ns <= 30_000_000_000):
            return 0
        arms = {name: PanelCapacity(**values) for name, values in observation['arms'].items()}
        if not 1 <= len(arms) <= 4:
            return 0
        limits = {name: sample.limit(run_root, 4) for name, sample in arms.items()}
        queue_counts = {sample.judge_queue_waiting for sample in arms.values()}
        if len(queue_counts) != 1 or sum(limits.values()) > min(12, max(0, 120 - queue_counts.pop())):
            return 0
        return min(arm_limit, limits[arm_id])
    except (AttributeError, KeyError, OSError, TypeError, ValueError):
        return 0


async def dispatch_cases(cases, run_case, capacity_limit):
    """Check capacity before every launch, drain started cases, return untouched work.

    Capacity is the total number of slots available to this arm, including
    its running tasks. A zero reading pauses an idle arm without creating new
    attempts. Exceptions drain already launched work before propagating.
    """
    next_case = 0
    active = set()
    finished = []
    try:
        while next_case < len(cases) or active:
            while next_case < len(cases):
                limit = capacity_limit()
                if type(limit) is not int or not 0 <= limit <= 4:
                    raise ValueError('Panel admission returned an invalid arm limit')
                if len(active) >= limit:
                    break
                active.add(asyncio.create_task(run_case(cases[next_case])))
                next_case += 1
            if not active:
                break
            done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            outcomes = await asyncio.gather(*done, return_exceptions=True)
            for outcome in outcomes:
                if isinstance(outcome, BaseException):
                    raise outcome
            finished.extend(outcomes)
    finally:
        if active:
            await asyncio.gather(*active, return_exceptions=True)
    return finished, cases[next_case:]

"""Pure, fail-closed capacity policy and atomic controller observations.

Slot inputs are conservative panel totals, including already running tasks.
Callers supply evidence and timestamps; this module does not sample the host.
"""

from collections.abc import Mapping
import json
import os
from pathlib import Path
import re
import tempfile


def build_observation(*, panel_sha256, arm_limits, observed_unix_ns,
                      disk_total_bytes, disk_free_bytes, projected_write_bytes,
                      memory_available_bytes, memory_reserve_bytes,
                      memory_per_active_qa_bytes, database_slots, provider_slots,
                      budget_slots, judge_queue_waiting):
    """Allocate shared capacity once, round-robin in the supplied arm order.

    Missing resource observations pause the whole panel. Invalid observations
    raise ValueError, rather than silently converting malformed evidence.
    Diagnostics describe admission gates only, not measured pipeline stages.
    """
    if not isinstance(panel_sha256, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', panel_sha256):
        raise ValueError('panel_sha256 must be a SHA-256 hex digest')
    if type(observed_unix_ns) is not int or observed_unix_ns < 0:
        raise ValueError('observed_unix_ns must be a nonnegative integer')
    if not isinstance(arm_limits, Mapping) or not 1 <= len(arm_limits) <= 4:
        raise ValueError('arm_limits must contain one to four ordered arms')
    for name, limit in arm_limits.items():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('arm names must be nonempty strings')
        if type(limit) is not int or not 1 <= limit <= 3:
            raise ValueError('arm limits must be integers between one and three')

    observations = dict(
        disk_total_bytes=disk_total_bytes, disk_free_bytes=disk_free_bytes,
        projected_write_bytes=projected_write_bytes,
        memory_available_bytes=memory_available_bytes,
        memory_reserve_bytes=memory_reserve_bytes,
        memory_per_active_qa_bytes=memory_per_active_qa_bytes,
        database_slots=database_slots, provider_slots=provider_slots,
        budget_slots=budget_slots, judge_queue_waiting=judge_queue_waiting)
    for name, value in observations.items():
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f'{name} must be a nonnegative integer or None')
    for name in ('disk_total_bytes', 'memory_per_active_qa_bytes'):
        if observations[name] == 0:
            raise ValueError(f'{name} must be positive when known')
    if (disk_total_bytes is not None and disk_free_bytes is not None
            and disk_free_bytes > disk_total_bytes):
        raise ValueError('disk_free_bytes cannot exceed disk_total_bytes')

    disk_known = all(observations[name] is not None for name in (
        'disk_total_bytes', 'disk_free_bytes', 'projected_write_bytes'))
    projected_free = disk_free_bytes - projected_write_bytes if disk_known else None
    disk_safe = disk_known and projected_free * 5 >= disk_total_bytes
    memory_known = all(observations[name] is not None for name in (
        'memory_available_bytes', 'memory_reserve_bytes', 'memory_per_active_qa_bytes'))
    memory_slots = (max(0, memory_available_bytes - memory_reserve_bytes)
                    // memory_per_active_qa_bytes if memory_known else 0)
    gate_slots = {
        'disk': 12 if disk_safe else 0,
        'memory': memory_slots,
        'database': database_slots if database_slots is not None else 0,
        'provider': provider_slots if provider_slots is not None else 0,
        'budget': budget_slots if budget_slots is not None else 0,
        'judge_queue': max(0, 120 - judge_queue_waiting) if judge_queue_waiting is not None else 0,
        'panel': 12,
        'arm_limits': sum(arm_limits.values()),
    }
    total_slots = min(gate_slots.values())
    allocation = dict.fromkeys(arm_limits, 0)
    remaining = total_slots
    while remaining:
        for name, limit in arm_limits.items():
            if allocation[name] < limit:
                allocation[name] += 1
                remaining -= 1
                if remaining == 0:
                    break

    # Repeat only each arm's allocation, never the common resource total.
    arms = {name: dict(database_slots=slots, memory_slots=slots,
                       provider_slots=slots, budget_slots=slots,
                       judge_queue_waiting=judge_queue_waiting if judge_queue_waiting is not None else 120)
            for name, slots in allocation.items()}
    return {
        'panel_sha256': panel_sha256,
        'observed_unix_ns': observed_unix_ns,
        'arms': arms,
        'diagnostics': {
            'observations': observations,
            'unknown_observations': [name for name, value in observations.items() if value is None],
            'projected_disk_free_bytes': projected_free,
            'disk_safe': bool(disk_safe),
            'gate_slots': gate_slots,
            'limiting_gates': [name for name, slots in gate_slots.items() if slots == total_slots],
            'allocated_total_slots': total_slots,
            'arm_allocations': allocation,
        },
    }


def publish_observation(path, data):
    """Replace a JSON observation using a flushed temporary in the same directory.

    Readers see either the old or new complete JSON. A failed serialization,
    fsync, or replace preserves the old target and removes the temporary file.
    """
    payload = json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2) + '\n'
    path = Path(path)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         prefix=f'.{path.name}.', suffix='.tmp',
                                         dir=path.parent, delete=False) as stream:
            temporary_path = Path(stream.name)
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

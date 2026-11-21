"""Pure F5 scheduling projection over a trusted evidence gate's decisions.

This module neither assesses performance nor authenticates gate decisions. The
caller must verify the assessment report and its DEV200 population binding before
supplying records. A digest here is only a reference to that external report.
"""

from dataclasses import dataclass
import re
from typing import Literal, Sequence


Direction = Literal['PROMPT', 'PARAMETER', 'CODE']
DIRECTIONS: tuple[Direction, ...] = ('PROMPT', 'PARAMETER', 'CODE')


@dataclass(frozen=True)
class GateRecord:
    candidate_id: str
    parent_id: str
    direction: Direction
    verdict: str
    evidence_mode: str
    paired_cases: int
    assessment_sha256: str


@dataclass(frozen=True)
class DirectionRetirement:
    direction: Direction
    parent_id: str
    candidate_id: str


@dataclass(frozen=True)
class DirectionProjection:
    # Each active direction receives exactly one scheduling slot.
    active_directions: tuple[Direction, ...]
    qualified_parent_ids: tuple[str, ...]
    retirements: tuple[DirectionRetirement, ...]
    records: tuple[GateRecord, ...]


def _require_identifier(value: str) -> None:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError('F5 identifiers must be nonempty canonical strings')


def project_directions(
    baseline_id: str, records: Sequence[GateRecord],
) -> DirectionProjection:
    """Replay ordered canonical gate records without changing external state.

    The baseline starts eligible. Only ADMITTED records add eligible parents;
    they require LIVE_METERED evidence and 190..200 paired DEV200 cases. Other
    verdicts remain diagnostic records, including UNKNOWN and synthetic runs.
    An admitted child retires its direction only when its exact direct parent
    was also admitted in that direction. Retired parents remain reusable across
    directions. Input order is authoritative; forward parent references fail.
    """
    _require_identifier(baseline_id)
    ordered = tuple(records)
    seen = {baseline_id}
    eligible: dict[str, GateRecord | None] = {baseline_id: None}
    retirements: dict[Direction, DirectionRetirement] = {}

    for record in ordered:
        if not isinstance(record, GateRecord):
            raise ValueError('F5 requires canonical GateRecord values')
        _require_identifier(record.candidate_id)
        _require_identifier(record.parent_id)
        if record.candidate_id in seen:
            raise ValueError('F5 duplicate candidate or baseline ID')
        seen.add(record.candidate_id)
        if record.parent_id == record.candidate_id:
            raise ValueError('F5 candidate cannot be its own parent')
        if record.direction not in DIRECTIONS:
            raise ValueError('F5 invalid direction')
        for value in (record.verdict, record.evidence_mode):
            _require_identifier(value)
        if type(record.paired_cases) is not int or not 0 <= record.paired_cases <= 200:
            raise ValueError('F5 paired_cases must be an integer within DEV200')
        if (not isinstance(record.assessment_sha256, str)
                or re.fullmatch(r'[0-9a-f]{64}', record.assessment_sha256) is None):
            raise ValueError('F5 assessment_sha256 must be canonical lowercase SHA-256')

        if record.verdict != 'ADMITTED':
            continue
        if record.evidence_mode != 'LIVE_METERED' or record.paired_cases < 190:
            raise ValueError('F5 admission requires LIVE_METERED and 190 paired DEV200 cases')
        if record.parent_id not in eligible:
            raise ValueError('F5 admitted candidate requires an already eligible exact parent')
        parent = eligible[record.parent_id]
        eligible[record.candidate_id] = record
        if parent is not None and parent.direction == record.direction:
            retirements.setdefault(record.direction, DirectionRetirement(
                record.direction, parent.candidate_id, record.candidate_id,
            ))

    return DirectionProjection(
        active_directions=tuple(direction for direction in DIRECTIONS if direction not in retirements),
        qualified_parent_ids=tuple(eligible),
        retirements=tuple(retirements[direction] for direction in DIRECTIONS if direction in retirements),
        records=ordered,
    )

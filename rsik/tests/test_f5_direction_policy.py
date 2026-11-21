from dataclasses import replace

import pytest

from rsik.f5_direction_policy import (
    DIRECTIONS, DirectionRetirement, GateRecord, project_directions,
)


def admitted(candidate_id, parent_id='baseline', direction='PROMPT', **changes):
    record = GateRecord(candidate_id, parent_id, direction, 'ADMITTED', 'LIVE_METERED', 190, 'a' * 64)
    return replace(record, **changes)


def test_initial_has_one_slot_for_each_direction_and_only_baseline_parent():
    projection = project_directions('baseline', [])
    assert projection.active_directions == DIRECTIONS
    assert projection.qualified_parent_ids == ('baseline',)
    assert projection.retirements == projection.records == ()


def test_independent_directions_retire_only_on_their_exact_same_direction_chain():
    records = [admitted('p1'), admitted('c1', direction='CODE'),
               admitted('q1', direction='PARAMETER')]
    assert project_directions('baseline', records).active_directions == DIRECTIONS
    records.extend([admitted('c2', 'c1', 'CODE'), admitted('p2', 'p1'),
                    admitted('q2', 'q1', 'PARAMETER')])
    projection = project_directions('baseline', records)
    assert projection.active_directions == ()
    assert projection.qualified_parent_ids == ('baseline', 'p1', 'c1', 'q1', 'c2', 'p2', 'q2')
    assert projection.retirements == (
        DirectionRetirement('PROMPT', 'p1', 'p2'),
        DirectionRetirement('PARAMETER', 'q1', 'q2'),
        DirectionRetirement('CODE', 'c1', 'c2'),
    )
    assert projection.records == tuple(records)
    assert project_directions('baseline', records) == projection


def test_retired_parents_remain_eligible_for_other_directions():
    records = [admitted('p1'), admitted('p2', 'p1'), admitted('c1', 'p1', 'CODE')]
    projection = project_directions('baseline', records)
    assert projection.active_directions == ('PARAMETER', 'CODE')
    assert projection.qualified_parent_ids == ('baseline', 'p1', 'p2', 'c1')
    # A cross-direction child alone does not retire CODE, but its CODE child does.
    records.append(admitted('c2', 'c1', 'CODE'))
    assert project_directions('baseline', records).active_directions == ('PARAMETER',)


@pytest.mark.parametrize('verdict,evidence_mode', [
    ('UNKNOWN', 'LIVE_METERED'), ('REJECTED', 'LIVE_METERED'),
    ('UNKNOWN', 'SYNTHETIC'), ('FAILED', 'SYNTHETIC'),
])
def test_nonadmitted_records_preserve_diagnostics_without_eligibility_or_retirement(verdict, evidence_mode):
    diagnostic = admitted('diagnostic', 'p1', verdict=verdict, evidence_mode=evidence_mode, paired_cases=0)
    records = [admitted('p1'), diagnostic]
    projection = project_directions('baseline', records)
    assert projection.active_directions == DIRECTIONS
    assert projection.qualified_parent_ids == ('baseline', 'p1')
    assert projection.records == tuple(records)
    with pytest.raises(ValueError, match='already eligible exact parent'):
        project_directions('baseline', records + [admitted('child', 'diagnostic')])


@pytest.mark.parametrize('changes', [
    {'evidence_mode': 'SYNTHETIC'}, {'evidence_mode': 'REAL_REPLAY'},
    {'paired_cases': 189}, {'paired_cases': 201}, {'paired_cases': True},
    {'paired_cases': 190.0}, {'assessment_sha256': ''},
    {'assessment_sha256': 'A' * 64}, {'assessment_sha256': 'g' * 64},
    {'assessment_sha256': 'a' * 63}, {'assessment_sha256': None},
    {'direction': 'prompt'}, {'direction': None}, {'candidate_id': ''},
    {'candidate_id': ' p1'}, {'candidate_id': 'baseline'}, {'parent_id': ''},
    {'parent_id': 'p1'}, {'verdict': ''}, {'evidence_mode': None},
])
def test_invalid_canonical_records_and_admission_bindings_are_rejected(changes):
    with pytest.raises(ValueError):
        project_directions('baseline', [replace(admitted('p1'), **changes)])


@pytest.mark.parametrize('parent', ['unknown', 'later'])
def test_unknown_and_forward_parent_references_are_rejected(parent):
    with pytest.raises(ValueError, match='already eligible exact parent'):
        project_directions('baseline', [admitted('child', parent), admitted('later')])


def test_duplicate_diagnostic_ids_are_also_rejected():
    record = admitted('unknown', verdict='UNKNOWN')
    with pytest.raises(ValueError, match='duplicate'):
        project_directions('baseline', [record, record])


@pytest.mark.parametrize('baseline', ['', ' baseline', None])
def test_invalid_baseline_is_rejected(baseline):
    with pytest.raises(ValueError):
        project_directions(baseline, [])


def test_untyped_record_is_rejected():
    with pytest.raises(ValueError, match='canonical GateRecord'):
        project_directions('baseline', [{}])


def test_retirement_keeps_first_exact_chain_and_input_order_is_not_mutated():
    records = [admitted('p1'), admitted('p2', 'p1'), admitted('p3', 'p2', paired_cases=200)]
    before = records.copy()
    projection = project_directions('baseline', records)
    assert records == before
    assert projection.retirements == (DirectionRetirement('PROMPT', 'p1', 'p2'),)
    assert projection.qualified_parent_ids == ('baseline', 'p1', 'p2', 'p3')

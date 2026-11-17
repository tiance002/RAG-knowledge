"""Fake agents and replay callbacks exercise the actual round coordinator."""
from copy import deepcopy
import hashlib
from pathlib import Path
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT));sys.path.insert(0, str(ROOT / 'scripts'))
from rsik.adjudicate import SLOTS, evaluate_candidate
from rsik.contracts import ContractError
from rsik.errors import StageFailure
from rsik.redact import make_visible, project_summary
from rsik.workflow import execute_round
from run_rsi_loop import apply_proposal


def public_context():
    return make_visible([project_summary('NONSEED_EXPANSION', {'questions': 20, 'nonseed_members': 30}, include_count_evidence=True)],
                        parent_code='SYNTHETIC public selector')


class FakeRoles:
    def __init__(self, *, same_patch=False, barrier=False, abstain=None, fail=None):
        self.same_patch, self.abstain, self.fail = same_patch, abstain, fail
        self.barrier = threading.Barrier(3, timeout=5) if barrier else None
        self.inputs = {}

    def diagnose(self, public):
        evidence = next(iter(public['evidence_quotes']))
        return {'status': 'OK', 'reason': None, 'result': {'schema_version': 'rsi-v2.2',
            'observation_refs': public['observation_refs'], 'problem_layer': 'SELECTION',
            'first_deviation': 'SYNTHETIC observed retained nonseed members', 'evidence_refs': [evidence],
            'evidence_quote': public['evidence_quotes'][evidence], 'hypothesis': 'SYNTHETIC compression hypothesis',
            'fixable_within_seam': True, 'required_data': [], 'confidence': .5}}

    def propose(self, angle, public, diagnosis):
        self.inputs[angle] = public
        if self.barrier: self.barrier.wait()
        if self.fail == angle: raise StageFailure('AGENT_ERROR', 'AGENT_UNAVAILABLE')
        if self.abstain == angle: return {'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC insufficient evidence'}
        number = SLOTS.index(angle)
        patch = '    return list(selected)' if self.same_patch or number == 0 else (
            '    return selected[:1]' if number == 1 else '    return selected[::-1]')
        return {'status': 'OK', 'reason': None, 'result': {'schema_version': 'rsi-v2.2',
            'proposal_id': 'SYNTHETIC-' + angle, 'round_id': public['round_id'], 'angle': angle,
            'parent_id': public['parent_alias'], 'observation_ref': public['observation_refs'][0],
            'hypothesis': 'SYNTHETIC hypothesis', 'change': 'SYNTHETIC selector change',
            'expected_impact': 'SYNTHETIC result', 'falsification': 'Any necessary fact or source loss refutes the hypothesis.',
            'worker_patch': {'old': '    return selected', 'new': patch}, 'evidence_refs': [],
            'handbook_entry_ids': [], 'risk_assessment': 'SYNTHETIC isolated test', 'confidence': number / 2}}


def fake_rows():
    return [{'qid': f'SYNTHETIC-{i}', 'source_group': f'group-{i}', 'chars_A': 100, 'chars_B': 90,
        'parent_facts': {'fact': 'PRESENT'}, 'candidate_facts': {'fact': 'PRESENT'},
        'parent_faults': [], 'candidate_faults': [], 'provenance_A': 'PRESENT', 'provenance_B': 'PRESENT',
        'pair_status': 'PAIRED', 'pair_identity': True, 'execution': 'COMPLETED',
        'context_A_sha256': 'a' * 64, 'context_B_sha256': 'b' * 64, 'upstream_hash': 'c' * 64} for i in range(20)]


def callbacks(*, mixed=False):
    reviewed, replayed = [], []
    parent = (ROOT / 'scripts/rsi_delivery_worker.py').read_bytes()
    def review(proposal):
        reviewed.append(proposal['angle'])
        legacy = {k: proposal[k] for k in ('hypothesis', 'change', 'falsification')}
        legacy.update(old=proposal['worker_patch']['old'], new=proposal['worker_patch']['new'], impact=proposal['expected_impact'])
        changed = apply_proposal(parent, legacy)
        compile(changed, 'SYNTHETIC-worker', 'exec')
        return hashlib.sha256(changed).hexdigest()
    def replay(proposal, artifact):
        replayed.append(proposal['angle'])
        rows = fake_rows()
        if mixed and proposal['angle'] == 'P2_BUDGET': rows[0]['candidate_facts']['fact'] = 'ABSENT'
        if mixed and proposal['angle'] == 'P3_ORDER': rows[0].update(execution='FAILED', pair_status='FAILED', fault_origin='INFRA')
        return evaluate_candidate({'angle': proposal['angle'], 'proposal_id': proposal['proposal_id'],
            'candidate_id': artifact, 'worker_hash': artifact, 'config_hash': 'fixed-SYNTHETIC'},
            rows, [r['qid'] for r in rows], total_questions=20)
    return review, replay, reviewed, replayed


def test_three_parallel_independent_proposers_and_duplicate_patches_all_replay():
    context = public_context();before = deepcopy(context)
    roles = FakeRoles(same_patch=True, barrier=True)
    review, replay, reviewed, replayed = callbacks()
    outcome = execute_round(context, roles, review, replay)
    assert reviewed == replayed == list(SLOTS)
    assert outcome['attempted_replays'] == list(SLOTS)
    assert len(outcome['candidate_decisions']) == 3
    assert outcome['adopted_proposal_id'] == 'SYNTHETIC-P1_DEDUP'
    assert len({id(value) for value in roles.inputs.values()}) == 3
    assert all('proposal_instances' not in value for value in roles.inputs.values()) and context == before


def test_good_fact_loss_and_infrastructure_outcomes_are_all_retained():
    review, replay, _, replayed = callbacks(mixed=True)
    outcome = execute_round(public_context(), FakeRoles(), review, replay)
    by_slot = {d['angle']: d for d in outcome['candidate_decisions']}
    assert replayed == list(SLOTS) and outcome['verdict'] == 'ADOPT_CANDIDATE'
    assert by_slot['P2_BUDGET']['verdict'] == 'REJECTED'
    assert by_slot['P3_ORDER']['verdict'] == 'INCONCLUSIVE'


def test_single_proposer_profile_keeps_three_statistics_slots():
    review, replay, _, replayed = callbacks()
    result = execute_round(public_context(), FakeRoles(), review, replay, enabled_slots=SLOTS[:1])
    assert replayed == ['P1_DEDUP'] and result['family_size'] == 3
    assert len(result['candidate_decisions']) == 3


def test_one_abstention_or_local_agent_failure_does_not_block_other_proposals():
    review, replay, _, replayed = callbacks()
    result = execute_round(public_context(), FakeRoles(abstain='P2_BUDGET', fail='P3_ORDER'), review, replay)
    assert replayed == ['P1_DEDUP'] and result['verdict'] == 'ADOPT_CANDIDATE'
    assert result['agent_call_results']['P2_BUDGET'] == 'ABSTAIN'


def test_registry_failure_stops_round_without_selecting_partial_winner():
    review, _, _, _ = callbacks()
    attempted = []
    def replay(proposal, artifact):
        attempted.append(proposal['angle'])
        raise StageFailure('REGISTRY_ERROR', 'REGISTRY_WRITE_FAILED')
    result = execute_round(public_context(), FakeRoles(), review, replay)
    assert result['verdict'] == 'ROUND_ABORTED' and result['adopted_candidate_id'] is None
    assert attempted == ['P1_DEDUP']


def test_expired_deadline_prevents_any_dispatch_or_partial_adoption():
    class ForbiddenRoles:
        def diagnose(self, public): raise AssertionError('expired round dispatched a model')
    result = execute_round(public_context(), ForbiddenRoles(), None, None, deadline=time.monotonic()-1)
    assert result['verdict'] == 'ROUND_ABORTED'


def test_critical_storage_failure_inside_agent_cannot_be_downgraded_to_abstain():
    class Broken(FakeRoles):
        def propose(self, angle, public, diagnosis):
            if angle == 'P2_BUDGET': raise StageFailure('STORAGE_ERROR', 'COST_RECEIPT_NOT_DURABLE')
            return super().propose(angle, public, diagnosis)
    review, replay, reviewed, replayed = callbacks()
    result = execute_round(public_context(), Broken(), review, replay)
    assert result['verdict'] == 'ROUND_ABORTED' and not reviewed and not replayed

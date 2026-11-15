"""Independent proposal instances, deterministic review/replay, one adoption.

The roles receive only the public context. Trusted adapters own registry,
materialization and replay. Callers own persistent locks/checkpoints and budgets.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import time

from .adjudicate import SLOTS, adjudicate_round
from .contracts import ContractError, unwrap_response, validate_diagnosis, validate_proposal
from .errors import StageFailure, normalized_error


def placeholder(angle, proposal_id, reason, *, verdict='INCONCLUSIVE'):
    return {'schema_version': 'rsi-candidate-decision/v2.2', 'angle': angle,
            'proposal_id': proposal_id, 'candidate_id': None, 'verdict': verdict,
            'reason_codes': [reason], 'gates_passed': False, 'round_stop': False,
            'sign_p_raw': {'numerator': 1, 'denominator': 1, 'value': 1.0},
            'sign_p_adjusted': None, 'per_question': [], 'delta_total': None}


def execute_round(public, roles, review, replay, *, enabled_slots=SLOTS, deadline=None):
    """Every statically qualified instance reaches replay, including duplicates.

    A confirmed global integrity failure stops further side effects and forbids
    a winner. A local replay failure leaves the other candidates eligible.
    """
    record = {'round_id': public['round_id'], 'errors': [], 'proposal_instances': {},
              'execution_state': 'RUNNING', 'agent_call_results': {}, 'qualified': [], 'attempted_replays': []}
    if deadline is not None and time.monotonic() >= deadline:
        record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason='ROUND_DEADLINE',
                      adopted_proposal_id=None, adopted_candidate_id=None)
        return record
    def failure(stage, category, reason, scope='CANDIDATE'):
        error = normalized_error(public['round_id'], stage, category, reason, scope=scope)
        record['errors'].append(error.to_dict())
    try:
        diagnosis_raw = roles.diagnose(deepcopy(public))
        diagnosis = unwrap_response(diagnosis_raw)
        if diagnosis is None:
            record.update(execution_state='COMPLETED', verdict='NO_ADOPTION', stop_reason='DIAGNOSIS_ABSTAIN',
                          adopted_proposal_id=None, adopted_candidate_id=None)
            return record
        validate_diagnosis(diagnosis, public)
    except (ContractError, StageFailure, TimeoutError) as error:
        reason = error.code if isinstance(error, ContractError) else error.reason_code if isinstance(error, StageFailure) else 'AGENT_TIMEOUT'
        failure('DIAGNOSIS', 'AGENT_ERROR', reason, 'AGENT')
        critical = isinstance(error, StageFailure) and error.global_stop
        record.update(execution_state='FAILED' if critical else 'COMPLETED',
                      verdict='ROUND_ABORTED' if critical else 'NO_ADOPTION', stop_reason=reason,
                      adopted_proposal_id=None, adopted_candidate_id=None)
        return record
    record['diagnosis'] = diagnosis
    if deadline is not None and time.monotonic() >= deadline:
        record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason='ROUND_DEADLINE',
                      adopted_proposal_id=None, adopted_candidate_id=None)
        return record
    if diagnosis['problem_layer'] != 'SELECTION' or not diagnosis['fixable_within_seam']:
        record.update(execution_state='COMPLETED', verdict='NO_ADOPTION', stop_reason='BLOCKED_BY_SEAM_OR_DATA',
                      adopted_proposal_id=None, adopted_candidate_id=None)
        return record
    if any(slot not in SLOTS for slot in enabled_slots):
        raise ValueError('unknown proposer slot')
    # Separate deep-copied contexts and requests; collection occurs only after
    # all initial proposers were dispatched. No candidate result feeds a peer.
    def propose(slot):
        try:
            raw = roles.propose(slot, deepcopy(public), deepcopy(diagnosis))
            proposal = unwrap_response(raw)
            if proposal is not None:
                validate_proposal(proposal, public, slot)
            return proposal, None
        except (ContractError, StageFailure, TimeoutError) as error:
            reason = error.code if isinstance(error, ContractError) else error.reason_code if isinstance(error, StageFailure) else 'AGENT_TIMEOUT'
            return None, {'reason': reason, 'global_stop': isinstance(error, StageFailure) and error.global_stop,
                          'category': error.category if isinstance(error, StageFailure) else 'AGENT_ERROR'}
    proposals = {}
    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = {slot: executor.submit(propose, slot) for slot in enabled_slots}
        for slot in enabled_slots:
            proposals[slot] = futures[slot].result()
    critical = next((error for _, error in proposals.values() if error and error['global_stop']), None)
    if critical:
        failure('PROPOSAL', critical['category'], critical['reason'], 'ROUND')
        record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason=critical['reason'],
                      adopted_proposal_id=None, adopted_candidate_id=None)
        return record
    decisions, artifacts = [], {}
    for slot in SLOTS:
        if slot not in proposals:
            decisions.append(placeholder(slot, 'disabled-' + slot, 'SLOT_DISABLED'))
            continue
        proposal, error = proposals[slot]
        if proposal is None:
            reason = error['reason'] if error else 'ABSTAIN'
            record['agent_call_results'][slot] = reason
            decisions.append(placeholder(slot, 'abstain-' + slot, reason))
            if error:
                failure('PROPOSAL', 'AGENT_ERROR', reason, 'AGENT')
            continue
        record['proposal_instances'][slot] = proposal
        try:
            artifact = review(proposal)
        except StageFailure as error:
            failure('STATIC_REVIEW', error.category, error.reason_code)
            verdict = 'REJECTED' if error.category in {'PATCH_ERROR','SEAM_ERROR','CONTRACT_ERROR'} else 'INCONCLUSIVE'
            decisions.append(placeholder(slot, proposal['proposal_id'], error.reason_code, verdict=verdict))
            if error.global_stop:
                record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason=error.reason_code,
                              adopted_proposal_id=None, adopted_candidate_id=None, candidate_decisions=decisions)
                return record
            continue
        artifacts[slot] = artifact
        record['qualified'].append(slot)
    for slot in SLOTS:
        if slot not in artifacts:
            continue
        proposal = record['proposal_instances'][slot]
        if deadline is not None and time.monotonic() >= deadline:
            record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason='ROUND_DEADLINE',
                          adopted_proposal_id=None, adopted_candidate_id=None, candidate_decisions=decisions)
            return record
        record['attempted_replays'].append(slot)
        try:
            decisions.append(replay(proposal, artifacts[slot]))
        except StageFailure as error:
            failure('REPLAY', error.category, error.reason_code)
            decisions.append(placeholder(slot, proposal['proposal_id'], error.reason_code))
            if error.global_stop:
                record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason=error.reason_code,
                              adopted_proposal_id=None, adopted_candidate_id=None, candidate_decisions=decisions)
                return record
    if deadline is not None and time.monotonic() >= deadline:
        record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason='ROUND_DEADLINE',
                      adopted_proposal_id=None, adopted_candidate_id=None, candidate_decisions=decisions)
        return record
    try:
        outcome = adjudicate_round(decisions, all_qualified_terminal=set(record['qualified']) == set(record['attempted_replays']))
    except Exception:
        failure('ADJUDICATION', 'JUDGE_ERROR', 'JUDGE_EXCEPTION', 'ROUND')
        record.update(execution_state='FAILED', verdict='ROUND_ABORTED', adopted_proposal_id=None,
                      adopted_candidate_id=None, stop_reason='JUDGE_EXCEPTION')
        return record
    record.update(outcome, execution_state='COMPLETED')
    return record

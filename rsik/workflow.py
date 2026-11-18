"""Independent proposal instances, deterministic review/replay, one adoption.

The roles receive only the public context. Trusted adapters own registry,
materialization and replay. Callers own persistent locks/checkpoints and budgets.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import time
import hashlib
import ast

from .role_profiles import LEGACY, M1_ADAPTIVE_V1, roles_for, profile_manifest
from .events import digest
from .adjudicate import SLOTS, adjudicate_round
from .contracts import ContractError, unwrap_response, validate_diagnosis, validate_proposal
from .errors import StageFailure, normalized_error


def placeholder(angle, proposal_id, reason, *, verdict='INCONCLUSIVE'):
    return {'schema_version': 'rsi-candidate-decision/v2.2', 'angle': angle,
            'proposal_id': proposal_id, 'candidate_id': None, 'verdict': verdict,
            'reason_codes': [reason], 'gates_passed': False, 'round_stop': False,
            'sign_p_raw': {'numerator': 1, 'denominator': 1, 'value': 1.0},
            'sign_p_adjusted': None, 'per_question': [], 'delta_total': None}


def full_worker_identity(artifact):
    """Identify the materialized source independently of the proposed edit."""
    worker = artifact.get('worker') if isinstance(artifact, dict) else None
    identity = {'worker_sha256': 'UNKNOWN', 'normalized_ast_sha256': 'UNKNOWN'}
    if isinstance(worker, bytes):
        identity['worker_sha256'] = hashlib.sha256(worker).hexdigest()
        try:
            tree = ast.dump(ast.parse(worker), include_attributes=False)
        except (SyntaxError, ValueError, RecursionError):
            pass
        else:
            identity['normalized_ast_sha256'] = hashlib.sha256(tree.encode('utf-8')).hexdigest()
    return identity


def execute_round(public, roles, review, replay, *, enabled_slots=SLOTS, deadline=None, role_profile=LEGACY, observer=None, dispatch_directory=None):
    """Every statically qualified instance reaches replay, including duplicates.

    A confirmed global integrity failure stops further side effects and forbids
    a winner. A local replay failure leaves the other candidates eligible.
    """
    observer = observer or getattr(roles, 'observer', None)
    mapping=roles_for(role_profile)
    def observe(kind, slot, proposal=None, **fields):
        if observer is None:
            return
        proposal = proposal if isinstance(proposal, dict) else {}
        instance_id = digest({'round_id': public['round_id'], 'slot': slot,
                              'proposal_id': proposal.get('proposal_id', 'UNKNOWN')})
        payload = {'proposal_instance_id': instance_id, 'proposal_id': proposal.get('proposal_id', 'UNKNOWN'),
                   'round_id': public['round_id'], 'slot': slot, 'agent_role': mapping[slot],
                   'role_profile': role_profile, 'role_profile_hash': getattr(observer, 'context', {}).get(
                       'role_profile_hash', digest(profile_manifest(role_profile))),
                   'parent_id': public.get('parent_id', getattr(observer, 'context', {}).get('parent_id', 'UNKNOWN')),
                   'attempt': 1, **fields}
        invocation = getattr(roles, 'observation_invocation', {})
        if kind in {'PROPOSAL_GENERATED', 'PROPOSAL_CONTRACT_VALIDATED', 'PROPOSAL_REFERENCED'}:
            payload.update({k: invocation[k] for k in ('invocation_id', 'request_key', 'cost_request_key', 'attempt') if k in invocation})
        for name, target in [('hypothesis','hypothesis_hash'), ('expected_impact','expected_impact_hash'), ('worker_patch','patch_hash')]:
            if name in proposal:
                payload[target] = digest(proposal[name])
        observer.emit(kind, payload, key=kind + ':' + instance_id)

    record = {'role_profile':role_profile,'proposal_roles':mapping,'raw_role_proposals':{},'round_id': public['round_id'], 'errors': [], 'proposal_instances': {},
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
        if diagnosis is None and role_profile != M1_ADAPTIVE_V1:
            record.update(execution_state='COMPLETED', verdict='NO_ADOPTION', stop_reason='DIAGNOSIS_ABSTAIN',
                          adopted_proposal_id=None, adopted_candidate_id=None)
            return record
        if diagnosis is not None:
            validate_diagnosis(diagnosis, public, adaptive=role_profile == M1_ADAPTIVE_V1)
    except (ContractError, StageFailure, TimeoutError) as error:
        reason = error.code if isinstance(error, ContractError) else error.reason_code if isinstance(error, StageFailure) else 'AGENT_TIMEOUT'
        failure('DIAGNOSIS', error.category if isinstance(error, StageFailure) else 'AGENT_ERROR',
                reason, 'SYSTEM' if isinstance(error, StageFailure) and error.global_stop else 'AGENT')
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
    if role_profile != M1_ADAPTIVE_V1 and (diagnosis['problem_layer'] != 'SELECTION' or not diagnosis['fixable_within_seam']):
        record.update(execution_state='COMPLETED', verdict='NO_ADOPTION', stop_reason='BLOCKED_BY_SEAM_OR_DATA',
                      adopted_proposal_id=None, adopted_candidate_id=None)
        return record
    if any(slot not in SLOTS for slot in enabled_slots):
        raise ValueError('unknown proposer slot')
    assignments = {}
    if role_profile == M1_ADAPTIVE_V1:
        dispatch_directory = dispatch_directory or getattr(roles, 'directory', None)
        if dispatch_directory is None:
            raise ValueError('ADAPTIVE_DISPATCH_DIRECTORY_REQUIRED')
        if observer is None:
            from .observability import observation_for
            observer = observation_for(dispatch_directory)
        plan = prepare_dispatch(public, diagnosis, directory=dispatch_directory, observer=observer)
        record['dispatch_plan'] = plan
        assignments = {a['slot']: a for a in plan['assignments']}
    # Separate deep-copied contexts and requests; collection occurs only after
    # all initial proposers were dispatched. No candidate result feeds a peer.
    def propose(slot):
        try:
            actual_role=mapping[slot]
            if slot in assignments:
                assignment = assignments[slot]
                if assignment['task_type'] == 'ABSTAIN':
                    return None, None
                raw = roles.propose(actual_role, deepcopy(public), deepcopy(diagnosis),
                                    assignment=deepcopy(assignment))
            else:
                raw = roles.propose(actual_role, deepcopy(public), deepcopy(diagnosis))
            proposal = unwrap_response(raw)
            if proposal is not None:
                # Observe the instance before validation; never merge identical patch bytes.
                observe('PROPOSAL_GENERATED', slot, proposal)
                try:
                    validate_proposal(proposal, public, actual_role)
                except ContractError as error:
                    observe('PROPOSAL_CONTRACT_VALIDATED', slot, proposal, contract_result='INVALID', reason=error.code)
                    raise
                observe('PROPOSAL_CONTRACT_VALIDATED', slot, proposal, contract_result='VALID')
                if proposal['handbook_entry_ids']:
                    observe('PROPOSAL_REFERENCED', slot, proposal, entry_ids=proposal['handbook_entry_ids'])
                record['raw_role_proposals'][slot]=deepcopy(proposal)
                proposal=deepcopy(proposal)
                proposal['angle']=slot
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
                failure('PROPOSAL', error['category'], reason, 'AGENT')
            continue
        record['proposal_instances'][slot] = proposal
        try:
            artifact = review(proposal)
        except StageFailure as error:
            observe('PROPOSAL_STATIC_REJECTED', slot, proposal, reason=error.reason_code, static_pass=False)
            failure('STATIC_REVIEW', error.category, error.reason_code)
            verdict = 'REJECTED' if error.category in {'PATCH_ERROR','SEAM_ERROR','CONTRACT_ERROR'} else 'INCONCLUSIVE'
            decisions.append(placeholder(slot, proposal['proposal_id'], error.reason_code, verdict=verdict))
            if error.global_stop:
                record.update(execution_state='FAILED', verdict='ROUND_ABORTED', stop_reason=error.reason_code,
                              adopted_proposal_id=None, adopted_candidate_id=None, candidate_decisions=decisions)
                return record
            continue
        if observer is not None:
            identity = full_worker_identity(artifact)
            observe('PROPOSAL_STATIC_PASSED', slot, proposal, static_pass=True,
                    worker_hash=identity['worker_sha256'], **identity)
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
        observe('PROPOSAL_REPLAY_ENTERED', slot, proposal, used=True)
        record['attempted_replays'].append(slot)
        try:
            decision = replay(proposal, artifacts[slot])
            decisions.append(decision)
            if decision.get('candidate_id'):
                observe('PROPOSAL_CANDIDATE_BOUND', slot, proposal, candidate_id=decision['candidate_id'])
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


def prepare_dispatch(public, diagnosis, *, directory=None, observer=None):
    """Persist and verify the immutable allocation before any proposer side effect.

    Only an explicitly typed dispatcher outage permits frozen generalist fallback.
    Contract, storage, security and checkpoint defects propagate without downgrade.
    Caller owns the same round lock used for model/replay side effects.
    """
    import json
    from pathlib import Path
    from .adaptive_dispatch import (build_dispatch_plan, validate_dispatch_plan,
                                    generalist_fallback, DispatchUnavailable)
    from .locking import atomic_write
    from .state import canonical
    target = Path(directory) / 'dispatch-plan.json' if directory is not None else None
    if target is not None and target.exists():
        plan = json.loads(target.read_bytes())
        validate_dispatch_plan(plan, public, diagnosis)
    else:
        try:
            plan = build_dispatch_plan(public, diagnosis)
        except (DispatchUnavailable, TimeoutError):
            plan = generalist_fallback(public, reason='DISPATCH_UNAVAILABLE', diagnosis=diagnosis)
        validate_dispatch_plan(plan, public, diagnosis)
        if target is not None:
            atomic_write(target, canonical(plan), exclusive=True)
            # Verify durable bytes before any downstream call, including first use.
            if json.loads(target.read_bytes()) != plan:
                raise StageFailure('RECOVERY_ERROR', 'DISPATCH_DURABLE_ASSIGNMENT_CHANGED')
    if observer is not None:
        if plan['mode'] == 'FROZEN_GENERALIST_FALLBACK':
            observer.emit('DISPATCH_FALLBACK', {'round_id': public['round_id'],
                'dispatch_sha256': plan['dispatch_sha256'], 'reason': plan['fallback_reason'],
                'fallback_profile': plan['fallback_profile'], 'fallback_version': plan['fallback_version']},
                key='dispatch-fallback:' + public['round_id'])
        observer.emit('DISPATCH_ASSIGNED', {'round_id': public['round_id'],
            'dispatch_sha256': plan['dispatch_sha256'], 'catalog_sha256': plan['catalog_sha256'],
            'dispatcher_version': plan['dispatcher_version'], 'mode': plan['mode']},
            key='dispatch-assigned:' + public['round_id'])
        for assignment in plan['assignments']:
            observer.emit('DISPATCH_SLOT_ASSIGNED', {'round_id': public['round_id'],
                'dispatch_sha256': plan['dispatch_sha256'],
                **{k: assignment[k] for k in ('slot', 'task_id', 'task_type', 'task_contract_hash')}},
                key='dispatch-slot:' + public['round_id'] + ':' + assignment['slot'])
    return plan

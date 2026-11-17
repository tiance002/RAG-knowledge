"""Eighteen synthetic negative controls for v1 observation semantics.

No real Gold, model calls, or database constructors are used.
"""
from copy import deepcopy

from rsik.analytics.metrics import compute_metrics
from rsik.analytics.projections import project_sources


CONTEXT = dict(schema_version='rsi-observation/v1', experiment_id='synthetic-negative-controls',
               epoch='m1-a', role_profile='LEGACY', role_profile_hash='profile-a',
               evidence_mode='SYNTHETIC_CONTROL', compatibility_fingerprint='fp',
               evaluator_version='e1', profile='synthetic', round_id='round-a')


def source(*events, sid='observation'):
    return dict(source_id=sid, sha256='a' * 64, path='synthetic/' + sid,
                events=[dict(event_id=str(i), sequence=i, event_type=kind,
                             payload={**CONTEXT, **payload})
                        for i, (kind, payload) in enumerate(events, 1)])


def invocation(outcome='SUCCESS', contract='VALID', identity='call-a', origin='NONE'):
    common = dict(invocation_id=identity, attempt=0)
    events = [('INVOCATION_STARTED', common)]
    if outcome not in {'TIMEOUT', 'API_ERROR', 'INFRA_FAILURE'}:
        events.append(('INVOCATION_RESPONSE_RECEIVED', common))
    events.append(('INVOCATION_FINISHED', {**common, 'outcome': outcome,
                                         'contract_result': contract, 'failure_origin': origin}))
    return events


def result(events, mid):
    rows = project_sources([source(*events)])
    groups = compute_metrics(rows)['groups']
    assert len(groups) == 1
    return next(m for m in groups[0]['metrics'] if m['metric_id'] == mid)


def check(events, mid, numerator, denominator, status='OK'):
    metric = result(events, mid)
    assert (metric['numerator'], metric['denominator'], metric['status']) == (numerator, denominator, status)


def test_01_success_invalid_schema_is_failure():
    events = invocation(contract='INVALID', origin='CONTRACT')
    check(events, 'A1', 0, 1)
    check(events, 'A2', 0, 1)
    check(events, 'A4', 1, 1)


def test_02_abstention_is_not_api_failure():
    events = invocation('ABSTAINED', 'VALID')
    check(events, 'A3', 1, 1)
    check(events, 'A4', 0, 1)
    check(events, 'A1', 0, 1)


def test_03_timeout_is_attempt_without_response():
    events = invocation('TIMEOUT', 'NOT_EVALUATED', origin='TRANSPORT')
    check(events, 'A4', 1, 1)
    check(events, 'A2', 0, 0, 'N/A')


def test_04_duplicate_response_is_not_extra_call():
    events = invocation()
    events.insert(2, events[1])
    check(events, 'A1', 1, 1)
    check(events, 'A2', 1, 1)


def proposal(identity, **values):
    return ('PROPOSAL_GENERATED', dict(proposal_instance_id=identity, proposal_id='same-id',
                                        schema_valid=True, static_pass=True, **values))


def test_05_duplicate_slots_remain_instances():
    events = [proposal('slot-a', slot='a', ast_sha256='same'), proposal('slot-b', slot='b', ast_sha256='same')]
    check(events, 'P1', 2, 2)
    check(events, 'P4', 1, 2)


def test_06_same_proposal_id_different_patch_stays_distinct():
    events = [proposal('slot-a', patch_sha256='a', ast_sha256='a'),
              proposal('slot-b', patch_sha256='b', ast_sha256='b')]
    check(events, 'P3', 2, 2)
    rows = project_sources([source(*events)])
    assert compute_metrics(rows)['groups'][0]['portfolio']['patch_bytes']['unique_count'] == 2


def test_07_retrieved_is_not_injected():
    events = [('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='r', entry_ids=['e']))]
    check(events, 'M1', 1, None)
    check(events, 'M2', 0, 1)
    empty = [('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='empty', entry_ids=[], returned_count=0))]
    check(empty, 'M1', 0, None)
    check(empty, 'M2', 0, 0, 'N/A')


def test_08_injected_is_not_referenced():
    events = [('MEMORY_RETRIEVAL_COMPLETED', dict(retrieval_id='r', entry_ids=['e'])),
              ('MEMORY_INJECTED', dict(retrieval_id='r', entry_ids=['e']))]
    check(events, 'M2', 1, 1)
    check(events, 'M3', 0, 1)


def test_09_referenced_is_not_verified_association():
    events = [('MEMORY_INJECTED', dict(entry_ids=['e'])),
              ('PROPOSAL_REFERENCED', dict(entry_ids=['e'], proposal_instance_id='p')),
              ('MEMORY_USED', dict(entry_ids=['e'], proposal_instance_id='p'))]
    check(events, 'M3', 1, 1)
    check(events, 'M4', 0, 0, 'N/A')
    assessed = events + [('MEMORY_CO_RETRIEVED', dict(retrieval_id='r', pair_entry_ids=['e', 'f'], conflicting=True)),
                         ('MEMORY_FILTERED', dict(entry_id='f', reason='CONDITIONS_MISMATCH',
                                                  entry_fingerprint='old', current_fingerprint='new'))]
    check(assessed, 'M5', 1, 1)
    check(assessed, 'M6', 1, 1)
    coverage = compute_metrics(project_sources([source(*assessed)]))['groups'][0]['observation_coverage']
    assert coverage['condition_mismatch_count'] == coverage['conflicting_pair_count'] == coverage['stale_entry_count'] == 1



def test_10_parent_admitted_never_selected():
    events = [('PARENT_ELIGIBILITY_CHECKED', dict(selection_id='s', parent_id='p', admitted=True, eligible=True))]
    check(events, 'S3', None, None, 'UNKNOWN')


def test_11_parent_selected_then_interrupted_still_selection():
    events = [('PARENT_SELECTED', dict(selection_id='s', parent_id='p', admitted=True))]
    check(events, 'S3', 1, 1)
    check(events, 'S6', 1, 1)


def test_12_recovery_retries_are_attempts():
    events = [('RECOVERY_STARTED', dict(recovery_attempt_id='r1')),
              ('RECOVERY_FAILED', dict(recovery_attempt_id='r1')),
              ('RECOVERY_STARTED', dict(recovery_attempt_id='r2')),
              ('RECOVERY_COMPLETED', dict(recovery_attempt_id='r2'))]
    check(events, 'R2', 1, 2)
    blocked = [('RECOVERY_STARTED', dict(recovery_attempt_id='r3')),
               ('RECOVERY_BLOCKED', dict(recovery_attempt_id='r3', failure_category='EVIDENCE_INTEGRITY'))]
    check(events + blocked, 'R2', 1, 3)



def test_13_repeated_execution_not_observer_idempotence():
    events = [('EXECUTION_STARTED', dict(actual_execution_id='x1', action_id='a')),
              ('EXECUTION_STARTED', dict(actual_execution_id='x2', action_id='a'))]
    check(events, 'R3', 1, 2)
    log = source(*events)
    rows = project_sources([log, deepcopy(log)])
    metric = next(m for m in compute_metrics(rows)['groups'][0]['metrics'] if m['metric_id'] == 'R3')
    assert (metric['numerator'], metric['denominator'], metric['status']) == (1, 2, 'OK')


def test_14_missing_terminal_unknown_and_incomplete():
    events = [('INVOCATION_STARTED', dict(invocation_id='call-a', attempt=0))]
    check(events, 'A1', None, 1, 'UNKNOWN')
    check(events, 'R6', 1, 1)
    row = project_sources([source(*events)])[0]
    assert (row['payload']['expected_count'], row['payload']['known_count'], row['payload']['missing_count']) == (2, 1, 1)
    missing_stage = [('STAGE_STARTED', dict(stage_attempt_id='interrupted'))]
    check(missing_stage, 'R1', None, 1, 'UNKNOWN')
    check(missing_stage, 'R6', 1, 1)
    cancelled_stage = missing_stage + [('STAGE_CANCELLED', dict(stage_attempt_id='interrupted'))]
    check(cancelled_stage, 'R1', 0, 1)
    check(cancelled_stage, 'R6', 0, 1)



def test_15_infrastructure_failure_is_not_strategy_evidence():
    events = invocation('INFRA_FAILURE', 'NOT_EVALUATED', origin='INFRASTRUCTURE')
    check(events, 'R4', 1, 1)
    check(events, 'S1', None, None, 'UNKNOWN')
    check(events, 'P2', None, None, 'UNKNOWN')


def test_16_unknown_cost_is_not_zero_or_double_attempt():
    observed = source(*[(kind, {**body, 'cost_request_key': 'budget-key'}) for kind, body in invocation()])
    billing = source(('MODEL_REQUEST_STARTED', dict(request_key='budget-key')),
                     ('MODEL_REQUEST_COMPLETED', dict(request_key='budget-key', status='RECEIVED', usage_status='UNKNOWN')),
                     sid='billing')
    # Old payloads have no semantic schema marker.
    for event in billing['events']: event['payload'].pop('schema_version')
    rows = project_sources([observed, billing])
    assert len([r for r in rows if r['projection_type'] == 'AgentInvocation']) == 1
    call = next(r for r in rows if r['projection_type'] == 'AgentInvocation')
    assert len(call['source_refs']) == 2
    group = compute_metrics(rows)['groups'][0]
    cost = next(m for m in group['metrics'] if m['metric_id'] == 'C1')
    assert (cost['numerator'], cost['denominator'], cost['status']) == (None, None, 'UNKNOWN')
    assert group['cost_breakdown']['ledger_upper_nano'] is None
    assert group['cost_breakdown']['requests'] == 1


def test_17_role_profile_manifest_hash_isolation():
    a, b = source(*invocation(), sid='a'), source(*invocation(), sid='b')
    for event in b['events']: event['payload']['role_profile_hash'] = 'other-manifest'
    groups = compute_metrics(project_sources([a, b]))['groups']
    assert len(groups) == 2
    assert all(next(m for m in g['metrics'] if m['metric_id'] == 'A1')['denominator'] == 1 for g in groups)


def test_18_evaluator_isolation_and_stage_lifecycle():
    a = source(('STAGE_STARTED', dict(stage_attempt_id='s')),
               ('STAGE_FAILED', dict(stage_attempt_id='s')), sid='a')
    b = source(('STAGE_STARTED', dict(stage_attempt_id='s')),
               ('STAGE_COMPLETED', dict(stage_attempt_id='s')), sid='b')
    for event in b['events']: event['payload']['evaluator_version'] = 'e2'
    groups = compute_metrics(project_sources([a, b]))['groups']
    assert len(groups) == 2
    metrics = [next(m for m in g['metrics'] if m['metric_id'] == 'R1') for g in groups]
    assert sorted((m['numerator'], m['denominator'], m['status']) for m in metrics) == [(0, 1, 'OK'), (1, 1, 'OK')]

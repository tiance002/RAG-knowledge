"""Synthetic controls for deterministic, conservative RSI statistics."""
import json
import pytest

from rsik.analytics.metrics import compute_metrics
from rsik.analytics.portfolio import analyze_portfolio
from rsik.analytics.schemas import METRICS


DIMS = dict(experiment_id='synthetic', epoch='v1', role_profile='LEGACY', evidence_mode='SYNTHETIC_CONTROL',
            role_profile_sha256='profilehash', compatibility_fingerprint='fp', evaluator_version='e1', profile='fixture')


def row(kind, identity, **payload):
    return dict(projection_type=kind, id=identity, dimensions=dict(DIMS), payload=payload,
                event_ids=[identity], source_refs=[])


def metric(records, mid):
    return next(item for item in compute_metrics(records)['groups'][0]['metrics'] if item['metric_id'] == mid)


def test_all_schema_fields():
    assert len(METRICS) == 31
    assert all({'metric_id', 'description', 'numerator', 'denominator', 'unit', 'aggregation_level',
                'evidence_source', 'eligibility_rule', 'unknown_policy', 'compatibility_scope'} <= set(v) for v in METRICS.values())


def test_duplicate_event_is_not_duplicate_attempt():
    item = row('AgentInvocation', 'a', attempted=True, received=True, success=True, schema_valid=True,
               abstained=False, failed=False, failure_origin='NONE')
    assert metric([item, item], 'A1')['denominator'] == 1
    assert metric([item, item], 'A1')['value'] == 1


def test_conflicting_duplicate_fails():
    with pytest.raises(ValueError, match='CONFLICTING'):
        compute_metrics([row('Proposal', 'p', schema_valid=True), row('Proposal', 'p', schema_valid=False)])


def test_missing_measurement_is_unknown():
    assert metric([row('Experiment', 'e')], 'A1')['status'] == 'UNKNOWN'
    assert metric([row('AgentInvocation', 'a', attempted=True, received=True)], 'A1')['value'] is None


def test_modes_and_versions_partition():
    first = row('Experiment', 'a')
    second = row('Experiment', 'b')
    second['dimensions']['evidence_mode'] = 'REAL_REPLAY'
    third = row('Experiment', 'c')
    third['dimensions']['evaluator_version'] = 'e2'
    assert len(compute_metrics([first, second, third])['groups']) == 3


def test_holdout_absent_na_and_adoption_not_verification():
    records = [row('Candidate', 'c', adopted=True)]
    assert metric(records, 'S2')['status'] == 'N/A'
    assert metric(records, 'C4')['status'] == 'N/A'


def test_mirrored_holdout_counts_candidate_once():
    records = [row('Holdout', 'h1', candidate_id='c', completed=True, independent=True, verdict='VERIFIED', improvement=True),
               row('Holdout', 'h2', candidate_id='c', completed=True, independent=True, verdict='VERIFIED', improvement=True)]
    assert metric(records, 'S2')['denominator'] == 1


def test_unknown_cost_never_zero_or_invoice():
    records = [row('CostEvent', 'k', request_key='k', usage_status='UNKNOWN', cost_nano_upper=None)]
    group = compute_metrics(records)['groups'][0]
    assert metric(records, 'C1')['status'] == 'UNKNOWN'
    assert group['cost_breakdown']['ledger_upper_nano'] is None
    assert group['cost_breakdown']['unknown_usage_requests'] == ['k']
    assert metric(records, 'C4')['status'] == 'N/A'


def test_bound_keeps_unknown_usage_and_failed_cost():
    records = [row('CostEvent', 'k', request_key='k', usage_status='UNKNOWN', cost_nano_upper=100,
                   transport_status='UNKNOWN', bounded=True)]
    summary = compute_metrics(records)['groups'][0]['cost_breakdown']
    assert summary['ledger_upper_nano'] == 100
    assert summary['provider_invoiced_nano'] is None
    assert summary['failed_request_upper_nano'] == 0
    assert summary['failed_request_count'] == 0
    assert summary['uncertain_request_upper_nano'] == 100
    assert summary['unknown_usage_upper_nano'] == 100
    assert summary['known_usage_request_count'] == 0
    assert summary['known_usage_subtotal']['input_tokens']['tokens'] is None
    assert summary['total_failure_count_status'] == 'UNKNOWN'
    assert summary['status'] == 'BOUNDED_USAGE_ESTIMATE'


def test_invoice_cost_is_exact():
    records = [row('CostEvent', 'k', request_key='k', usage_status='KNOWN', cost_nano_upper=300,
                   provider_invoiced_nano=100000001), row('Proposal', 'p', schema_valid=True, static_pass=True)]
    assert metric(records, 'C1')['value'] == '0.100000001'
    assert metric(records, 'C2')['value'] == '0.100000001'


def test_incomplete_round_never_completed_replay():
    records = [row('FamilyDecision', 'f', round_id='r', complete=False, family_size=3, candidate_decisions=[]),
               row('Replay', 'x', round_id='r', candidate_id='c', entered=True, completed=True, paired=True)]
    assert metric(records, 'P5')['status'] == 'UNKNOWN'


def test_complete_three_slot_family_and_infrastructure_separation():
    decisions = [dict(angle='P1_DEDUP', candidate_id='c1', verdict='VALIDATED'),
                 dict(angle='P2_BUDGET', candidate_id='c2', verdict='REJECTED'),
                 dict(angle='P3_ORDER', candidate_id=None, verdict='INCONCLUSIVE')]
    records = [row('FamilyDecision', 'f', round_id='r', complete=True, family_size=3, candidate_decisions=decisions, adopted_candidate_id='c1'),
               row('Replay', 'r1', round_id='r', candidate_id='c1', entered=True, completed=True, paired=True),
               row('Replay', 'r2', round_id='r', candidate_id='c2', entered=True, completed=False, paired=False, failure_origin='INFRASTRUCTURE')]
    assert metric(records, 'P5')['value'] == 0.5
    assert metric(records, 'S1')['denominator'] == 2


def test_portfolio_keeps_duplicate_slots_and_distinct_equivalences():
    records = [row('Proposal', 'p1', proposal_id='same', slot='P1', patch='x = 1', full_candidate_source=True, schema_valid=True, static_pass=True),
               row('Proposal', 'p2', proposal_id='same', slot='P2', patch='x=1', full_candidate_source=True, schema_valid=True, static_pass=True)]
    portfolio = analyze_portfolio(records)
    assert portfolio['proposal_instances'] == 2
    assert portfolio['patch_bytes']['unique_count'] == 2
    assert portfolio['ast_structure']['unique_count'] == 1
    assert portfolio['hypothesis']['unique_count'] is None
    assert metric(records, 'P4')['value'] == 0.5


def test_matched_is_not_raw_retrieval():
    records = [row('MemoryEvent', 'm', event_type='EXPERIENCE_MATCHED', round_id='r', entry_id='e'),
               row('MemoryEvent', 'p', event_type='PROPOSAL_REFERENCED', round_id='r', entry_id='e')]
    assert metric(records, 'M1')['status'] == 'UNKNOWN'
    assert metric(records, 'M2')['status'] == 'UNKNOWN'
    assert metric(records, 'M3')['value'] == 1
    assert metric(records, 'M5')['status'] == 'UNKNOWN'


def test_rollback_removes_current_promotion():
    records = [row('Promotion', 'p', status='PROMOTED', operation_id='p', candidate_id='c'),
               row('Promotion', 'z', status='ROLLED_BACK', operation_id='r', rolled_back_candidate_id='c')]
    assert compute_metrics(records)['groups'][0]['current_promoted_candidates'] == []
    assert metric(records, 'R5')['value'] == 1


def test_deterministic_json_and_no_source_text():
    records = [row('Proposal', 'p', patch='x=1', hypothesis='private natural language')]
    one = json.dumps(compute_metrics(records), sort_keys=True)
    assert one == json.dumps(compute_metrics(records), sort_keys=True)
    assert 'private natural language' not in one
    assert 'x=1' not in one


def test_patch_is_parsed_without_execution(tmp_path):
    target = tmp_path / 'should-not-exist'
    patch = f"open({str(target)!r}, 'w').write('executed')"
    assert analyze_portfolio([row('Proposal', 'p', patch=patch, full_candidate_source=True)])['ast_structure']['unique_count'] == 1
    assert not target.exists()


def test_conflicting_verification_unknown():
    records = [row('Holdout', 'h1', candidate_id='c', completed=True, independent=True, verdict='VERIFIED'),
               row('Holdout', 'h2', candidate_id='c', completed=True, independent=True, verdict='REGRESSION_SUSPECTED')]
    assert metric(records, 'S2')['status'] == 'UNKNOWN'


def test_parent_admission_is_not_selection_or_survival():
    records = [row('Parent', 'p', candidate_id='c', event_type='PARENT_ADMITTED')]
    for mid in ('S3', 'S4', 'S5', 'S6'):
        assert metric(records, mid)['status'] == 'UNKNOWN'


def test_replay_unknown_is_not_failed_or_zero():
    decisions = [dict(angle=slot, candidate_id=f'c{i}', verdict='REJECTED')
                 for i, slot in enumerate(('P1_DEDUP', 'P2_BUDGET', 'P3_ORDER'))]
    records = [row('FamilyDecision', 'f', round_id='r', complete=True, family_size=3, candidate_decisions=decisions),
               row('Replay', 'x', round_id='r', candidate_id='c0', entered=True, completed='UNKNOWN', paired='UNKNOWN')]
    assert metric(records, 'P5')['status'] == 'UNKNOWN'
    assert metric(records, 'P5')['numerator'] is None


def test_empty_portfolio_and_replacement_scope_unknown():
    assert analyze_portfolio([])['proposal_instances'] is None
    records = [row('Proposal', 'a', patch={'old': 'first', 'new': 'x=1'}),
               row('Proposal', 'b', patch={'old': 'second', 'new': 'x=1'})]
    result = analyze_portfolio(records)
    assert result['patch_bytes']['unique_count'] == 2
    assert result['ast_structure']['unique_count'] is None


def test_portfolio_stays_partitioned():
    first = row('Proposal', 'p1', patch='x=1')
    second = row('Proposal', 'p2', patch='x=1')
    second['dimensions']['compatibility_fingerprint'] = 'different'
    result = compute_metrics([first, second])
    assert result['portfolio']['status'] == 'PARTITIONED'
    assert all(group['portfolio']['proposal_instances'] == 1 for group in result['groups'])


def test_cost_local_request_keys_are_scoped_to_log():
    first = row('CostEvent', 'log1:k', request_key='k', usage_status='KNOWN', cost_nano_upper=10)
    second = row('CostEvent', 'log2:k', request_key='k', usage_status='KNOWN', cost_nano_upper=20)
    first['source_refs'] = [{'source_id': 'log1'}]
    second['source_refs'] = [{'source_id': 'log2'}]
    summary = compute_metrics([first, second])['groups'][0]['cost_breakdown']
    assert summary['requests'] == 2
    assert summary['ledger_upper_nano'] == 30
    mirror = {**first, 'id': 'mirror'}
    assert compute_metrics([first, mirror])['groups'][0]['cost_breakdown']['requests'] == 1
    changed = {**mirror, 'payload': {**mirror['payload'], 'cost_nano_upper': 999}}
    with pytest.raises(ValueError, match='CONFLICTING_COST_REQUEST_IDENTITY'):
        compute_metrics([first, changed])


def test_pending_unknown_transport_is_not_confirmed_failed_cost():
    records = [row('CostEvent', 'k', request_key='k', pending=True, usage_status='UNKNOWN',
                   transport_status='UNKNOWN', cost_nano_upper=None)]
    summary = compute_metrics(records)['groups'][0]['cost_breakdown']
    assert summary['failed_request_count'] == 0
    assert summary['uncertain_request_keys'] == ['k']
    assert summary['uncertain_request_upper_nano'] is None
    assert summary['ledger_upper_nano'] is None
    confirmed = row('CostEvent', 'failure', request_key='failure', pending=False,
                    transport_status='HTTP_ERROR', cost_nano_upper=42)
    summary = compute_metrics([confirmed])['groups'][0]['cost_breakdown']
    assert summary['failed_request_count'] == 1
    assert summary['failed_request_upper_nano'] == 42


def test_verified_improvement_missing_is_unknown_instead_of_zero():
    invoice = row('CostEvent', 'cost', request_key='cost', provider_invoiced_nano=10)
    holdout = row('Holdout', 'h', candidate_id='c', completed=True, independent=True, verdict='VERIFIED')
    assert metric([holdout], 'C4')['status'] == 'UNKNOWN'
    assert metric([invoice, holdout], 'C4')['status'] == 'UNKNOWN'
    assert metric([invoice, holdout], 'C4')['denominator'] is None
    holdout['payload']['improvement'] = False
    assert metric([invoice, holdout], 'C4')['status'] == 'N/A'
    holdout['payload']['improvement'] = True
    assert metric([holdout], 'C4')['status'] == 'UNKNOWN'
    assert metric([invoice, holdout], 'C4')['value'] == '1E-8'


@pytest.mark.parametrize('problem', ['missing_round', 'duplicate_slots', 'aborted', 'duplicate_enabled_slots'])
def test_family_requires_known_round_and_exact_three_slots(problem):
    decisions = [dict(angle=slot, candidate_id=f'c{i}', verdict='REJECTED')
                 for i, slot in enumerate(('P1_DEDUP', 'P2_BUDGET', 'P3_ORDER'))]
    family = row('FamilyDecision', 'f', round_id='r', complete=True, family_size=3,
                 candidate_decisions=decisions, verdict='NO_ADOPTION')
    replay = row('Replay', 'x', round_id='r', candidate_id='c0', entered=True, completed=True, paired=True)
    if problem == 'missing_round':
        family['payload'].pop('round_id')
        replay['payload'].pop('round_id')
    elif problem == 'duplicate_slots':
        family['payload']['candidate_decisions'][2]['angle'] = 'P1_DEDUP'
    elif problem == 'aborted':
        family['payload']['verdict'] = 'ROUND_ABORTED'
    else:
        family['payload']['enabled_slots'] = ['P1_DEDUP', 'P1_DEDUP']
    assert metric([family, replay], 'P5')['status'] == 'UNKNOWN'
    assert metric([family, replay], 'S1')['status'] == 'UNKNOWN'


def test_empty_cost_observation_is_unknown():
    summary = compute_metrics([row('Experiment', 'e')])['groups'][0]['cost_breakdown']
    assert summary['requests'] is None
    assert summary['observed_request_lower_bound'] == 0
    assert summary['ledger_upper_nano'] is None


def test_current_promotion_tracks_one_pointer_per_history():
    first = row('Promotion', 'one:a', status='PROMOTED', candidate_id='a', history_source_id='one', history_environment='sandbox', sequence=1)
    second = row('Promotion', 'one:b', status='PROMOTED', candidate_id='b', history_source_id='one', history_environment='sandbox', sequence=2)
    other = row('Promotion', 'two:b', status='PROMOTED', candidate_id='b', history_source_id='two', history_environment='sandbox', sequence=1)
    rollback = row('Promotion', 'one:rollback', status='ROLLED_BACK', rolled_back_candidate_id='b', candidate_id='a',
                   history_source_id='one', history_environment='sandbox', sequence=3)
    assert compute_metrics([first, second])['groups'][0]['current_promoted_candidates'] == ['b']
    group = compute_metrics([first, second, other, rollback])['groups'][0]
    assert group['current_promoted_candidates'] == ['a', 'b']
    assert {h['source_id']: h['candidate_id'] for h in group['current_promoted_histories']} == {'one': 'a', 'two': 'b'}


@pytest.mark.parametrize('attempted', [None, 'UNKNOWN'])
def test_infrastructure_rate_requires_known_attempt_eligibility(attempted):
    records = [row('AgentInvocation', 'a', attempted=attempted, failure_origin='INFRASTRUCTURE')]
    result = metric(records, 'R4')
    assert result['status'] == 'UNKNOWN'
    assert result['numerator'] is None and result['denominator'] is None


@pytest.mark.parametrize('entered', [None, 'UNKNOWN'])
def test_replay_rate_requires_known_entry_eligibility(entered):
    decisions = [dict(angle=slot, candidate_id=f'c{i}', verdict='REJECTED')
                 for i, slot in enumerate(('P1_DEDUP', 'P2_BUDGET', 'P3_ORDER'))]
    family = row('FamilyDecision', 'f', round_id='r', complete=True, family_size=3,
                 candidate_decisions=decisions, verdict='NO_ADOPTION')
    replay = row('Replay', 'x', round_id='r', candidate_id='c0', entered=entered, completed=True, paired=True)
    result = metric([family, replay], 'P5')
    assert result['status'] == 'UNKNOWN'
    assert result['numerator'] is None and result['denominator'] is None

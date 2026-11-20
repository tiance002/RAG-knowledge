"""Pure scheduling tests use in-memory audited-decision fixtures only."""
from copy import deepcopy

import pytest

from rsik.objectives import METRIC_CONTRACTS
from rsik.query_parent_pool import measure_parent, select_parent, validate_parent_selection


def profile(primary='context_chars', secondary=(), constraints=(), tie_breakers=()):
    metrics = {primary, *secondary, *tie_breakers, *(item['metric_id'] for item in constraints)}
    return {'objective_profile_id': 'parent-test', 'version': 1, 'description': 'parent scheduler test',
            'primary_metric': primary, 'direction': METRIC_CONTRACTS[primary]['direction'],
            'secondary_metrics': list(secondary), 'tie_breakers': list(tie_breakers),
            'components': ['context_selection'], 'hard_constraints': list(constraints),
            'required_metric_contracts': {metric: METRIC_CONTRACTS[metric]['metric_contract_version']
                                          for metric in metrics}}


def policy(mode='latest', seed=0):
    return {'schema': 'rsi-query-parent-selection/v1', 'mode': mode, 'seed': seed}


def decision(chars=50, qid='q1'):
    return {'schema_version': 'rsi-candidate-decision/v2.2', 'eligible_count': 1,
            'per_question': [{'qid': qid, 'chars_A': 100, 'chars_B': chars,
                              'pair_status': 'PAIRED', 'pair_identity': True, 'execution': 'COMPLETED',
                              'parent_facts': {'fact': 'PRESENT'}, 'candidate_facts': {'fact': 'PRESENT'},
                              'provenance_A': 'PRESENT', 'provenance_B': 'PRESENT',
                              'upstream_hash': 'frozen-' + qid, 'execution_environment_hash': 'runtime'}]}


def record(candidate='root', round_number=0, chars=100, kind='QUALIFIED_ROOT', cohort='same'):
    return {'candidate_id': candidate, 'kind': kind, 'admission_round': round_number,
            'metrics': {'context_chars': chars, 'candidate_chars': chars,
                        'necessary_fact_status': 'PASS', 'provenance_status': 'PASS'}, 'cohort_id': cohort}


@pytest.mark.parametrize('mode', ['latest', 'all_eligible', 'pareto', 'seeded_multi_branch'])
def test_valid_modes_and_profile_requirement(mode):
    assert validate_parent_selection(policy(mode), profile()) == policy(mode)
    with pytest.raises(ValueError):
        validate_parent_selection(policy(mode), None)
    assert validate_parent_selection(None, None) is None


@pytest.mark.parametrize('mutation', [dict(seed=True), dict(seed=-1), dict(seed=1.2),
                                      dict(schema='wrong'), dict(mode='random'), dict(mode=[]), dict(extra=1)])
def test_strict_policy_fields(mutation):
    with pytest.raises(ValueError):
        validate_parent_selection({**policy(), **mutation}, profile())


def test_absolute_primary_alias_and_duplicate_alias_rejection():
    assert validate_parent_selection(policy(), profile('candidate_chars')) == policy()
    with pytest.raises(ValueError, match='ABSOLUTE'):
        validate_parent_selection(policy(), profile('shorter_cases'))
    with pytest.raises(ValueError, match='DUPLICATE_METRIC_ALIAS'):
        validate_parent_selection(policy(), profile(secondary=['candidate_chars']))
    item = record()
    item['metrics'] = {'candidate_chars': 23}
    assert select_parent([item], policy(), profile())['selected_candidate_id'] == 'root'
    item['metrics']['context_chars'] = 24
    assert select_parent([item], policy(), profile())['selected_candidate_id'] is None


def test_primary_only_characters_all_suite_fact_coverage_and_cohort():
    measured = measure_parent([decision(20), decision(700, 'aux')])
    assert measured['metrics']['context_chars'] == measured['metrics']['candidate_chars'] == 20
    assert measured['metrics']['necessary_fact_status'] == 'PASS'
    assert measured['metrics']['shorter_cases'] is None
    assert measured['cohort_id'] == measure_parent([decision(80)])['cohort_id']
    assert measure_parent([decision()], arm='A')['metrics']['context_chars'] == 100
    auxiliary = decision(700, 'aux')
    auxiliary['per_question'][0]['candidate_facts']['fact'] = 'UNKNOWN'
    assert measure_parent([decision(), auxiliary])['metrics']['necessary_fact_status'] == 'UNKNOWN'
    auxiliary['per_question'][0]['candidate_facts']['fact'] = 'ABSENT'
    assert measure_parent([decision(), auxiliary])['metrics']['necessary_fact_status'] == 'FAIL'
    changed = decision()
    changed['per_question'][0]['execution_environment_hash'] = 'other'
    assert measure_parent([changed])['cohort_id'] != measured['cohort_id']


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'failed', 'identity'])
def test_incomplete_measurements_stay_unknown(fault):
    item = decision()
    if fault == 'missing':
        item['per_question'] = []
    elif fault == 'duplicate':
        item['per_question'] *= 2
        item['eligible_count'] = 2
    elif fault == 'failed':
        item['per_question'][0]['execution'] = 'FAILED'
    else:
        item['per_question'][0]['pair_identity'] = False
    result = measure_parent([item])
    assert result['metrics']['context_chars'] is None
    assert result['metrics']['necessary_fact_status'] == 'UNKNOWN'
    assert result['cohort_id'] is None


def test_unknown_fact_identity_and_missing_environment():
    item = decision()
    item['per_question'][0]['parent_facts']['unobserved'] = 'PRESENT'
    item['per_question'][0].pop('execution_environment_hash')
    measured = measure_parent([item])
    assert measured['metrics']['necessary_fact_status'] == 'UNKNOWN'
    assert measured['metrics']['context_chars'] == 50
    assert measured['cohort_id'] is None


def test_latest_requires_eligibility_and_keeps_tradeoff_history():
    items = [record(), record('trained', 1, 90, 'TRAINING_EXPLORATION'),
             record('tradeoff', 2, 10, 'TRADEOFF'), record('bad', 3, None, 'TRAINING_EXPLORATION')]
    result = select_parent(items, policy(), profile())
    assert result['selected_candidate_id'] == 'trained'
    assert len(result['population']) == 4
    assert 'PARENT_KIND_NOT_ELIGIBLE' in result['population'][2]['reason_codes']
    assert 'PRIMARY_METRIC_UNKNOWN' in result['population'][3]['reason_codes']
    assert items[0].get('eligible') is None


def test_all_eligible_balances_selection_count_then_admission():
    items = [record(), record('branch', 1, 90, 'TRAINING_EXPLORATION')]
    assert select_parent(items, policy('all_eligible'), profile())['selected_candidate_id'] == 'root'
    assert select_parent(items, policy('all_eligible'), profile(), ['root'])['selected_candidate_id'] == 'branch'
    assert select_parent(items[::-1], policy('all_eligible'), profile(), ['root'])['selected_candidate_id'] == 'branch'


def test_seeded_schedule_is_deterministic_balanced_and_input_order_independent():
    items = [record(str(index), index) for index in range(5)]
    history = []
    for _ in range(5):
        selected = select_parent(items, policy('seeded_multi_branch', 23), profile(), history)
        assert selected['selected_candidate_id'] == select_parent(items[::-1], policy('seeded_multi_branch', 23),
                                                                  profile(), history)['selected_candidate_id']
        history.append(selected['selected_candidate_id'])
    assert len(set(history)) == 5


def test_hard_constraints_fail_unknown_and_group_unknown_are_never_pass():
    constraint = {'metric_id': 'necessary_fact_status', 'operator': '==', 'value': 'PASS'}
    items = [record(), record('fail', 1), record('unknown', 2)]
    items[1]['metrics']['necessary_fact_status'] = 'FAIL'
    items[2]['metrics']['necessary_fact_status'] = 'UNKNOWN'
    result = select_parent(items, policy(), profile(constraints=[constraint]))
    assert result['selected_candidate_id'] == 'root'
    assert [row['constraint_status'] for row in result['population']] == ['PASS', 'FAIL', 'UNKNOWN']
    numeric = {'metric_id': 'context_chars', 'operator': '<=', 'value': 90}
    assert select_parent([record()], policy(), profile(constraints=[numeric]))['selected_candidate_id'] is None


def test_pareto_dominance_requires_matching_known_cohort_and_absolute_axes():
    items = [record('large', 0, 100), record('small', 1, 80), record('other', 2, 10, cohort='other'),
             record('unknown', 3, 5, cohort=None)]
    result = select_parent(items, policy('pareto'), profile())
    rows = {row['candidate_id']: row for row in result['population']}
    assert rows['large']['dominated_by'] == ['small']
    assert rows['small']['pareto_status'] == rows['other']['pareto_status'] == 'FRONTIER'
    assert rows['unknown']['pareto_status'] == 'UNKNOWN'
    assert set(result['pareto_candidate_ids']) == {'small', 'other'}
    for item in items:
        item['metrics']['shorter_cases'] = 500 if item['candidate_id'] == 'large' else 0
    relative = select_parent(items, policy('pareto'), profile(secondary=['shorter_cases']))
    assert relative['pareto_candidate_ids'] == result['pareto_candidate_ids']


def test_optional_secondary_unknown_never_becomes_numeric_or_claims_frontier():
    items = [record('a'), record('b', 1, 80)]
    result = select_parent(items, policy('pareto'), profile(secondary=['answer_tokens']))
    assert result['selected_candidate_id'] == 'b'
    assert result['pareto_candidate_ids'] == []
    assert all(row['pareto_status'] == 'UNKNOWN' for row in result['population'])


def test_pareto_does_not_order_numeric_values_across_cohorts():
    items = [record('a', 0, 900, cohort='older'), record('b', 1, 1, cohort='newer'),
             record('c', 2, 0, cohort=None)]
    assert select_parent(items, policy('pareto'), profile())['selected_candidate_id'] == 'a'
    assert select_parent(items, policy('pareto'), profile(), ['a'])['selected_candidate_id'] == 'b'
    assert select_parent(items, policy('pareto'), profile(), ['a', 'b'])['selected_candidate_id'] == 'c'


def test_explicit_tie_breakers_and_duplicate_ids():
    items = [record('a'), record('b', 1)]
    items[0]['metrics']['answer_tokens'] = 100
    items[1]['metrics']['answer_tokens'] = 90
    result = select_parent(items, policy('pareto'), profile(tie_breakers=['answer_tokens']))
    assert result['selected_candidate_id'] == 'b'
    with pytest.raises(ValueError, match='DUPLICATE'):
        select_parent([items[0], deepcopy(items[0])], policy(), profile())
    assert select_parent([], policy(), profile())['selected_candidate_id'] is None

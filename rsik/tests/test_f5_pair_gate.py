"""Synthetic decision-core checks; these are not live F5 experiment results."""

from copy import deepcopy

import pytest

from rsik.f5_pair_gate import adjudicate_pairs


def objective(**changes):
    result = {'metric': 'complete_correct', 'direction': 'maximize',
              'minimum_effect': 0.05, 'maximum_grade_regression_rate': 0,
              'critical_strata': ['critical'], 'protected_metrics': {},
              'accept_quality_tradeoff': False}
    result.update(changes)
    return result


def panel(size=60):
    return [{'qid': f'q{index}', 'strata': ['critical'] if index < 10 else [],
             'parent_grade': 'CORE_ONLY', 'candidate_grade': 'COMPLETE_CORRECT',
             'parent_metrics': {'answer_input_tokens': 100, 'latency': 10},
             'candidate_metrics': {'answer_input_tokens': 90, 'latency': 9}}
            for index in range(size)]


def decide(rows=None, phase='DEV60', **changes):
    return adjudicate_pairs(phase=phase, rows=panel() if rows is None else rows,
                            objective=objective(**changes))


@pytest.mark.parametrize('size,required,decision', [(20, 18, 'ADVANCE'),
    (60, 54, 'ADVANCE'), (200, 190, 'ADMITTED')])
def test_valid_pair_threshold_exact_boundary(size, required, decision):
    rows = panel(size)
    for row in rows[required:]:
        row['candidate_grade'] = None
    result = decide(rows, f'DEV{size}')
    assert result['decision'] == decision
    assert result['valid_pairs'] == required
    assert result['unknown_pairs'] == size - required
    assert result['primary_effect']['paired_count'] == required
    rows[required - 1]['parent_grade'] = None
    result = decide(rows, f'DEV{size}')
    assert result['decision'] == 'INCONCLUSIVE'
    assert 'INSUFFICIENT_VALID_PAIRS' in result['reason_codes']


def test_full_frozen_panel_required_and_extra_rows_rejected():
    result = decide(panel(59))
    assert result['decision'] == 'INCONCLUSIVE'
    assert result['missing_pairs'] == 1
    assert 'INCOMPLETE_PANEL' in result['reason_codes']
    with pytest.raises(ValueError, match='panel size'):
        decide(panel(61))


def test_unknown_is_retained_without_zero_imputation():
    rows = panel()
    rows[59]['parent_grade'] = None
    rows[59]['candidate_metrics'] = {}
    result = decide(rows)
    assert result['valid_pairs'] == 59
    assert result['unknown_qids'] == ['q59']
    assert result['primary_effect']['parent_mean'] == 0
    assert result['primary_effect']['candidate_mean'] == 1
    assert result['grade_wins'] == 59
    assert result['grade_losses'] == result['grade_ties'] == 0
    assert result['decision'] == 'ADVANCE'


def test_all_unknown_and_empty_panel_are_inconclusive():
    rows = panel()
    for row in rows:
        row['parent_grade'] = row['candidate_grade'] = None
    for result in (decide(rows), decide([])):
        assert result['decision'] == 'INCONCLUSIVE'
        assert result['valid_pairs'] == 0
        assert result['grade_regression_rate'] is None
        assert result['primary_effect']['improvement'] is None


def test_each_explicit_critical_stratum_needs_ten_valid_pairs():
    rows = panel()
    for row in rows[10:20]:
        row['strata'] = ['other']
    assert decide(rows, critical_strata=['critical', 'other'])['decision'] == 'ADVANCE'
    rows[19]['candidate_grade'] = None
    result = decide(rows, critical_strata=['critical', 'other'])
    assert result['critical_strata_pairs'] == {'critical': 10, 'other': 9}
    assert result['decision'] == 'INCONCLUSIVE'
    assert 'INSUFFICIENT_CRITICAL_STRATUM:other' in result['reason_codes']


def test_missing_primary_metric_is_inconclusive_even_with_enough_other_measurements():
    rows = panel()
    del rows[59]['candidate_metrics']['answer_input_tokens']
    result = decide(rows, metric='answer_input_tokens', direction='minimize', minimum_effect=10)
    assert result['decision'] == 'INCONCLUSIVE'
    assert result['primary_effect']['paired_count'] == 59
    assert result['primary_effect']['required_count'] == 60
    assert 'PRIMARY_METRIC_INCOMPLETE' in result['reason_codes']


def test_primary_metric_direction_and_raw_threshold_boundary():
    result = decide(metric='answer_input_tokens', direction='minimize', minimum_effect=10)
    assert result['decision'] == 'ADVANCE'
    assert result['primary_effect']['raw_difference'] == -10
    assert result['primary_effect']['improvement'] == 10
    assert result['primary_effect']['paired_bootstrap_ci95'] == pytest.approx([-10, -10])
    assert decide(metric='answer_input_tokens', direction='minimize', minimum_effect=10.01)['decision'] == 'REJECTED'
    assert decide(metric='answer_input_tokens', direction='maximize')['decision'] == 'REJECTED'


@pytest.mark.parametrize('direction,parent,candidate', [('maximize', 10, 8), ('minimize', 8, 10)])
def test_protected_metric_regression_limit_and_measurement(direction, parent, candidate):
    rows = panel()
    for row in rows:
        row['parent_metrics']['latency'] = parent
        row['candidate_metrics']['latency'] = candidate
    rule = {'latency': {'direction': direction, 'maximum_regression': 2}}
    assert decide(rows, protected_metrics=rule)['decision'] == 'ADVANCE'
    rule['latency']['maximum_regression'] = 1.99
    result = decide(rows, protected_metrics=rule)
    assert result['decision'] == 'REJECTED'
    assert 'PROTECTED_METRIC_REGRESSION:latency' in result['reason_codes']
    del rows[0]['parent_metrics']['latency']
    result = decide(rows, protected_metrics=rule)
    assert result['decision'] == 'INCONCLUSIVE'
    assert 'PROTECTED_METRIC_INCOMPLETE:latency' in result['reason_codes']


def test_quality_tradeoff_requires_acceptance_and_cannot_bypass_regression_cap():
    rows = panel()
    for row in rows:
        row['parent_grade'] = 'COMPLETE_CORRECT'
        row['candidate_grade'] = 'COMPLETE_CORRECT'
    for row in rows[:6]:
        row['candidate_grade'] = 'CORE_ONLY'
    settings = {'metric': 'answer_input_tokens', 'direction': 'minimize',
                'minimum_effect': 10, 'maximum_grade_regression_rate': 0.1}
    result = decide(rows, **settings)
    assert result['decision'] == 'REJECTED'
    assert result['grade_losses'] == 6
    assert result['grade_regression_rate'] == 0.1
    assert result['reason_codes'] == ['QUALITY_TRADEOFF_NOT_ACCEPTED']
    assert decide(rows, **settings, accept_quality_tradeoff=True)['decision'] == 'ADVANCE'
    settings['maximum_grade_regression_rate'] = 0.099
    result = decide(rows, **settings, accept_quality_tradeoff=True)
    assert result['decision'] == 'REJECTED'
    assert result['reason_codes'] == ['GRADE_REGRESSION_RATE_EXCEEDED']


def test_paired_quality_effects_and_exact_mcnemar_are_informational():
    rows = panel()
    for row in rows[3:]:
        row['candidate_grade'] = 'CORE_ONLY'
    result = decide(rows)
    assert result['decision'] == 'ADVANCE'  # 3/60 meets 0.05 despite p=0.25.
    complete = result['quality_effects']['complete_correct']
    assert complete['paired_wins'] == 3
    assert complete['paired_losses'] == 0
    assert complete['mcnemar_exact_two_sided_p'] == 0.25
    assert complete['improvement'] == 0.05
    core = result['quality_effects']['core_correct']
    assert core['parent_mean'] == core['candidate_mean'] == 1
    assert core['mcnemar_exact_two_sided_p'] == 1
    assert decide(rows) == result


def test_derived_core_metric_and_protection_use_grades():
    rows = panel()
    for row in rows:
        row['parent_grade'] = 'INCORRECT'
        row['candidate_grade'] = 'CORE_ONLY'
    result = decide(rows, metric='core_correct', protected_metrics={
        'complete_correct': {'direction': 'maximize', 'maximum_regression': 0}})
    assert result['decision'] == 'ADVANCE'
    assert result['primary_effect']['improvement'] == 1
    assert result['protected_effects']['complete_correct']['improvement'] == 0


@pytest.mark.parametrize('changes', [
    {'minimum_effect': 0}, {'minimum_effect': -1}, {'minimum_effect': True},
    {'minimum_effect': float('nan')}, {'direction': 'less'}, {'direction': []},
    {'maximum_grade_regression_rate': 1.01}, {'maximum_grade_regression_rate': -0.1},
    {'accept_quality_tradeoff': 'yes'}, {'critical_strata': 'critical'},
    {'critical_strata': ['a', 'a']}, {'critical_strata': ['']},
    {'protected_metrics': []}, {'protected_metrics': {'latency': {}}},
    {'protected_metrics': {'latency': {'direction': 'minimize', 'maximum_regression': -1}}},
])
def test_invalid_objective_rejected(changes):
    with pytest.raises(ValueError):
        decide(**changes)


@pytest.mark.parametrize('mutation', [
    lambda rows: rows[0].update(qid=''),
    lambda rows: rows[1].update(qid='q0'),
    lambda rows: rows[0].update(strata='critical'),
    lambda rows: rows[0].update(strata=['critical', 'critical']),
    lambda rows: rows[0].update(parent_grade='UNKNOWN'),
    lambda rows: rows[0].update(parent_grade=[]),
    lambda rows: rows[0].update(candidate_metrics=None),
    lambda rows: rows[0]['candidate_metrics'].update(latency=float('inf')),
    lambda rows: rows[0]['candidate_metrics'].update(latency=None),
    lambda rows: rows[0]['candidate_metrics'].update(latency=True),
    lambda rows: rows[0]['candidate_metrics'].update(complete_correct=1),
    lambda rows: rows[0].pop('strata'),
])
def test_invalid_rows_rejected(mutation):
    rows = panel()
    mutation(rows)
    with pytest.raises(ValueError):
        decide(rows)


def test_inputs_are_not_mutated_and_objective_fields_cannot_default():
    rows = panel()
    settings = objective()
    before_rows, before_settings = deepcopy(rows), deepcopy(settings)
    adjudicate_pairs(phase=60, rows=rows, objective=settings)
    assert rows == before_rows
    assert settings == before_settings
    for name in settings:
        incomplete = dict(settings)
        del incomplete[name]
        with pytest.raises(ValueError, match='predeclared'):
            adjudicate_pairs(phase='DEV60', rows=rows, objective=incomplete)


@pytest.mark.parametrize('phase', [True, None, [], 'TEST', 61])
def test_invalid_phase(phase):
    with pytest.raises(ValueError, match='phase'):
        decide(phase=phase)

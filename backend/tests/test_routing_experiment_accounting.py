import importlib.util
from pathlib import Path
import pytest

path = Path(__file__).resolve().parents[2] / 'scripts/summarize_routing_experiment.py'
spec = importlib.util.spec_from_file_location('routing_accounting', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_absent_arms_and_unknown_cost_are_not_filled():
    r = module.summarize([{'arm': 'CHEAP_ONLY', 'qid': 'synthetic-a', 'evaluation_identity': 'v1', 'core_correctness': 'PASS'}])
    assert r['DYNAMIC']['status'] == 'NOT_RUN'
    assert r['CHEAP_ONLY']['cost'] is None
    assert r['CHEAP_ONLY']['upgrade_rate_per_planned'] is None
    assert r['comparison']['status'] == 'NOT_RUN'


def test_duplicate_and_different_identity_rejected():
    row = {'arm': 'CHEAP_ONLY', 'qid': 'synthetic-a', 'evaluation_identity': 'v1'}
    with pytest.raises(ValueError): module.summarize([row, row])
    with pytest.raises(ValueError): module.summarize([row, {**row, 'arm': 'DYNAMIC', 'evaluation_identity': 'v2'}])


def test_partial_unknown_have_separate_denominators_and_real_quantiles():
    rows = [{'arm': 'DYNAMIC', 'qid': str(i), 'evaluation_identity': 'v1', 'core_correctness': score,
             'latency_ms': delay, 'upgraded': False} for i, (score, delay) in enumerate([('PASS', 10), ('PARTIAL', 20), ('UNKNOWN', 30)])]
    r = module.summarize(rows)['DYNAMIC']
    assert r['strict_core_pass_per_reviewed'] == .5
    assert r['confirmed_pass_per_planned'] == 1/3
    assert r['p50_ms'] == 20 and r['p95_ms_nearest_rank'] == 30


def row(arm='DYNAMIC', qid='a', **values):
    return {'arm': arm, 'qid': qid, 'evaluation_identity': 'v1', **values}


def test_frozen_plan_keeps_missing_questions_and_arms_in_denominator():
    r = module.summarize([row(core_correctness='PASS')], planned_qids=['a', 'b'])
    assert r['DYNAMIC']['n_planned'] == 2
    assert r['DYNAMIC']['n_observed'] == 1
    assert r['DYNAMIC']['core_counts'] == {'PASS': 1, 'PARTIAL': 0, 'FAIL': 0, 'UNKNOWN': 1}
    assert r['DYNAMIC']['confirmed_pass_per_planned'] == .5
    assert r['DYNAMIC']['strict_core_pass_per_reviewed'] == 1
    assert r['CHEAP_ONLY']['status'] == 'NOT_RUN'
    assert r['CHEAP_ONLY']['n_planned'] == 2
    assert r['CHEAP_ONLY']['n_unknown'] == 2
    assert r['comparison']['missing_qids_by_arm']['DYNAMIC'] == ['b']


def test_union_is_labelled_as_inferred_and_detects_unpaired_samples():
    rows = [row(a, q, comparison_input_hash='input') for a, q in
            [('CHEAP_ONLY', 'a'), ('DYNAMIC', 'b'), ('EXPENSIVE_ONLY', 'a')]]
    r = module.summarize(rows)
    assert r['comparison']['status'] == 'PARTIAL'
    assert r['comparison']['shared_qids'] == []
    assert r['DYNAMIC']['n_planned'] == 2
    assert r['DYNAMIC']['denominator_basis'] == 'OBSERVED_UNION_NOT_FROZEN_PLAN'


@pytest.mark.parametrize('grade', ['TRUNCATED', 'pass', 'ERROR', False, 0, ''])
def test_quality_is_not_silently_conflated_with_execution(grade):
    with pytest.raises(ValueError, match='invalid core_correctness'):
        module.summarize([row(core_correctness=grade)])


def test_provider_unknown_and_truncation_remain_separate_from_core_quality():
    r = module.summarize([
        row(qid='a', core_correctness='PARTIAL', execution_status='COMPLETED',
            generation_status='TRUNCATED', finish_reason='length'),
        row(qid='b', execution_status='PROVIDER_FAILURE'),
        row(qid='c', execution_status='UNKNOWN'),
    ])['DYNAMIC']
    assert r['core_counts']['PARTIAL'] == 1 and r['core_counts']['UNKNOWN'] == 2
    assert r['execution_counts']['PROVIDER_FAILURE'] == 1
    assert r['execution_counts']['UNKNOWN'] == 1
    assert r['generation_counts']['TRUNCATED'] == 1
    assert r['finish_reason_counts']['length'] == 1


def test_invalid_numeric_data_cannot_become_zero_cost_or_latency():
    rows = [row(qid=str(i), latency_ms=value, input_tokens=value, cost=value)
            for i, value in enumerate([True, -1, float('nan'), float('inf'), None, 0, 10])]
    r = module.summarize(rows)['DYNAMIC']
    assert r['latency_n'] == 2
    assert r['p50_ms'] == 5 and r['p95_ms_nearest_rank'] == 10
    assert r['input_tokens'] is None and r['cost'] is None
    assert r['metric_missingness']['input_tokens'] == {'valid_n': 2, 'missing_or_invalid_n': 5, 'invalid_n': 4}


def test_cost_requires_complete_same_currency_records_and_stays_unverified():
    rows = [row(qid=q, cost=c, cost_currency='CNY', input_tokens=1, output_tokens=2)
            for q, c in [('a', .1), ('b', .2)]]
    r = module.summarize(rows)['DYNAMIC']
    assert r['cost'] == pytest.approx(.3)
    assert r['cost_currency'] == 'CNY'
    assert r['cost_status'] == 'REPORTED_NOT_BILL_VERIFIED'
    assert r['input_tokens'] == 2 and r['output_tokens'] == 4
    assert module.summarize(rows, ['a', 'b', 'c'])['DYNAMIC']['cost'] is None
    rows[1]['cost_currency'] = 'USD'
    assert module.summarize(rows)['DYNAMIC']['cost'] is None


def test_pairing_requires_frozen_input_hash_and_known_quality():
    rows = [row(a, q, comparison_input_hash=q, core_correctness=grade)
            for q in ('a', 'b', 'c')
            for a, grade in [('CHEAP_ONLY', 'FAIL'), ('DYNAMIC', 'PARTIAL'), ('EXPENSIVE_ONLY', 'PASS')]]
    rows[3]['core_correctness'] = 'UNKNOWN'
    r = module.summarize(rows, ['a', 'b', 'c'])
    assert r['comparison']['status'] == 'AVAILABLE'
    pair = r['comparison']['paired_core_coverage']['CHEAP_ONLY_vs_EXPENSIVE_ONLY']
    assert pair['right_better'] == 2 and pair['unknown_quality'] == 1
    assert r['comparison']['cost_savings'] is None
    rows[0]['comparison_input_hash'] = 'different-prior-answer'
    assert module.summarize(rows)['comparison']['status'] == 'INCOMPARABLE'
    del rows[0]['comparison_input_hash']
    assert module.summarize(rows)['comparison']['status'] == 'METADATA_UNKNOWN'


@pytest.mark.parametrize('plan', [['a', 'a'], ['b']])
def test_invalid_manifest_is_rejected(plan):
    with pytest.raises(ValueError):
        module.summarize([row()], plan)


def test_empty_plan_and_missing_experiment_identity_cannot_claim_comparison():
    r = module.summarize([], [])
    assert r['comparison']['status'] == 'NOT_RUN'
    assert r['DYNAMIC']['confirmed_pass_per_planned'] is None
    with pytest.raises(ValueError, match='evaluation_identity'):
        module.summarize([row(evaluation_identity='')])

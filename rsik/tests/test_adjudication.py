"""Known synthetic outcomes; no generated answers, model, files or replay."""
import ast
from copy import deepcopy
from fractions import Fraction
import hashlib
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rsik import adjudicate as judge


def rows(n=20, a=100, b=90):
    return [{'qid': f'SYNTHETIC-{i}', 'source_group': f'SYNTHETIC-group-{i}', 'chars_A': a, 'chars_B': b,
             'parent_facts': {'clause': 'PRESENT'}, 'candidate_facts': {'clause': 'PRESENT'},
             'provenance_A': 'PRESENT', 'provenance_B': 'PRESENT', 'pair_status': 'PAIRED',
             'pair_identity': True, 'parent_faults': [], 'candidate_faults': [], 'execution': 'COMPLETED',
             'context_A_sha256': hashlib.sha256(('a' * a).encode()).hexdigest(),
             'context_B_sha256': hashlib.sha256(('b' * b).encode()).hexdigest(), 'upstream_hash': 's' * 64}
            for i in range(n)]


def decision(data=None, slot='P1_DEDUP', candidate='candidate-one', **kwargs):
    data = rows() if data is None else data
    return judge.evaluate_candidate({'round_id': 'SYNTHETIC', 'proposal_id': 'proposal-' + slot,
        'candidate_id': candidate, 'parent_id': 'parent', 'angle': slot,
        'worker_hash': 'worker-' + candidate, 'config_hash': 'fixed-synthetic'}, data,
        [r['qid'] for r in data], total_questions=len(data), **kwargs)


def test_exact_sign_test_has_no_clean_sweep_shortcut_or_tie_denominator():
    assert judge.sign_test(3, 0, 20) == Fraction(1, 4)
    assert judge.sign_test(0, 0, 20) == 1
    assert judge.sign_test(8, 0) == Fraction(1, 128)
    assert judge.sign_test(7, 1) == Fraction(9, 128)
    assert judge.sign_test(1, 7) == judge.sign_test(7, 1)


def test_holm_uses_complete_family_and_monotone_adjustment():
    assert judge.holm([Fraction(1, 100), Fraction(4, 100), Fraction(1)]) == [Fraction(3, 100), Fraction(8, 100), Fraction(1)]
    assert judge.holm([Fraction(1, 4), Fraction(1), Fraction(1)])[0] == Fraction(3, 4)


def test_three_candidates_are_adjudicated_independently():
    good, lost, infrastructure = rows(), rows(), rows()
    lost[0]['candidate_facts']['clause'] = 'ABSENT'
    infrastructure[0].update(execution='FAILED', pair_status='FAILED', fault_origin='INFRA')
    result = judge.adjudicate_round([decision(good), decision(lost, 'P2_BUDGET'), decision(infrastructure, 'P3_ORDER')])
    assert result['verdict'] == 'ADOPT_CANDIDATE' and result['family_size'] == 3
    actual = {d['angle']: d for d in result['candidate_decisions']}
    assert actual['P1_DEDUP']['verdict'] == 'VALIDATED'
    assert actual['P2_BUDGET']['reason_codes'] == ['FACT_LOSS']
    assert actual['P3_ORDER']['verdict'] == 'INCONCLUSIVE'
    assert result['adopted_proposal_id'] == 'proposal-P1_DEDUP'


def test_no_winner_before_all_qualified_proposals_have_terminated():
    result = judge.adjudicate_round([decision()], all_qualified_terminal=False)
    assert result['verdict'] == 'ROUND_ABORTED' and result['adopted_candidate_id'] is None


def test_parent_failure_invalidates_the_entire_round():
    bad = rows()
    bad[0]['parent_facts']['clause'] = 'ABSENT'
    result = judge.adjudicate_round([decision(), decision(bad, 'P2_BUDGET')])
    assert result['verdict'] == 'ROUND_ABORTED'
    assert result['candidate_decisions'][1]['reason_codes'] == ['PARENT_FAULT']


@pytest.mark.parametrize('fault,reason', [('unknown_fact', 'FACT_OR_PROVENANCE_UNKNOWN'),
    ('unknown_source', 'FACT_OR_PROVENANCE_UNKNOWN'), ('pair', 'INVALID_PAIR'),
    ('negative_chars', 'INVALID_CHARACTER_COUNT'), ('execution', 'INFRA_FAILURE')])
def test_uncertainty_is_not_a_quality_failure(fault, reason):
    data = rows()
    if fault == 'unknown_fact': data[0]['candidate_facts']['clause'] = 'UNKNOWN'
    elif fault == 'unknown_source': data[0]['provenance_B'] = 'UNKNOWN'
    elif fault == 'pair': data[0]['pair_identity'] = False
    elif fault == 'negative_chars': data[0]['chars_B'] = -1
    elif fault == 'execution': data[0].update(execution='FAILED', pair_status='FAILED')
    result = decision(data)
    assert result['verdict'] == 'INCONCLUSIVE' and result['reason_codes'] == [reason]


def test_runtime_missing_question_cannot_be_deleted_from_the_denominator():
    data = rows()
    result = judge.evaluate_candidate({'angle': 'P1_DEDUP'}, data[:-1], [r['qid'] for r in data], total_questions=20)
    assert result['verdict'] == 'INCONCLUSIVE' and result['reason_codes'] == ['PARTIAL_REPLAY']


def test_confirmed_fact_loss_can_reject_partial_replay_without_full_statistics():
    data = rows()
    data[0]['candidate_facts']['clause'] = 'ABSENT'
    result = judge.evaluate_candidate({'angle': 'P1_DEDUP'}, data[:1], [r['qid'] for r in data], total_questions=20)
    assert result['verdict'] == 'REJECTED' and result['reason_codes'] == ['FACT_LOSS']
    assert result['delta_total'] is None


def test_local_regression_is_an_exact_integer_gate():
    data = rows()
    data[0]['chars_B'] = 103
    assert decision(data)['reason_codes'] == ['LOCAL_REGRESSION']
    data[0]['chars_B'] = 102
    assert decision(data)['gates_passed'] is True
    assert decision(data)['question_counts']['unfavorable'] == 1


def test_small_individual_regressions_cannot_hide_a_net_increase():
    data = rows(b=101)
    data[0]['chars_B'] = 99
    result = decision(data)
    assert result['reason_codes'] == ['NO_NET_GAIN'] and result['delta_total'] == 18


def test_zero_baseline_and_all_ties_are_handled_without_division():
    result = decision(rows(a=0, b=0))
    assert result['reason_codes'] == ['INCONCLUSIVE_BASE_ZERO'] and result['reduction_rate'] is None
    data = rows()
    data[0].update(chars_A=0, chars_B=1)
    assert decision(data)['reason_codes'] == ['LOCAL_REGRESSION']
    result = decision(rows(a=100, b=100))
    assert result['reason_codes'] == ['NO_GAIN']


def test_three_to_zero_is_observed_not_validated_with_other_groups_tied():
    data = rows(b=100)
    for row in data[:3]: row['chars_B'] = 90
    result = judge.adjudicate_round([decision(data)])
    item = result['candidate_decisions'][0]
    assert item['verdict'] == 'OBSERVED'
    assert item['group_counts'] == {'favorable': 3, 'unfavorable': 0, 'ties': 17}
    assert item['sign_p_adjusted']['value'] == .75


def test_duplicate_patch_instances_do_not_shrink_statistical_family():
    result = judge.adjudicate_round([decision(slot=slot) for slot in judge.SLOTS])
    assert len(result['candidate_decisions']) == result['family_size'] == 3
    assert result['adopted_proposal_id'] == 'proposal-P1_DEDUP'


def test_identical_worker_config_input_with_different_delivery_aborts_round():
    first, second = decision(), decision(slot='P2_BUDGET')
    second['per_question'][0]['context_B_sha256'] = 'different'
    result = judge.adjudicate_round([first, second])
    assert result['verdict'] == 'ROUND_ABORTED'


def test_parent_delivery_is_cross_checked_across_candidates():
    first, second = decision(), decision(slot='P2_BUDGET', candidate='other-worker')
    second['per_question'][0]['context_A_sha256'] = 'different'
    assert judge.adjudicate_round([first, second])['verdict'] == 'ROUND_ABORTED'


def test_minimum_group_count_and_preblocked_denominator_are_frozen():
    data = rows()
    for row in data: row['source_group'] = 'same-document'
    assert decision(data)['reason_codes'] == ['INSUFFICIENT_SOURCE_GROUPS']
    data = rows(n=17)
    result = judge.evaluate_candidate({'angle': 'P1_DEDUP'}, data, [r['qid'] for r in data], total_questions=20, pre_blocked=3)
    assert result['reason_codes'] == ['PRE_BLOCKED_LIMIT']


def test_confidence_never_changes_winner_and_worst_question_breaks_ties():
    first, second = rows(), rows()
    second[0]['chars_B'], second[1]['chars_B'] = 80, 100
    a, b = decision(first, candidate='uniform'), decision(second, 'P2_BUDGET', candidate='uneven')
    a['confidence'], b['confidence'] = 0, 1
    result = judge.adjudicate_round([b, a])
    assert result['adopted_candidate_id'] == 'uniform'


def test_holdout_requires_both_comparisons_with_family_two_even_when_shared():
    first = decision(rows(n=10))
    result = judge.adjudicate_holdout(first, first)
    assert result['verdict'] == 'VERIFIED' and result['family_size'] == 2
    assert result['production_approved'] is False
    lost = rows(n=10)
    lost[0]['candidate_facts']['clause'] = 'ABSENT'
    assert judge.adjudicate_holdout(first, decision(lost))['verdict'] == 'REGRESSION_SUSPECTED'


def test_judge_has_no_io_environment_or_llm_imports(monkeypatch):
    source = Path(judge.__file__).read_text(encoding='utf-8')
    tree = ast.parse(source)
    modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert modules <= {'copy', 'fractions', 'math'}
    assert not any(isinstance(n, ast.Import) for n in ast.walk(tree))
    with monkeypatch.context() as patch:
        def forbidden(*args, **kwargs): raise AssertionError('judge attempted IO')
        patch.setattr('builtins.open', forbidden)
        result = judge.adjudicate_round([decision()])
    assert result['verdict'] == 'ADOPT_CANDIDATE'

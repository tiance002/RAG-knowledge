from pathlib import Path
import sys
from copy import deepcopy
import json

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.robustness import gate_training,gate_training_suite
from rsik.adjudicate import adjudicate_round
from rsik.lineage import LineagePolicy,ParentPool

def primary():
    from test_adjudication import decision
    return decision()


def test_primary_training_gain_cannot_override_a_source_layout_fact_loss():
    decision=primary();before=deepcopy(decision)
    receipt={'passed':False,'round_stop':False,'reason_codes':['FACT_LOSS'],'candidate_fact_loss_questions':20,
             'scope':'SYNTHETIC gate unit test; not a live stress receipt'}
    rejected=gate_training(decision,receipt)
    assert rejected['verdict']=='REJECTED' and rejected['reason_codes']==['ROBUSTNESS_FACT_LOSS']
    assert adjudicate_round([rejected])['adopted_candidate_id'] is None
    assert decision==before


def test_stress_without_additional_gain_can_pass_hard_gate_only():
    decision=primary()
    receipt={'passed':True,'round_stop':False,'reason_codes':['NO_GAIN'],'candidate_fact_loss_questions':0,
             'scope':'SYNTHETIC hard-gate unit test'}
    result=gate_training(decision,receipt)
    assert result['gates_passed'] and result['sign_p_raw']==decision['sign_p_raw']
    assert len(result['per_question'])==20  # no doubled statistical denominator


def test_unknown_stress_is_not_a_confirmed_algorithm_failure():
    result=gate_training(primary(),{'passed':False,'round_stop':False,'reason_codes':['INFRA_FAILURE'],
        'candidate_fact_loss_questions':0})
    assert result['verdict']=='INCONCLUSIVE' and not result['gates_passed']


def test_known_source_regression_excludes_parent_without_freezing_siblings(tmp_path):
    policy=LineagePolicy(tmp_path/'policy');pool=ParentPool(tmp_path/'pool')
    receipt=tmp_path/'receipt.json';receipt.write_text('{"verdict":"VALIDATED"}')
    pool.admit('bad','lineage',receipt,kind='EXPLORATION',chars=100,replay_seconds=1)
    assert pool.select(policy,exploration=True)['candidate_id']=='bad'
    pool.quarantine('bad',receipt,'SYNTHETIC_FACT_LOSS')
    assert pool.select(policy,exploration=True) is None
    assert not policy.state('lineage')['frozen']


def test_second_direction_fact_loss_cannot_be_hidden_by_first_direction_pass():
    good={'passed':True,'round_stop':False,'reason_codes':['NO_GAIN'],'candidate_fact_loss_questions':0}
    bad={'passed':False,'round_stop':False,'reason_codes':['FACT_LOSS'],'candidate_fact_loss_questions':3}
    result=gate_training_suite(primary(),[good,bad])
    assert result['verdict']=='REJECTED' and not result['gates_passed']
    assert len(result['robustness_suite'])==2 and adjudicate_round([result])['adopted_candidate_id'] is None

import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.lineage import LineagePolicy, ParentPool


def finish(policy, i, gain=False):
    policy.start_round('lineage',str(i),input_hash=str(i))
    policy.finish_round('lineage',str(i),{'verdict':'NO_ADOPTION','qualified':['P1_DEDUP'],'attempted_replays':['P1_DEDUP'],'candidate_decisions':[
        {'gates_passed':gain,'delta_total':-1 if gain else 0,'reason_codes':['STATISTICS_PENDING_FAMILY'] if gain else ['NO_GAIN'],
         'eligible_count':1,'per_question':[{'pair_status':'PAIRED','pair_identity':True,'provenance_A':'PRESENT','provenance_B':'PRESENT'}]}]},result_hash='a'*64)


def test_three_no_gain_rounds_trigger_diagnosis_instead_of_count_stop(tmp_path):
    policy=LineagePolicy(tmp_path)
    for i in range(3):finish(policy,i)
    assert policy.state('lineage')['stop_reason'] is None
    assert policy.state('lineage')['no_gain_action']=='DIAGNOSE_AND_ADJUST'
    policy.start_round('lineage','4',input_hash='x')


def test_owner_budget_policy_allows_more_than_five_rounds(tmp_path):
    policy=LineagePolicy(tmp_path)
    for i in range(12):finish(policy,i,True)
    assert policy.state('lineage')['rounds']==12 and policy.state('lineage')['round_limit'] is None
    policy.start_round('lineage','13',input_hash='x')


def test_abstentions_do_not_count_as_three_trustworthy_no_gain_replays(tmp_path):
    policy=LineagePolicy(tmp_path)
    for i in range(3):
        policy.start_round('lineage',str(i),input_hash='x')
        policy.finish_round('lineage',str(i),{'verdict':'NO_ADOPTION','candidate_decisions':[]},result_hash='a'*64)
    assert policy.state('lineage')['no_gain_streak']==0
    assert policy.state('lineage')['stop_reason'] is None


def test_unknown_round_and_reused_holdout_are_not_retried(tmp_path):
    policy=LineagePolicy(tmp_path)
    policy.start_round('lineage','1',input_hash='x')
    with pytest.raises(RuntimeError,match='ROUND_STATUS_UNKNOWN'):policy.start_round('lineage','2',input_hash='x')
    policy.claim_holdout('lineage','candidate',holdout_freeze_hash='x')
    with pytest.raises(RuntimeError,match='ALREADY_USED'):policy.claim_holdout('lineage','candidate',holdout_freeze_hash='x')
    with pytest.raises(RuntimeError,match='ALREADY_USED'):policy.claim_holdout('other-lineage','other-candidate',holdout_freeze_hash='x')
    policy.finish_holdout('lineage','candidate','REJECTED',receipt_hash='r')
    assert policy.state('lineage')['frozen']
    with pytest.raises(RuntimeError,match='LINEAGE_FROZEN'):policy.start_round('lineage','3',input_hash='x')


def test_verified_pool_and_receipt_hash_are_required(tmp_path):
    policy=LineagePolicy(tmp_path/'policy');pool=ParentPool(tmp_path/'pool')
    receipt=tmp_path/'verified.json';receipt.write_text(json.dumps({'verdict':'VERIFIED'}))
    pool.admit('candidate','lineage',receipt,kind='VERIFIED',chars=100,replay_seconds=1)
    assert pool.select(policy)['candidate_id']=='candidate'
    receipt.write_text('{}')
    with pytest.raises(RuntimeError,match='PARENT_RECEIPT_CHANGED'):pool.select(policy)


def test_exploration_never_enters_default_parent_selection(tmp_path):
    policy=LineagePolicy(tmp_path/'policy');pool=ParentPool(tmp_path/'pool')
    receipt=tmp_path/'exploration.json';receipt.write_text(json.dumps({'verdict':'VALIDATED'}))
    pool.admit('candidate','lineage',receipt,kind='EXPLORATION',chars=100,replay_seconds=1)
    assert pool.select(policy) is None
    assert pool.select(policy,exploration=True)['candidate_id']=='candidate'
    policy.claim_holdout('lineage','candidate',holdout_freeze_hash='x')
    policy.finish_holdout('lineage','candidate','REJECTED',receipt_hash='r')
    assert pool.select(policy,exploration=True) is None

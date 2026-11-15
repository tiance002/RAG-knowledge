import pytest
from rsik.campaign import matched_plan,budget_feasibility,SearchBudget,nominate
from rsik.budgets import PRICES


def test_nine_distinct_equal_starts_and_balanced_groups():
    plan=matched_plan(epoch_id='epoch',baseline_id='b',input_manifest_hash='i',initial_memory_hash='m')
    assert len(plan['runs'])==9 and len({r['learning_namespace'] for r in plan['runs']})==9
    assert {g:sum(r['group']==g for r in plan['runs']) for g in ['A_NEW_K3','B_K1_P2','C_OLD_K3']}==dict.fromkeys(['A_NEW_K3','B_K1_P2','C_OLD_K3'],3)
    assert all(r['nominal_usd']=='0.20' and r['baseline_id']=='b' for r in plan['runs'])
    assert nominate([])['status']=='NO_QUALIFIED_CANDIDATE'


def test_unknown_qa_is_blocked_before_unmatched_run(tmp_path):
    budget=SearchBudget(tmp_path/'ledger',start_nano=0)
    plan=matched_plan(epoch_id='e',baseline_id='b',input_manifest_hash='i',initial_memory_hash='m')
    f=budget_feasibility(plan,budget)
    assert 'QA_BUDGET_BOUND_UNKNOWN' in f['blocking_reasons'] and budget.snapshot()['attempts']==0
    plan.update(qa_input_token_bound=16000,qa_price=PRICES['gpt-6-luna'])
    assert budget_feasibility(plan,budget)['status']=='PASS'


def test_serial_request_settlement_and_nominal_allowance(tmp_path):
    b=SearchBudget(tmp_path/'ledger',start_nano=0,nominal_usd='0.00000001')
    b.start('a','P1','gpt-6-luna','hash')
    with pytest.raises(RuntimeError,match='BUDGET_SERIALIZE_REQUIRED'):b.start('b','P2','gpt-6-luna','hash')
    b.complete('a',{'input_tokens':1,'output_tokens':1},status='RECEIVED')
    with pytest.raises(RuntimeError,match='BUDGET_EXHAUSTED'):b.start('b','P2','gpt-6-luna','hash')
    assert b.snapshot()['attempts']==1 and b.snapshot()['reservation_usd']=='0'


def test_partial_search_campaign_cannot_nominate_a_favorable_run():
    plan=matched_plan(epoch_id='e',baseline_id='b',input_manifest_hash='i',initial_memory_hash='m')
    one={'run_id':plan['runs'][0]['run_id'],'execution_state':'COMPLETED','family_size':3,
         'candidate_decisions':[{'verdict':'VALIDATED','gates_passed':True}]}
    assert nominate([one],plan=plan)['status']=='BLOCKED_SEARCH_INCOMPLETE'

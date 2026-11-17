from rsik.qa_validation import qa_plan,summarize,run_paired_qa
import pytest


def test_three_balanced_pairs_no_missing_usage_as_zero():
    plan=qa_plan(['q']);assert plan['planned_generations']==6
    rows=[{**i,'quality':'PASS','status':'COMPLETED','input_tokens':100 if i['arm']=='PARENT' else 80,
           'critical_omissions':0} for i in plan['order']]
    s=summarize(rows,expected_qids=['q'])
    assert s['complete_pairs']==3 and all(x['cost_delta'] is None for x in s['effects'][0]['effects'])
    assert not s['population_noninferiority_proven']


def test_fallback_or_missing_pair_not_qa_success():
    s=summarize([{'qid':'q','repeat':0,'arm':'PARENT'}],expected_qids=['q'])
    assert s['quality_status']=='QUALITY_UNKNOWN' and s['complete_pairs']==0


def test_no_unverified_context_generation():
    with pytest.raises(ValueError,match='VERIFIED_CONTEXT_REQUIRED'):
        run_paired_qa(plan={},frozen_cases={},generator=lambda x:None,blind_judge=lambda x:None,
            budget=None,model='gpt-6-luna',launch=None,output=None,context_receipt={},prompt='',decode={},clock=lambda:0)


def test_missing_critical_review_is_unknown_even_if_quality_string_passes():
    rows=[{**i,'quality':'PASS','status':'COMPLETED'} for i in qa_plan(['q'])['order']]
    assert summarize(rows,expected_qids=['q'])['quality_status']=='QUALITY_UNKNOWN'

def test_paid_judge_callback_is_not_silently_free():
    with pytest.raises(ValueError,match='SCORING_TRANSPORT_ACCOUNTING_REQUIRED'):
        run_paired_qa(plan={},frozen_cases={},generator=lambda x:None,blind_judge=lambda x:None,
            budget=None,model='gpt-6-luna',launch=None,output=None,context_receipt={'verdict':'VERIFIED'},prompt='',decode={},clock=lambda:0)

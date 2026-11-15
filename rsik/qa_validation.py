"""Pre-registered paired QA orchestration and transparent measurement collection.

No answer can qualify a selector without the preceding independent context gate.
The injected generator owns actual transport; no generator or score is faked.
"""
from collections import defaultdict
import random
from .state import digest, canonical
from .locking import atomic_write


def qa_plan(qids, *, seed=20261115):
    if not qids or len(set(qids))!=len(qids):raise ValueError('QA_CASE_IDENTITIES_REQUIRED')
    order=[]
    for qid in qids:
        for repeat in range(3):
            arms=['PARENT','CANDIDATE']
            random.Random(f'{seed}:{qid}:{repeat}').shuffle(arms)
            for arm in arms:order.append({'qid':qid,'repeat':repeat,'arm':arm})
    return {'schema':'rsi-paired-qa/v1','order':order,'seed':seed,'repeats':3,
            'planned_generations':len(order),'qa_generation_is_not_search_request_limit':True,
            'rubric_policy':'frozen exact equivalents; blinded independent scoring; disputed -> QUALITY_UNKNOWN'}


def summarize(records, *, expected_qids):
    pairs=defaultdict(dict);unknown=[]
    for r in records:
        key=(r['qid'],r['repeat'])
        if r['arm'] in pairs[key]:raise ValueError('DUPLICATE_QA_ARM')
        pairs[key][r['arm']]=r
    effects=[];quality='PASS_ON_THIS_SAMPLE'
    for qid in expected_qids:
        rows=[]
        for repeat in range(3):
            pair=pairs.get((qid,repeat),{})
            if set(pair)!={'PARENT','CANDIDATE'}:
                unknown.append({'qid':qid,'repeat':repeat,'reason':'INCOMPLETE_PAIR'});continue
            a,b=pair['PARENT'],pair['CANDIDATE']
            for r in (a,b):
                if (r.get('quality') in (None,'UNKNOWN','DISPUTED') or r.get('status')!='COMPLETED') and quality!='CONFIRMED_REGRESSION':quality='QUALITY_UNKNOWN'
            critical=('critical_omissions','entity_value_condition_errors','unsupported_key_claims','citation_mismatches')
            if any(type(a.get(k))is not int or type(b.get(k))is not int for k in critical) and quality!='CONFIRMED_REGRESSION':quality='QUALITY_UNKNOWN'
            if any(type(b.get(k))is int and type(a.get(k))is int and b[k]>a[k] for k in critical):quality='CONFIRMED_REGRESSION'
            def diff(k):
                x,y=a.get(k),b.get(k)
                return y-x if type(x) in (int,float) and type(y) in (int,float) else None
            rows.append({'input_tokens_delta':diff('input_tokens'),'cost_delta':diff('cost_usd'),
                         'latency_delta':diff('latency_seconds'),'ttft_delta':diff('ttft_seconds')})
        effects.append({'qid':qid,'complete_pairs':len(rows),'effects':rows})
    if unknown and quality!='CONFIRMED_REGRESSION':quality='QUALITY_UNKNOWN'
    return {'questions_expected':len(expected_qids),'repeat_pairs_expected':3*len(expected_qids),
        'generations_observed':len(records),'complete_pairs':sum(e['complete_pairs'] for e in effects),
        'quality_status':quality,'unknown':unknown,'effects':effects,
        'missing_usage_is_zero':False,'population_noninferiority_proven':False,'production_changed':False}


def run_paired_qa(*, plan, frozen_cases, generator, blind_judge, budget, model, launch,
                  output, context_receipt, prompt, decode, clock):
    """Actual execution is gated; a missing candidate is SKIPPED, never substituted.

    All transport attempts are post-usage charged to the same supplied ledger.
    An unbounded response stops before any next arm. TTFT may remain UNKNOWN if
    the provider transport cannot measure it; that is retained as a limitation.
    """
    from pathlib import Path
    from .launch import validate_launch
    if context_receipt.get('verdict')!='VERIFIED':raise ValueError('VERIFIED_CONTEXT_REQUIRED_BEFORE_QA')
    if type(blind_judge)is not DeterministicJudge:raise ValueError('SCORING_TRANSPORT_ACCOUNTING_REQUIRED')
    validate_launch(launch,phase='QA_VALIDATION')
    from .experiment import load
    permit=load(launch.plan_path)
    if (permit.get('qa_plan_sha256')!=digest(plan) or permit.get('qa_cases_sha256')!=digest(frozen_cases)
            or permit.get('qa_context_receipt_sha256')!=digest(context_receipt)):
        raise ValueError('QA_FROZEN_INPUT_BINDING_REQUIRED')
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    if {x['qid'] for x in plan['order']}!=set(frozen_cases):raise ValueError('QA_FROZEN_CASE_SET_CHANGED')
    atomic_write(out/'plan.json',canonical(plan),exclusive=True)
    records=[]
    for index,item in enumerate(plan['order']):
        validate_launch(launch,phase='QA_VALIDATION')
        case=frozen_cases[item['qid']];arm=item['arm']
        body={'model':model,'store':False,'input':[{'role':'system','content':prompt},
            {'role':'user','content':case['question']+'\n'+case['contexts'][arm]}],**decode}
        key=launch.run_id+'-QA-'+str(index);cost_before=budget.snapshot()['recorded_cost_nano_upper'];budget.start(key,'QA_GENERATION',model,digest(body))
        t=clock()
        try:receipt=generator(body)
        except Exception:receipt={'status':'UNKNOWN','usage':None}
        atomic_write(out/(str(index)+'.receipt.json'),canonical(receipt),exclusive=True)
        state=budget.complete(key,receipt.get('usage'),status=receipt.get('status','UNKNOWN'))
        if state['unbounded_usage_requests'] or state['pending_request_keys']:raise ValueError('BILLING_OR_REQUEST_STATUS_UNKNOWN')
        generation_seconds=clock()-t
        # The judge sees a neutral alias and the frozen rubric, never A/B label.
        blinded={'answer_alias':'answer-'+digest({'request':key})[:16],
            'answer':receipt.get('answer'),'rubric':case['rubric']}
        score=blind_judge(blinded)
        if score.get('evidence_kind') not in {'DETERMINISTIC_RULES','INDEPENDENT_CALIBRATED_REVIEW'}:
            score={'quality':'UNKNOWN','evidence_kind':'UNRESOLVED_REVIEW'}
        row={**item,**score,'status':receipt.get('status','UNKNOWN'),
             'latency_seconds':generation_seconds,'ttft_seconds':receipt.get('ttft_seconds'),
             'input_tokens':(receipt.get('usage') or {}).get('input_tokens'),
             'output_tokens':(receipt.get('usage') or {}).get('output_tokens'),
             'cached_tokens':((receipt.get('usage') or {}).get('input_tokens_details') or {}).get('cached_tokens'),
             'cost_usd':(state['recorded_cost_nano_upper']-cost_before)/1_000_000_000,'cost_basis':'USAGE_RATE_UPPER_ESTIMATE_NOT_INVOICE','empty':not bool(receipt.get('answer')),
             'truncated':receipt.get('truncated','UNKNOWN'),'model':model,'prompt_hash':digest(prompt)}
        records.append(row);atomic_write(out/(str(index)+'.result.json'),canonical(row),exclusive=True)
    summary=summarize(records,expected_qids=list(frozen_cases))
    atomic_write(out/'summary.json',canonical(summary),exclusive=True)
    return summary


class DeterministicJudge:
    """No network/callbacks. Open-ended rubric coverage stays UNKNOWN."""
    def __call__(self,blinded):
        rubric=blinded['rubric'];answer=blinded.get('answer')
        if not isinstance(answer,str):return {'quality':'UNKNOWN','evidence_kind':'DETERMINISTIC_RULES'}
        fields=('critical_omissions','entity_value_condition_errors','unsupported_key_claims','citation_mismatches')
        required=rubric.get('required',[]);forbidden=rubric.get('forbidden',{})
        result={k:0 for k in fields}
        result['critical_omissions']=sum(not any(v in answer for v in equivalents) for equivalents in required)
        for key in fields[1:]:
            if key not in forbidden:return {'quality':'UNKNOWN','evidence_kind':'DETERMINISTIC_RULES'}
            result[key]=sum(v in answer for v in forbidden[key])
        result['quality']='PASS' if rubric.get('coverage_review')=='COMPLETE_EXACT_RULES' and not any(result.values()) else 'UNKNOWN'
        return {**result,'evidence_kind':'DETERMINISTIC_RULES'}

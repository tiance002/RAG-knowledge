"""Pre-registered paired QA orchestration and transparent measurement collection.

No answer can qualify a selector without the preceding independent context gate.
The injected generator owns actual transport; no generator or score is faked.
"""
from collections import defaultdict
import random
from .state import digest, canonical
from .locking import atomic_write


def freeze_fixed_history_qa_cases(*, run_directory, fixture_directory, rubrics, context_receipt):
    """Freeze actual ordinary A/B provider inputs after a VERIFIED context run."""
    from pathlib import Path
    from .experiment import load
    from .execution_evidence import ExecutionEvidence
    from .launch import file_binding
    from . import fixed_history_profile as profile
    run, fixture = Path(run_directory).resolve(), Path(fixture_directory).resolve()
    manifest = load(run/'manifest.json'); identity = manifest['arms']['B']
    policy = ExecutionEvidence(**manifest['execution_policy'])
    proposal = {'angle':'P2_BUDGET','proposal_id':manifest['schema']}
    decision = profile.read_decision(run,fixture,identity['candidate_id'],proposal,
        identity['worker_sha256'],identity['sha256'],evidence=policy)
    if decision.get('verdict') != 'VERIFIED' or digest(decision) != digest(context_receipt):
        raise ValueError('QA_VERIFIED_ORDINARY_CONTEXT_REQUIRED')
    items = load(fixture/'cases.json')
    if set(rubrics) != {i['qid'] for i in items}: raise ValueError('QA_ALL20_RUBRICS_REQUIRED')
    source = {'run_directory':str(run),'fixture_directory':str(fixture),
        'context_receipt_sha256':digest(decision)}
    cases = {}
    for index,item in enumerate(items,1):
        captures={arm:run/f'case-{index:03}'/label/'capture.json'
                  for arm,label in [('PARENT','A'),('CANDIDATE','B')]}
        capture=load(captures['PARENT'])
        cases[item['qid']] = {'evaluator_profile':profile.PROFILE,'question':item['case']['question'],
            'rubric':rubrics[item['qid']], 'verified_context_source':source,
            **{k:capture[k] for k in ('history_sha256','sidecar_sha256','qid','lineage_id',
                'base_question_sha256','current_question_sha256','query_derivation_sha256')},
            'actual_business_captures':{arm:file_binding(path) for arm,path in captures.items()},
            'actual_business_receipts':{arm:file_binding(path.parent/'execution-receipt.json')
                for arm,path in captures.items()}}
    return cases


def verify_fixed_history_qa_cases(frozen_cases,context_receipt):
    first=next(iter(frozen_cases.values()))
    source=first.get('verified_context_source',{})
    if source.get('context_receipt_sha256') != digest(context_receipt):
        raise ValueError('QA_FIXED_HISTORY_CONTEXT_BINDING_REQUIRED')
    rebuilt=freeze_fixed_history_qa_cases(run_directory=source['run_directory'],
        fixture_directory=source['fixture_directory'],
        rubrics={qid:case['rubric'] for qid,case in frozen_cases.items()},context_receipt=context_receipt)
    if rebuilt != frozen_cases: raise ValueError('QA_FIXED_HISTORY_FROZEN_CASES_CHANGED')


def fixed_history_qa_input(case, arm):
    """Use hash-bound actual business messages; a no-call arm stays no-call.

    Launch admission additionally verifies the complete ordinary context run.
    The bindings here prevent a caller from rebuilding a different QA prompt.
    """
    from .launch import read_binding
    from .fixed_history_profile import PROFILE, HISTORY_PROFILE, business_control
    capture = read_binding(case['actual_business_captures'][arm])
    receipt = read_binding(case['actual_business_receipts'][arm])
    binding = case['actual_business_captures'][arm]
    if (case.get('evaluator_profile') != PROFILE or capture.get('evaluator_profile') != PROFILE
            or capture.get('history_profile') != HISTORY_PROFILE
            or receipt.get('capture_sha256') != binding['sha256']
            or any(capture.get(k) != case.get(k) or receipt.get(k) != case.get(k)
                   for k in ('history_sha256','sidecar_sha256','qid','lineage_id',
                       'base_question_sha256','current_question_sha256','query_derivation_sha256'))):
        raise ValueError('QA_FIXED_HISTORY_CAPTURE_BINDING_REQUIRED')
    if capture.get('provider_delivery_status') == 'NO_PROVIDER_REQUEST':
        if business_control(capture)['status'] != 'PRESENT':
            raise ValueError('QA_FIXED_HISTORY_NO_CALL_UNVERIFIED')
        return None
    messages = capture.get('messages')
    if (capture.get('request_calls') != 1
            or capture.get('provider_delivery_status') != 'PROVIDER_REQUEST_CAPTURED'
            or not isinstance(messages,list) or not messages
            or messages[-1] != {'role':'user','content':case['question']}):
        raise ValueError('QA_FIXED_HISTORY_MESSAGES_REQUIRED')
    return messages


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
    effects=[];quality='PASS_ON_THIS_SAMPLE';no_call_pairs=0
    for qid in expected_qids:
        rows=[]
        for repeat in range(3):
            pair=pairs.get((qid,repeat),{})
            if set(pair)!={'PARENT','CANDIDATE'}:
                unknown.append({'qid':qid,'repeat':repeat,'reason':'INCOMPLETE_PAIR'});continue
            a,b=pair['PARENT'],pair['CANDIDATE']
            if (a.get('status') == b.get('status') == 'NO_PROVIDER_REQUEST'
                    and all(r.get('evidence_kind') == 'BUSINESS_EXECUTION_ASSERTION'
                            and r.get('control_status') == 'PRESENT' for r in (a,b))):
                no_call_pairs += 1
                rows.append({'input_tokens_delta':None,'cost_delta':0,
                    'latency_delta':None,'ttft_delta':None,'business_control_pair':True})
                continue
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
        'generations_observed':sum(r.get('status')!='NO_PROVIDER_REQUEST' for r in records),
        'slots_observed':len(records),'no_provider_control_pairs':no_call_pairs,
        'quality_scope':'generated answers; no-provider controls are separate business assertions',
        'complete_pairs':sum(e['complete_pairs'] for e in effects),
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
    fixed = {case.get('evaluator_profile') == 'source-relations-fixed-history/v1' for case in frozen_cases.values()}
    if True in fixed:
        if fixed != {True}: raise ValueError('QA_MIXED_PROFILES_FORBIDDEN')
        if set(decode) & {'model','store','input'}:
            raise ValueError('QA_FIXED_HISTORY_REQUEST_OVERRIDE_FORBIDDEN')
        verify_fixed_history_qa_cases(frozen_cases,context_receipt)
    out=Path(output);out.mkdir(parents=True,exist_ok=False)
    if {x['qid'] for x in plan['order']}!=set(frozen_cases):raise ValueError('QA_FROZEN_CASE_SET_CHANGED')
    atomic_write(out/'plan.json',canonical(plan),exclusive=True)
    records=[]
    for index,item in enumerate(plan['order']):
        validate_launch(launch,phase='QA_VALIDATION')
        case=frozen_cases[item['qid']];arm=item['arm']
        actual_messages = None
        if case.get('evaluator_profile') == 'source-relations-fixed-history/v1':
            actual_messages = fixed_history_qa_input(case,arm)
            if actual_messages is None:
                row={**item,'status':'NO_PROVIDER_REQUEST','quality':'UNKNOWN',
                    'evidence_kind':'BUSINESS_EXECUTION_ASSERTION',
                    'control_status':'PRESENT',
                    'qa_answer_generated':False,'input_tokens':None,'output_tokens':None,
                    'cost_usd':0,'latency_seconds':None,'ttft_seconds':None}
                records.append(row);atomic_write(out/(str(index)+'.result.json'),canonical(row),exclusive=True)
                continue
        body={'model':model,'store':False,'input':actual_messages if actual_messages is not None else [
            {'role':'system','content':prompt},
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
             'truncated':receipt.get('truncated','UNKNOWN'),'model':model,
             'prompt_hash':digest(actual_messages) if actual_messages is not None else digest(prompt)}
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

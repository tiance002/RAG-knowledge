"""Fixed actual-history sidecars for a NEW isolated QA500 input policy.

No Gold assistant messages, candidate access, model calls, or admission claims.
Standalone cases always use empty history, including those whose old baseline
prefix contains FINAL identifiers. Semantic prefixes check IDs before contents.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.audit_rsi_selected_qa500_dev import binding,jsonl_rows,load,selections,verify_binding,partition_selections
from rsik.events import canonical,digest
from rsik.locking import restrict_directory

PROFILE='qa500-fixed-actual-history-sidecar/v1'
FORBIDDEN_HISTORY_KEYS={'answer_reference','ground_truth','gold_answer','gold','reference_answer'}


def read_bound_line(ref):
    verify_binding(ref['file'])
    lines=Path(ref['file']['path']).read_bytes().splitlines()
    number=ref['line_number']
    if type(number)is not int or not 1<=number<=len(lines):raise ValueError('QA500_BOUND_LINE_MISSING')
    raw=lines[number-1]
    if hashlib.sha256(raw).hexdigest()!=ref['line_sha256']:raise ValueError('QA500_BOUND_LINE_CHANGED')
    return json.loads(raw.decode('utf-8-sig'))


def reject_gold(row):
    if FORBIDDEN_HISTORY_KEYS&set(row) or row.get('answer_reference_provenance'):
        raise ValueError('QA500_GOLD_HISTORY_FORBIDDEN')


def prefix_from_actual(item,runtime_binding,final_ids):
    prior=item['prior_input_identities']
    # This guard runs before selecting any predecessor question/answer body.
    if any(r['item_id'] in final_ids for r in prior):
        raise ValueError('QA500_FINAL_PREDECESSOR_HISTORY_BLOCKED')
    result_ref=item['baseline_ref']
    if not result_ref:raise ValueError('QA500_ACTUAL_BASELINE_REQUIRED')
    current=read_bound_line(result_ref);reject_gold(current)
    if current.get('item_id')!=item['item_id']:raise ValueError('QA500_CURRENT_BASELINE_IDENTITY_MISMATCH')
    expected={r['item_id']:r for r in prior}
    results={}
    for number,h,row in jsonl_rows(result_ref['file']['path']):
        if row.get('item_id') not in expected:continue
        if row['item_id'] in results:raise ValueError('QA500_AMBIGUOUS_ACTUAL_PREFIX')
        results[row['item_id']]=(row,{'file':result_ref['file'],'line_number':number,'line_sha256':h})
    if set(results)!=set(expected):raise ValueError('QA500_ACTUAL_PREFIX_MISSING')
    messages=[];evidence=[];gaps=[]
    for identity in prior:
        qid=identity['item_id'];result,ref=results[qid]
        input_ref={'file':runtime_binding,'line_number':identity['line_number'],'line_sha256':identity['line_sha256']}
        user=read_bound_line(input_ref);reject_gold(user);reject_gold(result)
        if (any(user.get(k)!=identity[k] or result.get(k)!=identity[k] for k in ('item_id','session_id','index','turn_index'))
                or result.get('run_id')!=current.get('run_id')):
            raise ValueError('QA500_ACTUAL_PREFIX_IDENTITY_MISMATCH')
        chunks=[e.get('data',{}).get('content','') for e in result.get('events',[]) if e.get('event')=='answer_chunk']
        if any(not isinstance(s,str) for s in chunks) or ''.join(chunks)!=result.get('answer',''):
            raise ValueError('QA500_EMITTED_ANSWER_EVIDENCE_MISMATCH')
        question=user.get('question');answer=result.get('answer','')
        if not isinstance(question,str) or not isinstance(answer,str):raise ValueError('QA500_ACTUAL_PREFIX_TEXT_REQUIRED')
        messages.append({'role':'user','content':question})
        # Preserve partial/nonempty answers even on failed turns. Empty answers
        # remain absent exactly as the original serial runner constructed them.
        if answer:messages.append({'role':'assistant','content':answer})
        if not result.get('complete'):gaps.append(qid)
        evidence.append({'item_id':qid,'input':input_ref,'result':ref,'run_id':result.get('run_id'),
                         'complete':result.get('complete'),'assistant_message_present':bool(answer)})
    if (sum(m['role']=='user' for m in messages)!=current.get('prior_user_turns')
            or sum(m['role']=='assistant' for m in messages)!=current.get('prior_assistant_turns')
            or len(messages)+1!=current.get('message_count')):
        raise ValueError('QA500_ACTUAL_PREFIX_COUNT_MISMATCH')
    if set(gaps)!=set(current.get('context_gap_qids',[])):
        raise ValueError('QA500_ACTUAL_PREFIX_GAP_MISMATCH')
    return messages,evidence,gaps


def validate_sidecar(sidecar):
    if (sidecar.get('schema')!=PROFILE or sidecar.get('status')!='AUDIT_ONLY_NOT_ADMITTED'
            or sidecar.get('subject_sha256')!=digest({k:v for k,v in sidecar.items() if k!='subject_sha256'})
            or sidecar.get('history_sha256')!=digest(sidecar.get('history'))):
        raise ValueError('QA500_FIXED_HISTORY_SIDECAR_CHANGED')
    return sidecar


def paired_prompt_inputs(sidecar,*,question,context_a,context_b):
    """Return passive QA inputs; this neither formats nor sends model requests."""
    validate_sidecar(sidecar)
    if hashlib.sha256(question.encode()).hexdigest()!=sidecar['question_sha256']:
        raise ValueError('QA500_PAIRED_QUESTION_CHANGED')
    return {arm:{'question':question,'history':deepcopy(sidecar['history']),
                 'context':context,'history_sha256':sidecar['history_sha256'],
                 'execution_status':'PASSIVE_INPUT_ONLY_NOT_EXECUTED'}
            for arm,context in (('A',context_a),('B',context_b))}


def build(*,audit_report,output,partition='DEV'):
    report=load(audit_report)
    schema='qa500-selected-dev-passive-audit/v1' if partition=='DEV' else 'qa500-selected-final-passive-audit/v1'
    if (partition not in {'DEV','FINAL'} or report.get('schema')!=schema
            or report.get('status')!='AUDIT_ONLY_NOT_ADMITTED'
            or report.get('partition','DEV') != ('DEV' if partition=='DEV' else 'FINAL_HOLDOUT')):
        raise ValueError('QA500_PASSIVE_AUDIT_REQUIRED')
    for ref in report['inputs'].values():verify_binding(ref)
    dev,final=partition_selections(report['inputs']['dev_csv']['path'],report['inputs']['final_csv']['path'],partition)
    ids=[r['item_id'] for r in dev]
    if ids!=report['item_ids'] or ids!=[r['item_id'] for r in report['items']]:
        raise ValueError('QA500_SIDECAR_SELECTED_IDS_CHANGED')
    sidecars=[];blocked=[]
    for item in report['items']:
        if partition=='FINAL' and item.get('item_kind')!='standalone':
            raise ValueError('QA500_FINAL_STANDALONE_REQUIRED')
        qid=item['item_id'];identity=item['runtime_identity']
        ref={'file':report['inputs']['runtime_input'],'line_number':identity['line_number'],'line_sha256':identity['line_sha256']}
        current=read_bound_line(ref);reject_gold(current)
        if current['item_id']!=qid or hashlib.sha256(current['question'].encode()).hexdigest()!=item['question_sha256']:
            raise ValueError('QA500_SELECTED_RUNTIME_QUESTION_CHANGED')
        semantic=item['item_kind']=='session_turn'
        if item['item_kind'] not in {'session_turn','standalone'}:raise ValueError('QA500_ITEM_KIND_UNSUPPORTED')
        # Standalone NEVER enters prefix_from_actual, regardless of old FINAL
        # overlaps. No old predecessor body is assembled in the stateless mode.
        try:
            history,evidence,gaps=prefix_from_actual(item,report['inputs']['runtime_input'],final) if semantic else ([],[],[])
        except ValueError as error:
            blocked.append({'item_id':qid,'reason':str(error),'history_body_projected':False})
            continue
        sidecar={'schema':PROFILE,'status':'AUDIT_ONLY_NOT_ADMITTED','item_id':qid,
            'item_kind':item['item_kind'],'question_sha256':item['question_sha256'],
            'input_policy':'FIXED_ACTUAL_SEMANTIC_PREFIX' if semantic else 'EMPTY_HISTORY_NEW_ISOLATED_RUN',
            'baseline_reproduction_claim':False,'baseline_history_prefix_reproduction_claim':semantic,
            'source_stage':'HISTORY_ONLY_PAIRED_QA_INPUT_NOT_SELECTOR_MEMBERS',
            'control_class':item['capture']['control_class'],'history':history,'history_sha256':digest(history),
            'prefix_evidence':evidence,'current_input':ref,'current_baseline_ref':item['baseline_ref'] if semantic else None,
            'upstream_continuity_status':item['upstream_continuity_status'] if semantic else 'NEW_STATELESS_PROFILE',
            'prefix_record_status':('KNOWN_ACTUAL_PREFIX_WITH_FAILED_TURNS' if gaps else 'KNOWN_ACTUAL_PREFIX_COMPLETE') if semantic else 'EMPTY_NEW_STATELESS_INPUT',
            'declared_dependency_ids':item['declared_dependency_ids'],
            'failed_declared_dependency_ids':sorted(set(gaps)&set(item['declared_dependency_ids'])),
            'dependency_status':('UNKNOWN_DEPENDENCY' if not item['declared_dependency_metadata_complete'] or set(gaps)&set(item['declared_dependency_ids'])
                else 'DECLARED_DEPENDENCY_TURNS_RECORDED_COMPLETE') if semantic else 'NOT_APPLICABLE_STATELESS',
            'dependency_semantic_sufficiency_claim':False,
            'upstream_gap_ids':gaps,'upstream_gap_is_selector_failure':False,
            'old_history_FINAL_ids_metadata_only':item['prior_selected_FINAL_ids_metadata_only'],
            'old_standalone_history_body_assembled':False,'candidate_history_access':False,
            'audit_binding':binding(audit_report),'gold_history_used':False}
        if partition=='FINAL':sidecar['partition']='FINAL_HOLDOUT'
        sidecar['subject_sha256']=digest(sidecar);sidecars.append(sidecar)
    output=Path(output).resolve()
    if output.exists():raise ValueError('FRESH_QA500_HISTORY_OUTPUT_REQUIRED')
    output.mkdir(parents=True);restrict_directory(output)
    for sidecar in sidecars:(output/(sidecar['item_id']+'.history.restricted.json')).write_bytes(canonical(sidecar))
    summary={'schema':'qa500-history-sidecar-build/v1','status':'AUDIT_ONLY_NOT_ADMITTED',
        'audit':binding(audit_report),'selected_item_count':len(ids),'selected_item_ids':ids,
        'written_sidecars':len(sidecars),'blocked':blocked,'auto_dropped_or_replaced_items':0,
        'sidecar_files':{s['item_id']:binding(output/(s['item_id']+'.history.restricted.json')) for s in sidecars},
        'standalone_empty_history_count':sum(s['item_kind']=='standalone' for s in sidecars),
        'semantic_actual_history_count':sum(s['item_kind']=='session_turn' for s in sidecars),
        'upstream_unknown_count':sum(bool(s['upstream_gap_ids']) for s in sidecars),
        'execution_status':'NOT_EXECUTED','paid_calls':0,'production_qualified':False,
        'limitations':['Fixed passive history inputs only; no new evaluator, candidate seam, QA execution or qualification.',
            'Standalone inputs are a new stateless epoch; original baseline labels are selection strata only.']}
    (output/'build-summary.restricted.json').write_bytes(canonical(summary))
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--audit-report',required=True);p.add_argument('--output',required=True)
    p.add_argument('--partition',choices=['DEV','FINAL'],default='DEV')
    a=p.parse_args();r=build(**vars(a));print(json.dumps({k:r[k] for k in ('status','selected_item_count','written_sidecars','blocked','upstream_unknown_count')}))

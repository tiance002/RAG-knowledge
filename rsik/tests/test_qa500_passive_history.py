"""Synthetic DEV/history controls only; no service, model or actual QA run."""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts.audit_rsi_selected_qa500_dev import audit,binding,load,snapshot_paths,source_references
from scripts.build_rsi_qa500_history_sidecars import (build,paired_prompt_inputs,prefix_from_actual,
                                                     validate_sidecar)
from rsik.events import canonical,digest


def write_jsonl(path,rows):
    path.write_bytes(b'\n'.join(canonical(r) for r in rows)+b'\n')


def control(tmp_path,*,gold_prior=False,required_failed=False):
    dev=tmp_path/'dev.csv';final=tmp_path/'final.csv'
    dev.write_bytes(b'item_id,existing_answer_label,existing_citation_label\nstandalone-a,CORRECT,NONE\nfollowup-a,CORRECT,ACCURATE\nfollowup-b,CORRECT,ACCURATE\nstandalone-b,PARTIAL,PARTIAL\n')
    final.write_bytes(b'item_id\nFINAL-SYNTHETIC-ID\n')
    specs=[('FINAL-SYNTHETIC-ID','standalone-session',0),('standalone-a','standalone-session',1),
           ('ancestor-a','semantic-a',0),('ancestor-failed','semantic-a',1),('followup-a','semantic-a',2),
           ('ancestor-b','semantic-b',0),('followup-b','semantic-b',1),('standalone-b','stateless-b',0)]
    runtime=[];results=[]
    for index,(qid,sid,turn) in enumerate(specs):
        question='SYNTHETIC question '+qid
        runtime.append({'item_id':qid,'session_id':sid,'index':index,'turn_index':turn,'question':question})
        answer='' if qid=='ancestor-failed' else 'SYNTHETIC actual answer '+qid
        if qid=='followup-a':users,assist=2,1;gaps=['ancestor-failed']
        else:users=assist=turn;gaps=[]
        r={'item_id':qid,'session_id':sid,'index':index,'turn_index':turn,'run_id':'SYNTHETIC-ACTUAL-RUN',
           'answer':answer,'complete':qid!='ancestor-failed','prior_user_turns':users,'prior_assistant_turns':assist,
           'message_count':users+assist+1,'context_gap_qids':gaps,
           'context_continuity_status':'UNKNOWN_DUE_TO_PRIOR_FAILED_TURN' if gaps else 'ACTUAL_HISTORY_COMPLETE',
           'events':[{'event':'answer_chunk','data':{'content':answer}}] if answer else []}
        if gold_prior and qid=='ancestor-a':r['answer_reference']='SYNTHETIC forbidden Gold'
        if qid=='followup-b':
            content='C'*4096
            r['events'].append({'event':'rag_result','data':{'citations':[{'content':content,
               'content_hash':hashlib.sha256(content.encode()).hexdigest(),'context_group_id':'CONTROL-GROUP',
               'chunk_id':'CONTROL-CHUNK','fileId':'CONTROL-FILE','kb_id':'CONTROL-KB','seed_child_ids':['CONTROL-SEED'],
               'parent_chunk_id':'CONTROL-PARENT'}],'expansion_delivery_status':'DELIVERED','source_mapping_status':'VERIFIED'}})
        results.append(r)
    inp=tmp_path/'input.jsonl';write_jsonl(inp,runtime)
    baseline=tmp_path/'baseline';baseline.mkdir();write_jsonl(baseline/'results.jsonl',results)
    items=[]
    for qid in ('standalone-a','followup-a','followup-b','standalone-b'):
        x=next(r for r in runtime if r['item_id']==qid);semantic=qid.startswith('followup')
        item={'item_id':qid,'item_kind':'session_turn' if semantic else 'standalone','language':'en',
              'question':x['question'],'ground_truth':'SYNTHETIC GOLD NEVER HISTORY','evidence':[]}
        if semantic:item.update(session_id=x['session_id'],turn_index=x['turn_index']+1,
            depends_on_turns=[2] if required_failed and qid=='followup-a' else [1],
            transitive_ancestor_turns=[2] if required_failed and qid=='followup-a' else [1])
        items.append(item)
    gold=tmp_path/'gold.json';gold.write_bytes(canonical({'items':items}))
    reviews=tmp_path/'reviews.jsonl';write_jsonl(reviews,[{'item_id':x['item_id'],'answer_grade':'CONTROL','citation_grade':'NONE'} for x in items])
    snapshot=tmp_path/'snapshot';snapshot.mkdir()
    out=tmp_path/'audit';report=audit(dev_csv=dev,final_csv=final,gold=gold,audit_reviews=reviews,
         runtime_input=inp,baseline_root=baseline,source_snapshot=snapshot,output=out,expected_count=4)
    return report,out/'audit.restricted.json',inp,baseline


def test_passive_audit_and_sidecars_keep_questions_and_new_stateless_policy(tmp_path):
    report,path,_,_=control(tmp_path)
    assert report['status']=='AUDIT_ONLY_NOT_ADMITTED' and report['selected_dev_count']==4
    assert report['summary']['over4000_group_spec_mismatch_rows']==1
    assert 'PER expanded group' in next(x for x in report['items'] if x['item_id']=='followup-b')['capture']['length_rule']
    result=build(audit_report=path,output=tmp_path/'sidecars')
    assert result['selected_item_count']==result['written_sidecars']==4
    assert result['auto_dropped_or_replaced_items']==0 and not result['blocked']
    standalone=load(tmp_path/'sidecars/standalone-a.history.restricted.json')
    assert standalone['history']==[] and standalone['old_history_FINAL_ids_metadata_only']==['FINAL-SYNTHETIC-ID']
    assert not standalone['old_standalone_history_body_assembled']
    assert standalone['input_policy']=='EMPTY_HISTORY_NEW_ISOLATED_RUN'
    assert not standalone['baseline_reproduction_claim']
    request=load(path.parent/'readonly-fetch-request.restricted.json')
    assert request['remote_calls_made']==0 and not request['service_or_index_start_allowed']
    assert not request['whole_database_copy_allowed'] and not request['final_content_requested']


def test_actual_failed_prefix_is_preserved_and_irrelevant_gap_is_separate(tmp_path):
    _,path,_,_=control(tmp_path)
    build(audit_report=path,output=tmp_path/'sidecars')
    s=load(tmp_path/'sidecars/followup-a.history.restricted.json')
    assert [m['role'] for m in s['history']]==['user','assistant','user']
    assert s['upstream_gap_ids']==['ancestor-failed']
    assert s['prefix_record_status']=='KNOWN_ACTUAL_PREFIX_WITH_FAILED_TURNS'
    assert s['upstream_continuity_status']=='UNKNOWN_DUE_TO_PRIOR_FAILED_TURN'
    assert s['dependency_status']=='DECLARED_DEPENDENCY_TURNS_RECORDED_COMPLETE'
    assert s['failed_declared_dependency_ids']==[] and not s['dependency_semantic_sufficiency_claim']
    assert not s['upstream_gap_is_selector_failure']


def test_required_failed_dependency_remains_unknown_without_gold_repair(tmp_path):
    _,path,_,_=control(tmp_path,required_failed=True)
    build(audit_report=path,output=tmp_path/'sidecars')
    s=load(tmp_path/'sidecars/followup-a.history.restricted.json')
    assert s['dependency_status']=='UNKNOWN_DEPENDENCY'
    assert s['failed_declared_dependency_ids']==['ancestor-failed']
    assert len(s['history'])==3 and not s['gold_history_used']


def test_gold_answer_reference_is_refused_not_used_as_actual_history(tmp_path):
    _,path,_,_=control(tmp_path,gold_prior=True)
    result=build(audit_report=path,output=tmp_path/'sidecars')
    assert result['selected_item_count']==4 and result['written_sidecars']==3
    assert result['blocked']==[{'item_id':'followup-a','reason':'QA500_GOLD_HISTORY_FORBIDDEN','history_body_projected':False}]
    assert result['auto_dropped_or_replaced_items']==0


def test_final_predecessor_guard_runs_before_history_body_access(tmp_path,monkeypatch):
    report,_,_,_=control(tmp_path)
    item=deepcopy(next(r for r in report['items'] if r['item_id']=='followup-a'))
    item['prior_input_identities'][0]['item_id']='FINAL-SYNTHETIC-ID'
    import scripts.build_rsi_qa500_history_sidecars as module
    monkeypatch.setattr(module,'read_bound_line',lambda *a,**k:pytest.fail('History body accessed before FINAL guard'))
    with pytest.raises(ValueError,match='FINAL_PREDECESSOR_HISTORY_BLOCKED'):
        prefix_from_actual(item,report['inputs']['runtime_input'],{'FINAL-SYNTHETIC-ID'})


def test_sidecar_tamper_and_changed_bound_inputs_fail(tmp_path):
    _,path,inp,_=control(tmp_path)
    build(audit_report=path,output=tmp_path/'sidecars')
    s=load(tmp_path/'sidecars/followup-b.history.restricted.json');s['history'][0]['content']='changed'
    with pytest.raises(ValueError,match='FIXED_HISTORY_SIDECAR_CHANGED'):validate_sidecar(s)
    inp.write_bytes(inp.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='BOUND_INPUT_CHANGED'):build(audit_report=path,output=tmp_path/'rejected')


def test_passive_pair_uses_identical_fixed_history_in_both_arms(tmp_path):
    _,path,_,_=control(tmp_path)
    build(audit_report=path,output=tmp_path/'sidecars')
    s=load(tmp_path/'sidecars/followup-b.history.restricted.json')
    pair=paired_prompt_inputs(s,question='SYNTHETIC question followup-b',context_a='source A',context_b='source B')
    assert pair['A']['history']==pair['B']['history']==s['history']
    assert pair['A']['context']!=pair['B']['context']
    pair['A']['history'][0]['content']='isolated mutation'
    assert pair['B']['history']==s['history']
    with pytest.raises(ValueError,match='PAIRED_QUESTION_CHANGED'):
        paired_prompt_inputs(s,question='other',context_a='',context_b='')


def test_bound_manifest_resolves_supplemental_sources_and_checks_actual_bytes(tmp_path):
    local=tmp_path/'supplemental.txt';local.write_bytes(b'SYNTHETIC source bytes')
    h=hashlib.sha256(local.read_bytes()).hexdigest();remote='/CONTROL/original.txt'
    manifest=tmp_path/'snapshot.json'
    row={'path':remote,'local_path':str(local),'remote_sha256':h,'local_sha256':h}
    manifest.write_bytes(canonical({'schema':'qa500-ecs-source-snapshot/v2','files':[], 'additional_files':[row]}))
    index,ref=snapshot_paths(manifest)
    item={'item_id':'CONTROL','evidence':[{'source_path':remote,'source_sha256':h}]}
    result=source_references(item,tmp_path/'missing-primary-mirror',index,ref)
    assert result[0]['source_byte_status']=='HASH_MATCH'
    assert result[0]['snapshot_lookup']=='BOUND_MANIFEST_EXACT_REMOTE_PATH'
    assert result[0]['local_binding']==binding(local)
    local.write_bytes(b'changed')
    with pytest.raises(ValueError,match='SOURCE_MANIFEST_BYTES_CHANGED'):
        source_references(item,tmp_path/'missing-primary-mirror',index,ref)
    manifest.write_bytes(canonical({'schema':'qa500-ecs-source-snapshot/v2','files':[row],
                                   'additional_files':[{**row,'remote_sha256':'0'*64}]}))
    with pytest.raises(ValueError,match='SNAPSHOT_REMOTE_IDENTITY_CONFLICT'):snapshot_paths(manifest)

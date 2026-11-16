"""Synthetic evaluator data only; no real execution/admission is fabricated."""
import copy
import json
from pathlib import Path
import shutil

import pytest

from rsik import fixed_history_coverage as coverage
from rsik import fixed_history_profile as profile
from rsik import source_relations as relations
from rsik.events import canonical,digest
from rsik.execution_evidence import ExecutionEvidence
from rsik.fixed_history_worker import build_worker
from rsik.query_aware import QueryRegistry
from test_source_relations import _build_fixture,_capture,_append_citations


def write(path,obj):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(canonical(obj));return path


def test_final_fixture_refused_before_profile_or_body_read(tmp_path,monkeypatch):
    write(tmp_path/'manifest.json',{'partition':'FINAL_HOLDOUT'})
    monkeypatch.setattr(profile,'check_fixture',lambda *a,**k:pytest.fail('FINAL body must not be read'))
    with pytest.raises(ValueError,match='FIXED_COVERAGE_DEV_ONLY'):
        coverage.fixture_inputs(tmp_path)


def fixture(tmp_path,monkeypatch):
    # History provenance is deliberately a synthetic validator double here;
    # tests cannot pass REAL_REPLAY receipt verification using this fixture.
    monkeypatch.setattr(profile,'validate_history',lambda sidecar,**kwargs:sidecar['history'])
    old=_build_fixture(tmp_path/'legacy',count=17)
    new=tmp_path/'fixed';shutil.copytree(old,new)
    cases=profile._load(new/'cases.json');specs=profile._load(new/'relations.restricted.json')
    controls=[];sidecars={};business={}
    for i in range(3):
        qid=f'BUSINESS-{i}'
        cases.append({'qid':qid,'case':{'question':'SYNTHETIC business query '+str(i),'allowed_kb_ids':['CONTROL-KB'],
            'files':[],'chunks':[],'seed_ids':[],'expected_seed_count':0}})
        business[qid]={'expected_status':'INSUFFICIENT_EVIDENCE','expected_provider_requests':0}
    for i,item in enumerate(cases):
        qid=item['qid'];case=item['case'];sidecar={'schema':profile.HISTORY_PROFILE,'item_id':qid,
            'item_kind':'session_turn' if i<6 else 'standalone','history':[],
            'question_sha256':relations.sha(case['question'].encode()),
            'control_class':'SOURCE_CONTEXT_CAPTURE' if qid in specs else 'CONTROL_NO_RETRIEVAL'}
        path=write(new/'history'/f'{qid}.json',sidecar)
        sidecars[qid]={'path':path.relative_to(new).as_posix(),'sha256':relations.sha(path.read_bytes())}
        if qid in specs:controls.append({'qid':qid,'atom_id':specs[qid]['atoms'][0]['atom_id'],'removed_member_ids':['m2']})
    write(new/'cases.json',cases);write(new/'control-spec.restricted.json',controls)
    manifest=profile._load(new/'manifest.json');manifest.update(profile=profile.PROFILE,partition='DEV',history_profile=profile.HISTORY_PROFILE,
        history_sidecars=sidecars,business_controls=business,
        artifact_hashes={n:relations.sha((new/n).read_bytes()) for n in ('cases.json','relations.restricted.json')})
    write(new/'manifest.json',manifest)
    return old,new,cases,specs


def tag_capture(cap,case,sidecar):
    qsha=sidecar['question_sha256'];messages=[{'role':'user','content':case['question']}]
    cap.update(evaluator_profile=profile.PROFILE,history_profile=profile.HISTORY_PROFILE,
        history_sha256=digest([]),sidecar_sha256=digest(sidecar),qid=sidecar['item_id'],
        base_question_sha256=qsha,current_question_sha256=qsha,query_derivation_sha256=digest(None),
        business_input_messages=messages,business_input_messages_sha256=digest(messages))
    if cap.get('request_calls')==0:return cap
    cap.update(request_calls=1,provider_delivery_status='PROVIDER_REQUEST_CAPTURED')
    cap['messages']+=messages
    return cap


def record(tmp_path,monkeypatch):
    old,new,cases,specs=fixture(tmp_path,monkeypatch);controls=[];business=[];tasks=[]
    manifest=profile._load(new/'manifest.json')
    for item in cases:
        qid=item['qid'];case=item['case'];sidecar=profile._load(new/manifest['history_sidecars'][qid]['path'])
        if qid in specs:
            pos=tag_capture(_append_citations(_capture(case),specs[qid]),case,sidecar)
            neg=tag_capture(_append_citations(_capture(case,include=('m1',)),specs[qid]),case,sidecar)
            positive=write(tmp_path/'captures'/f'{qid}-positive.json',pos);negative=write(tmp_path/'captures'/f'{qid}-negative.json',neg)
            controls.append({'fixture':str(new.resolve()),'qid':qid,'atom_id':specs[qid]['atoms'][0]['atom_id'],
                'removed_member_ids':['m2'],'positive_capture':str(positive),'negative_capture':str(negative),
                'positive_sha256':relations.sha(positive.read_bytes()),'negative_sha256':relations.sha(negative.read_bytes())})
            domain='IR_SOURCE_FACTS'
        else:
            data={'answer_mode':'INSUFFICIENT_EVIDENCE','retrieval_status':'NO_RESULTS','generation_status':'NOT_RUN'}
            cap=tag_capture({'status':'OK','members':[],'context':'','messages':[],'request_calls':0,
                'provider_delivery_status':'NO_PROVIDER_REQUEST','terminal_events':[{'event':'done','data':data}],
                'business_outcome':{**data,'terminal_event':'done','provider_calls':0}},case,sidecar)
            path=write(tmp_path/'captures'/f'{qid}.json',cap)
            business.append({'fixture':str(new.resolve()),'qid':qid,'capture':str(path),'sha256':relations.sha(path.read_bytes())})
            domain='BUSINESS_BRANCH_CONTROL'
        tasks.append({'fixture':str(new.resolve()),'qid':qid,'domain':domain})
    data={'schema':coverage.RECORD_SCHEMA,'evaluator_profile':profile.PROFILE,'base_dir':str(tmp_path),'fixtures':[str(new.resolve())],
        'controls':controls,'business_controls':business,'execution_tasks':tasks,'review_policy':'research-source-family/v1'}
    return old,new,data


def test_seventeen_IR_matrix_matches_exact_legacy_formulas_and_controls_add_zero_credit(tmp_path,monkeypatch):
    old,new,data=record(tmp_path,monkeypatch)
    result=coverage.recompute_coverage(data)
    old_controls=[{**c,'fixture':str(old.resolve())} for c in data['controls']]
    legacy=relations.recompute_coverage({'schema':'rsi-source-relations-coverage/v1','fixtures':[str(old.resolve())],
        'controls':old_controls,'review_policy':'research-source-family/v1','base_dir':str(tmp_path)})
    assert result['matrix']==legacy['matrix']
    assert result['status']=='PASS'
    assert result['fixed_slot_counts']=={'total':20,'retrieval':17,'business_controls':3,'dropped':0}
    assert all(c['IR_coverage_credit']==0 and c['context_chars']==0 for c in result['business_controls'])
    assert all(r['positive_example_count']==17 and r['negative_control_count']==17 for r in result['matrix'].values())


def test_business_no_call_and_all_twenty_slots_are_mandatory(tmp_path,monkeypatch):
    _,_,data=record(tmp_path,monkeypatch)
    changed=copy.deepcopy(data);changed['execution_tasks']=changed['execution_tasks'][:-1]
    with pytest.raises(ValueError,match='ALL_20_SLOTS_REQUIRED'):coverage.recompute_coverage(changed)
    row=data['business_controls'][0];cap=profile._load(row['capture']);cap['request_calls']=1
    write(row['capture'],cap);row['sha256']=relations.sha(Path(row['capture']).read_bytes())
    assert coverage.recompute_coverage(data)['status']=='BLOCKED_BUSINESS_CONTROL'


def test_old_or_synthetic_receipts_cannot_upgrade_new_coverage():
    with pytest.raises(ValueError,match='FIXED_REAL_COVERAGE_EXECUTION_REQUIRED'):
        coverage.verify_coverage_execution({'schema':coverage.RECORD_SCHEMA},ExecutionEvidence.synthetic_control())
    with pytest.raises(ValueError,match='FIXED_COVERAGE_NEW_RECORD_REQUIRED'):
        coverage.recompute_coverage({'schema':'rsi-source-relations-coverage/v1'})


def test_collection_plan_covers_all_atoms_and_three_business_slots(tmp_path,monkeypatch):
    from rsik.launch import file_binding
    _,new,_,_=fixture(tmp_path,monkeypatch)
    registry=QueryRegistry(tmp_path/'registry');cid=registry.put(build_worker(),b'{"selection":"all"}',
        parent_id='OWNER_ROOT',origin='OWNER_SPECIFIED',evidence={'SYNTHETIC_ONLY':True})
    plan=coverage.prepare_collection(registry=registry.directory,candidate_id=cid,fixtures=[new],output=tmp_path/'prepared')
    launch={'fixtures':plan['fixtures'],'coverage_collection':file_binding(tmp_path/'prepared/collection-plan.restricted.json')}
    tasks=coverage.collection_tasks(launch,registry.directory)
    assert len(tasks)==20 and sum(t['domain']=='BUSINESS_BRANCH_CONTROL' for t in tasks)==3
    assert all(len(t['controls'])==1 for t in tasks if t['domain']=='BUSINESS_BRANCH_CONTROL')
    assert all(len(t['controls'])==2 for t in tasks if t['domain']=='IR_SOURCE_FACTS')
    bad=copy.deepcopy(plan);bad['tasks']=bad['tasks'][:-1]
    bad['review']['object_sha256']=digest({k:v for k,v in bad.items() if k!='review'})
    write(tmp_path/'prepared/collection-plan.restricted.json',bad)
    launch['coverage_collection']=file_binding(tmp_path/'prepared/collection-plan.restricted.json')
    with pytest.raises(ValueError,match='ALL_20_SLOTS_REQUIRED'):coverage.collection_tasks(launch,registry.directory)

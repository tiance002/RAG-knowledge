"""Invented FINAL transport fixtures only; no actual selected FINAL access."""
from copy import deepcopy
import json
import pytest
from rsik.events import canonical, digest
from rsik import fixed_history_profile as profile
from rsik.exposure import ExposureLedger
from scripts.audit_rsi_selected_qa500_dev import audit
from scripts.build_rsi_qa500_history_sidecars import build
from test_source_relations import _build_fixture


def write_lines(path,rows):
    path.write_bytes(b'\n'.join(canonical(row) for row in rows)+b'\n')


def final_fixture(tmp_path,control_count=0):
    fixture=_build_fixture(tmp_path/'fixture',count=20,source_review='AI_REVIEWED')
    cases=json.loads((fixture/'cases.json').read_text())
    specs=json.loads((fixture/'relations.restricted.json').read_text())
    controls={}
    for item in cases[20-control_count:] if control_count else []:
        specs.pop(item['qid'])
        item['case'].update(seed_ids=[],expected_seed_count=0)
        controls[item['qid']]={'expected_status':'INSUFFICIENT_EVIDENCE','expected_provider_requests':0}
    (fixture/'cases.json').write_bytes(canonical(cases))
    (fixture/'relations.restricted.json').write_bytes(canonical(specs))
    dev=tmp_path/'dev.csv';dev.write_text('item_id\nSYNTHETIC-DEV-ONLY\n')
    final=tmp_path/'final.csv';final.write_text('item_id\n'+'\n'.join(i['qid'] for i in cases)+'\n')
    gold=tmp_path/'gold.json';gold.write_bytes(canonical({'items':[{'item_id':i['qid'],
        'item_kind':'standalone','language':'en','question':i['case']['question'],
        'ground_truth':'INVENTED GOLD NEVER HISTORY','evidence':[]} for i in cases]}))
    runtime=tmp_path/'runtime.jsonl'
    write_lines(runtime,[{'item_id':i['qid'],'session_id':'session-'+i['qid'],'index':n,
        'turn_index':0,'question':i['case']['question']} for n,i in enumerate(cases)])
    baseline=tmp_path/'baseline';baseline.mkdir()
    write_lines(baseline/'results.jsonl',[{'item_id':i['qid'],'session_id':'session-'+i['qid'],
        'index':n,'turn_index':0,'run_id':'SYNTHETIC-FINAL','complete':True,'answer':'SYNTHETIC',
        'events':[{'event':'answer_chunk','data':{'content':'SYNTHETIC'}}]+
                 ([{'event':'rag_result','data':{'citations':[]}}] if n<20-control_count else [])}
        for n,i in enumerate(cases)])
    reviews=tmp_path/'reviews.jsonl';write_lines(reviews,[{'item_id':i['qid']} for i in cases])
    snapshot=tmp_path/'source-snapshot';snapshot.mkdir()
    audit(dev_csv=dev,final_csv=final,gold=gold,audit_reviews=reviews,runtime_input=runtime,
        baseline_root=baseline,source_snapshot=snapshot,output=tmp_path/'audit',partition='FINAL')
    sidecars=fixture/'histories'
    build(audit_report=tmp_path/'audit/audit.restricted.json',output=sidecars,partition='FINAL')
    ledger=ExposureLedger(tmp_path/'synthetic-exposure.sqlite')
    source_ids=[]
    for spec in specs.values():
        for sid,s in spec['sources'].items():
            source_ids.append(sid)
            row={k:s[k] for k in ('document_id','version','content_sha256','source_family_id',
                'origin','relation','related_source_ids','exposure_status')}
            row['source_id']=sid
            ledger.register(row,review={'status':'AI_REVIEWED','subject_sha256':digest(row),
                'reviewer':'SYNTHETIC','reviewed_at':'2026-11-16T00:00:00Z'})
    manifest=json.loads((fixture/'manifest.json').read_text())
    manifest['artifact_hashes']={name:profile.relations.sha((fixture/name).read_bytes())
        for name in ('cases.json','relations.restricted.json')}
    manifest.update(profile=profile.PROFILE,history_profile=profile.HISTORY_PROFILE,partition='FINAL_HOLDOUT',
        experiment_id='SYNTHETIC-FINAL',exposure_snapshot=ledger.snapshot(source_ids,
            experiment_id='SYNTHETIC-FINAL',review_policy='research-source-family/v1'),business_controls=controls,
        case_domain_counts={'retrieval_questions':20-control_count,'business_controls':control_count,'actual_history_questions':0,'empty_history_questions':20},
        history_sidecars={i['qid']:{'path':'histories/'+i['qid']+'.history.restricted.json',
            'sha256':profile.relations.sha((sidecars/(i['qid']+'.history.restricted.json')).read_bytes())} for i in cases})
    (fixture/'manifest.json').write_bytes(canonical(manifest))
    return fixture,manifest,cases


def test_final20_ir_empty_histories_use_frozen_final_partition(tmp_path):
    fixture,manifest,cases=final_fixture(tmp_path)
    result=profile.check_fixture(fixture,review_policy='research-source-family/v1')
    assert result['retrieval_questions']==20 and result['business_controls']==0
    assert result['actual_history_questions']==0 and result['empty_history_questions']==20
    rows=[{'qid':i['qid'],'domain':'RETRIEVAL_SOURCE_FACT','chars_A':100,'chars_B':50} for i in cases]
    accounting=profile.fixed_slot_accounting(rows,[i['qid'] for i in cases],expected_counts=result)
    assert accounting['total_questions']==20 and accounting['control_character_ties']==0
    assert result['production_qualified'] is False


def test_final_sidecar_cannot_be_relabelled_dev_or_business_control(tmp_path):
    fixture,manifest,cases=final_fixture(tmp_path)
    qid=cases[0]['qid'];path=fixture/manifest['history_sidecars'][qid]['path']
    sidecar=json.loads(path.read_text())
    with pytest.raises(ValueError,match='PARTITION_MISMATCH'):
        profile.validate_history(sidecar,qid=qid,question=cases[0]['case']['question'],expected_partition='DEV')
    changed=deepcopy(sidecar);changed['partition']='DEV'
    changed['subject_sha256']=digest({k:v for k,v in changed.items() if k!='subject_sha256'})
    with pytest.raises(ValueError,match='AUDIT_PARTITION_MISMATCH'):
        profile.validate_history(changed,qid=qid,question=cases[0]['case']['question'])
    changed=deepcopy(sidecar);changed['control_class']='CONTROL_NO_RETRIEVAL'
    changed['subject_sha256']=digest({k:v for k,v in changed.items() if k!='subject_sha256'})
    with pytest.raises(ValueError,match='ORIGINAL_CONTROL_CLASS_CHANGED'):
        profile.validate_history(changed,qid=qid,question=cases[0]['case']['question'])
    manifest['case_domain_counts']['retrieval_questions']=19
    (fixture/'manifest.json').write_bytes(canonical(manifest))
    with pytest.raises(ValueError,match='FROZEN_DOMAIN_COUNTS_MISMATCH'):
        profile.check_fixture(fixture,review_policy='research-source-family/v1')


def test_final_builder_requires_explicit_partition_and_standalone(tmp_path):
    fixture,manifest,cases=final_fixture(tmp_path)
    audit_path=tmp_path/'audit/audit.restricted.json'
    with pytest.raises(ValueError,match='PASSIVE_AUDIT_REQUIRED'):
        build(audit_report=audit_path,output=tmp_path/'wrong-dev')
    report=json.loads(audit_path.read_text());report['items'][0]['item_kind']='session_turn'
    audit_path.write_bytes(canonical(report))
    with pytest.raises(ValueError,match='FINAL_STANDALONE_REQUIRED'):
        build(audit_report=audit_path,output=tmp_path/'wrong-final',partition='FINAL')


def test_final_actual_no_retrieval_classifier_keeps_control_slot(tmp_path):
    fixture,manifest,cases=final_fixture(tmp_path,control_count=1)
    result=profile.check_fixture(fixture,review_policy='research-source-family/v1')
    assert result['retrieval_questions']==19 and result['business_controls']==1
    rows=[{'qid':i['qid'],'domain':'BUSINESS_BRANCH_CONTROL' if i['qid'] in manifest['business_controls']
           else 'RETRIEVAL_SOURCE_FACT','chars_A':0 if i['qid'] in manifest['business_controls'] else 100,
           'chars_B':0 if i['qid'] in manifest['business_controls'] else 50} for i in cases]
    assert profile.fixed_slot_accounting(rows,[i['qid'] for i in cases],expected_counts=result)['control_character_ties']==1

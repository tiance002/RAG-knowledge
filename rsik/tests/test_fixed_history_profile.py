"""Engineering controls; synthetic fixtures confer no real admission."""
from copy import deepcopy
import json
from pathlib import Path
import pytest

from rsik import fixed_history_profile as profile
from rsik.events import digest, canonical
from rsik.execution_evidence import ExecutionEvidence
from rsik.baseline_qualification import adapters
from test_source_relations import _build_fixture, _capture, _append_citations
from test_qa500_passive_history import control
from scripts.build_rsi_qa500_history_sidecars import build


def sidecar_control(tmp_path):
    _, audit, _, _ = control(tmp_path)
    output = tmp_path/'history'
    build(audit_report=audit,output=output)
    sidecar = json.loads((output/'followup-a.history.restricted.json').read_text(encoding='utf-8'))
    return sidecar, 'SYNTHETIC question followup-a'


def test_actual_history_query_and_upstream_tamper_are_rechecked(tmp_path):
    sidecar, question = sidecar_control(tmp_path)
    assert profile.validate_history(sidecar,qid='followup-a',question=question) == sidecar['history']
    with pytest.raises(ValueError,match='CURRENT_USER_CHANGED|QUERY_BINDING|DERIVATION_REQUIRED'):
        profile.validate_history(sidecar,qid='followup-a',question=question+'changed')
    changed = deepcopy(sidecar); changed['history'][1]['content'] += ' forged'
    changed['history_sha256'] = digest(changed['history'])
    changed['subject_sha256'] = digest({k:v for k,v in changed.items() if k!='subject_sha256'})
    with pytest.raises(ValueError,match='ACTUAL_PREFIX_CHANGED'):
        profile.validate_history(changed,qid='followup-a',question=question)


def test_history_cannot_supply_missing_retrieved_support(tmp_path,monkeypatch):
    fixture = _build_fixture(tmp_path/'fixture')
    item = json.loads((fixture/'cases.json').read_text())[0]
    spec = json.loads((fixture/'relations.restricted.json').read_text())[item['qid']]
    sources = {sid:(fixture/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
    history = [{'role':'user','content':'SYNTHETIC'}, {'role':'assistant','content':' '.join(s['text'] for a in spec['atoms'] for s in a['supports'])}]
    question_hash=profile.relations.sha(item['case']['question'].encode())
    sidecar = {'item_id':item['qid'],'history':history,'question_sha256':question_hash}
    monkeypatch.setattr(profile,'validate_history',lambda *a,**k:history)
    capture = _append_citations(_capture(item['case'],include=('m1',)),spec)
    capture.update(evaluator_profile=profile.PROFILE,history_profile=profile.HISTORY_PROFILE,
        history_sha256=digest(history),sidecar_sha256=digest(sidecar),request_calls=1,
        qid=item['qid'],business_input_messages=history+[{'role':'user','content':item['case']['question']}],
        base_question_sha256=question_hash,current_question_sha256=question_hash,query_derivation_sha256=digest(None),
        business_input_messages_sha256=digest(history+[{'role':'user','content':item['case']['question']}]),
        provider_delivery_status='PROVIDER_REQUEST_CAPTURED')
    capture['messages'] += history + [{'role':'user','content':item['case']['question']}]
    result = profile.assess(item['case'],capture,spec,sources,sidecar=sidecar)
    assert result['status'] == 'ABSENT'
    assert result['history_is_retrieved_support'] is False
    capture['history_sha256'] = '0'*64
    assert profile.assess(item['case'],capture,spec,sources,sidecar=sidecar)['status'] == 'UNKNOWN'


def no_call():
    data = {'answer_mode':'INSUFFICIENT_EVIDENCE','retrieval_status':'NO_RESULTS','generation_status':'SKIPPED'}
    return {'status':'OK','request_calls':0,'messages':[],'context':'','members':[],
        'provider_delivery_status':'NO_PROVIDER_REQUEST','terminal_events':[{'event':'done','data':data}],
        'business_outcome':{**data,'terminal_event':'done','provider_calls':0}}


def test_business_control_is_separate_and_requires_actual_terminal():
    capture=no_call()
    assert profile.business_control(capture)['status']=='PRESENT'
    assert profile.business_control(capture)['retrieval_atoms']==[]
    capture['terminal_events']=[]
    assert profile.business_control(capture)['status']=='UNKNOWN'
    capture=no_call();capture['request_calls']=1
    assert profile.business_control(capture)['status']=='UNKNOWN'


def test_fixed20_keeps_three_zero_character_ties():
    rows=[{'qid':str(i),'domain':'BUSINESS_BRANCH_CONTROL' if i>=17 else 'RETRIEVAL_SOURCE_FACT',
           'chars_A':0 if i>=17 else 100,'chars_B':0 if i>=17 else 50} for i in range(20)]
    qids=[r['qid'] for r in rows]
    assert profile.fixed_slot_accounting(rows,qids)['control_character_ties']==3
    with pytest.raises(ValueError,match='20_SLOTS'):
        profile.fixed_slot_accounting(rows[:17],qids[:17])
    rows[-1]['chars_B']=1
    with pytest.raises(ValueError,match='ZERO_CHAR'):
        profile.fixed_slot_accounting(rows,qids)


def test_explicit_profile_dispatch_preserves_old_adapter(tmp_path):
    path=tmp_path/'manifest.json';path.write_bytes(canonical({'profile':profile.PROFILE}))
    assert adapters(tmp_path)[0] is profile.check_fixture
    path.write_bytes(canonical({'profile':'source-relations/v1'}))
    from rsik.source_relations import check_fixture
    assert adapters(tmp_path)[0] is check_fixture


def test_receipt_is_mandatory_even_for_synthetic_control(tmp_path):
    with pytest.raises(ValueError,match='RECEIPT_REQUIRED'):
        profile._receipt(ExecutionEvidence.synthetic_control(),tmp_path/'absent',manifest={},previous_sequence=0)


def test_cross_history_receipt_and_wrong_run_rejected(tmp_path,monkeypatch):
    import rsik.execution_evidence as evidence
    monkeypatch.setattr(evidence,'check_receipt',lambda *a,**k:{})
    fields={'history_sha256':'a'*64,'sidecar_sha256':'b'*64,'qid':'q','lineage_id':'new',
        'base_question_sha256':'f'*64,'current_question_sha256':'f'*64,'query_derivation_sha256':digest(None)}
    body={**fields,'evaluator_profile':profile.PROFILE,'history_profile':profile.HISTORY_PROFILE}
    capture=tmp_path/'capture';capture.write_bytes(canonical(body))
    inp=tmp_path/'input';inp.write_bytes(canonical(fields))
    job='c'*32
    binding={'capsule_job_id':job,'launch_run_id':'new-run','launch_plan_sha256':'d'*64}
    receipt={**body,**binding,'schema':'rsi-execution-evidence/v2.3','stage':'deliver',
        'exit_code':0,'reason':None,'sequence':1,'capture_sha256':profile.relations.sha(capture.read_bytes()),
        'container_name':'rsi-v22-'+job[:10]+'-1'}
    path=tmp_path/'receipt';path.write_bytes(canonical(receipt))
    args={'manifest':{'execution_context':binding},'previous_sequence':0,'stage':'deliver',
          'input_path':inp,'capture_path':capture}
    assert profile._receipt(ExecutionEvidence.synthetic_control(),path,**args)[1]==1
    receipt['history_sha256']='e'*64;path.write_bytes(canonical(receipt))
    with pytest.raises(ValueError,match='RECEIPT_HISTORY'):
        profile._receipt(ExecutionEvidence.synthetic_control(),path,**args)
    receipt['history_sha256']=fields['history_sha256'];receipt['launch_run_id']='old-run';path.write_bytes(canonical(receipt))
    with pytest.raises(ValueError,match='RECEIPT_RUN'):
        profile._receipt(ExecutionEvidence.synthetic_control(),path,**args)


def test_cross_profile_fixture_cannot_upgrade(tmp_path):
    (tmp_path/'manifest.json').write_bytes(canonical({'profile':'source-relations/v1'}))
    with pytest.raises(ValueError,match='PROFILE_REQUIRED'):
        profile.check_fixture(tmp_path)


def test_original_control_class_cannot_be_relabelled(tmp_path):
    sidecar, question=sidecar_control(tmp_path)
    changed=deepcopy(sidecar);changed['control_class']='SOURCE_CONTEXT_CAPTURE'
    changed['subject_sha256']=digest({k:v for k,v in changed.items() if k!='subject_sha256'})
    with pytest.raises(ValueError,match='ORIGINAL_CONTROL_CLASS_CHANGED'):
        profile.validate_history(changed,qid=changed['item_id'],question=question)


def test_query_variant_keeps_original_sidecar_and_requires_frozen_derivation(tmp_path):
    sidecar, question=sidecar_control(tmp_path); original=deepcopy(sidecar)
    variant=question+' paraphrased'
    derivation={'profile':'reviewed-fixed-query-variant/v1','base_question_sha256':sidecar['question_sha256'],
        'current_question_sha256':profile.relations.sha(variant.encode())}
    derivation['review']={'status':'AI_REVIEWED','subject_sha256':digest(derivation),
        'reviewer':'SYNTHETIC','reviewed_at':'2026-11-16T00:00:00Z'}
    assert profile.validate_history(sidecar,qid=sidecar['item_id'],question=variant,
        query_derivation=derivation)==sidecar['history']
    assert sidecar==original
    derivation['current_question_sha256']='0'*64
    with pytest.raises(ValueError,match='DERIVATION_CHANGED'):
        profile.validate_history(sidecar,qid=sidecar['item_id'],question=variant,query_derivation=derivation)


def test_qa_actual_messages_are_hash_bound_and_nocall_is_not_generation(tmp_path):
    from rsik.qa_validation import fixed_history_qa_input, summarize
    from rsik.launch import file_binding
    fields={'evaluator_profile':profile.PROFILE,'history_profile':profile.HISTORY_PROFILE,
        'history_sha256':'a'*64,'sidecar_sha256':'b'*64,'qid':'q','lineage_id':'new',
        'base_question_sha256':'c'*64,'current_question_sha256':'c'*64,'query_derivation_sha256':digest(None)}
    capture=tmp_path/'capture';receipt=tmp_path/'receipt'
    body={**no_call(),**fields};capture.write_bytes(canonical(body))
    receipt.write_bytes(canonical({**fields,'capture_sha256':profile.relations.sha(capture.read_bytes())}))
    case={**fields,'question':'Q','actual_business_captures':{'PARENT':file_binding(capture)},
        'actual_business_receipts':{'PARENT':file_binding(receipt)}}
    assert fixed_history_qa_input(case,'PARENT') is None
    capture.write_bytes(canonical({**body,'request_calls':1}))
    with pytest.raises(ValueError,match='STALE_BINDING'): fixed_history_qa_input(case,'PARENT')
    records=[{'qid':'q','repeat':repeat,'arm':arm,'status':'NO_PROVIDER_REQUEST',
        'evidence_kind':'BUSINESS_EXECUTION_ASSERTION','control_status':'PRESENT'}
        for repeat in range(3) for arm in ('PARENT','CANDIDATE')]
    summary=summarize(records,expected_qids=['q'])
    assert summary['no_provider_control_pairs']==3
    assert summary['generations_observed']==0 and summary['slots_observed']==6


def test_business_control_groups_cannot_supply_retrieval_independence():
    five={str(i):'family-'+str(i%5) for i in range(17)}
    with pytest.raises(ValueError,match='INSUFFICIENT_RETRIEVAL_SOURCE_GROUPS'):
        profile.require_retrieval_clusters(five)
    eight={str(i):'family-'+str(i%8) for i in range(17)}
    assert profile.require_retrieval_clusters(eight) is None


def test_actual_launch_plan_rejects_transplanted_run_directory(tmp_path):
    from rsik.launch import file_binding, fixture_identity
    original=tmp_path/'original-run';original.mkdir()
    copied=tmp_path/'transplanted-run';copied.mkdir()
    fixture=tmp_path/'fixture';fixture.mkdir();(fixture/'frozen').write_text('SYNTHETIC')
    policy=ExecutionEvidence.synthetic_control()
    plan=tmp_path/'launch.json'
    plan.write_bytes(canonical({'schema':'rsi-launch/v1','run_id':'actual-original-run',
        'run_directory':str(original.resolve()),'evaluator_profile':profile.PROFILE,
        'execution_policy':policy.to_dict(),'allowed_phases':['BASELINE_QUALIFICATION'],
        'fixtures':{str(fixture.resolve()):fixture_identity(fixture)}}))
    binding=file_binding(plan)
    manifest={'launch_plan':binding,'launch_phase':'BASELINE_QUALIFICATION',
        'execution_context':{'launch_run_id':'actual-original-run','launch_plan_sha256':binding['sha256']}}
    assert profile.verify_run_plan(manifest,original/'suite-1',fixture,policy)['run_id']=='actual-original-run'
    with pytest.raises(ValueError,match='LAUNCH_RUN_BINDING_MISMATCH'):
        profile.verify_run_plan(manifest,copied/'suite-1',fixture,policy)

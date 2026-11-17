"""Synthetic admission controls; no Capsule RPC or network is executed."""
from dataclasses import replace
from pathlib import Path
import json
import pytest

from test_launch_baseline import launch_binding_control
from rsik.launch import validate_launch, file_binding
from rsik.query_aware import QueryRegistry
from rsik.state import canonical, digest
from rsik.experiment import bytes_hash, executor, load


def freeze(context, plan):
    Path(context.plan_path).write_bytes(canonical(plan))
    return replace(context,plan_sha256=bytes_hash(Path(context.plan_path).read_bytes()))


def collection(context, fixture, candidate, plan):
    registry=QueryRegistry(context.registry)
    negative=registry.put(candidate['worker'],b'{"selection":"exact_duplicate"}',
        parent_id=candidate['candidate_id'],origin='OWNER_SPECIFIED',evidence={})
    neg=registry.get(negative)
    ident=lambda c:{'candidate_id':c['candidate_id'],'worker_sha256':bytes_hash(c['worker']),
                    'config_sha256':bytes_hash(c['config'])}
    tasks=[]
    for item in load(fixture/'cases.json'):
        case=executor.checked_case(item['case'])
        tasks.append({'fixture_path':str(fixture.resolve()),'qid':item['qid'],
            'prepare_input_sha256':bytes_hash(canonical(case)), 'controls':[
            {'kind':'POSITIVE','removed_member_ids':[],'candidate':ident(candidate)},
            {'kind':'MEMBER_REMOVAL','removed_member_ids':[case['chunks'][0]['id']],'candidate':ident(neg)}]})
    subject={'schema':'rsi-coverage-collection-plan/v1','fixtures':plan['fixtures'],'tasks':tasks}
    value={**subject,'review':{'status':'AI_REVIEWED','reviewer':'SYNTHETIC_CONTROL',
        'reviewed_at':'2026-11-15T00:00:00+00:00','object_sha256':digest(subject)}}
    path=Path(context.plan_path).parent/'collection.json';path.write_bytes(canonical(value))
    plan['coverage_collection']=file_binding(path);plan['allowed_phases']=['COVERAGE_COLLECTION']
    return freeze(replace(context,phase='COVERAGE_COLLECTION'),plan),value


def test_collection_does_not_need_fictional_capture_or_qualification(launch_binding_control,monkeypatch):
    context,policy,fixture,candidate,plan=launch_binding_control
    context,_=collection(context,fixture,candidate,plan)
    import rsik.source_relations as relations
    monkeypatch.setattr(relations,'recompute_coverage',lambda _:pytest.fail('cannot require captures before collection'))
    Path(plan['coverage']['path']).unlink()
    result=validate_launch(context,evidence=policy,boundary='CAPSULE_EXECUTION')
    assert result['phase']=='COVERAGE_COLLECTION' and result['status']=='PASS'
    for boundary in (None,'MODEL_REQUEST','QUALIFICATION','ADOPT','VERIFIED'):
        with pytest.raises(ValueError,match='CAPSULE_ONLY'):
            validate_launch(context,evidence=policy,boundary=boundary)


@pytest.mark.parametrize('fault',['review','fixture','input','controls','candidate','missing_task'])
def test_collection_rejects_unreviewed_or_changed_intentions(launch_binding_control,fault):
    context,policy,fixture,candidate,plan=launch_binding_control
    context,value=collection(context,fixture,candidate,plan)
    if fault=='review':value['review']['object_sha256']='0'*64
    elif fault=='fixture':value['fixtures']={}
    elif fault=='input':value['tasks'][0]['prepare_input_sha256']='0'*64
    elif fault=='controls':value['tasks'][0]['controls']=value['tasks'][0]['controls'][:1]
    elif fault=='candidate':value['tasks'][0]['controls'][0]['candidate']['worker_sha256']='0'*64
    else:value['tasks'].pop()
    if fault!='review':value['review']['object_sha256']=digest({k:v for k,v in value.items() if k!='review'})
    path=Path(plan['coverage_collection']['path']);path.write_bytes(canonical(value));plan['coverage_collection']=file_binding(path)
    context=freeze(context,plan)
    with pytest.raises(ValueError,match='LAUNCH_'):
        validate_launch(context,evidence=policy,boundary='CAPSULE_EXECUTION')


def test_model_boundary_requires_frozen_transport(launch_binding_control,monkeypatch):
    context,policy,fixture,candidate,plan=launch_binding_control
    monkeypatch.setenv('RSI_OPENAI_HTTPS_PROXY','http://127.0.0.1:1234')
    with pytest.raises(ValueError,match='TRANSPORT_IDENTITY'):
        validate_launch(context,evidence=policy,boundary='MODEL_REQUEST')
    from rsik.http_worker import transport_identity
    plan['transport_identity']=transport_identity();context=freeze(context,plan)
    assert validate_launch(context,evidence=policy,boundary='MODEL_REQUEST')['status']=='PASS'
    monkeypatch.setenv('RSI_OPENAI_HTTPS_PROXY','http://127.0.0.1:1235')
    with pytest.raises(ValueError,match='TRANSPORT_IDENTITY'):
        validate_launch(context,evidence=policy,boundary='MODEL_REQUEST')


def test_capsule_collection_routes_only_actual_prepared_and_listed_controls(launch_binding_control,monkeypatch,tmp_path):
    import base64
    from rsik.capsule import Capsule
    context,policy,fixture,candidate,plan=launch_binding_control
    context,_=collection(context,fixture,candidate,plan)
    monkeypatch.setattr(Capsule,'evidence_policy',property(lambda self:policy))
    capsule=object.__new__(Capsule)
    capsule.launch_context=context;capsule.ready=True;capsule.sequence=0;capsule.deadline=None
    capsule.business_root=Path(context.business_root);capsule.image='SYNTHETIC';capsule.receipts=[]
    capsule.code_archive_sha256='SYNTHETIC_NOT_EXECUTED'
    calls=[]
    snapshot={'expanded':[],'chunks':[],'files':[],'allowed_kb_ids':[]}
    def rpc(request,**kwargs):
        calls.append(request)
        capture=base64.b64encode(canonical(snapshot))
        runtime=base64.b64encode(canonical({'SYNTHETIC':'NOT_EXECUTED'}))
        output=b'RSI_CAPTURE_BASE64='+capture+b'\nRSI_RUNTIME_BASE64='+runtime+b'\n'
        return {'output_base64':base64.b64encode(output).decode(),'exit_code':0,'reason':None}
    capsule.rpc=rpc
    unknown=tmp_path/'unknown.json';unknown.write_bytes(b'{}')
    with pytest.raises(ValueError,match='UNLISTED_PREPARE_INPUT'):
        capsule.child('prepare',[unknown,tmp_path/'absent.json'],tmp_path)
    assert not calls
    prep=tmp_path/'prep';prep.mkdir()
    case=executor.checked_case(load(fixture/'cases.json')[0]['case'])
    inp=prep/'input.json';inp.write_bytes(canonical(case))
    capsule.child('prepare',[inp,prep/'snapshot.json'],prep)
    assert len(calls)==1
    delivery=tmp_path/'delivery';delivery.mkdir()
    worker=delivery/'worker.py';worker.write_bytes(candidate['worker'])
    config=delivery/'config.json';config.write_bytes(candidate['config'])
    with pytest.raises(ValueError,match='UNLISTED_DELIVERY'):
        capsule.child('deliver',[unknown,config,delivery/'absent.json'],delivery,worker=worker)
    assert len(calls)==1
    derived=delivery/'input.json';derived.write_bytes(canonical(executor.execution_view(snapshot)))
    capsule.child('deliver',[derived,config,delivery/'capture.json'],delivery,worker=worker)
    assert len(calls)==2 and (delivery/'capture.json').is_file()


def test_terra_model_is_not_admitted(launch_binding_control):
    context,policy,fixture,candidate,plan=launch_binding_control
    plan['models']['diagnose']='gpt-6.1-terra';context=freeze(context,plan)
    with pytest.raises(ValueError,match='MODEL_IDENTITY'):
        validate_launch(context,evidence=policy,boundary='MODEL_REQUEST')


def test_normal_admission_requires_real_coverage_execution_check(launch_binding_control,monkeypatch):
    context,policy,fixture,candidate,plan=launch_binding_control
    import rsik.source_relations as relations
    def reject(record, actual_policy):
        assert actual_policy is policy
        raise ValueError('SYNTHETIC_EXECUTION_EVIDENCE_REJECTED')
    monkeypatch.setattr(relations,'verify_coverage_execution',reject,raising=False)
    with pytest.raises(ValueError,match='EXECUTION_EVIDENCE_REJECTED'):
        validate_launch(context,evidence=policy)


@pytest.mark.parametrize('fault',['source_id','content_sha256','document_id','version','source_family_id',
    'relation','related_source_ids','exposure_status','review_status'])
def test_relational_fixture_source_identity_must_join_live_snapshot(tmp_path,fault):
    from rsik.launch import bind_fixture_sources
    fixture=tmp_path/'relational';fixture.mkdir()
    (fixture/'manifest.json').write_bytes(canonical({'profile':'source-relations/v1'}))
    source={'source_id':'SYNTHETIC','content_sha256':'1'*64,'document_id':'DOC','version':'1',
        'source_family_id':'FAMILY','relation':'ORIGINAL','related_source_ids':[],
        'exposure_status':'KNOWN_HISTORY','review_status':'AI_REVIEWED'}
    (fixture/'relations.restricted.json').write_bytes(canonical({'q':{'sources':{'SYNTHETIC':source}}}))
    plan={'fixtures':{str(fixture):{}}}
    bind_fixture_sources(plan,{'sources':[source]})
    wrong={**source,fault:['WRONG'] if fault=='related_source_ids' else 'WRONG'}
    with pytest.raises(ValueError,match='FIXTURE_EXPOSURE_MISMATCH'):
        bind_fixture_sources(plan,{'sources':[wrong]})

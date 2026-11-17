"""Offline engineering controls only; no synthetic real qualification."""
import copy
import json

import pytest

from rsik.coverage_collection import identity, prepare_collection, removal_worker
from rsik.events import canonical, digest
from rsik.execution_evidence import ExecutionEvidence
from rsik.errors import StageFailure
from rsik.experiment import ROOT, executor
from rsik.launch import collection_tasks, file_binding, fixture_identity
from rsik.query_aware import QueryRegistry, adapt_legacy
from rsik.source_relations import recompute_coverage, sha, verify_coverage_execution
from run_rsi_loop import selector
from test_source_relations import _build_fixture


def test_removal_worker_changes_only_selector_and_preserves_invariants():
    original = adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes())
    negative = removal_worker(original,['drop'])
    old_selector = selector(original.decode())[1]
    new_selector = selector(negative.decode())[1]
    assert original.decode().replace(old_selector,'<SELECTOR>',1) == negative.decode().replace(new_selector,'<SELECTOR>',1)
    namespace = {}; exec(new_selector,namespace)
    members = [{'id':'seed'},{'id':'keep'},{'id':'drop'}]
    args = (members,{}, {}, ['seed'],'all','CONTROL question')
    before = copy.deepcopy(args)
    assert namespace['select_member_indices'](*args) == [0,1]
    assert args == before
    assert namespace['select_member_indices'](members,{}, {},['drop'],'all','CONTROL question') == [0,1,2]
    assert namespace['select_member_indices'](members,{}, {},['seed'],'all','x') == [0,1,2]


def test_offline_preparation_registers_only_declared_nonseed_controls(tmp_path):
    fixture = _build_fixture(tmp_path/'fixture',count=1)
    case = json.loads((fixture/'cases.json').read_text(encoding='utf-8'))[0]
    spec = json.loads((fixture/'relations.restricted.json').read_text(encoding='utf-8'))[case['qid']]
    (fixture/'control-spec.restricted.json').write_bytes(canonical([{
        'qid':case['qid'],'atom_id':spec['atoms'][0]['atom_id'],'removed_member_ids':['m2']}]))
    registry = tmp_path/'registry'
    for name in ['workers','configs','candidates']:(registry/name).mkdir(parents=True)
    reg = QueryRegistry(registry)
    worker = adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes())
    cid = reg.put(worker,canonical({'selection':'all'}),parent_id=None,
        origin='ENGINEERING_CONTROL_ONLY',evidence={'real_qualification':False})
    result = prepare_collection(registry=registry,candidate_id=cid,fixtures=[fixture],output=tmp_path/'prepared')
    minimal_plan = {'fixtures':result['fixtures'],'candidate':identity(reg.get(cid)),
        'coverage_collection':file_binding(tmp_path/'prepared/collection-plan.restricted.json')}
    assert len(collection_tasks(minimal_plan,registry)) == 1
    negative = result['tasks'][0]['controls'][1]
    assert negative['removed_member_ids'] == ['m2']
    assert reg.get(negative['candidate']['candidate_id'])['record']['origin'] == 'ENGINEERING_COVERAGE_NEGATIVE'
    assert not list((tmp_path/'prepared').rglob('execution-receipt.json'))
    controls = json.loads((fixture/'control-spec.restricted.json').read_text(encoding='utf-8'))
    controls[0]['removed_member_ids'] = ['m1']
    (fixture/'control-spec.restricted.json').write_bytes(canonical(controls))
    with pytest.raises(ValueError,match='COVERAGE_NEGATIVE_MUST_REMOVE_NONSEED'):
        prepare_collection(registry=registry,candidate_id=cid,fixtures=[fixture],output=tmp_path/'bad')


def test_unknown_origin_support_does_not_receive_coverage_credit(tmp_path):
    fixture = _build_fixture(tmp_path/'fixture',count=1)
    record={'schema':'rsi-source-relations-coverage/v1','base_dir':str(tmp_path),
        'fixtures':['fixture'],'controls':[],'review_policy':'research-source-family/v1'}
    known = recompute_coverage(record)
    assert all(row['positive_example_count']==1 for row in known['matrix'].values())
    path = fixture/'relations.restricted.json'
    specs = json.loads(path.read_text(encoding='utf-8'))
    for spec in specs.values():
        for source in spec['sources'].values():
            source['relation']='UNKNOWN';source['exposure_status']='EXPOSURE_UNKNOWN'
    path.write_bytes(canonical(specs))
    manifest = json.loads((fixture/'manifest.json').read_text(encoding='utf-8'))
    manifest['artifact_hashes']['relations.restricted.json']=sha(path.read_bytes())
    (fixture/'manifest.json').write_bytes(canonical(manifest))
    result = recompute_coverage(record)
    assert result['status']=='BLOCKED_DATA'
    assert all(row['positive_example_count']==0 for row in result['matrix'].values())


def test_real_coverage_verifier_rejects_synthetic_policy():
    with pytest.raises(ValueError,match='REAL_COVERAGE_EXECUTION_REQUIRED'):
        verify_coverage_execution({'evidence_mode':'SYNTHETIC_CONTROL'},ExecutionEvidence.synthetic_control())


def test_real_coverage_verifier_rejects_missing_prepare_receipt(tmp_path):
    fixture = _build_fixture(tmp_path/'fixture',count=1)
    item = json.loads((fixture/'cases.json').read_text(encoding='utf-8'))[0]
    input_path = tmp_path/'input.json';input_path.write_bytes(canonical(executor.checked_case(item['case'])))
    collection = {'schema':'rsi-coverage-collection-plan/v1',
        'fixtures':{str(fixture.resolve()):fixture_identity(fixture)},'tasks':[]}
    collection['review']={'status':'AI_REVIEWED','object_sha256':digest(collection)}
    collection_path=tmp_path/'collection.json';collection_path.write_bytes(canonical(collection))
    policy=ExecutionEvidence('REAL_REPLAY','a'*64,'b'*64,'CONTROL-PINNED-IMAGE','CONTROL-BASE','c'*64)
    record={'execution_policy':policy.to_dict(),'evidence_mode':'REAL_REPLAY','real_eligible':True,
        'collection_plan':file_binding(collection_path),'base_dir':str(tmp_path),
        'controls':[{'fixture':str(fixture),'qid':item['qid'],'prepare_input':str(input_path),
            'prepare_capture':str(tmp_path/'snapshot.json'),'arm_input':str(tmp_path/'arm-input.json'),
            'prepare_receipt':str(tmp_path/'absent-execution-receipt.json')} ]}
    launch={'schema':'rsi-launch/v1','allowed_phases':['COVERAGE_COLLECTION'],
        'run_id':'CONTROL-MISSING-RECEIPT','coverage_collection':record['collection_plan'],
        'execution_policy':policy.to_dict(),'run_directory':str(tmp_path)}
    launch_path=tmp_path/'launch.json';launch_path.write_bytes(canonical(launch))
    record.update(launch_plan=file_binding(launch_path),launch_run_id=launch['run_id'],
        launch_plan_sha256=sha(launch_path.read_bytes()),capsule_job_id='a'*32)
    with pytest.raises(StageFailure,match='EXECUTION_EVIDENCE_MISSING'):
        verify_coverage_execution(record,policy)

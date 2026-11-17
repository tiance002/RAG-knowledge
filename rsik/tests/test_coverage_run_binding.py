"""CONTROL-only counterexamples for run-bound coverage receipt reuse."""
import copy

import pytest

from rsik.events import canonical,digest
from rsik.execution_evidence import ExecutionEvidence
from rsik.launch import file_binding
from rsik.source_relations import _coverage_receipt_run_binding,verify_coverage_execution


def _run():
    record={'capsule_job_id':'a'*32,'launch_run_id':'CONTROL-CURRENT-RUN',
        'launch_plan_sha256':'b'*64,'collection_plan':{'sha256':'c'*64}}
    receipt={'capsule_job_id':record['capsule_job_id'],'launch_run_id':record['launch_run_id'],
        'launch_plan_sha256':record['launch_plan_sha256'],'coverage_collection_sha256':'c'*64,
        'sequence':7,'container_name':'rsi-v22-aaaaaaaaaa-7'}
    return record,receipt


def test_matching_receipt_run_binding_is_accepted():
    record,receipt=_run()
    assert _coverage_receipt_run_binding(receipt,record) is None


@pytest.mark.parametrize('field,value',[
    ('capsule_job_id','d'*32),('launch_run_id','CONTROL-OLD-RUN'),
    ('launch_plan_sha256','e'*64),('coverage_collection_sha256','f'*64),
    ('sequence',0),('sequence',-1),('sequence',True),('sequence','7'),
    ('container_name','rsi-v22-bbbbbbbbbb-7'),('container_name','rsi-v22-aaaaaaaaaa-8')])
def test_single_receipt_binding_fault_is_rejected(field,value):
    record,receipt=_run();receipt[field]=value
    with pytest.raises(ValueError,match='COVERAGE_RECEIPT_RUN_MISMATCH'):
        _coverage_receipt_run_binding(receipt,record)


@pytest.mark.parametrize('field',['capsule_job_id','launch_run_id','launch_plan_sha256',
    'coverage_collection_sha256','sequence','container_name'])
def test_unbound_legacy_receipt_cannot_supply_current_binding(field):
    record,receipt=_run();receipt.pop(field)
    with pytest.raises(ValueError,match='COVERAGE_RECEIPT_RUN_MISMATCH'):
        _coverage_receipt_run_binding(receipt,record)


def test_previously_matching_receipt_cannot_be_transplanted_to_new_job_and_run():
    old,receipt=_run()
    _coverage_receipt_run_binding(receipt,old)
    current=copy.deepcopy(old)
    current.update(capsule_job_id='d'*32,launch_run_id='CONTROL-NEW-RUN',launch_plan_sha256='e'*64)
    with pytest.raises(ValueError,match='COVERAGE_RECEIPT_RUN_MISMATCH'):
        _coverage_receipt_run_binding(receipt,current)


def _top_record(tmp_path):
    policy=ExecutionEvidence('REAL_REPLAY','a'*64,'b'*64,'CONTROL-IMAGE','CONTROL-BASE','c'*64)
    collection={'schema':'rsi-coverage-collection-plan/v1','fixtures':{},'tasks':[]}
    collection['review']={'status':'AI_REVIEWED','object_sha256':digest(collection)}
    collection_path=tmp_path/'collection.json';collection_path.write_bytes(canonical(collection))
    launch={'schema':'rsi-launch/v1','allowed_phases':['COVERAGE_COLLECTION'],
        'run_id':'CONTROL-CURRENT-RUN','coverage_collection':file_binding(collection_path),
        'execution_policy':policy.to_dict(),'run_directory':str(tmp_path)}
    launch_path=tmp_path/'launch.json';launch_path.write_bytes(canonical(launch))
    record={'execution_policy':policy.to_dict(),'evidence_mode':'REAL_REPLAY','real_eligible':True,
        'collection_plan':file_binding(collection_path),'launch_plan':file_binding(launch_path),
        'launch_plan_sha256':file_binding(launch_path)['sha256'],'launch_run_id':launch['run_id'],
        'capsule_job_id':'a'*32,'base_dir':str(tmp_path),'controls':[]}
    return policy,record


def test_top_validator_requires_declared_launch_hash_to_match_bound_launch(tmp_path):
    policy,record=_top_record(tmp_path)
    # With valid top bindings it reaches the independent missing-controls gate.
    with pytest.raises(ValueError,match='REAL_COVERAGE_CONTROLS_REQUIRED'):
        verify_coverage_execution(record,policy)
    record['launch_plan_sha256']='0'*64
    with pytest.raises(ValueError,match='COVERAGE_RUN_BINDING_MISMATCH'):
        verify_coverage_execution(record,policy)


@pytest.mark.parametrize('job',[None,'a'*31,'a'*33,'A'*32,'g'*32])
def test_top_validator_rejects_noncanonical_capsule_job_id(tmp_path,job):
    policy,record=_top_record(tmp_path);record['capsule_job_id']=job
    with pytest.raises(ValueError,match='COVERAGE_CAPSULE_JOB_REQUIRED'):
        verify_coverage_execution(record,policy)

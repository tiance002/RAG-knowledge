"""F1 regression controls: invented captures/receipts, never actual execution.

These test the real reader boundary with host-configured policies. Fabricated
positive receipts test validation plumbing, not REAL_REPLAY qualification.
"""
import hashlib
import json
from pathlib import Path
import pytest

from rsik.execution_evidence import ExecutionEvidence
from rsik.experiment import read_decision
from rsik.adjudicate import adjudicate_round
from rsik.holdout import replay_artifacts
from rsik.errors import StageFailure
from portable_fixtures import ROOT, write, SyntheticCapsule, canonical


def control_policy():
    # Caller policy selected by this trusted test, never by captured JSON.
    runtime={'python':'3.12.1','uid':65534,'packages':{'SYNTHETIC':'NOT_EXECUTED'}}
    return ExecutionEvidence('REAL_REPLAY', '1'*64, '2'*64, 'CONTROL_IMAGE', '3'*40,
        hashlib.sha256(canonical(runtime)).hexdigest())


def audited(sample, *, evidence=None):
    manifest = json.loads((sample['run']/'manifest.json').read_bytes())
    arm = manifest['arms']['B']
    return read_decision(sample['run'], sample['fixture'], arm['candidate_id'],
        {'angle':'P2_BUDGET', 'proposal_id':'SYNTHETIC_VALIDATOR_CONTROL'},
        arm['worker_sha256'], arm['sha256'], evidence=evidence)


def install_receipt_controls(sample, policy):
    """Invent realistic-shaped attestations solely for parser negative controls."""
    run = sample['run']; manifest = json.loads((run/'manifest.json').read_bytes())
    sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    for directory in run.glob('case-*'):
        for arm in ('prepare', 'A', 'B'):
            prepare = arm == 'prepare'
            parent = directory if prepare else directory/arm
            capture = directory/'snapshot.json' if prepare else parent/'capture.json'
            receipt = {'schema':'rsi-execution-evidence/v2.3',
                'stage':'prepare' if prepare else 'deliver',
                'execution_adapter_sha256':policy.adapter_sha256,
                'source_base_commit':policy.base_commit,
                'code_archive_sha256':policy.archive_sha256, 'image':policy.image,
                'worker_runtime':{'python':'3.12.1','uid':65534,'packages':{'SYNTHETIC':'NOT_EXECUTED'}},
                'worker_sha256':sha(ROOT/'scripts/rsi_delivery_worker.py') if prepare else manifest['arms'][arm]['worker_sha256'],
                'input_sha256':sha(directory/('input.json' if prepare else 'arm-input.json')),
                'config_sha256':None if prepare else sha(run/(arm+'.json')),
                'capture_sha256':sha(capture), 'exit_code':0, 'reason':None,
                'container_removed':True}
            write(parent/'execution-receipt.json', receipt)


def test_real_reader_missing_receipts_stops_before_adoption(synthetic_replay):
    # Old reader silently accepted every capture and proceeded to adjudication.
    assert not list(synthetic_replay['run'].rglob('execution-receipt.json'))
    with pytest.raises(StageFailure, match='EXECUTION_EVIDENCE_MISSING') as error:
        audited(synthetic_replay)
    assert error.value.global_stop and error.value.category == 'EVALUATION_ERROR'
    control = audited(synthetic_replay, evidence=ExecutionEvidence.synthetic_control())
    assert control['evidence_mode'] == 'SYNTHETIC_CONTROL' and not control['real_eligible']


def test_unexecuted_gain_capture_can_only_exercise_synthetic_adoption_math(tmp_path, synthetic_replay):
    class ShrinkingDouble(SyntheticCapsule):
        def child(self, stage, paths, cwd, **kwargs):
            result = super().child(stage, paths, cwd, **kwargs)
            if stage == 'deliver' and Path(cwd).name == 'B':
                snapshot=json.loads(Path(paths[0]).read_bytes())
                members=[m for m in result['members'] if m['id'] in snapshot['seed_ids']]
                context='\n'.join(m['content'] for m in members); proof=[]; offset=0
                for member in members:
                    end=offset+len(member['content'])
                    proof.append({'id':member['id'],'context_start':offset,'context_end':end,
                        'prompt_start':offset,'prompt_end':end}); offset=end+1
                result.update(members=members,context=context,messages=[{'content':context}],proof=proof)
                write(paths[-1],result)
            return result
    run=tmp_path/'unexecuted-gain'
    control=replay_artifacts(ShrinkingDouble(),synthetic_replay['fixture'],synthetic_replay['artifacts'],
        run,deadline=10**12,schema='rsi-safety-engineering/v1',scope='SYNTHETIC_NOT_EXECUTED')
    assert control['delta_total'] < 0 and all(control['gates'].values())
    assert adjudicate_round([control])['adopted_candidate_id'] == control['candidate_id']
    assert not control['real_eligible']
    with pytest.raises(StageFailure,match='EXECUTION_EVIDENCE_MISSING'):
        audited({**synthetic_replay,'run':run})


@pytest.mark.parametrize('stage', ['prepare','A','B'])
def test_missing_any_planned_receipt_stops_real_entry(synthetic_replay, stage):
    policy = control_policy(); install_receipt_controls(synthetic_replay, policy)
    baseline = audited(synthetic_replay, evidence=policy)
    assert baseline['real_eligible'] and len(baseline['per_question']) == 20
    directory = synthetic_replay['run']/'case-020'
    (directory/('execution-receipt.json' if stage == 'prepare' else stage+'/execution-receipt.json')).unlink()
    with pytest.raises(StageFailure, match='EXECUTION_EVIDENCE_MISSING'):
        audited(synthetic_replay, evidence=policy)


@pytest.mark.parametrize('field', ['worker_sha256','config_sha256','input_sha256','capture_sha256',
    'execution_adapter_sha256','code_archive_sha256','image','source_base_commit','stage','worker_runtime'])
def test_mismatched_identity_stops_real_entry(synthetic_replay, field):
    policy = control_policy(); install_receipt_controls(synthetic_replay, policy)
    path = synthetic_replay['run']/'case-020/B/execution-receipt.json'
    value = json.loads(path.read_bytes()); value[field] = 'SYNTHETIC_WRONG_IDENTITY'; write(path, value)
    with pytest.raises(StageFailure, match='EXECUTION_IDENTITY_UNKNOWN'):
        audited(synthetic_replay, evidence=policy)


def test_two_arm_runtime_identity_disagreement_stops_real_entry(synthetic_replay):
    policy = control_policy(); install_receipt_controls(synthetic_replay, policy)
    path=synthetic_replay['run']/'case-001/B/execution-receipt.json'
    value=json.loads(path.read_bytes()); value['worker_runtime']['python']='3.12.9';write(path,value)
    with pytest.raises(StageFailure,match='EXECUTION_IDENTITY_UNKNOWN|EXECUTION_ENVIRONMENT_CHANGED'):
        audited(synthetic_replay,evidence=policy)


def test_capture_or_manifest_self_report_cannot_change_real_mode(synthetic_replay):
    path=synthetic_replay['run']/'manifest.json'; value=json.loads(path.read_bytes())
    value.update(evidence_mode='SYNTHETIC_CONTROL',real_eligible=True);write(path,value)
    with pytest.raises(StageFailure,match='EXECUTION_EVIDENCE_MISSING'):
        audited(synthetic_replay)
    with pytest.raises(StageFailure,match='UNTRUSTED_EXECUTION_POLICY'):
        audited(synthetic_replay,evidence={'mode':'SYNTHETIC_CONTROL'})


def test_raw_and_normalized_config_are_both_bound_at_real_entry(synthetic_replay):
    policy=control_policy(); install_receipt_controls(synthetic_replay,policy)
    # Semantically equal reformatted bytes invalidate the recorded raw identity.
    path=synthetic_replay['run']/'B.json';path.write_bytes(json.dumps({'selection':'all'},indent=2).encode())
    with pytest.raises(StageFailure,match='EXECUTION_IDENTITY_UNKNOWN'):
        audited(synthetic_replay,evidence=policy)

    install_receipt_controls(synthetic_replay,policy)
    assert audited(synthetic_replay,evidence=policy)['gates']['G1']
    path.write_bytes(canonical({'selection':'exact_duplicate'})); install_receipt_controls(synthetic_replay,policy)
    with pytest.raises(StageFailure,match='EXECUTION_IDENTITY_UNKNOWN'):
        audited(synthetic_replay,evidence=policy)


def test_real_reader_binds_config_to_callers_expected_identity(synthetic_replay):
    policy=control_policy();install_receipt_controls(synthetic_replay,policy)
    valid=audited(synthetic_replay,evidence=policy)
    assert valid['real_eligible'] and len(valid['per_question'])==20
    arm=json.loads((synthetic_replay['run']/'manifest.json').read_bytes())['arms']['B']
    assert arm['sha256']!='0'*64
    with pytest.raises(StageFailure,match='INVALID_PAIR') as error:
        read_decision(synthetic_replay['run'],synthetic_replay['fixture'],arm['candidate_id'],
            {'angle':'P2_BUDGET','proposal_id':'SYNTHETIC_VALIDATOR_CONTROL'},
            arm['worker_sha256'],'0'*64,evidence=policy)
    assert error.value.category=='EVALUATION_ERROR' and error.value.global_stop


def test_synthetic_mode_cannot_enter_legacy_real_sealed_qualification(tmp_path):
    from rsik.holdout import verify_sealed
    with pytest.raises(ValueError,match='SYNTHETIC_CONTROL_NOT_REAL_QUALIFICATION'):
        verify_sealed(registry=tmp_path/'registry',training='NEVER_OPENED',train_fixture='NEVER_OPENED',
            sealed_fixture='NEVER_OPENED',candidate_id='SYNTHETIC',trusted_id='SYNTHETIC',
            lineage_id='SYNTHETIC',policy=None,capsule=SyntheticCapsule(),output=tmp_path/'never')
    assert not (tmp_path/'registry').exists() and not (tmp_path/'never').exists()


def test_synthetic_parent_pool_is_explicitly_isolated_from_real_qualification(tmp_path):
    from rsik.lineage import ParentPool, LineagePolicy
    path=tmp_path/'receipt.json'
    write(path,{'verdict':'VERIFIED','candidate_id':'SYNTHETIC',
        'evidence_mode':'SYNTHETIC_CONTROL','real_eligible':False})
    real=ParentPool(tmp_path/'real-pool')
    with pytest.raises(ValueError,match='REAL_PARENT_EVIDENCE_REQUIRED'):
        real.admit('SYNTHETIC','lineage',path,kind='VERIFIED',chars=100,replay_seconds=1,
            evidence=control_policy())
    assert real.log.read()==[]
    control=ParentPool(tmp_path/'control-pool',evidence_mode='SYNTHETIC_CONTROL')
    control.admit('SYNTHETIC','lineage',path,kind='VERIFIED',chars=100,replay_seconds=1)
    assert control.select(LineagePolicy(tmp_path/'control-policy'))['candidate_id']=='SYNTHETIC'
    with pytest.raises(ValueError,match='PARENT_POOL_SCOPE_MISMATCH'):
        ParentPool(tmp_path/'control-pool')


def test_legacy_untyped_parent_events_are_preserved_but_not_renewed_as_real(tmp_path):
    from rsik.lineage import ParentPool, LineagePolicy
    pool=ParentPool(tmp_path/'legacy-pool')
    path=tmp_path/'legacy-receipt.json';write(path,{'verdict':'VERIFIED','candidate_id':'old'})
    pool.log.append('legacy','PARENT_ADMITTED',{'candidate_id':'old','lineage_id':'old',
        'kind':'VERIFIED','receipt_path':str(path),'receipt_hash':hashlib.sha256(path.read_bytes()).hexdigest(),
        'chars':100,'replay_seconds':1})
    before=pool.log.read()
    assert pool.select(LineagePolicy(tmp_path/'policy')) is None
    assert pool.log.read()==before
    with pytest.raises(ValueError,match='PARENT_NOT_IN_VERIFIED_POOL'):
        pool.require_verified('old',LineagePolicy(tmp_path/'policy'))


@pytest.mark.parametrize('mismatch',['parent_worker','candidate_config','shared_anchor'])
def test_real_parent_admission_binds_comparison_artifacts_to_registry(tmp_path,monkeypatch,mismatch):
    from rsik.lineage import ParentPool
    from rsik.experiment import registry_api
    policy=control_policy(); directory=tmp_path/'sealed'
    receipt=directory/'receipt.restricted.json'
    write(receipt,{'verdict':'VERIFIED','candidate_id':'child','parent_id':'parent','trusted_id':'parent',
        'shared_physical_comparison':True,'evidence_mode':'REAL_REPLAY','real_eligible':True,
        'execution_policy':policy.to_dict()})
    records={'child':{'parent':'parent','worker':'1'*64,'config':'2'*64},
        'parent':{'worker':'3'*64,'config':'4'*64}}
    monkeypatch.setattr(registry_api,'candidate',lambda registry,cid:records[cid])
    manifest={'schema':'SYNTHETIC_VERIFIER_CONTROL','arms':{
        'A':{'candidate_id':'parent','worker_sha256':'3'*64,'sha256':'4'*64},
        'B':{'candidate_id':'child','worker_sha256':'1'*64,'sha256':'2'*64}}}
    if mismatch=='shared_anchor':
        value=json.loads(receipt.read_bytes());value['trusted_id']='different-anchor';write(receipt,value)
    else:
        manifest['arms']['A' if mismatch=='parent_worker' else 'B'][
            'worker_sha256' if mismatch=='parent_worker' else 'sha256']='0'*64
    write(directory/'vs-parent/manifest.json',manifest)
    pool=ParentPool(tmp_path/'pool')
    with pytest.raises(ValueError,match='PARENT_CANDIDATE_MISMATCH|PARENT_COMPARISON_IDENTITY_MISMATCH'):
        pool.admit('child','lineage',receipt,kind='VERIFIED',chars=100,replay_seconds=1,
            registry=tmp_path/'registry',fixture=tmp_path/'fixture',evidence=policy)
    assert pool.log.read()==[]

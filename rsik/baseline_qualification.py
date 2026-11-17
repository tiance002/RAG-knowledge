"""Independent root qualification: A/A evidence, never improvement or VERIFIED."""
from pathlib import Path
import time

from .experiment import bytes_hash, load
from .execution_evidence import ExecutionEvidence
from .foundation import verify_boundary
from .locking import atomic_write, restrict_directory
from .query_aware import QueryRegistry, invariant_probe_source, validate_function
from .state import canonical, digest
from .launch import guard_real
from run_rsi_loop import selector


def adapters(fixture):
    manifest=load(Path(fixture)/'manifest.json')
    if manifest.get('profile')=='source-relations-fixed-history/v1':
        from .fixed_history_profile import check_fixture, replay_artifacts, read_decision
        return check_fixture,replay_artifacts,read_decision
    if manifest.get('profile')=='source-relations/v1':
        from .source_relations import check_fixture, replay_artifacts, read_decision
        return check_fixture,replay_artifacts,read_decision
    from .foundation import check_split
    from .holdout import replay_artifacts
    from .experiment import read_decision
    return check_split,replay_artifacts,read_decision


def _check_equal(decision):
    if (not all(decision['gates']['G'+str(i)] is True for i in range(1,5))
            or not decision.get('per_question') or decision.get('delta_total')!=0):
        raise ValueError('BASELINE_AA_HARD_GATE_FAILED')
    for row in decision['per_question']:
        if (row.get('chars_A')!=row.get('chars_B')
                or row.get('context_A_sha256')!=row.get('context_B_sha256')
                or row.get('parent_facts')!=row.get('candidate_facts')):
            raise ValueError('BASELINE_AA_NONDETERMINISTIC')


def qualify_baseline(*,registry,candidate_id,fixtures,capsule,output,origin,launch=None):
    """Caller must explicitly select an already registered root artifact.

    Ordinary generated children cannot nominate themselves as baseline roots.
    A real qualification requires host launch admission before capsule setup.
    """
    if origin not in {'ENGINEER_GUIDED','MIGRATED','OWNER_SPECIFIED'}:
        raise ValueError('BASELINE_ORIGIN_REQUIRED')
    registry=QueryRegistry(registry);registry.require_not_quarantined(candidate_id)
    candidate=registry.get(candidate_id)
    if candidate['record'].get('origin') in {'OPENAI_MODEL_PROPOSAL','RSI_MODEL_PROPOSAL'}:
        raise ValueError('ORDINARY_CHILD_CANNOT_SELF_QUALIFY')
    policy=capsule.evidence_policy
    guard_real(policy,launch,phase='BASELINE_QUALIFICATION',candidate=candidate,fixtures=fixtures,directory=output)
    if len(fixtures)!=4:raise ValueError('FOUR_BASELINE_SUITES_REQUIRED')
    verify_boundary();validate_function(selector(candidate['worker'].decode())[1])
    freezes={str(Path(f).resolve()):adapters(f)[0](f,expected_count=20) for f in fixtures}
    output=Path(output)
    if output.exists():raise ValueError('FRESH_BASELINE_QUALIFICATION_REQUIRED')
    output.mkdir(parents=True);restrict_directory(output)
    atomic_write(output/'qualification-input.json',canonical({'candidate_id':candidate_id,'origin':origin,
        'fixture_freezes':freezes,'worker_sha256':bytes_hash(candidate['worker']),
        'config_sha256':bytes_hash(candidate['config']),'execution_policy':policy.to_dict(),
        'launch_plan_sha256':launch.plan_sha256 if launch else None}),exclusive=True)
    try:
        capsule.launch_context=launch
        capsule.setup();deadline=time.monotonic()+1800
        probes=output/'probes';probes.mkdir()
        atomic_write(probes/'worker.py',invariant_probe_source(candidate['worker']),exclusive=True)
        atomic_write(probes/'input.json',b'{}',exclusive=True)
        unit=capsule.child('prepare',[probes/'input.json',probes/'capture.json'],probes,
            worker=probes/'worker.py',business_root=capsule.business_root)
        if unit.get('status')!='PASS':raise ValueError('BASELINE_INVARIANT_PROBE_FAILED')
        decisions=[]
        for index,fixture in enumerate(fixtures,1):
            guard_real(policy,launch,phase='BASELINE_QUALIFICATION',candidate=candidate,fixtures=fixtures,directory=output)
            decision=adapters(fixture)[1](capsule,fixture,{'A':candidate,'B':candidate},output/f'suite-{index}',
                deadline=deadline,schema='rsi-baseline-aa/v1',scope='root A/A qualification; no improvement hypothesis')
            _check_equal(decision);decisions.append(decision)
        receipt={'schema':'rsi-baseline-qualification/v1',
            'status':'BASELINE_QUALIFIED' if policy.real_eligible else 'CONTROL_BASELINE_QUALIFIED',
            'candidate_id':candidate_id,'origin':origin,'qualification_scope':'EXPERIMENT_ROOT_AND_TRUSTED_ANCHOR_ONLY',
            'worker_sha256':bytes_hash(candidate['worker']),'config_sha256':bytes_hash(candidate['config']),
            'fixture_freezes':freezes,'suite_decisions':decisions,'suite_expected':4,
            'evidence_mode':policy.mode,'execution_policy':policy.to_dict(),'real_eligible':policy.real_eligible,
            'autonomous_adoption':False,'helpful_credit':False,'improvement_claim':False,
            'production_qualified':False,'production_changed':False}
        atomic_write(output/'baseline-qualification.json',canonical(receipt),exclusive=True)
        return receipt
    finally:capsule.close()


def verify_baseline_qualification(*,registry,receipt,fixtures,evidence):
    registry=QueryRegistry(registry);path=Path(receipt);record=load(path)
    if not isinstance(evidence,ExecutionEvidence):raise ValueError('TRUSTED_EXECUTION_POLICY_REQUIRED')
    expected='BASELINE_QUALIFIED' if evidence.real_eligible else 'CONTROL_BASELINE_QUALIFIED'
    if (record.get('schema')!='rsi-baseline-qualification/v1' or record.get('status')!=expected
            or record.get('evidence_mode')!=evidence.mode or record.get('execution_policy')!=evidence.to_dict()
            or record.get('real_eligible') is not evidence.real_eligible
            or record.get('autonomous_adoption') is not False or record.get('improvement_claim') is not False):
        raise ValueError('BASELINE_QUALIFICATION_INVALID')
    cid=record['candidate_id'];registry.require_not_quarantined(cid);candidate=registry.get(cid)
    if (bytes_hash(candidate['worker'])!=record['worker_sha256']
            or bytes_hash(candidate['config'])!=record['config_sha256']
            or candidate['record'].get('origin') in {'OPENAI_MODEL_PROPOSAL','RSI_MODEL_PROPOSAL'}):
        raise ValueError('BASELINE_ARTIFACT_CHANGED')
    if len(fixtures)!=4 or record.get('suite_expected')!=4:raise ValueError('FOUR_BASELINE_SUITES_REQUIRED')
    freezes={str(Path(f).resolve()):adapters(f)[0](f,expected_count=20) for f in fixtures}
    if freezes!=record['fixture_freezes']:raise ValueError('BASELINE_FIXTURE_CHANGED')
    probe=path.parent/'probes'
    if (probe/'worker.py').read_bytes()!=invariant_probe_source(candidate['worker']) or load(probe/'capture.json').get('status')!='PASS':
        raise ValueError('BASELINE_INVARIANT_PROBE_CHANGED')
    from .execution_evidence import check_receipt
    check_receipt(evidence,probe/'execution-receipt.json',stage='prepare',worker_hash=bytes_hash((probe/'worker.py').read_bytes()),
        input_path=probe/'input.json',capture_path=probe/'capture.json')
    for index,fixture in enumerate(fixtures,1):
        run=path.parent/f'suite-{index}';manifest=load(run/'manifest.json')
        if any(manifest['arms'][a]['candidate_id']!=cid or manifest['arms'][a]['worker_sha256']!=record['worker_sha256']
               or manifest['arms'][a]['sha256']!=record['config_sha256'] for a in ('A','B')):
            raise ValueError('BASELINE_AA_LINEAGE_CHANGED')
        decision=adapters(fixture)[2](run,fixture,cid,{'angle':'P2_BUDGET','proposal_id':'rsi-baseline-aa/v1'},
            record['worker_sha256'],record['config_sha256'],evidence=evidence)
        _check_equal(decision)
        if decision!=record['suite_decisions'][index-1]:raise ValueError('BASELINE_EXECUTION_EVIDENCE_CHANGED')
    return candidate,record

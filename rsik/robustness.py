"""Source-layout hard gates; explicitly engineering-only, never a holdout claim."""
from copy import deepcopy
from pathlib import Path
import time

from .adjudicate import _p_fields
from fractions import Fraction
from .errors import StageFailure
from .foundation import check_split
from .locking import atomic_write
from .state import canonical


def assess_receipt(run,fixture,registry,candidate_id):
    from .experiment import load,bytes_hash,read_decision,registry_api
    run,fixture,registry=Path(run),Path(fixture),Path(registry)
    if load(fixture/'manifest.json').get('v22_scope',{}).get('mode') not in {'SOURCE_LAYOUT_STRESS','SOURCE_QUERY_STRESS'}:
        raise ValueError('NOT_A_FROZEN_SOURCE_LAYOUT_STRESS')
    check_split(fixture,expected_count=20)
    freeze=load(fixture/'freeze.json')
    for name,expected in freeze['artifact_hashes'].items():
        if bytes_hash((fixture/name).read_bytes())!=expected:raise ValueError('ROBUSTNESS_FIXTURE_CHANGED')
    candidate=registry_api.candidate(registry,candidate_id)
    decision=read_decision(run,fixture,candidate_id,{'angle':'P2_BUDGET','proposal_id':'source-layout-hard-gate'},
                           candidate['worker'],candidate['config'])
    hard_pass=all(decision['gates'].get('G'+str(i)) is True for i in range(1,5))
    return {'schema_version':'rsi-source-layout-hard-gate/v1','candidate_id':candidate_id,
        'passed':hard_pass,'round_stop':decision['round_stop'],'reason_codes':decision['reason_codes'],
        'fixture_path':str(fixture.resolve()),'run_path':str(run.resolve()),
        'fixture_freeze_sha256':bytes_hash((fixture/'freeze.json').read_bytes()),
        'run_results_sha256':bytes_hash((run/'results.json').read_bytes()),
        'questions':20,'paired':sum(row.get('pair_status')=='PAIRED' for row in decision['per_question']),
        'candidate_fact_loss_questions':sum(any(s=='ABSENT' for s in row.get('candidate_facts',{}).values()) for row in decision['per_question']),
        'delta_chars':sum(row['chars_B']-row['chars_A'] for row in decision['per_question']
                          if type(row.get('chars_A')) is int and type(row.get('chars_B')) is int),
        'scope':('same-source real alternate-query stress' if load(fixture/'manifest.json').get('v22_scope',{}).get('mode')=='SOURCE_QUERY_STRESS'
                 else 'same-source controlled layout stress')+'; hard gates only, not independent holdout or another statistical win'}


def gate_training(decision,receipt):
    result=deepcopy(decision);result['robustness']=deepcopy(receipt)
    if receipt['passed']:return result
    confirmed='FACT_LOSS' in receipt['reason_codes'] or 'LOCAL_REGRESSION' in receipt['reason_codes'] or 'EXEC_FAULT' in receipt['reason_codes']
    result.update(gates_passed=False,verdict='REJECTED' if confirmed else 'INCONCLUSIVE',
        reason_codes=['ROBUSTNESS_FACT_LOSS' if receipt['candidate_fact_loss_questions'] else 'ROBUSTNESS_FAILED'],
        round_stop=receipt['round_stop'],sign_p_raw=_p_fields(Fraction(1)))
    result['gates']['G2']=False if receipt['candidate_fact_loss_questions'] else None
    return result


def gate_training_suite(decision,receipts):
    result=deepcopy(decision)
    for receipt in receipts:
        result=gate_training(result,receipt)
        if not receipt['passed']:break
    result['robustness_suite']=deepcopy(receipts)
    if receipts:result['robustness']=deepcopy(receipts[0])
    return result


def replay_robustness(registry,parent_id,candidate_id,fixture,output,capsule,journal,stage,deadline):
    from .experiment import load,bytes_hash,registry_api,executor
    registry,fixture,output=Path(registry),Path(fixture),Path(output)
    bundle,run=output/'bundle',output/'run'
    expected={'cases':bytes_hash(canonical(load(fixture/'cases.json'))),
              'evaluation':bytes_hash(canonical(load(fixture/'evaluation.json'))),
              'scope':'engineering-only; no comparison to R0 results'}
    def materialized(_):
        if not bundle.exists():return {'status':'ABSENT','safe_retry':True}
        if not (bundle/'manifest.json').exists():return {'status':'UNKNOWN'}
        manifest=load(bundle/'manifest.json');registry_api.verify_arm_sources(manifest,bundle)
        if manifest['r1'].get('fixture')!=expected or manifest['r1']['child']!=candidate_id:return {'status':'CONFLICT'}
        return {'status':'MATCHED','output':{'manifest_sha256':bytes_hash((bundle/'manifest.json').read_bytes())}}
    def materialize(_):
        output.mkdir(parents=True,exist_ok=True)
        registry_api.materialize(registry,parent_id,candidate_id,bundle,
                                cases_path=fixture/'cases.json',evaluation_path=fixture/'evaluation.json')
        return materialized(None)['output']
    journal.execute(stage+'-materialize',{'parent':parent_id,'candidate':candidate_id,'fixture':expected},materialize,materialized)
    def replayed(_):
        if not run.exists():return {'status':'ABSENT','safe_retry':True}
        if not (run/'results.json').exists():return {'status':'UNKNOWN'}
        return {'status':'MATCHED','output':{'files':{p.relative_to(run).as_posix():bytes_hash(p.read_bytes()) for p in run.rglob('*') if p.is_file()}}}
    def replay(_):
        capsule.deadline=min(time.monotonic()+300,deadline)
        with capsule.bind(executor):executor.run(bundle/'manifest.json',run,business_root=capsule.business_root)
        return replayed(None)['output']
    journal.execute(stage+'-replay',{'manifest_sha256':bytes_hash((bundle/'manifest.json').read_bytes())},replay,replayed)
    receipt=assess_receipt(run,fixture,registry,candidate_id)
    path=output/'receipt.json'
    if path.exists():
        if load(path)!=receipt:raise StageFailure('RECOVERY_ERROR','ROBUSTNESS_RECEIPT_CHANGED')
    else:atomic_write(path,canonical(receipt),exclusive=True)
    return receipt

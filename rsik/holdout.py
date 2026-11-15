"""External, one-use sealed comparison using the unchanged public evaluator.

No engineering-fixture override, registry derivation, training result rewriting or
model access. The adapter emits its own explicitly external identity contract.
"""
from pathlib import Path
import json
import time
import uuid

from .adjudicate import adjudicate_holdout, adjudicate_round
from .experiment import (bytes_hash, executor, load, read_decision, registry_api)
from .foundation import check_split, verify_boundary
from .locking import WorkspaceLock, atomic_write, restrict_directory
from .state import canonical
from .lineage import ParentPool
from .handbook.store import Handbook


def verify_training_adoption(training, train_fixture, registry, candidate_id):
    training, registry = Path(training), Path(registry)
    if any(e['event_type']=='PARENT_QUARANTINED' and e['payload']['candidate_id']==candidate_id
           for e in ParentPool(registry/'policy').log.read()):raise ValueError('CANDIDATE_ROBUSTNESS_QUARANTINED')
    record=load(training/'round.json')
    if record.get('adopted_candidate_id') != candidate_id:
        raise ValueError('NO_TRAINING_ADOPTION')
    decisions=[]
    # Recompute actual replay judgments; a model/user-supplied VALIDATED string
    # or detached round.json is not sufficient admission evidence.
    for slot in record['qualified']:
        proposal=record['proposal_instances'][slot]
        existing=load(training/slot/'decision.json')
        candidate=registry_api.candidate(registry,existing['candidate_id'])
        decision=read_decision(training/slot/'run',train_fixture,existing['candidate_id'],proposal,
                               candidate['worker'],candidate['config'])
        if 'robustness' in existing:
            from .robustness import assess_receipt,gate_training,gate_training_suite
            receipts=[]
            for old in existing.get('robustness_suite',[existing['robustness']]):
                stress_run=Path(old['run_path'])
                if not stress_run.resolve().is_relative_to(training.resolve()):raise ValueError('UNBOUND_ROBUSTNESS_RUN')
                receipt=assess_receipt(stress_run,old['fixture_path'],registry,existing['candidate_id'])
                if receipt!=old:raise ValueError('ROBUSTNESS_EVIDENCE_CHANGED')
                receipts.append(receipt)
            decision=gate_training_suite(decision,receipts) if 'robustness_suite' in existing else gate_training(decision,receipts[0])
        decisions.append(decision)
    recomputed=adjudicate_round(decisions,all_qualified_terminal=set(record['qualified'])==set(record['attempted_replays']))
    if recomputed.get('adopted_candidate_id') != candidate_id:
        raise ValueError('TRAINING_ADOPTION_NOT_REPRODUCIBLE')
    return bytes_hash((training/'round.json').read_bytes())


def external_pair(capsule, registry, fixture, parent_id, candidate_id, output, *, deadline):
    """Use original checked_case/execution_view/assess/compare_pair functions."""
    registry=Path(registry)
    parent,candidate=(registry_api.candidate(registry,cid) for cid in (parent_id,candidate_id))
    artifacts={arm:{'candidate_id':cid,'worker':registry_api.get(registry,record['worker']),
                   'config':registry_api.get(registry,record['config'])}
               for arm,cid,record in [('A',parent_id,parent),('B',candidate_id,candidate)]}
    return replay_artifacts(capsule,fixture,artifacts,output,deadline=deadline,
        schema='rsi-external-holdout/v2.2',scope='external sealed verification; never lineage training')


def replay_artifacts(capsule, fixture, artifacts, output, *, deadline, schema, scope):
    """Shared execution plumbing; caller supplies explicit experiment identity."""
    fixture,output=Path(fixture),Path(output)
    if schema not in {'rsi-external-holdout/v2.2','rsi-query-aware-engineering/v1','rsi-safety-engineering/v1'}:
        raise ValueError('UNKNOWN_REPLAY_PROFILE')
    if set(artifacts)!={'A','B'}:raise ValueError('TWO_ARTIFACT_ARMS_REQUIRED')
    output.mkdir(parents=True,exist_ok=False)
    cases,rubric=load(fixture/'cases.json'),load(fixture/'evaluation.json')
    run_id='external-'+uuid.uuid4().hex
    manifest={'schema':schema,'run_id':run_id,
        'fixture_scope':scope,
        'cases':{'sha256':bytes_hash(canonical(cases))},'evaluation':{'sha256':bytes_hash(canonical(rubric))},
        'evaluator_version':executor.EVALUATOR,'fixed':executor.FIXED,'arms':{}}
    for arm,record in artifacts.items():
        cid,worker,config=record['candidate_id'],record['worker'],record['config']
        atomic_write(output/(arm+'_worker.py'),worker,exclusive=True)
        atomic_write(output/(arm+'.json'),config,exclusive=True)
        manifest['arms'][arm]={'candidate_id':cid,'worker_sha256':bytes_hash(worker),'sha256':bytes_hash(config)}
    atomic_write(output/'manifest.json',canonical(manifest),exclusive=True)
    rows=[]
    capsule.deadline=min(time.monotonic()+300,deadline)
    for index,item in enumerate(cases,1):
        case=executor.checked_case(item['case']);qid=item['qid'];directory=output/f'case-{index:03}'
        directory.mkdir();atomic_write(directory/'input.json',canonical(case),exclusive=True)
        row={'qid':qid,'run_id':run_id,'input_sha256':executor.fingerprint(case),'status':'FAILED','arms':{}}
        try:
            raw=capsule.child('prepare',[directory/'input.json',directory/'snapshot.json'],directory,
                              business_root=capsule.business_root)
            snapshot=executor.execution_view(raw)
            atomic_write(directory/'arm-input.json',canonical(snapshot),exclusive=True)
            up=executor.fingerprint(snapshot);row['upstream_sha256']=up
            for arm in ('A','B'):
                arm_dir=directory/arm;arm_dir.mkdir()
                capture=capsule.child('deliver',[directory/'arm-input.json',output/(arm+'.json'),arm_dir/'capture.json'],arm_dir,
                    worker=output/(arm+'_worker.py'),business_root=capsule.business_root)
                if capture.get('status')!='OK' or capture.get('upstream_sha256')!=up:
                    raise ValueError('CAPTURE_OR_PAIR_UNKNOWN')
                score=executor.assess(case,capture,rubric[qid],arm)
                score.update(qid=qid,run_id=run_id,upstream_sha256=up,
                    fixed_config_sha256=executor.fingerprint(executor.FIXED),evaluator_version=executor.EVALUATOR,
                    candidate_id=manifest['arms'][arm]['candidate_id'],
                    receipt_path=(arm_dir/'capture.json').relative_to(output).as_posix(),
                    receipt_sha256=bytes_hash((arm_dir/'capture.json').read_bytes()))
                row['arms'][arm]=score
            executor.compare_pair(row['arms']['A'],row['arms']['B'])
            row['status']='PAIRED'
        except (OSError,ValueError,RuntimeError,KeyError):
            row['reason']='EXTERNAL_PAIR_INCOMPLETE'
        rows.append(row);atomic_write(directory/'result.json',canonical(row),exclusive=True)
    atomic_write(output/'results.json',canonical({'rows':rows,'summary':{
        'total':len(cases),'paired':sum(r['status']=='PAIRED' for r in rows),
        'scope':scope,'external_model_calls':0,'answer_quality':'NOT_RUN'}}),exclusive=True)
    candidate=artifacts['B']
    return read_decision(output,fixture,candidate['candidate_id'],{'angle':'P2_BUDGET','proposal_id':schema},
                         bytes_hash(candidate['worker']),bytes_hash(candidate['config']))


def verify_sealed(*, registry, training, train_fixture, sealed_fixture, candidate_id,
                  trusted_id, lineage_id, policy, capsule, output):
    registry,output,fixture=Path(registry),Path(output),Path(sealed_fixture)
    with WorkspaceLock(registry/'writer.lock','sealed-holdout'):
        training_hash=verify_training_adoption(training,train_fixture,registry,candidate_id)
        candidate=registry_api.candidate(registry,candidate_id)
        parent_id=candidate['parent']
        verify_boundary()
        sealed_count=len(load(fixture/'cases.json'))
        if not 10<=sealed_count<=20:raise ValueError('SEALED_SIZE_OUTSIDE_FROZEN_RANGE')
        check_split(fixture,expected_count=sealed_count)
        freeze=load(fixture/'freeze.json')
        for name,expected in freeze['artifact_hashes'].items():
            if bytes_hash((fixture/name).read_bytes())!=expected:raise ValueError('HOLDOUT_CHANGED')
        if output.exists():raise ValueError('HOLDOUT_OUTPUT_ALREADY_EXISTS')
        policy.claim_holdout(lineage_id,candidate_id,holdout_freeze_hash=bytes_hash((fixture/'freeze.json').read_bytes()))
        output.mkdir(parents=True)
        restrict_directory(output)
        try:
            capsule.setup();deadline=time.monotonic()+1800
            parent_comparison=external_pair(capsule,registry,fixture,parent_id,candidate_id,output/'vs-parent',deadline=deadline)
            if trusted_id==parent_id:
                trusted_comparison=json.loads(json.dumps(parent_comparison))
                shared=True
            else:
                trusted_comparison=external_pair(capsule,registry,fixture,trusted_id,candidate_id,output/'vs-trusted',deadline=deadline)
                shared=False
            result=adjudicate_holdout(parent_comparison,trusted_comparison)
            result.update(schema_version='rsi-external-holdout/v2.2',candidate_id=candidate_id,parent_id=parent_id,
                trusted_id=trusted_id,shared_physical_comparison=shared,training_result_sha256=training_hash,
                training_cases_sha256=bytes_hash(canonical(load(Path(train_fixture)/'cases.json'))),
                holdout_freeze_sha256=bytes_hash((fixture/'freeze.json').read_bytes()),production_changed=False)
            atomic_write(output/'receipt.restricted.json',canonical(result),exclusive=True)
            policy.finish_holdout(lineage_id,candidate_id,result['verdict'],
                                  receipt_hash=bytes_hash((output/'receipt.restricted.json').read_bytes()))
            Handbook(registry/'handbook').log.append('holdout:'+candidate_id,
                'HOLDOUT_PASSED' if result['verdict']=='VERIFIED' else 'HOLDOUT_FAILED',
                {'candidate_id':candidate_id,'reason':'VERIFIED' if result['verdict']=='VERIFIED' else
                 'REGRESSION' if result['verdict']=='REGRESSION_SUSPECTED' else 'UNKNOWN',
                 'receipt_hash':bytes_hash((output/'receipt.restricted.json').read_bytes())})
            if result['verdict']=='VERIFIED':
                # Pool fitness must stay on the common training distribution;
                # sealed document lengths are qualification evidence, not a
                # comparable parent-selection objective.
                primary=next(d for d in load(Path(training)/'round.json')['candidate_decisions'] if d.get('candidate_id')==candidate_id)
                primary_run=Path(training)/primary['angle']/'run'
                seconds=sum(load(p).get('elapsed_seconds',0) for p in primary_run.rglob('execution-receipt.json'))
                ParentPool(registry/'policy').admit(candidate_id,lineage_id,output/'receipt.restricted.json',
                    kind='VERIFIED',chars=sum(r['chars_B'] for r in primary['per_question']),replay_seconds=seconds)
            return {'verdict':result['verdict'],'family_size':2,'production_changed':False}
        finally:
            capsule.close()

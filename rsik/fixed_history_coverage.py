"""NEW receipt-bound fixed-history coverage; old receipts cannot be upgraded.

Seventeen retrieval slots retain the exact source-relations coverage formulas.
Three actual business no-call controls have zero retrieved-character/IR credit.
"""
from datetime import datetime,timezone
from pathlib import Path
import json
import time

from .events import canonical,digest
from .experiment import executor,ROOT,bytes_hash
from .execution_evidence import ExecutionEvidence
from .locking import atomic_write,restrict_directory
from .query_aware import QueryRegistry
from .fixed_history_worker import build_worker
from . import fixed_history_profile as profile
from . import source_relations as relations

PLAN_SCHEMA='rsi-fixed-history-coverage-collection-plan/v1'
RECORD_SCHEMA='rsi-fixed-history-coverage/v1'
_load=profile._load
CATEGORIES=relations.CATEGORIES
REVIEW_POLICIES=relations.REVIEW_POLICIES
sha=relations.sha
_resolve_record_path=relations._resolve_record_path


def identity(candidate):
    return {'candidate_id':candidate['candidate_id'],'worker_sha256':bytes_hash(candidate['worker']),
            'config_sha256':bytes_hash(candidate['config'])}


def artifact(registry,cid):
    item=registry.get(cid);registry.require_not_quarantined(cid)
    if build_worker(item['worker'])!=item['worker']:raise ValueError('FIXED_COVERAGE_TRUSTED_WORKER_TEMPLATE_REQUIRED')
    return item


def fixture_inputs(fixture):
    fixture=Path(fixture).resolve();manifest=_load(fixture/'manifest.json')
    if manifest.get('partition')!='DEV':
        raise ValueError('FIXED_COVERAGE_DEV_ONLY')
    profile.check_fixture(fixture,review_policy='research-source-family/v1')
    cases=_load(fixture/'cases.json');specs=_load(fixture/'relations.restricted.json')
    out={}
    for item in cases:
        qid=item['qid'];sidecar=_load(fixture/manifest['history_sidecars'][qid]['path'])
        derivation=manifest.get('query_derivations',{}).get(qid)
        envelope=profile.preparation_input(item,sidecar,manifest['lineage_id'],query_derivation=derivation)
        out[qid]={'item':item,'sidecar':sidecar,'derivation':derivation,'envelope':envelope,
            'domain':'IR_SOURCE_FACTS' if qid in specs else 'BUSINESS_BRANCH_CONTROL',
            'spec':specs.get(qid)}
    return manifest,cases,out


def prepare_collection(*,registry,candidate_id,fixtures,output):
    from .coverage_collection import removal_worker
    from .launch import fixture_identity
    registry=QueryRegistry(registry);positive=artifact(registry,candidate_id)
    tasks=[]
    for value in fixtures:
        fixture=Path(value).resolve();manifest,cases,inputs=fixture_inputs(fixture)
        controls=_load(fixture/'control-spec.restricted.json')
        if any(c['qid'] not in inputs or inputs[c['qid']]['domain']!='IR_SOURCE_FACTS' for c in controls):
            raise ValueError('FIXED_COVERAGE_BUSINESS_CANNOT_SUPPLY_IR_ATOMS')
        for item in cases:
            qid=item['qid'];data=inputs[qid];case=data['envelope']['case']
            planned=[{'kind':'POSITIVE' if data['domain']=='IR_SOURCE_FACTS' else 'BUSINESS_CONTROL',
                      'removed_member_ids':[],'candidate':identity(positive)}]
            if data['domain']=='IR_SOURCE_FACTS':
                grouped={}
                for control in controls:
                    if control['qid']!=qid:continue
                    removed=tuple(sorted(control['removed_member_ids']))
                    if not removed or set(removed)&set(case['seed_ids']):raise ValueError('COVERAGE_NEGATIVE_MUST_REMOVE_NONSEED')
                    grouped.setdefault(removed,[]).append(control['atom_id'])
                if {a['atom_id'] for a in data['spec']['atoms']}!={a for atoms in grouped.values() for a in atoms}:
                    raise ValueError('COVERAGE_EVERY_ATOM_CONTROL_REQUIRED')
                for removed,atoms in sorted(grouped.items()):
                    worker=removal_worker(positive['worker'],removed)
                    cid=registry.put(worker,positive['config'],parent_id=candidate_id,origin='ENGINEERING_COVERAGE_NEGATIVE',
                        evidence={'purpose':'FIXED_HISTORY_DECLARED_NONSEED_REMOVAL_NOT_GAIN','fixture':str(fixture),
                                  'qid':qid,'removed_member_ids':list(removed),'atom_ids':atoms,'production_qualified':False})
                    planned.append({'kind':'MEMBER_REMOVAL','removed_member_ids':list(removed),'atom_ids':atoms,
                                    'candidate':identity(artifact(registry,cid))})
            tasks.append({'fixture_path':str(fixture),'qid':qid,'domain':data['domain'],
                'prepare_input_sha256':sha(canonical(data['envelope'])),'controls':planned})
    record={'schema':PLAN_SCHEMA,'evaluator_profile':profile.PROFILE,
        'prepare_worker_sha256':sha((ROOT/'scripts/rsi_fixed_history_worker_v1.py').read_bytes()),
        'fixtures':{str(Path(f).resolve()):fixture_identity(f) for f in fixtures},'tasks':tasks}
    record['review']={'status':'AI_REVIEWED','object_sha256':digest(record),
        'reviewer':'Trusted deterministic fixed-history coverage engineering','reviewed_at':datetime.now(timezone.utc).isoformat()}
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False);restrict_directory(output)
    atomic_write(output/'collection-plan.restricted.json',canonical(record),exclusive=True)
    return record


def collection_tasks(plan,registry):
    from .launch import read_binding,fixture_identity
    collection=read_binding(plan['coverage_collection']);subject={k:v for k,v in collection.items() if k!='review'}
    review=collection.get('review',{})
    if (collection.get('schema')!=PLAN_SCHEMA or collection.get('evaluator_profile')!=profile.PROFILE
            or review.get('status') not in {'AI_REVIEWED','HUMAN_REVIEWED'} or review.get('object_sha256')!=digest(subject)
            or collection.get('fixtures')!=plan.get('fixtures')
            or collection.get('prepare_worker_sha256')!=sha((ROOT/'scripts/rsi_fixed_history_worker_v1.py').read_bytes())):
        raise ValueError('FIXED_COVERAGE_COLLECTION_PLAN_REQUIRED')
    registry=QueryRegistry(registry);expected={}
    for fixture in plan['fixtures']:
        if fixture_identity(fixture)!=plan['fixtures'][fixture]:raise ValueError('FIXED_COVERAGE_FIXTURE_CHANGED')
        _,cases,inputs=fixture_inputs(fixture)
        for item in cases:expected[(str(Path(fixture).resolve()),item['qid'])]=inputs[item['qid']]
    seen=set()
    for task in collection['tasks']:
        key=(task['fixture_path'],task['qid']);data=expected.get(key)
        if key in seen or data is None or task['domain']!=data['domain'] or task['prepare_input_sha256']!=sha(canonical(data['envelope'])):
            raise ValueError('FIXED_COVERAGE_UNPLANNED_SLOT')
        seen.add(key);controls=task['controls']
        if data['domain']=='BUSINESS_BRANCH_CONTROL':
            if len(controls)!=1 or controls[0]['kind']!='BUSINESS_CONTROL' or controls[0]['removed_member_ids']:
                raise ValueError('FIXED_COVERAGE_BUSINESS_DOMAIN_REQUIRED')
        else:
            if sum(c['kind']=='POSITIVE' for c in controls)!=1 or any(c['kind'] not in {'POSITIVE','MEMBER_REMOVAL'} for c in controls):
                raise ValueError('FIXED_COVERAGE_IR_CONTROLS_REQUIRED')
            atoms={a['atom_id'] for a in data['spec']['atoms']};targeted=set()
            for c in controls:
                if c['kind']=='MEMBER_REMOVAL':
                    if not c['removed_member_ids'] or set(c['removed_member_ids'])&set(data['envelope']['case']['seed_ids']):
                        raise ValueError('COVERAGE_NEGATIVE_MUST_REMOVE_NONSEED')
                    targeted.update(c['atom_ids'])
            if targeted!=atoms:raise ValueError('COVERAGE_EVERY_ATOM_CONTROL_REQUIRED')
        for c in controls:
            if identity(artifact(registry,c['candidate']['candidate_id']))!=c['candidate']:
                raise ValueError('FIXED_COVERAGE_ARTIFACT_CHANGED')
    if seen!=set(expected):raise ValueError('FIXED_COVERAGE_ALL_20_SLOTS_REQUIRED')
    if [(t['fixture_path'],t['qid']) for t in collection['tasks']]!=list(expected):
        raise ValueError('FIXED_COVERAGE_TASK_ORDER_CHANGED')
    return collection['tasks']


def _assess(case,capture,spec,sources,fixture):
    manifest=_load(Path(fixture)/'manifest.json');qid=capture.get('qid')
    ref=manifest['history_sidecars'].get(qid)
    if not ref:raise ValueError('FIXED_COVERAGE_CAPTURE_QID_CHANGED')
    return profile.assess(case,capture,spec,sources,sidecar=_load(Path(fixture)/ref['path']),
                          query_derivation=manifest.get('query_derivations',{}).get(qid))


def _slot_rows(record,base):
    expected=[];inputs={}
    for value in record['fixtures']:
        fixture=_resolve_record_path(value,base);_,cases,data=fixture_inputs(fixture)
        for item in cases:
            key=(str(fixture),item['qid']);expected.append(key);inputs[key]=data[item['qid']]
    tasks=record.get('execution_tasks',[])
    if [(t['fixture'],t['qid']) for t in tasks]!=expected:raise ValueError('FIXED_COVERAGE_ALL_20_SLOTS_REQUIRED')
    for task in tasks:
        if task['domain']!=inputs[(task['fixture'],task['qid'])]['domain']:raise ValueError('FIXED_COVERAGE_SLOT_DOMAIN_CHANGED')
    return tasks,inputs


def recompute_coverage(record):
    if isinstance(record,(str,Path)):
        path=Path(record).resolve();record=_load(path);base=path.parent
    else:base=Path(record.get('base_dir','.')).resolve()
    if record.get('schema')!=RECORD_SCHEMA or record.get('evaluator_profile')!=profile.PROFILE:
        raise ValueError('FIXED_COVERAGE_NEW_RECORD_REQUIRED')
    tasks,inputs=_slot_rows(record,base)
    result=_recompute_ir_coverage(record)
    business=record.get('business_controls',[]);expected={(t['fixture'],t['qid']) for t in tasks if t['domain']=='BUSINESS_BRANCH_CONTROL'}
    if {(r['fixture'],r['qid']) for r in business}!=expected or len(business)!=len(expected):
        raise ValueError('FIXED_COVERAGE_THREE_BUSINESS_CONTROLS_REQUIRED')
    outcomes=[]
    for row in business:
        data=inputs[(row['fixture'],row['qid'])];path=_resolve_record_path(row['capture'],base)
        if sha(path.read_bytes())!=row['sha256']:raise ValueError('FIXED_COVERAGE_CONTROL_CAPTURE_CHANGED')
        capture=_load(path)
        profile.validate_capture_history(data['item']['case'],capture,data['sidecar'],query_derivation=data['derivation'])
        assessed=profile.business_control(capture)
        outcomes.append({'fixture':row['fixture'],'qid':row['qid'],**assessed,'IR_coverage_credit':0})
    result.update(schema='rsi-fixed-history-coverage-result/v1',business_controls=outcomes,
        fixed_slot_counts={'total':len(tasks),'retrieval':sum(t['domain']=='IR_SOURCE_FACTS' for t in tasks),
                           'business_controls':len(business),'dropped':0},history_is_retrieved_support=False)
    if any(r['status']!='PRESENT' for r in outcomes):result['status']='BLOCKED_BUSINESS_CONTROL'
    return result


def verify_coverage_execution(record,policy):
    from .launch import read_binding,fixture_identity
    if (record.get('schema')!=RECORD_SCHEMA or record.get('evaluator_profile')!=profile.PROFILE
            or not isinstance(policy,ExecutionEvidence) or not policy.real_eligible
            or record.get('execution_policy')!=policy.to_dict() or record.get('evidence_mode')!='REAL_REPLAY'
            or record.get('real_eligible') is not True):raise ValueError('FIXED_REAL_COVERAGE_EXECUTION_REQUIRED')
    launch=read_binding(record['launch_plan']);collection=read_binding(record['collection_plan'])
    if (record['launch_plan_sha256']!=record['launch_plan']['sha256'] or launch.get('allowed_phases')!=['COVERAGE_COLLECTION']
            or launch.get('schema')!='rsi-launch/v1' or launch.get('coverage_collection')!=record['collection_plan']
            or launch.get('run_id')!=record['launch_run_id'] or launch.get('execution_policy')!=policy.to_dict()
            or Path(launch['run_directory']).resolve()!=Path(record['base_dir']).resolve()):
        raise ValueError('FIXED_COVERAGE_RUN_BINDING_MISMATCH')
    planned=collection_tasks(launch,launch['registry']);tasks,inputs=_slot_rows(record,Path(record['base_dir']).resolve())
    if [(t['fixture_path'],t['qid']) for t in planned]!=[(t['fixture'],t['qid']) for t in tasks]:
        raise ValueError('FIXED_COVERAGE_TASK_ORDER_CHANGED')
    runtime=None;sequence=0;manifest={'execution_context':{k:record[k] for k in ('capsule_job_id','launch_run_id','launch_plan_sha256')}}
    base=Path(record['base_dir']).resolve()
    expected_ir=[];expected_business=[]
    for task,approved in zip(tasks,planned):
        fixture=Path(task['fixture']);data=inputs[(task['fixture'],task['qid'])]
        if fixture_identity(fixture)!=collection['fixtures'][str(fixture)]:raise ValueError('FIXED_COVERAGE_FIXTURE_CHANGED')
        prep=_resolve_record_path(task['prepare_input'],base);snap=_resolve_record_path(task['prepare_capture'],base)
        arm=_resolve_record_path(task['arm_input'],base)
        if _load(prep)!=data['envelope'] or sha(prep.read_bytes())!=approved['prepare_input_sha256']:
            raise ValueError('FIXED_COVERAGE_PREPARE_INPUT_CHANGED')
        receipt=_resolve_record_path(task['prepare_receipt'],base)
        runtime,sequence=profile._receipt(policy,receipt,manifest=manifest,previous_sequence=sequence,stage='prepare',
            worker_hash=collection['prepare_worker_sha256'],input_path=prep,capture_path=snap,runtime_identity=runtime)
        relations._coverage_receipt_run_binding(_load(receipt),record)
        prepared=executor.execution_view(profile.validate_prepared(_load(snap),data['envelope']))
        if _load(arm)!=prepared:raise ValueError('FIXED_COVERAGE_ARM_INPUT_CHANGED')
        ordered=sorted(approved['controls'],key=lambda c:c['kind']=='MEMBER_REMOVAL')
        deliveries=task['deliveries']
        if len(deliveries)!=len(ordered):raise ValueError('FIXED_COVERAGE_DELIVERY_COUNT_CHANGED')
        positive=None
        for delivery,control in zip(deliveries,ordered):
            if delivery['control']!=control:raise ValueError('FIXED_COVERAGE_UNPLANNED_DELIVERY')
            cap=_resolve_record_path(delivery['capture'],base);cfg=_resolve_record_path(delivery['config'],base)
            worker=_resolve_record_path(delivery['worker'],base);rcp=_resolve_record_path(delivery['receipt'],base)
            if sha(worker.read_bytes())!=control['candidate']['worker_sha256'] or sha(cfg.read_bytes())!=control['candidate']['config_sha256']:
                raise ValueError('FIXED_COVERAGE_CONTROL_ARTIFACT_CHANGED')
            runtime,sequence=profile._receipt(policy,rcp,manifest=manifest,previous_sequence=sequence,stage='deliver',
                worker_hash=control['candidate']['worker_sha256'],input_path=arm,capture_path=cap,config_path=cfg,
                canonical_config_hash=sha(canonical(_load(cfg))),runtime_identity=runtime)
            relations._coverage_receipt_run_binding(_load(rcp),record)
            capture=_load(cap)
            if sha(cap.read_bytes())!=delivery['sha256'] or capture.get('upstream_sha256')!=executor.fingerprint(prepared):
                raise ValueError('FIXED_COVERAGE_DELIVERY_CAPTURE_CHANGED')
            profile.validate_capture_history(data['item']['case'],capture,data['sidecar'],query_derivation=data['derivation'])
            if control['kind']=='BUSINESS_CONTROL':
                if profile.business_control(capture)['status']!='PRESENT':raise ValueError('FIXED_COVERAGE_BUSINESS_CONTROL_FAILED')
                expected_business.append((str(fixture),task['qid'],str(cap),delivery['sha256']))
            elif control['kind']=='POSITIVE':positive=(delivery,capture,str(cap))
            else:
                if positive is None:raise ValueError('FIXED_COVERAGE_POSITIVE_MUST_EXECUTE_FIRST')
                ids=[m['id'] for m in positive[1]['members'] if m['id'] not in control['removed_member_ids']]
                if [m['id'] for m in capture['members']]!=ids:raise ValueError('COLLECTION_NEGATIVE_NOT_EXACT_DECLARED_REMOVAL')
                for atom in control['atom_ids']:
                    expected_ir.append((str(fixture),task['qid'],atom,tuple(control['removed_member_ids']),positive[2],str(cap),
                                        positive[0]['sha256'],delivery['sha256']))
    actual_ir=[(r['fixture'],r['qid'],r['atom_id'],tuple(r['removed_member_ids']),
                str(_resolve_record_path(r['positive_capture'],base)),str(_resolve_record_path(r['negative_capture'],base)),
                r['positive_sha256'],r['negative_sha256']) for r in record['controls']]
    actual_business=[(r['fixture'],r['qid'],str(_resolve_record_path(r['capture'],base)),r['sha256']) for r in record['business_controls']]
    if actual_ir!=expected_ir or actual_business!=expected_business:
        raise ValueError('FIXED_COVERAGE_FACT_OR_BUSINESS_REFERENCES_NOT_EXECUTED')
    recompute_coverage(record)
    return {'status':'PASS','evidence_mode':'REAL_REPLAY','slots':len(tasks),'receipt_stages':sequence,
            'IR_control_count':len(record['controls']),'business_controls':len(record['business_controls'])}


def collect(*,registry,candidate_id,fixtures,capsule,output,launch,timeout_seconds=7200):
    from .launch import guard_real,file_binding
    registry=QueryRegistry(registry);candidate=artifact(registry,candidate_id);policy=capsule.evidence_policy
    if registry.directory.resolve()!=Path(launch.registry).resolve():raise ValueError('COLLECTION_REGISTRY_MISMATCH')
    if not policy.real_eligible or launch.phase!='COVERAGE_COLLECTION':raise ValueError('REAL_COVERAGE_COLLECTION_ADMISSION_REQUIRED')
    output=Path(output).resolve()
    guard_real(policy,launch,phase='COVERAGE_COLLECTION',candidate=candidate,fixtures=fixtures,directory=output,boundary='CAPSULE_EXECUTION')
    launch_plan=_load(launch.plan_path);tasks=collection_tasks(launch_plan,launch.registry)
    output.mkdir(parents=True,exist_ok=False);restrict_directory(output)
    atomic_write(output/'collection-input.json',canonical({'launch_plan_sha256':launch.plan_sha256,
        'collection_plan':launch_plan['coverage_collection'],'execution_policy':policy.to_dict()}),exclusive=True)
    controls=[];business=[];executed=[];deadline=time.monotonic()+timeout_seconds;runtime=None;sequence=0
    capsule.launch_context=launch;completed=False
    try:
        capsule.setup()
        run_binding={'capsule_job_id':capsule.job,'launch_run_id':launch.run_id,'launch_plan_sha256':launch.plan_sha256,
                     'collection_plan':launch_plan['coverage_collection']}
        receipt_manifest={'execution_context':{k:run_binding[k] for k in ('capsule_job_id','launch_run_id','launch_plan_sha256')}}
        for number,task in enumerate(tasks,1):
            fixture=Path(task['fixture_path']);_,_,inputs=fixture_inputs(fixture);data=inputs[task['qid']]
            directory=output/f'task-{number:03}';directory.mkdir()
            atomic_write(directory/'input.json',canonical(data['envelope']),exclusive=True)
            if sha((directory/'input.json').read_bytes())!=task['prepare_input_sha256']:raise ValueError('COLLECTION_PREPARE_IDENTITY_CHANGED')
            capsule.deadline=min(deadline,time.monotonic()+300)
            raw=capsule.child('prepare',[directory/'input.json',directory/'snapshot.json'],directory,
                worker=ROOT/'scripts/rsi_fixed_history_worker_v1.py',business_root=capsule.business_root)
            runtime,sequence=profile._receipt(policy,directory/'execution-receipt.json',manifest=receipt_manifest,
                previous_sequence=sequence,stage='prepare',worker_hash=sha((ROOT/'scripts/rsi_fixed_history_worker_v1.py').read_bytes()),
                input_path=directory/'input.json',capture_path=directory/'snapshot.json',runtime_identity=runtime)
            relations._coverage_receipt_run_binding(_load(directory/'execution-receipt.json'),run_binding)
            if executor.execution_view(raw)!=executor.execution_view(_load(directory/'snapshot.json')):
                raise ValueError('FIXED_COVERAGE_PREPARE_CAPTURE_CHANGED')
            snapshot=executor.execution_view(profile.validate_prepared(raw,data['envelope']))
            atomic_write(directory/'arm-input.json',canonical(snapshot),exclusive=True)
            entry={'fixture':str(fixture),'qid':task['qid'],'domain':task['domain'],
                'prepare_input':str(directory/'input.json'),'prepare_capture':str(directory/'snapshot.json'),
                'prepare_receipt':str(directory/'execution-receipt.json'),'arm_input':str(directory/'arm-input.json'),'deliveries':[]}
            positive=None
            for index,control in enumerate(sorted(task['controls'],key=lambda c:c['kind']=='MEMBER_REMOVAL')):
                item=artifact(registry,control['candidate']['candidate_id'])
                arm=directory/f'control-{index:03}';arm.mkdir()
                atomic_write(arm/'worker.py',item['worker'],exclusive=True);atomic_write(arm/'config.json',item['config'],exclusive=True)
                capsule.deadline=min(deadline,time.monotonic()+300)
                cap=capsule.child('deliver',[directory/'arm-input.json',arm/'config.json',arm/'capture.json'],arm,
                    worker=arm/'worker.py',business_root=capsule.business_root)
                runtime,sequence=profile._receipt(policy,arm/'execution-receipt.json',manifest=receipt_manifest,
                    previous_sequence=sequence,stage='deliver',worker_hash=control['candidate']['worker_sha256'],
                    input_path=directory/'arm-input.json',capture_path=arm/'capture.json',config_path=arm/'config.json',
                    canonical_config_hash=sha(canonical(_load(arm/'config.json'))),runtime_identity=runtime)
                relations._coverage_receipt_run_binding(_load(arm/'execution-receipt.json'),run_binding)
                if cap!=_load(arm/'capture.json') or cap.get('upstream_sha256')!=executor.fingerprint(snapshot):raise ValueError('COLLECTION_CAPTURE_INPUT_MISMATCH')
                delivery={'control':control,'capture':str(arm/'capture.json'),'sha256':sha((arm/'capture.json').read_bytes()),
                    'config':str(arm/'config.json'),'worker':str(arm/'worker.py'),'receipt':str(arm/'execution-receipt.json')}
                entry['deliveries'].append(delivery)
                profile.validate_capture_history(data['item']['case'],cap,data['sidecar'],query_derivation=data['derivation'])
                if task['domain']=='BUSINESS_BRANCH_CONTROL':
                    if profile.business_control(cap)['status']!='PRESENT':raise ValueError('FIXED_COVERAGE_BUSINESS_CONTROL_FAILED')
                    business.append({'fixture':str(fixture),'qid':task['qid'],'capture':delivery['capture'],'sha256':delivery['sha256']})
                else:
                    spec=data['spec'];sources={sid:(fixture/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
                    assessed=profile.assess(data['item']['case'],cap,spec,sources,sidecar=data['sidecar'],query_derivation=data['derivation'])
                    if control['kind']=='POSITIVE':
                        if assessed['status']!='PRESENT':raise ValueError('COLLECTION_POSITIVE_FACT_FAILED')
                        structural=executor.assess(data['item']['case'],cap,{'facts':[],'forbidden':[]},'A')
                        if structural['faults']:raise ValueError('COLLECTION_POSITIVE_STRUCTURE_FAILED')
                        positive=(delivery,cap)
                    else:
                        expected=[m['id'] for m in positive[1]['members'] if m['id'] not in control['removed_member_ids']]
                        if [m['id'] for m in cap['members']]!=expected:raise ValueError('COLLECTION_NEGATIVE_NOT_EXACT_DECLARED_REMOVAL')
                        by_atom={a['atom_id']:a['status'] for a in assessed['atoms']}
                        for atom in control['atom_ids']:
                            if by_atom.get(atom)!='ABSENT':raise ValueError('COLLECTION_TARGET_ATOM_NOT_ABSENT')
                            controls.append({'fixture':str(fixture),'qid':task['qid'],'atom_id':atom,
                                'removed_member_ids':control['removed_member_ids'],'positive_capture':positive[0]['capture'],
                                'negative_capture':delivery['capture'],'positive_sha256':positive[0]['sha256'],'negative_sha256':delivery['sha256']})
            executed.append(entry)
        record={'schema':RECORD_SCHEMA,'evaluator_profile':profile.PROFILE,'base_dir':str(output),
            'fixtures':[str(Path(f).resolve()) for f in fixtures],'controls':controls,'business_controls':business,'execution_tasks':executed,
            'review_policy':'research-source-family/v1','execution_policy':policy.to_dict(),'evidence_mode':'REAL_REPLAY',
            'launch_plan':file_binding(launch.plan_path),'launch_plan_sha256':launch.plan_sha256,'launch_run_id':launch.run_id,
            'capsule_job_id':capsule.job,'collection_plan':launch_plan['coverage_collection'],'real_eligible':True}
        record['execution_verification']=verify_coverage_execution(record,policy)
        result=recompute_coverage(record);record.update(matrix=result['matrix'],status=result['status'],slot_accounting=result['fixed_slot_counts'])
        record['review']={'status':'AI_REVIEWED','object_sha256':digest(record['matrix']),
            'reviewer':'Receipt-bound fixed-history coverage engineering','reviewed_at':datetime.now(timezone.utc).isoformat()}
        atomic_write(output/'coverage.restricted.json',canonical(record),exclusive=True);completed=True
        return record
    finally:
        cleanup=capsule.close();atomic_write(output/'cleanup-outcome.json',canonical(cleanup),exclusive=True)
        if completed and (cleanup.get('cleanup_status')!='REMOVED' or cleanup.get('journal_status')!='PERSISTED'):
            raise ValueError('INFRA_FAILURE: fixed-history coverage cleanup/journal UNKNOWN')


# Exact legacy IR coverage algorithm below; only profile transport and17-slot projection differ.
def _recompute_ir_coverage(record):
    """Recompute the six-category evidence matrix from frozen fixtures/captures.

    Positive families come only from exact, reviewed source support alternatives.
    A negative control must pair a hash-bound complete positive capture with a
    capture that removes required members and consequently fails the same atom.
    Saved status booleans in the record are ignored.
    """
    if isinstance(record, (str, Path)):
        record_path = Path(record).resolve()
        record_data = _load(record_path)
        base = record_path.parent
    elif isinstance(record, dict):
        record_data = record
        base = Path(record_data.get('base_dir', '.')).resolve()
    else:
        raise ValueError('RELATIONS_COVERAGE_RECORD_INVALID')
    if record_data.get('schema') != RECORD_SCHEMA:
        raise ValueError('RELATIONS_COVERAGE_SCHEMA_REQUIRED')
    review_policy = record_data.get('review_policy', 'production-source-family/v1')
    if not isinstance(review_policy, str) or review_policy not in REVIEW_POLICIES:
        raise ValueError('RELATIONS_REVIEW_POLICY_INVALID')
    fixture_paths = record_data.get('fixtures')
    controls = record_data.get('controls')
    if not isinstance(fixture_paths, list) or not fixture_paths or not isinstance(controls, list):
        raise ValueError('RELATIONS_COVERAGE_INPUTS_REQUIRED')

    fixtures = {}
    freezes = {}
    positive = {category: {} for category in CATEGORIES}
    for fixture_value in fixture_paths:
        fixture = _resolve_record_path(fixture_value, base)
        freeze = profile.check_fixture(fixture, review_policy=review_policy)
        freezes[str(fixture)] = freeze
        specs = _load(fixture / 'relations.restricted.json')
        cases = [item for item in _load(fixture / 'cases.json') if item['qid'] in specs]
        fixtures[str(fixture)] = (cases, specs)
        for item in cases:
            qid = item['qid']; spec = specs[qid]
            for atom in spec['atoms']:
                if atom['review']['status'] == 'UNREVIEWED':
                    continue
                # Different hash labels do not establish independent families.
                # Preserve UNKNOWN in fixtures, but never credit it as coverage.
                supporting_sources = [spec['sources'][support['source_id']] for support in atom['supports']]
                if any(source['relation'] == 'UNKNOWN'
                       or source['exposure_status'] != 'KNOWN_HISTORY'
                       or source['review_status'] not in REVIEW_POLICIES[review_policy]
                       for source in supporting_sources):
                    continue
                families = {spec['sources'][support['source_id']]['source_family_id']
                            for support in atom['supports']}
                example = {'fixture':str(fixture), 'qid':qid, 'atom_id':atom['atom_id'],
                           'review_status':atom['review']['status'],
                           'source_family_ids':sorted(families)}
                for category in atom['categories']:
                    positive[category][(str(fixture), qid, atom['atom_id'])] = example

    negative = {category: {} for category in CATEGORIES}
    control_evidence = []
    for control in controls:
        fixture = _resolve_record_path(control.get('fixture', ''), base)
        fixture_key = str(fixture)
        if fixture_key not in fixtures:
            raise ValueError('RELATIONS_CONTROL_FIXTURE_UNREGISTERED')
        cases, specs = fixtures[fixture_key]
        qid, atom_id = control.get('qid'), control.get('atom_id')
        if qid not in specs:
            raise ValueError('RELATIONS_CONTROL_CASE_UNREGISTERED')
        spec = specs[qid]
        atoms = {atom['atom_id']: atom for atom in spec['atoms']}
        atom = atoms.get(atom_id)
        if atom is None or atom['review']['status'] == 'UNREVIEWED':
            raise ValueError('RELATIONS_CONTROL_ATOM_UNREVIEWED')
        case = next(item['case'] for item in cases if item['qid'] == qid)
        sources = {sid:(fixture / source['relative_path']).read_bytes()
                   for sid, source in spec['sources'].items()}
        positive_path = _resolve_record_path(control.get('positive_capture', ''), base)
        negative_path = _resolve_record_path(control.get('negative_capture', ''), base)
        positive_bytes = positive_path.read_bytes()
        negative_bytes = negative_path.read_bytes()
        if (sha(positive_bytes) != control.get('positive_sha256')
                or sha(negative_bytes) != control.get('negative_sha256')):
            raise ValueError('RELATIONS_CONTROL_CAPTURE_CHANGED')
        positive_capture = json.loads(positive_bytes.decode('utf-8'))
        negative_capture = json.loads(negative_bytes.decode('utf-8'))
        positive_result = _assess(case, positive_capture, spec, sources, fixture)
        negative_result = _assess(case, negative_capture, spec, sources, fixture)
        positive_ids = {member.get('id') for member in positive_capture.get('members', [])}
        negative_ids = {member.get('id') for member in negative_capture.get('members', [])}
        removed = control.get('removed_member_ids')
        positive_atom = next((row for row in positive_result['atoms'] if row['atom_id'] == atom_id), {})
        negative_atom = next((row for row in negative_result['atoms'] if row['atom_id'] == atom_id), {})
        if (positive_result['status'] != 'PRESENT' or negative_result['status'] != 'ABSENT'
                or positive_atom.get('status') != 'PRESENT' or negative_atom.get('status') != 'ABSENT'
                or not isinstance(removed, list) or not removed
                or len(set(removed)) != len(removed)
                or not set(removed) <= positive_ids - negative_ids):
            raise ValueError('RELATIONS_NEGATIVE_CONTROL_FAILED')
        family_ids = sorted({spec['sources'][support['source_id']]['source_family_id']
                             for support in atom['supports']})
        for category in atom['categories']:
            if (fixture_key, qid, atom_id) in positive[category]:
                negative[category][(fixture_key, qid, atom_id)] = {
                    'fixture':fixture_key, 'qid':qid, 'atom_id':atom_id,
                    'source_family_ids':family_ids,
                    'positive_capture_sha256':control['positive_sha256'],
                    'negative_capture_sha256':control['negative_sha256'],
                    'removed_member_ids':sorted(removed)}
        control_evidence.append({'fixture':fixture_key,'qid':qid,'atom_id':atom_id,
            'positive_status':positive_result['status'],'negative_status':negative_result['status'],
            'positive_capture_sha256':control['positive_sha256'],
            'negative_capture_sha256':control['negative_sha256']})

    matrix = {}
    for category in sorted(CATEGORIES):
        pos = positive[category]; neg = negative[category]
        positive_families = sorted({family for row in pos.values() for family in row['source_family_ids']})
        negative_families = sorted({family for row in neg.values() for family in row['source_family_ids']})
        missing_negative = sorted(set(positive_families) - set(negative_families))
        missing_examples = sorted([{'fixture':row['fixture'],'qid':row['qid'],'atom_id':row['atom_id']}
            for key,row in pos.items() if key not in neg], key=lambda row: (row['fixture'],row['qid'],row['atom_id']))
        ready = len(pos) >= 2 and len(positive_families) >= 2 and not missing_negative and not missing_examples
        matrix[category] = {'status':'PASS' if ready else 'BLOCKED_DATA',
            'positive_example_count':len(pos), 'positive_source_family_ids':positive_families,
            'negative_control_count':len(neg), 'negative_source_family_ids':negative_families,
            'positive_families_missing_negative_control':missing_negative,
            'positive_examples_missing_negative_control':missing_examples}
    status = 'PASS' if all(row['status'] == 'PASS' for row in matrix.values()) else 'BLOCKED_DATA'
    return {'schema':'rsi-fixed-history-ir-coverage-result/v1','status':status,
        'matrix':matrix,'fixture_freezes':freezes,'verified_negative_controls':control_evidence,
        'review_policy':review_policy,
        'production_qualified':False,
        'claim_scope':'source-interval support and member-removal controls only; no answer-quality claim'}

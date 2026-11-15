"""Predeclared real delivery controls; negative artifacts confer no eligibility."""
from datetime import datetime, timezone
from pathlib import Path
import time

from .events import canonical, digest
from .experiment import executor, ROOT, bytes_hash, load
from .execution_evidence import check_receipt
from .launch import collection_tasks, fixture_identity, file_binding, guard_real
from .locking import atomic_write, restrict_directory
from .query_aware import QueryRegistry, validate_function
from .source_relations import assess, check_fixture, recompute_coverage
from run_rsi_loop import selector


def identity(candidate):
    return {'candidate_id':candidate['candidate_id'],
        'worker_sha256':bytes_hash(candidate['worker']),
        'config_sha256':bytes_hash(candidate['config'])}


def removal_worker(positive_worker, removed):
    """Fixed engineering artifact, changed only in the six-argument selector."""
    _, before = selector(positive_worker.decode())
    validate_function(before)
    after = ("def select_member_indices(members, chunks, files, seed_ids, selection, query):\n"
        "    selected = []\n"
        "    for i, member in enumerate(members):\n"
        f"        if len(query) < 2 or member['id'] in seed_ids or member['id'] not in {tuple(sorted(removed))!r}:\n"
        "            selected.append(i)\n"
        "    return selected\n")
    validate_function(after)
    result = positive_worker.decode().replace(before,after,1).encode()
    compile(result,'<engineering-coverage-removal>','exec')
    return result


def prepare_collection(*, registry, candidate_id, fixtures, output):
    """Offline preparation only: register deterministic negatives and review plan.

    No capsule setup, network, execution receipts or coverage verdict is made.
    """
    registry = QueryRegistry(registry); positive = registry.get(candidate_id)
    registry.require_not_quarantined(candidate_id)
    output = Path(output).resolve(); output.mkdir(parents=True,exist_ok=False)
    restrict_directory(output)
    tasks = []
    for fixture in fixtures:
        fixture = Path(fixture).resolve()
        check_fixture(fixture,review_policy='research-source-family/v1')
        controls = load(fixture/'control-spec.restricted.json')
        specs = load(fixture/'relations.restricted.json')
        for item in load(fixture/'cases.json'):
            case = executor.checked_case(item['case']); qid = item['qid']
            grouped = {}
            for control in controls:
                if control['qid'] != qid: continue
                removed = tuple(sorted(control['removed_member_ids']))
                if not removed or set(removed)&set(case['seed_ids']):
                    raise ValueError('COVERAGE_NEGATIVE_MUST_REMOVE_NONSEED')
                grouped.setdefault(removed,[]).append(control['atom_id'])
            if {a['atom_id'] for a in specs[qid]['atoms']} != {a for atoms in grouped.values() for a in atoms}:
                raise ValueError('COVERAGE_EVERY_ATOM_CONTROL_REQUIRED')
            planned = [{'kind':'POSITIVE','removed_member_ids':[], 'candidate':identity(positive)}]
            for removed, atoms in sorted(grouped.items()):
                worker = removal_worker(positive['worker'],removed)
                cid = registry.put(worker,positive['config'],parent_id=candidate_id,
                    origin='ENGINEERING_COVERAGE_NEGATIVE',evidence={
                        'purpose':'PREDECLARED_MEMBER_REMOVAL_ONLY_NOT_IMPROVEMENT',
                        'fixture':str(fixture),'qid':qid,'removed_member_ids':list(removed),
                        'atom_ids':atoms,'production_qualified':False})
                planned.append({'kind':'MEMBER_REMOVAL','removed_member_ids':list(removed),
                    'atom_ids':atoms,'candidate':identity(registry.get(cid))})
            tasks.append({'fixture_path':str(fixture),'qid':qid,
                'prepare_input_sha256':bytes_hash(canonical(case)),'controls':planned})
    record = {'schema':'rsi-coverage-collection-plan/v1',
        'fixtures':{str(Path(f).resolve()):fixture_identity(f) for f in fixtures},'tasks':tasks}
    record['review'] = {'status':'AI_REVIEWED','object_sha256':digest(record),
        'reviewer':'Codex deterministic six-argument engineering control review',
        'reviewed_at':datetime.now(timezone.utc).isoformat()}
    atomic_write(output/'collection-plan.restricted.json',canonical(record),exclusive=True)
    return record


def collect(*, registry, candidate_id, fixtures, capsule, output, launch, timeout_seconds=7200):
    """Execute approved frozen prepare/positive/removal stages and verify receipts."""
    registry = QueryRegistry(registry); candidate = registry.get(candidate_id)
    if registry.directory.resolve() != Path(launch.registry).resolve():
        raise ValueError('COLLECTION_REGISTRY_MISMATCH')
    policy = capsule.evidence_policy
    if not policy.real_eligible or launch.phase != 'COVERAGE_COLLECTION':
        raise ValueError('REAL_COVERAGE_COLLECTION_ADMISSION_REQUIRED')
    output = Path(output).resolve()
    guard_real(policy,launch,phase='COVERAGE_COLLECTION',candidate=candidate,
        fixtures=fixtures,directory=output,boundary='CAPSULE_EXECUTION')
    plan = load(launch.plan_path); tasks = collection_tasks(plan,launch.registry)
    output.mkdir(parents=True,exist_ok=False); restrict_directory(output)
    atomic_write(output/'collection-input.json',canonical({'launch_plan_sha256':launch.plan_sha256,
        'collection_plan':plan['coverage_collection'],'execution_policy':policy.to_dict()}),exclusive=True)
    deadline = time.monotonic()+timeout_seconds; runtime = None; controls = []
    capsule.launch_context = launch
    completed = False
    try:
        capsule.setup()
        for index, task in enumerate(tasks,1):
            directory = output/f'task-{index:03}'; directory.mkdir()
            fixture = Path(task['fixture_path']); qid = task['qid']
            item = next(i for i in load(fixture/'cases.json') if i['qid']==qid)
            case = executor.checked_case(item['case']); spec = load(fixture/'relations.restricted.json')[qid]
            sources = {sid:(fixture/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
            atomic_write(directory/'input.json',canonical(case),exclusive=True)
            if bytes_hash((directory/'input.json').read_bytes()) != task['prepare_input_sha256']:
                raise ValueError('COLLECTION_PREPARE_IDENTITY_CHANGED')
            capsule.deadline = min(deadline,time.monotonic()+300)
            raw = capsule.child('prepare',[directory/'input.json',directory/'snapshot.json'],directory,
                business_root=capsule.business_root)
            runtime = check_receipt(policy,directory/'execution-receipt.json',stage='prepare',
                worker_hash=bytes_hash((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),
                input_path=directory/'input.json',capture_path=directory/'snapshot.json',runtime_identity=runtime)
            snapshot = executor.execution_view(raw)
            if snapshot != executor.execution_view(load(directory/'snapshot.json')):
                raise ValueError('COLLECTION_PREPARE_CAPTURE_CHANGED')
            atomic_write(directory/'arm-input.json',canonical(snapshot),exclusive=True)
            positive = None; outcomes = []
            # Positive must run first, regardless of supplied task list order.
            ordered = sorted(task['controls'],key=lambda c:c['kind']!='POSITIVE')
            if sum(c['kind']=='POSITIVE' for c in ordered)!=1:
                raise ValueError('ONE_COLLECTION_POSITIVE_REQUIRED')
            for number, control in enumerate(ordered):
                artifact = registry.get(control['candidate']['candidate_id'])
                if identity(artifact)!=control['candidate']:
                    raise ValueError('COLLECTION_ARTIFACT_CHANGED')
                arm = directory/f'control-{number:03}'; arm.mkdir()
                atomic_write(arm/'worker.py',artifact['worker'],exclusive=True)
                atomic_write(arm/'config.json',artifact['config'],exclusive=True)
                capsule.deadline = min(deadline,time.monotonic()+300)
                capture = capsule.child('deliver',[directory/'arm-input.json',arm/'config.json',arm/'capture.json'],
                    arm,worker=arm/'worker.py',business_root=capsule.business_root)
                runtime = check_receipt(policy,arm/'execution-receipt.json',stage='deliver',
                    worker_hash=control['candidate']['worker_sha256'],input_path=directory/'arm-input.json',
                    capture_path=arm/'capture.json',config_path=arm/'config.json',
                    canonical_config_hash=bytes_hash(canonical(load(arm/'config.json'))),runtime_identity=runtime)
                if capture != load(arm/'capture.json') or capture.get('upstream_sha256')!=executor.fingerprint(snapshot):
                    raise ValueError('COLLECTION_CAPTURE_INPUT_MISMATCH')
                assessed = assess(case,capture,spec,sources)
                if control['kind']=='POSITIVE':
                    if assessed['status']!='PRESENT': raise ValueError('COLLECTION_POSITIVE_FACT_FAILED')
                    structural = executor.assess(case,capture,{'facts':[],'forbidden':[]},'A')
                    if structural['faults']: raise ValueError('COLLECTION_POSITIVE_STRUCTURE_FAILED')
                    positive = (arm,capture)
                else:
                    expected = [m['id'] for m in positive[1]['members'] if m['id'] not in control['removed_member_ids']]
                    if [m['id'] for m in capture['members']] != expected:
                        raise ValueError('COLLECTION_NEGATIVE_NOT_EXACT_DECLARED_REMOVAL')
                    by_atom = {a['atom_id']:a['status'] for a in assessed['atoms']}
                    for atom_id in control['atom_ids']:
                        if by_atom.get(atom_id)!='ABSENT': raise ValueError('COLLECTION_TARGET_ATOM_NOT_ABSENT')
                        controls.append({'fixture':str(fixture),'qid':qid,'atom_id':atom_id,
                            'removed_member_ids':control['removed_member_ids'],
                            'positive_capture':str(positive[0]/'capture.json'),
                            'negative_capture':str(arm/'capture.json'),
                            'positive_sha256':bytes_hash((positive[0]/'capture.json').read_bytes()),
                            'negative_sha256':bytes_hash((arm/'capture.json').read_bytes()),
                            'prepare_input':str(directory/'input.json'),'prepare_capture':str(directory/'snapshot.json'),
                            'prepare_receipt':str(directory/'execution-receipt.json'),
                            'arm_input':str(directory/'arm-input.json'),
                            'positive_receipt':str(positive[0]/'execution-receipt.json'),
                            'negative_receipt':str(arm/'execution-receipt.json'),
                            'positive_candidate':ordered[0]['candidate'],'negative_candidate':control['candidate'],
                            'positive_config':str(positive[0]/'config.json'),'negative_config':str(arm/'config.json')})
                outcomes.append({'kind':control['kind'],'assessment':assessed,'candidate':control['candidate']})
            atomic_write(directory/'outcomes.restricted.json',canonical(outcomes),exclusive=True)
        record = {'schema':'rsi-source-relations-coverage/v1','base_dir':str(output),
            'fixtures':[str(Path(f).resolve()) for f in fixtures], 'controls':controls,
            'review_policy':'research-source-family/v1','execution_policy':policy.to_dict(),
            'evidence_mode':'REAL_REPLAY','launch_plan_sha256':launch.plan_sha256,
            'launch_plan':file_binding(launch.plan_path),'launch_run_id':launch.run_id,
            'capsule_job_id':capsule.job,
            'collection_plan':plan['coverage_collection'],'real_eligible':True}
        from .source_relations import verify_coverage_execution
        record['execution_verification'] = verify_coverage_execution(record,policy)
        result = recompute_coverage(record)
        record['matrix'] = result['matrix']; record['status'] = result['status']
        record['review'] = {'status':'AI_REVIEWED','object_sha256':digest(record['matrix']),
            'reviewer':'Codex receipt-bound deterministic coverage collection',
            'reviewed_at':datetime.now(timezone.utc).isoformat()}
        atomic_write(output/'coverage.restricted.json',canonical(record),exclusive=True)
        completed = True
        return record
    finally:
        cleanup = capsule.close()
        atomic_write(output/'cleanup-outcome.json',canonical(cleanup),exclusive=True)
        if completed and (cleanup.get('cleanup_status') != 'REMOVED'
                          or cleanup.get('journal_status') != 'PERSISTED'):
            raise ValueError('INFRA_FAILURE: coverage cleanup or journal UNKNOWN; evidence retained')

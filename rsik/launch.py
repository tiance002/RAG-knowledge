"""Recomputed run-bound admission, separate from advisory preparation booleans.

These are local host attestations, not independent third-party signatures.
Every actual network/execution boundary requires the same pinned context.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from .budgets import Budget, PRICE_VERSION
from .experiment import bytes_hash, load
from .execution_evidence import ExecutionEvidence
from .foundation import verify_boundary
from .state import digest


@dataclass(frozen=True)
class LaunchContext:
    plan_path: str
    plan_sha256: str
    run_id: str
    phase: str
    budget_ledger: str
    registry: str
    business_root: str
    session: object = None
    exploration_training: str | None = None


def file_binding(path):
    path = Path(path).resolve()
    return {'path': str(path), 'sha256': bytes_hash(path.read_bytes())}


def read_binding(record):
    if not isinstance(record, dict) or set(record) != {'path','sha256'}:
        raise ValueError('LAUNCH_INVALID_BINDING')
    data = Path(record['path']).read_bytes()
    if bytes_hash(data) != record['sha256']:
        raise ValueError('LAUNCH_STALE_BINDING')
    return load(record['path'])


def fixture_identity(path):
    path = Path(path).resolve()
    # Full fixture bytes, not the user-editable approved flag or a filename.
    return {p.relative_to(path).as_posix(): bytes_hash(p.read_bytes())
            for p in sorted(path.rglob('*')) if p.is_file()}


def _review(record, object_hash):
    if (not isinstance(record, dict) or record.get('object_sha256',record.get('subject_sha256')) != object_hash
            or record.get('status') not in {'AI_REVIEWED','HUMAN_REVIEWED'}
            or not record.get('reviewer') or not record.get('reviewed_at')):
        raise ValueError('LAUNCH_REVIEW_REQUIRED')
    datetime.fromisoformat(record['reviewed_at'].replace('Z','+00:00'))


def validate_launch(context, *, phase=None, candidate=None, fixtures=None, models=None,
                    evidence=None, directory=None, role_profile=None, boundary=None):
    """No model/capsule effects. Unknown identity stops, never authorizes by flag.

    The expected plan hash comes from the caller's frozen run configuration.
    Runtime budget/frontier may advance only inside that same identified run.
    """
    if not isinstance(context, LaunchContext):
        raise ValueError('TRUSTED_LAUNCH_CONTEXT_REQUIRED')
    path = Path(context.plan_path)
    if bytes_hash(path.read_bytes()) != context.plan_sha256:
        raise ValueError('LAUNCH_PLAN_CHANGED')
    plan = load(path)
    fixed_history = plan.get('evaluator_profile') == 'source-relations-fixed-history/v1'
    if any((load(Path(f)/'manifest.json').get('profile') == 'source-relations-fixed-history/v1') != fixed_history
           for f in plan.get('fixtures', {})):
        raise ValueError('LAUNCH_EVALUATOR_PROFILE_MISMATCH')
    selected_phase = phase or context.phase
    if (plan.get('schema') != 'rsi-launch/v1' or plan.get('run_id') != context.run_id
            or selected_phase != context.phase or selected_phase not in plan.get('allowed_phases',[])):
        raise ValueError('LAUNCH_RUN_OR_PHASE_MISMATCH')
    collecting = selected_phase == 'COVERAGE_COLLECTION'
    if collecting and boundary != 'CAPSULE_EXECUTION':
        raise ValueError('COVERAGE_COLLECTION_CAPSULE_ONLY')
    if boundary == 'MODEL_REQUEST':
        from .http_worker import transport_identity
        actual_transport = transport_identity()
        if (actual_transport['mode'] != 'DIRECT' or 'transport_identity' in plan):
            if plan.get('transport_identity') != actual_transport:
                raise ValueError('LAUNCH_TRANSPORT_IDENTITY_MISMATCH')
    if directory is not None:

        expected=Path(plan.get('run_directory','')).resolve()
        actual=Path(directory).resolve()
        if actual!=expected and not (context.phase=='AUTONOMOUS_SEARCH' and actual.is_relative_to(expected)):
            raise ValueError('LAUNCH_RUN_DIRECTORY_MISMATCH')
    from .query_workflow import implementation_hashes
    if plan.get('implementation') != implementation_hashes() or plan.get('protected') != verify_boundary():
        raise ValueError('LAUNCH_IMPLEMENTATION_CHANGED')
    policy = evidence or ExecutionEvidence.real(context.business_root)
    if not isinstance(policy, ExecutionEvidence) or not policy.real_eligible or plan.get('execution_policy') != policy.to_dict():
        raise ValueError('LAUNCH_EXECUTION_POLICY_MISMATCH')
    registry = Path(context.registry).resolve()
    if plan.get('registry')!=str(registry):raise ValueError('LAUNCH_REGISTRY_CHANGED')
    from .query_aware import QueryRegistry
    planned_candidate=QueryRegistry(registry).get(plan.get('candidate',{}).get('candidate_id',''))
    QueryRegistry(registry).require_not_quarantined(planned_candidate['candidate_id'])
    if ({'candidate_id':planned_candidate['candidate_id'],'worker_sha256':bytes_hash(planned_candidate['worker']),
         'config_sha256':bytes_hash(planned_candidate['config'])}!=plan.get('candidate')):raise ValueError('LAUNCH_CANDIDATE_CHANGED')
    ledger = Path(context.budget_ledger).resolve()
    if ledger != registry.parent.parent/'task-model-budget.sqlite' or not ledger.is_file():
        raise ValueError('ORIGINAL_TASK_BUDGET_LEDGER_REQUIRED')
    if (plan.get('budget',{}).get('path') != str(ledger)
            or plan['budget'].get('price_version') != PRICE_VERSION
            or plan['budget'].get('limit_usd') != '5' or plan['budget'].get('reserve_usd') != '0'):
        raise ValueError('LAUNCH_BUDGET_IDENTITY_MISMATCH')
    budget=Budget(ledger)
    events=budget.log.read()
    anchor=plan['budget'].get('event_prefix')
    if (not isinstance(anchor,dict) or type(anchor.get('sequence')) is not int or anchor['sequence']<0
            or anchor['sequence']>len(events)
            or (events[anchor['sequence']-1]['event_hash'] if anchor['sequence'] else '0'*64)!=anchor.get('event_hash')):
        raise ValueError('LAUNCH_BUDGET_PREFIX_CHANGED')
    snapshot = budget.snapshot()
    if snapshot['pending_request_keys'] or snapshot['unbounded_usage_requests']:
        raise ValueError('LAUNCH_BILLING_UNKNOWN')
    if snapshot['recorded_cost_nano_upper'] >= 5_000_000_000:
        raise ValueError('BUDGET_EXHAUSTED')
    # A review names actual official provider identities/prices and validity;
    # checking membership in a hardcoded historical model table is insufficient.
    price = read_binding(plan['price_review'])
    _review(price.get('review'), digest(price.get('prices')))
    if (price.get('price_version') != PRICE_VERSION or urlparse(price.get('official_source','')).scheme!='https'
            or urlparse(price.get('official_source','')).hostname not in {'openai.com','platform.openai.com','developers.openai.com'}
            or datetime.fromisoformat(price['valid_until'].replace('Z','+00:00')) <= datetime.now(timezone.utc)):
        raise ValueError('LAUNCH_PRICE_REVIEW_STALE')
    if role_profile is not None:
        from .role_profiles import profile_manifest
        if plan.get('role_profile')!=profile_manifest(role_profile):raise ValueError('LAUNCH_ROLE_PROFILE_MISMATCH')
    configured = plan.get('models',{})
    if (not configured or any(any(name in str(v).lower() for name in ('astra','terra')) for v in configured.values())
            or models is not None and configured != models
            or not set(configured.values()) <= set(price.get('prices',{}))):
        raise ValueError('LAUNCH_MODEL_IDENTITY_MISMATCH')
    if fixtures is not None:
        actual = {str(Path(f).resolve()):fixture_identity(f) for f in fixtures}
        if actual != plan.get('fixtures'):
            raise ValueError('LAUNCH_FIXTURE_CHANGED')
    for fixture, expected in plan.get('fixtures',{}).items():
        from .baseline_qualification import adapters
        adapters(fixture)[0](fixture,expected_count=len(load(Path(fixture)/'cases.json')))
        if fixture_identity(fixture) != expected:
            raise ValueError('LAUNCH_FIXTURE_CHANGED')
    if not plan.get('fixtures') or not plan.get('evaluator_profile') or not plan.get('seam_profile'):
        raise ValueError('LAUNCH_DATA_PROFILE_REQUIRED')
    families = read_binding(plan['source_families'])
    exposure = read_binding(plan['exposure_snapshot'])
    coverage = None if collecting else read_binding(plan['coverage'])
    if not collecting:_review(coverage.get('review'), digest(coverage.get('matrix')))
    if (not families or not exposure):
        raise ValueError('LAUNCH_DATA_READINESS_REQUIRED')
    # Rebuild coverage and source exposure through their trusted adapters.
    if fixed_history:
        from .fixed_history_coverage import recompute_coverage, verify_coverage_execution
        if not collecting and coverage.get('schema') != 'rsi-fixed-history-coverage/v1':
            raise ValueError('FIXED_HISTORY_NEW_COVERAGE_REQUIRED')
    else:
        from .source_relations import recompute_coverage, verify_coverage_execution
    from .exposure import validate_snapshot
    actual_coverage=None if collecting else recompute_coverage(coverage)
    if not collecting:
        verify_coverage_execution(coverage, policy)
    checked_exposure=validate_snapshot(exposure,require_final=selected_phase=='FINAL_VERIFICATION')
    if families!=checked_exposure.get('sources'):raise ValueError('LAUNCH_SOURCE_FAMILIES_CHANGED')
    bind_fixture_sources(plan, checked_exposure)
    if not collecting and (actual_coverage.get('status')!='PASS' or actual_coverage.get('matrix')!=coverage.get('matrix')):
        raise ValueError('LAUNCH_COVERAGE_NOT_REPRODUCIBLE')
    if collecting:
        collection_tasks(plan, registry)
    if selected_phase=='BASELINE_QUALIFICATION' and planned_candidate['record'].get('origin') in {'OPENAI_MODEL_PROPOSAL','RSI_MODEL_PROPOSAL'}:
        raise ValueError('ORDINARY_CHILD_CANNOT_SELF_QUALIFY')
    if selected_phase not in {'BASELINE_QUALIFICATION','COVERAGE_COLLECTION'}:
        qualification = read_binding(plan['qualification'])
        if qualification.get('status') == 'BASELINE_QUALIFIED':
            from .baseline_qualification import verify_baseline_qualification
            qualified_candidate,_=verify_baseline_qualification(registry=registry, receipt=plan['qualification']['path'],
                fixtures=qualification.get('fixture_freezes',{}).keys(), evidence=policy)
            if qualified_candidate['candidate_id']!=plan['candidate']['candidate_id']:raise ValueError('LAUNCH_PARENT_QUALIFICATION_MISMATCH')
        elif qualification.get('verdict') == 'VERIFIED':
            from .query_learning import VerifiedCandidateEvidence
            ctx = read_binding(plan['verification_context'])
            qualified=VerifiedCandidateEvidence.from_archives(registry=registry,training=ctx['training'],
                train_fixtures=ctx['train_fixtures'],verification=Path(plan['qualification']['path']).parent,evidence=policy)
            if qualified.candidate['candidate_id']!=plan['candidate']['candidate_id']:raise ValueError('LAUNCH_PARENT_QUALIFICATION_MISMATCH')
        else:
            raise ValueError('LAUNCH_PARENT_QUALIFICATION_REQUIRED')
    if candidate is not None:
        actual = {'candidate_id':candidate['candidate_id'],'worker_sha256':bytes_hash(candidate['worker']),
                  'config_sha256':bytes_hash(candidate['config'])}
        expected_candidate=plan.get('candidate')
        if selected_phase=='FINAL_VERIFICATION':
            nominee=plan.get('nomination')
            if not isinstance(nominee,dict) or not nominee.get('training'):raise ValueError('LAUNCH_FINAL_NOMINATION_REQUIRED')
            expected_candidate=nominee.get('candidate')
            nomination_training=read_binding(nominee['training'])
            if nomination_training.get('adopted_candidate_id')!=actual['candidate_id']:raise ValueError('LAUNCH_FINAL_NOMINATION_MISMATCH')
            from .query_verification import verify_training
            verify_training(registry,Path(nominee['training']['path']).parent,list(nomination_training['fixture_freezes']),actual['candidate_id'],evidence=policy)
        if actual != expected_candidate:
            if selected_phase=='FINAL_VERIFICATION':raise ValueError('LAUNCH_FINAL_NOMINATION_MISMATCH')
            if not context.exploration_training:raise ValueError('LAUNCH_CANDIDATE_CHANGED')
            from .query_verification import verify_training
            archived=load(Path(context.exploration_training)/'round.json')
            coordinator=load(Path(archived['coordinator_directory'])/'round.json')
            if coordinator.get('launch_run_id')!=context.run_id or coordinator.get('launch_plan_sha256')!=context.plan_sha256:
                raise ValueError('LAUNCH_EXPLORATION_RUN_MISMATCH')
            candidate,_=verify_training(registry,context.exploration_training,list(plan['fixtures']),actual['candidate_id'],evidence=policy)
            seen=set();ancestor=candidate
            while ancestor['candidate_id']!=plan['candidate']['candidate_id']:
                if ancestor['candidate_id'] in seen:raise ValueError('LAUNCH_LINEAGE_CYCLE')
                seen.add(ancestor['candidate_id'])
                ancestor=QueryRegistry(registry).get(ancestor['record']['parent_id'])
            if not plan['experiment'].get('allow_train_only_parents',False):raise ValueError('LAUNCH_EXPLORATION_NOT_REGISTERED')
    memory = read_binding(plan['initial_memory'])
    if not memory or context.session is None:
        raise ValueError('LAUNCH_MEMORY_SESSION_REQUIRED')
    if Path(context.session.initial).resolve() != Path(plan['initial_memory']['path']).resolve():
        raise ValueError('LAUNCH_MEMORY_SESSION_MISMATCH')
    context.session.verify()
    experiment = plan.get('experiment',{})
    if (not experiment.get('epoch_id') or not experiment.get('group')
            or experiment.get('repetition') not in (1,2,3)
            or experiment.get('final_nomination_limit') != 1
            or experiment.get('final_holdout_uses') != 1
            or not experiment.get('analysis_points') or not experiment.get('stop_rules')):
        raise ValueError('LAUNCH_EXPERIMENT_POLICY_REQUIRED')
    return {'schema':'rsi-launch-validation/v1','run_id':context.run_id,'phase':selected_phase,
            'plan_sha256':context.plan_sha256,'status':'PASS','production_changed':False}


def bind_fixture_sources(plan, snapshot):
    """Join relational fixture assertions to the live-verified exposure identities."""
    fields=('source_id','content_sha256','document_id','version','source_family_id',
            'relation','related_source_ids','exposure_status','review_status')
    relational=[]
    for fixture in plan.get('fixtures',{}):
        manifest=Path(fixture)/'manifest.json'
        if manifest.is_file() and load(manifest).get('profile') in {
                'source-relations/v1','source-relations-fixed-history/v1'}:
            relational.append(Path(fixture))
    if not relational:return
    rows=snapshot.get('sources')
    if not isinstance(rows,list) or any(not isinstance(row,dict) or not row.get('source_id') for row in rows):
        raise ValueError('LAUNCH_FIXTURE_EXPOSURE_MISMATCH')
    sources={row['source_id']:row for row in rows}
    if len(sources)!=len(rows):raise ValueError('LAUNCH_FIXTURE_EXPOSURE_MISMATCH')
    for fixture in relational:
        for spec in load(fixture/'relations.restricted.json').values():
            for sid,source in spec['sources'].items():
                expected={key:(sid if key=='source_id' else source.get(key)) for key in fields}
                if ('source_id' in source and source['source_id']!=sid
                        or sid not in sources or any(value is None for value in expected.values())
                        or expected!={key:sources[sid].get(key) for key in fields}):
                    raise ValueError('LAUNCH_FIXTURE_EXPOSURE_MISMATCH')


def collection_tasks(plan, registry):
    """Validate reviewed intentions, never substitute intentions for captures."""
    if plan.get('evaluator_profile') == 'source-relations-fixed-history/v1':
        from .fixed_history_coverage import collection_tasks as fixed_tasks
        return fixed_tasks(plan, registry)
    collection = read_binding(plan['coverage_collection'])
    subject = {k:v for k,v in collection.items() if k != 'review'}
    _review(collection.get('review'), digest(subject))
    if (collection.get('schema') != 'rsi-coverage-collection-plan/v1'
            or collection.get('fixtures') != plan.get('fixtures')
            or not isinstance(collection.get('tasks'), list) or not collection['tasks']):
        raise ValueError('LAUNCH_COLLECTION_PLAN_REQUIRED')
    from .experiment import executor
    from .query_aware import QueryRegistry
    from .state import canonical
    tasks = {}
    for fixture in plan['fixtures']:
        for item in load(Path(fixture)/'cases.json'):
            case = executor.checked_case(item['case'])
            tasks[(str(Path(fixture).resolve()), item['qid'])] = (case, bytes_hash(canonical(case)))
    seen = set()
    for task in collection['tasks']:
        key = (task.get('fixture_path'), task.get('qid'))
        if key not in tasks or key in seen or task.get('prepare_input_sha256') != tasks[key][1]:
            raise ValueError('LAUNCH_COLLECTION_TASK_MISMATCH')
        seen.add(key)
        member_ids = {c['id'] for c in tasks[key][0]['chunks']}
        controls = task.get('controls')
        if not isinstance(controls,list) or len(controls)<2:
            raise ValueError('LAUNCH_COLLECTION_CONTROLS_REQUIRED')
        kinds=set(); signatures=set()
        for control in controls:
            kind=control.get('kind');removed=control.get('removed_member_ids')
            if (kind not in {'POSITIVE','MEMBER_REMOVAL'} or not isinstance(removed,list)
                    or len(set(removed))!=len(removed) or not set(removed)<=member_ids
                    or (kind=='POSITIVE') != (not removed)):
                raise ValueError('LAUNCH_COLLECTION_CONTROLS_REQUIRED')
            candidate=QueryRegistry(registry).get(control.get('candidate',{}).get('candidate_id',''))
            QueryRegistry(registry).require_not_quarantined(candidate['candidate_id'])
            identity={'candidate_id':candidate['candidate_id'],'worker_sha256':bytes_hash(candidate['worker']),
                      'config_sha256':bytes_hash(candidate['config'])}
            if (identity!=control.get('candidate') or kind=='POSITIVE' and identity!=plan['candidate']):
                raise ValueError('LAUNCH_COLLECTION_CANDIDATE_MISMATCH')
            signature=(identity['worker_sha256'],identity['config_sha256'])
            if signature in signatures:raise ValueError('LAUNCH_COLLECTION_CONTROL_DUPLICATE')
            signatures.add(signature);kinds.add(kind)
        if kinds!={'POSITIVE','MEMBER_REMOVAL'}:raise ValueError('LAUNCH_COLLECTION_CONTROLS_REQUIRED')
    if seen!=set(tasks):raise ValueError('LAUNCH_COLLECTION_TASK_MISMATCH')
    return collection['tasks']


def guard_real(evidence, launch, **expected):
    if not isinstance(evidence, ExecutionEvidence):
        raise ValueError('TRUSTED_EXECUTION_POLICY_REQUIRED')
    if evidence.real_eligible:
        return validate_launch(launch, evidence=evidence, **expected)
    return {'status':'SYNTHETIC_CONTROL','real_eligible':False}

"""Frozen autonomous target-policy search; engineers do not choose hypotheses.

This controller never modifies RSI system code or admits production changes.
Diagnosis and patches come from isolated OpenAI roles; verdicts are recomputed.
"""
import ast,json,uuid
from dataclasses import replace
from pathlib import Path
from .autonomy_contract import validate_history,remember,project_search_memory,validate_memory_archive
from .budgets import Budget
from .capsule import Capsule
from .errors import StageFailure
from .events import EventLog
from .experiment import bytes_hash,load
from .locking import atomic_write,WorkspaceLock,restrict_directory
from .openai_roles import OpenAIRoles
from .query_aware import autonomy_interface_contract as interface_contract,QueryRegistry
from .query_preview import frames
from .query_verification import verify_training
from .redact import LeakScanner,make_visible,project_summary
from .state import canonical,digest
from .test_phase import assert_frozen
from run_rsi_loop import selector


def proposer_model(history):
    """Escalate coding failures only; no escalation merely for absent gains."""
    return 'gpt-6.2-sol' if len(history)>=2 and all(h['outcome'] in {'PATCH_INVALID','UNIT_FAILED'} for h in history[-2:]) else 'gpt-6-luna'


def history_item(proposal,outcome,reasons=(),counts=None,*,quality_verdict=None,execution_status=None,failure_origin=None):
    # Outcome is a search label; it must never erase the deterministic verdict.
    quality=quality_verdict or (outcome if outcome in {'OBSERVED','INCONCLUSIVE','REJECTED','VALIDATED'} else 'NOT_EVALUATED')
    item={'feedback_schema':'rsi-search-feedback/v2',
        'hypothesis':(proposal or {}).get('hypothesis','No candidate hypothesis was returned.')[:512],
        'change':(proposal or {}).get('change','No candidate change was returned.')[:512],
        'outcome':outcome,'quality_verdict':quality,
        'execution_status':execution_status or ('COMPLETED' if quality in {'OBSERVED','REJECTED','VALIDATED'} else 'NOT_STARTED'),
        'failure_origin':failure_origin or ('CANDIDATE' if outcome in {'PATCH_INVALID','UNIT_FAILED'} else 'UNKNOWN' if quality=='INCONCLUSIVE' else 'NONE'),
        'reason_codes':list(dict.fromkeys(reasons))[:8],'counts':counts or {}}
    validate_history([item]);return item


def search_feedback(decision,*,qualified,round_result):
    """Only whitelisted aggregates and independently identified failure origins."""
    reasons=decision.get('reason_codes',[]) or [round_result.get('stop_reason') or 'ROUND_RESULT_UNKNOWN']
    quality=decision.get('verdict','NOT_EVALUATED')
    if quality not in {'OBSERVED','INCONCLUSIVE','REJECTED','VALIDATED'}:quality='NOT_EVALUATED'
    counts={}
    allowed={'favorable','unfavorable','ties','fact_loss_cases','failed_suites','primary_questions','auxiliary_cases'}
    for source in (decision.get('group_counts') or decision.get('question_counts') or {},decision.get('feedback_counts',{})):
        counts.update({k:v for k,v in source.items() if k in allowed and type(v)is int and v>=0})
    for key in ('delta_total','reduction_rate'):
        if decision.get(key) is not None:counts[key]=decision[key]
    rows=decision.get('per_question',[])
    if rows:
        counts.setdefault('fact_loss_cases',sum(any(v=='ABSENT' for v in row.get('candidate_facts',{}).values()) for row in rows))
        counts.setdefault('primary_questions',len(rows))
    global_stop=round_result.get('execution_state')=='FAILED' and not decision
    if global_stop:
        return 'STOPPED',quality,'STOPPED','GLOBAL_STOP',reasons,counts
    if not qualified:
        # A slot that never reached static review is not proven candidate failure.
        static=quality=='REJECTED' or any(code.startswith(('STATIC_','AST_')) for code in reasons)
        return ('PATCH_INVALID' if static else 'NOT_EVALUATED'),quality,'NOT_STARTED',('CONTRACT' if static else 'UNKNOWN'),reasons,counts
    if 'QUERY_UNIT_FAILED' in reasons:
        return 'UNIT_FAILED',quality,'FAILED','CANDIDATE',reasons,counts
    if quality=='INCONCLUSIVE':
        origin='PARENT' if 'PARENT_FAULT' in reasons else 'INFRASTRUCTURE' if any(code in {'INFRA_FAILURE','PARTIAL_REPLAY','QUERY_REPLAY_FAILURE','EXECUTION_EVIDENCE_MISSING','EXECUTION_EVIDENCE_INVALID'} for code in reasons) else 'UNKNOWN'
        status='PARTIAL' if 'PARTIAL_REPLAY' in reasons else 'FAILED' if origin=='INFRASTRUCTURE' else 'UNKNOWN'
        return 'INCONCLUSIVE',quality,status,origin,reasons,counts
    if quality=='OBSERVED':return 'OBSERVED',quality,'COMPLETED','NONE',reasons,counts
    if quality=='VALIDATED':return 'VALIDATED',quality,'COMPLETED','NONE',reasons,counts
    if quality=='REJECTED':
        no_gain=bool(set(reasons)&{'NO_GAIN','NO_NET_GAIN'}) and not counts.get('fact_loss_cases') and not set(reasons)&{'FACT_LOSS','EXEC_FAULT','LOCAL_REGRESSION'}
        return ('NO_GAIN' if no_gain else 'REJECTED'),quality,'COMPLETED',('NONE' if no_gain else 'CANDIDATE'),reasons,counts
    return 'NOT_EVALUATED',quality,'NOT_STARTED','UNKNOWN',reasons,counts


def stop_guard(directory,freeze,freeze_sha256):
    # Invalidation takes precedence when both requests exist: it is not an owner stop.
    if (directory/'invalidation-request.json').exists():
        raise StageFailure('CONTRACT_ERROR','TEST_INVALIDATED_BY_ENGINEER',global_stop=True)
    if (directory/'stop-request.json').exists():
        raise StageFailure('CANCELLED','OWNER_REQUESTED_STOP',global_stop=True)
    try:assert_frozen(freeze,freeze_sha256)
    except ValueError:raise StageFailure('CONTRACT_ERROR','FROZEN_SYSTEM_CHANGED',global_stop=True) from None


def patch_identity(worker,patch):
    """Hash normalized candidate syntax without executing untrusted code."""
    try:
        old=selector(worker.decode())[1]
        before,after=(patch[k].replace('\r\n','\n') for k in ('old','new'))
        old=old.replace('\r\n','\n')
        if old.count(before)!=1:raise ValueError('PATCH_NOT_UNIQUE')
        tree=ast.parse(old.replace(before,after,1))
        return digest({'selector_ast':ast.dump(tree,include_attributes=False)})
    except (ValueError,SyntaxError,KeyError,TypeError):
        return digest({'invalid_patch':patch})


def static_patch_reason(worker,patch,error):
    """Only enum codes cross the role boundary; original patch stays local."""
    if isinstance(error,SyntaxError):return 'STATIC_SYNTAX_INVALID'
    direct={'ONE_QUERY_PATCH_REQUIRED':'STATIC_PATCH_SHAPE','QUERY_PATCH_NOT_UNIQUE':'STATIC_PATCH_NOT_UNIQUE',
        'ONE_SELECTOR_REQUIRED':'STATIC_ONE_SELECTOR_REQUIRED','QUERY_SEAM_SIGNATURE_CHANGED':'STATIC_SIGNATURE_CHANGED',
        'Selector references a global name':'STATIC_GLOBAL_NAME','Selector may not write to objects':'STATIC_OBJECT_WRITE',
        'Unsupported selector attribute':'STATIC_ATTRIBUTE_FORBIDDEN','Indirect calls are prohibited':'STATIC_INDIRECT_CALL',
        'Append requires a private list':'STATIC_APPEND_REQUIRES_PRIVATE_LIST'}
    if str(error) in direct:return direct[str(error)]
    try:
        original=selector(worker.decode())[1].replace('\r\n','\n')
        source=original.replace(patch['old'].replace('\r\n','\n'),patch['new'].replace('\r\n','\n'),1)
        tree=ast.parse(source)
        forbidden={ast.Mod:'STATIC_OPERATOR_MOD_FORBIDDEN',ast.FloorDiv:'STATIC_OPERATOR_FLOORDIV_FORBIDDEN',
                   ast.Div:'STATIC_OPERATOR_DIV_FORBIDDEN',ast.Mult:'STATIC_OPERATOR_MULT_FORBIDDEN',
                   ast.AugAssign:'STATIC_AUGASSIGN_FORBIDDEN',ast.While:'STATIC_WHILE_FORBIDDEN',
                   ast.Lambda:'STATIC_LAMBDA_FORBIDDEN',ast.keyword:'STATIC_KEYWORD_ARGUMENT_FORBIDDEN'}
        for node in ast.walk(tree):
            if type(node) in forbidden:return forbidden[type(node)]
    except (ValueError,SyntaxError,KeyError,TypeError):pass
    return 'STATIC_POLICY_GUARD_REJECTED'


def remember_round(memory,history,result,worker,round_number, **memory_scope):
    """One factual memory entry per proposal instance, even identical policies."""
    items=[]
    decisions={d['angle']:d for d in result.get('candidate_decisions',[])}
    for slot,proposal in result.get('proposal_instances',{}).items():
        decision=decisions.get(slot,{})
        outcome,quality,execution,origin,reasons,counts=search_feedback(decision,
            qualified=slot in result.get('qualified',[]),round_result=result)
        item=history_item(proposal,outcome,reasons,counts,quality_verdict=quality,
            execution_status=execution,failure_origin=origin)
        key=patch_identity(worker,proposal['worker_patch'])
        entry=remember(memory,key,round_number,item, **memory_scope)
        history.append(item);items.append({'slot':slot,**entry})
    if not items:
        from .response_audit import NON_STRATEGY_OUTPUT_FAILURES
        reasons = set(result.get('agent_call_results', {}).values()) | {result.get('stop_reason')}
        if reasons & NON_STRATEGY_OUTPUT_FAILURES:
            return items
        stopped=result.get('execution_state')=='FAILED' or result.get('stop_reason') in {'OWNER_REQUESTED_STOP','TEST_INVALIDATED_BY_ENGINEER','FROZEN_SYSTEM_CHANGED'}
        item=history_item(None,'STOPPED' if stopped else 'ABSTAIN',[result.get('stop_reason') or 'NO_PROPOSAL_RETURNED'],
            execution_status='STOPPED' if stopped else 'NOT_STARTED',failure_origin='GLOBAL_STOP' if stopped else 'NONE')
        entry=remember(memory,digest({'abstain_round':round_number}),round_number,item, **memory_scope)
        history.append(item);items.append({'slot':'NONE',**entry})
    return items


def parent_archive_files(directory):
    directory = Path(directory).absolute()
    def unsafe(path):
        # Reparse points include Windows junctions; works on Python 3.11 too.
        return path.is_symlink() or bool(getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
    if directory.resolve() != directory or unsafe(directory):
        raise ValueError('AUTONOMY_POOL_ARCHIVE_PATH_UNSAFE')
    paths = []; pending = list(directory.iterdir())
    while pending:
        path = pending.pop()
        if unsafe(path) or not path.resolve().is_relative_to(directory):
            raise ValueError('AUTONOMY_POOL_ARCHIVE_PATH_UNSAFE')
        if path.is_dir():
            pending.extend(path.iterdir())
        elif path.is_file():
            paths.append(path)
    return sorted(paths)


def parent_frame_identity(directory, frame):
    """Bind a replay frame to its own immutable ordinary archive, without copying it."""
    directory = Path(directory).resolve()
    return {'ordinary_round': str(directory), 'arm': 'B', 'candidate_id': frame['parent_id'],
            'worker_sha256': bytes_hash(frame['worker']), 'config_sha256': bytes_hash(frame['config']),
            'entries_sha256': digest(frame['entries']), 'suite_decisions_sha256': digest(frame['suite_decisions']),
            'evidence_files': {p.relative_to(directory).as_posix(): bytes_hash(p.read_bytes())
                               for p in parent_archive_files(directory)}}


def checked_parent_frame(registry, directory, fixtures, candidate_id):
    parent_archive_files(directory)
    registry.require_not_quarantined(candidate_id)
    candidate = registry.get(candidate_id)
    frame = frames(directory, fixtures, 'B')
    if (frame['parent_id'] != candidate_id or frame['worker'] != candidate['worker']
            or frame['config'] != candidate['config']):
        raise ValueError('AUTONOMY_POOL_PARENT_ARTIFACT_CHANGED')
    return frame, candidate


def run(config, *, launch=None):
    from .launch import validate_launch
    validate_launch(launch,phase='AUTONOMOUS_SEARCH',fixtures=config['fixtures'],directory=config.get('directory'))
    from .launch import read_binding, read_frozen_launch_plan
    plan = read_frozen_launch_plan(launch) if getattr(launch, 'plan_path', None) else {}
    objective_transition = plan.get('objective_transition')
    from .query_parent_pool import validate_parent_selection, measure_parent, select_parent
    parent_spec = plan.get('experiment', {}).get('parent_selection')
    objective = None
    memory_scope = {}
    if objective_transition is not None:
        from .objectives import validate_context_selection_objective, validate_transition
        change = validate_transition(objective_transition)
        memory_scope['objective_profile'] = change['current_objective_profile']
        validate_context_selection_objective(memory_scope['objective_profile'])
        objective = memory_scope['objective_profile']
    parent_spec = validate_parent_selection(parent_spec, objective)
    if 'parent_selection' in config:
        raise ValueError('PARENT_SELECTION_RUNTIME_OVERRIDE_FORBIDDEN')
    if parent_spec is not None and not plan['experiment'].get('allow_train_only_parents', False):
        raise ValueError('LAUNCH_EXPLORATION_NOT_REGISTERED')
    directory=Path(config['directory']);registry=Path(config['query_registry']);fixtures=[Path(p) for p in config['fixtures']]
    budget=Budget(config['budget_ledger'])
    if 'search_start_nano' in config:
        from .campaign import SearchBudget
        if (plan['experiment'].get('nominal_usd')!=config.get('search_nominal_usd','0.20')
                or plan['experiment'].get('search_start_nano')!=config['search_start_nano']):raise ValueError('SEARCH_BUDGET_CONFIGURATION_CHANGED')
        budget=SearchBudget(config['budget_ledger'],start_nano=config['search_start_nano'],nominal_usd=config.get('search_nominal_usd','0.20'))
    log=EventLog(directory/'events.sqlite')
    baseline_round=Path(config['baseline_round']);arm='B';history=[];round_number=0
    memory={'schema':'rsi-autonomy-memory/v1','rounds':0,'evicted_entries':0,'entries':[]}
    prior_proposal_rows = []
    if plan.get('previous_search_memory') is not None:
        from copy import deepcopy
        memory = deepcopy(validate_memory_archive(read_binding(plan['previous_search_memory'])))
    initial_memory_rounds = memory['rounds']
    if memory_scope:
        from .launch import fixture_identity
        from .experiment import executor
        memory_scope.update(component='context_selection', compatibility_fingerprint=digest({
            'component': 'context_selection', 'interface_contract': interface_contract(),
            'evaluator': executor.EVALUATOR, 'fixed': executor.FIXED,
            'fixtures': [fixture_identity(fixture) for fixture in fixtures]}))
    profile=config.get('profile','parallel')
    role_profile=config.get('role_profile','query-k3-dependency/v1')
    strategy = {}
    if config.get('strategy_config'):
        path=Path(config['strategy_config'])
        if bytes_hash(path.read_bytes())!=config['strategy_config_sha256']:raise ValueError('STRATEGY_CONFIGURATION_CHANGED')
        strategy=load(path)
        if (strategy['profile']!=profile or strategy['role_profile']!=role_profile
                or strategy['handbook_format']!=config.get('handbook_format','OTHER')
                or strategy['handbook_features']!=config.get('handbook_features',{})):
            raise ValueError('STRATEGY_CONFIGURATION_CHANGED')
    if parent_spec is not None and strategy.get('parent_selection') != parent_spec:
        raise ValueError('PARENT_SELECTION_STRATEGY_ECHO_REQUIRED')
    if parent_spec is None and strategy.get('parent_selection') is not None:
        raise ValueError('PARENT_SELECTION_STRATEGY_CHANGED')
    if profile not in {'single','parallel'}:raise ValueError('INVALID_AUTONOMY_PROFILE')
    from .adjudicate import SLOTS
    enabled_slots=('P2_BUDGET',) if profile=='single' else SLOTS
    from .query_workflow import run_query_round
    from .handbook.store import Handbook
    handbook=Handbook(config['handbook']) if config.get('handbook') else None
    from .query_learning import ExperienceSession
    experience_session=ExperienceSession(directory/'learning-session',handbook) if handbook else None
    state={'schema':'rsi-autonomous-test/v1','phase':'AUTONOMOUS_TEST','execution_state':'RUNNING',
        'freeze_sha256':config['freeze_sha256'],'initial_parent_id':config['baseline_id'],'current_parent_id':config['baseline_id'],
        'current_parent_round':str(baseline_round),'rounds':0,'candidate_author':'RSI_OPENAI_MODEL',
        'monitor_supplies_strategy':False,'system_self_modification':False,'production_changed':False,'profile':profile}
    parent_pool = []
    selected_ids = []
    selection_phase = None
    if parent_spec is not None:
        selection_phase = {'schema': 'rsi-parent-selection-phase/v1', 'config': parent_spec,
            'config_sha256': digest(parent_spec), 'objective': objective, 'objective_sha256': digest(objective),
            'launch_run_id': launch.run_id, 'launch_plan_sha256': launch.plan_sha256,
            'root_receipt_path':str(Path(config['baseline_verification']).resolve()),
            'root_receipt_sha256':bytes_hash(Path(config['baseline_verification']).read_bytes())}
        atomic_write(directory/'parent-selection-phase.json', canonical(selection_phase), exclusive=True)
        state['parent_selection_phase_sha256'] = digest(selection_phase)
    def save():
        state['budget']=budget.snapshot();atomic_write(directory/'state.json',canonical(state))
    def guard():
        stop_guard(directory,config['freeze'],config['freeze_sha256'])
        if experience_session:experience_session.verify()
    def finish(reason,execution='COMPLETED'):
        state.update(execution_state=execution,stop_reason=reason);save()
        experiment_id = getattr(launch, 'run_id', None)
        log.append('finished','AUTONOMY_TEST_ENDED',
                   {**state, 'experiment_id': experiment_id} if experiment_id else state)
    with WorkspaceLock(registry/'autonomy.lock','frozen-autonomy-test'), WorkspaceLock(registry/'writer.lock','frozen-autonomy-query-writer'):
        try:guard()
        except StageFailure as error:
            finish(error.reason_code,'COMPLETED' if error.reason_code=='OWNER_REQUESTED_STOP' else 'FAILED')
            if error.reason_code=='OWNER_REQUESTED_STOP':return state
            raise
        if load(config['baseline_verification']).get('status')=='BASELINE_QUALIFIED':
            from .baseline_qualification import verify_baseline_qualification
            from .execution_evidence import ExecutionEvidence
            verify_baseline_qualification(registry=registry,receipt=config['baseline_verification'],fixtures=fixtures,
                evidence=ExecutionEvidence.real(config['business_root']))
        else:verify_training(registry,baseline_round,fixtures,config['baseline_id'])
        seed_receipt=Path(config['baseline_verification'])
        verified=load(seed_receipt)
        if (verified.get('verdict')!='VERIFIED' and verified.get('status')!='BASELINE_QUALIFIED') or verified.get('candidate_id')!=config['baseline_id']:
            raise ValueError('VERIFIED_INITIAL_PARENT_REQUIRED')
        sealed_log=EventLog(registry/'sealed.sqlite')
        if verified.get('status')!='BASELINE_QUALIFIED' and not any(e['event_type']=='QUERY_SEALED_BUNDLE_FINISHED' and e['payload'].get('candidate_id')==config['baseline_id']
                   and e['payload'].get('verdict')=='VERIFIED' and e['payload'].get('receipt_sha256')==bytes_hash(seed_receipt.read_bytes()) for e in sealed_log.read()):
            raise ValueError('UNBOUND_INITIAL_VERIFICATION')
        if parent_spec is not None:
            root_frame, root_candidate = checked_parent_frame(QueryRegistry(registry), baseline_round, fixtures, config['baseline_id'])
            validate_launch(replace(launch, exploration_training=None), phase='AUTONOMOUS_SEARCH',
                            candidate=root_candidate, fixtures=fixtures)
            parent_pool.append({'candidate_id': config['baseline_id'], 'kind': 'QUALIFIED_ROOT',
                'admission_round': 0, **measure_parent(root_frame['suite_decisions'], arm='B'),
                'frame': parent_frame_identity(baseline_round, root_frame)})
        log.append('started','AUTONOMY_TEST_STARTED',state);save()
        try:
            while True:
                guard();money=budget.snapshot()
                if money['unbounded_usage_requests'] or money['pending_request_keys']:
                    finish('BILLING_OR_REQUEST_STATUS_UNKNOWN','FAILED');return state
                if money['recorded_cost_nano_upper']>=budget.limit_nano or (hasattr(budget,'search_start_nano') and money['recorded_cost_nano_upper']-budget.search_start_nano>=budget.search_allowance_nano):
                    finish('BUDGET_EXHAUSTED');return state
                round_number+=1;trial=directory/f'round-{round_number:06}';trial.mkdir();restrict_directory(trial)
                state.update(rounds=round_number,stage='DIAGNOSIS',current_round=str(trial));save()
                selection_binding = {}
                if parent_spec is not None:
                    available = {}; population = []
                    for admitted in parent_pool:
                        record = {k: v for k, v in admitted.items() if k != 'frame'}
                        try:
                            selected_dir = Path(admitted['frame']['ordinary_round'])
                            restored, candidate = checked_parent_frame(QueryRegistry(registry), selected_dir, fixtures, admitted['candidate_id'])
                            if parent_frame_identity(selected_dir, restored) != admitted['frame']:
                                raise ValueError('AUTONOMY_POOL_EVIDENCE_CHANGED')
                            candidate_launch = replace(launch, exploration_training=(str(selected_dir) if admitted['kind']=='TRAINING_EXPLORATION' else None))
                            validate_launch(candidate_launch, phase='AUTONOMOUS_SEARCH', candidate=candidate, fixtures=fixtures)
                            available[admitted['candidate_id']] = (candidate_launch, selected_dir, admitted['frame'])
                            del restored
                        except (ValueError, OSError, RuntimeError) as error:
                            record['excluded_reason'] = str(error) if str(error).isupper() and len(str(error))<100 else type(error).__name__
                        population.append(record)
                    selection_error = None
                    try:
                        selection = select_parent(population, parent_spec, objective, previous_selected_ids=selected_ids)
                    except ValueError as error:
                        selection_error = error
                        selection = {'selected_candidate_id':None, 'population':population,
                                     'selection_mode':parent_spec['mode'], 'error':str(error)}
                    selected_id = selection['selected_candidate_id']
                    frame_identity = None
                    if selected_id in available:
                        launch, baseline_round, frame_identity = available[selected_id]
                        frame, selected_candidate = checked_parent_frame(QueryRegistry(registry), baseline_round, fixtures, selected_id)
                        if parent_frame_identity(baseline_round, frame) != frame_identity:
                            raise ValueError('AUTONOMY_POOL_EVIDENCE_CHANGED')
                        validate_launch(launch, phase='AUTONOMOUS_SEARCH', candidate=selected_candidate, fixtures=fixtures)
                        selected_ids.append(selected_id)
                        state.update(current_parent_id=selected_id, current_parent_round=str(baseline_round),
                            current_parent_quality=('QUALIFIED_ROOT' if selected_id==config['baseline_id']
                                else 'TRAINING_VALIDATED_EXPLORATION; new sealed verification required'))
                    selection_record = {**selection, 'schema':'rsi-query-parent-selection-receipt/v1',
                        'round':round_number, 'phase_sha256':digest(selection_phase),
                        'config_sha256':digest(parent_spec), 'objective':objective,
                        'launch_run_id':launch.run_id, 'launch_plan_sha256':launch.plan_sha256,
                        'root_receipt_sha256':selection_phase['root_receipt_sha256'],
                        'frame':frame_identity, 'frame_sha256':digest(frame_identity),
                        'exclusions':[{ 'candidate_id':r['candidate_id'], 'reason':r['excluded_reason']}
                                      for r in population if r.get('excluded_reason')]}
                    selection_path = trial/'parent-selection.json'
                    atomic_write(selection_path, canonical(selection_record), exclusive=True)
                    selection_binding = {'parent_selection_path':str(selection_path.resolve()),
                                         'parent_selection_sha256':bytes_hash(selection_path.read_bytes())}
                    state.update(selection_binding); save()
                    log.append('parent-selection:'+str(round_number), 'AUTONOMY_PARENT_SELECTED', selection_binding)
                    if selection_error is not None:
                        raise selection_error
                    if selected_id not in available:
                        raise ValueError('AUTONOMY_POOL_NO_ELIGIBLE_PARENT')
                else:
                    frame=frames(baseline_round,fixtures,arm)
                if frame['parent_id']!=state['current_parent_id']:raise ValueError('AUTONOMY_PARENT_CHANGED')
                scanner=LeakScanner([frame['parent_id']]+[str(f.resolve()) for f in fixtures])
                primary=[b for b in frame['baseline'].values() if b['suite']==1]
                counts={'questions':20,'source_groups':20,'paired':20,'failed':0,'rounds':round_number-1,
                    'context_chars':sum(len(b['capture']['context']) for b in primary),
                    'delivered_members':sum(len(b['capture']['members']) for b in primary),
                    'seed_members':sum(len(b['case']['seed_ids']) for b in primary)}
                counts['nonseed_members']=counts['delivered_members']-counts['seed_members']
                from .selector_contract import structural_counts
                counts.update(structural_counts(baseline_round/'suite-1',arm))
                public=make_visible([project_summary('REPLAY_SUMMARY',counts,include_count_evidence=True)],
                    parent_code=selector(frame['worker'].decode())[1],round_id='round-'+uuid.uuid4().hex,scanner=scanner)
                recent = history[-3:]
                search_memory = project_search_memory(memory, recent=recent, **memory_scope)
                public.update(selector_contract=interface_contract(),experiment_intent='STRUCTURAL_POLICY_SEARCH',
                    parent_selection=json.loads(frame['config'])['selection'],search_history=recent,
                    search_memory=search_memory)
                atomic_write(trial/'public-context.json',canonical(public),exclusive=True)
                atomic_write(trial/'search-context-plan.json', canonical({
                    'schema':'rsi-search-context-plan/v1','measurement_scope':'PRE_REQUEST_CONTEXT',
                    'history_items_planned': len(recent), 'memory_entries_planned': len(search_memory['entries']),
                    'history_references_planned': sum('recent_history_index' in entry['history'] for entry in search_memory['entries']),
                    'history_and_memory_bytes_planned': len(canonical([recent, search_memory])),
                }), exclusive=True)
                from .foundation import check_split
                freezes={str(f.resolve()):check_split(f,expected_count=20) for f in fixtures}
                atomic_write(trial/'fixture-freezes.restricted.json',canonical(freezes),exclusive=True)
                atomic_write(trial/'baseline.json',canonical({'parent_id':frame['parent_id'],'parent_worker_sha256':bytes_hash(frame['worker']),
                    'parent_config_sha256':bytes_hash(frame['config']),'ordinary_round':str(baseline_round.resolve()),
                    'ordinary_round_sha256':bytes_hash((baseline_round/'round.json').read_bytes()),'arm':arm,**selection_binding}),exclusive=True)
                roles=OpenAIRoles(budget,trial/'roles',config['key_file'],scanner,seam_profile='query-autonomous-v1',role_profile=role_profile,launch=launch,strategy_context=experience_session.verify if experience_session else None,
                    models={'P2_BUDGET':proposer_model(history)},before_request=guard)
                advisory_roles=OpenAIRoles(budget,trial/'advisory-roles',config['key_file'],scanner,
                    seam_profile='legacy',before_request=guard,launch=launch,strategy_context=experience_session.verify if experience_session else None)
                parent=QueryRegistry(registry).get(state['current_parent_id'])
                def capsule_factory(output):
                    return Capsule(config['host'],Path(config['identity']),Path(config['business_root']),output)
                state['stage']='INTEGRATED_ROUND';save()
                result=run_query_round(public=public,roles=roles,advisory_roles=advisory_roles,
                    parent=parent,fixtures=fixtures,registry=QueryRegistry(registry),directory=trial/'integrated',
                    capsule_factory=capsule_factory,scanner=scanner,enabled_slots=enabled_slots,guard=guard,
                    role_profile=role_profile,experience_session=experience_session,launch=launch,handbook=handbook,compatibility_fingerprint=config.get('handbook_compatibility_fingerprint'),
                    features=config.get('handbook_features'),format=config.get('handbook_format','OTHER'),
                    **({'objective_transition': objective_transition} if objective_transition is not None else {}))
                guard()
                items=remember_round(memory,history,result,frame['worker'],initial_memory_rounds+round_number, **memory_scope)
                atomic_write(trial/'outcomes.json',canonical(items),exclusive=True)
                atomic_write(directory/'search-memory.json',canonical(memory))
                from .analytics.proposal_repeats import project_proposal_repeats
                repeat_receipt = project_proposal_repeats(
                    result.get('proposal_instances', {}), items, round_number=round_number,
                    scope={'parent_id': frame['parent_id'], 'parent_worker_sha256': bytes_hash(frame['worker']),
                           'parent_config_sha256': bytes_hash(frame['config']), 'fixture_freezes_sha256': digest(freezes),
                           'objective_sha256': digest(objective) if objective else 'NOT_CONFIGURED',
                           'compatibility_fingerprint': memory_scope.get('compatibility_fingerprint', config['freeze_sha256'])},
                    prior_rows=prior_proposal_rows)
                prior_proposal_rows.extend(repeat_receipt['rows'])
                repeat_path = trial/'proposal-repeats.json'
                repeat_bytes = canonical(repeat_receipt)
                atomic_write(repeat_path, repeat_bytes, exclusive=True)
                repeat_binding = {'proposal_repeats_path': str(repeat_path.resolve()),
                                  'proposal_repeats_sha256': bytes_hash(repeat_bytes)}
                state['last_outcomes']=items
                state.update(repeat_binding)
                log.append('round:'+str(round_number),'AUTONOMY_ROUND_FINISHED',{'round':round_number,
                    'outcomes':items,'parent_id':state['current_parent_id'],'candidate_author':'RSI_OPENAI_MODEL',
                    **selection_binding, **repeat_binding})
                save()
                if result.get('execution_state')=='FAILED':
                    reason=result.get('stop_reason','INTEGRATED_ROUND_FAILED')
                    if reason=='OWNER_REQUESTED_STOP':finish(reason);return state
                    finish(reason,'FAILED');return state
                diagnosis=result.get('diagnosis',{})
                if diagnosis and not diagnosis.get('fixable_within_seam',False):
                    finish('RSI_REQUESTED_SYSTEM_DEVELOPMENT');return state
                cid=result.get('adopted_candidate_id')
                if cid:
                    ordinary=Path(result['selected_training_directory'])
                    if parent_spec is not None:
                        parent_archive_files(ordinary.parent)
                        parent_archive_files(ordinary)
                    verify_training(registry,ordinary,fixtures,cid)
                    if parent_spec is not None:
                        if ordinary.resolve().parent != (trial/'integrated').resolve():
                            raise ValueError('AUTONOMY_POOL_TRAINING_DIRECTORY_CHANGED')
                        training_record = load(ordinary/'round.json')
                        coordinator = load(ordinary.parent/'round.json')
                        if (training_record.get('adopted_candidate_id') != cid
                                or training_record.get('family_id') != result.get('family_id')
                                or not result.get('family_id')
                                or coordinator.get('adopted_candidate_id') != cid
                                or coordinator.get('family_id') != result['family_id']
                                or coordinator.get('parent_id') != state['current_parent_id']
                                or coordinator.get('launch_run_id') != launch.run_id
                                or coordinator.get('launch_plan_sha256') != launch.plan_sha256):
                            raise ValueError('AUTONOMY_POOL_ADOPTION_BINDING_CHANGED')
                        admitted_frame, admitted_candidate = checked_parent_frame(QueryRegistry(registry), ordinary, fixtures, cid)
                        if admitted_candidate['record'].get('parent_id') != state['current_parent_id']:
                            raise ValueError('AUTONOMY_POOL_ADOPTION_PARENT_CHANGED')
                        validate_launch(replace(launch, exploration_training=str(ordinary.resolve())),
                                        phase='AUTONOMOUS_SEARCH', candidate=admitted_candidate, fixtures=fixtures)
                        if not any(p['candidate_id']==cid for p in parent_pool):
                            parent_pool.append({'candidate_id':cid, 'kind':'TRAINING_EXPLORATION',
                                'admission_round':round_number, **measure_parent(admitted_frame['suite_decisions'], arm='B'),
                                'frame':parent_frame_identity(ordinary, admitted_frame)})
                    launch=replace(launch,exploration_training=str(ordinary.resolve()))
                    baseline_round=ordinary;state.update(current_parent_id=cid,current_parent_round=str(ordinary),
                        current_parent_quality='TRAINING_VALIDATED_EXPLORATION; new sealed verification required')
                    save()
        except (ValueError,RuntimeError,StageFailure,TimeoutError,OSError) as error:
            reason=error.reason_code if isinstance(error,StageFailure) else str(error) if str(error).isupper() and len(str(error))<100 else type(error).__name__
            if reason in {'BUDGET_EXHAUSTED','OWNER_REQUESTED_STOP'}:
                finish(reason);return state
            finish(reason,'FAILED');raise

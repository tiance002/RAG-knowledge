"""Fixed actual-history engineering epoch with independent receipt-bound gates.

Retrieval predicates delegate to source-relations/v1 without treating historical
text as retrieved evidence. Business controls have a separate result domain.
"""
from copy import deepcopy
from pathlib import Path
import json

from .events import digest
from . import source_relations as relations

PROFILE = 'source-relations-fixed-history/v1'
HISTORY_PROFILE = 'qa500-fixed-actual-history-sidecar/v1'


def _load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))



def validate_query_derivation(sidecar, question, derivation):
    base = sidecar.get('question_sha256'); current = relations.sha(question.encode())
    if derivation is None:
        if base != current: raise ValueError('FIXED_HISTORY_QUERY_DERIVATION_REQUIRED')
        return
    if (not isinstance(derivation,dict) or derivation.get('profile') != 'reviewed-fixed-query-variant/v1'
            or derivation.get('base_question_sha256') != base
            or derivation.get('current_question_sha256') != current):
        raise ValueError('FIXED_HISTORY_QUERY_DERIVATION_CHANGED')
    subject = {k:v for k,v in derivation.items() if k != 'review'}
    review = derivation.get('review',{})
    if (review.get('subject_sha256') != digest(subject)
            or review.get('status') not in {'AI_REVIEWED','HUMAN_REVIEWED'}
            or not review.get('reviewer') or not review.get('reviewed_at')):
        raise ValueError('FIXED_HISTORY_QUERY_DERIVATION_REVIEW_REQUIRED')


def validate_history(sidecar, *, qid, question, verify_sources=True, query_derivation=None, expected_partition=None):
    """Recompute immutable history and its original actual-record provenance."""
    from scripts.build_rsi_qa500_history_sidecars import (
        validate_sidecar, read_bound_line, prefix_from_actual, reject_gold)
    from scripts.audit_rsi_selected_qa500_dev import verify_binding, partition_selections
    from .fixed_history_worker import validate_history_sidecar
    validate_sidecar(sidecar)
    partition = sidecar.get('partition','DEV')
    if (partition not in {'DEV','FINAL_HOLDOUT'}
            or expected_partition is not None and partition != expected_partition
            or partition=='FINAL_HOLDOUT' and sidecar.get('item_kind')!='standalone'):
        raise ValueError('FIXED_HISTORY_PARTITION_MISMATCH')
    base_question = read_bound_line(sidecar['current_input'])['question'] if verify_sources else question
    validate_history_sidecar(sidecar,base_question)
    validate_query_derivation(sidecar,question,query_derivation)
    if (sidecar.get('item_id') != qid or sidecar.get('question_sha256') != relations.sha(base_question.encode())
            or sidecar.get('gold_history_used') is not False
            or sidecar.get('candidate_history_access') is not False):
        raise ValueError('FIXED_HISTORY_QUERY_BINDING_MISMATCH')
    history = sidecar.get('history')
    if (not isinstance(history, list) or any(not isinstance(m, dict)
            or set(m) != {'role', 'content'} or m['role'] not in {'user', 'assistant'}
            or not isinstance(m['content'], str) for m in history)):
        raise ValueError('FIXED_HISTORY_MESSAGE_INVALID')
    semantic = sidecar.get('item_kind') == 'session_turn'
    if (sidecar.get('item_kind') not in {'session_turn', 'standalone'}
            or sidecar.get('input_policy') != ('FIXED_ACTUAL_SEMANTIC_PREFIX' if semantic else 'EMPTY_HISTORY_NEW_ISOLATED_RUN')
            or not semantic and (history or sidecar.get('prefix_evidence'))):
        raise ValueError('FIXED_HISTORY_INPUT_POLICY_MISMATCH')
    if verify_sources:
        current = read_bound_line(sidecar['current_input']); reject_gold(current)
        if current.get('item_id') != qid or current.get('question') != base_question:
            raise ValueError('FIXED_HISTORY_CURRENT_QUERY_CHANGED')
        verify_binding(sidecar['audit_binding'])
        audit = _load(sidecar['audit_binding']['path'])
        schema = ('qa500-selected-final-passive-audit/v1' if partition=='FINAL_HOLDOUT'
                  else 'qa500-selected-dev-passive-audit/v1')
        if audit.get('schema')!=schema or audit.get('partition','DEV')!=partition:
            raise ValueError('FIXED_HISTORY_AUDIT_PARTITION_MISMATCH')
        for ref in audit['inputs'].values(): verify_binding(ref)
        dev, final = partition_selections(audit['inputs']['dev_csv']['path'],
            audit['inputs']['final_csv']['path'],'FINAL' if partition=='FINAL_HOLDOUT' else 'DEV')
        if audit.get('item_ids') != [r['item_id'] for r in dev]:
            raise ValueError('FIXED_HISTORY_SELECTION_CHANGED')
        if qid not in {r['item_id'] for r in dev}:
            raise ValueError('FIXED_HISTORY_SELECTION_CHANGED')
        item = next(r for r in audit['items'] if r['item_id'] == qid)
        if item['item_kind'] != sidecar['item_kind']:
            raise ValueError('FIXED_HISTORY_KIND_CHANGED')
        if sidecar.get('control_class') != item['capture']['control_class']:
            raise ValueError('FIXED_HISTORY_ORIGINAL_CONTROL_CLASS_CHANGED')
        if semantic:
            actual, evidence, gaps = prefix_from_actual(item, audit['inputs']['runtime_input'], final)
            if (actual != history or evidence != sidecar['prefix_evidence']
                    or gaps != sidecar['upstream_gap_ids']):
                raise ValueError('FIXED_HISTORY_ACTUAL_PREFIX_CHANGED')
    return deepcopy(history)


def check_fixture(directory, *, expected_count=None, review_policy='production-source-family/v1'):
    directory = Path(directory).resolve(); manifest = _load(directory/'manifest.json')
    if review_policy not in relations.REVIEW_POLICIES:
        raise ValueError('RELATIONS_REVIEW_POLICY_INVALID')
    if (manifest.get('profile') != PROFILE or not manifest.get('lineage_id')
            or manifest.get('history_profile') != HISTORY_PROFILE
            or manifest.get('partition') not in relations.PARTITIONS):
        raise ValueError('FIXED_HISTORY_PROFILE_REQUIRED')
    cases = _load(directory/'cases.json'); specs = _load(directory/'relations.restricted.json')
    ids = [i['qid'] for i in cases]
    if len(ids) != 20 or len(set(ids)) != 20 or expected_count not in (None, 20):
        raise ValueError('FIXED_HISTORY_20_SLOTS_REQUIRED')
    controls = manifest.get('business_controls', {})
    sidecars = manifest.get('history_sidecars', {})
    final_partition = manifest['partition']=='FINAL_HOLDOUT'
    if ((not final_partition and (len(specs)!=17 or len(controls)!=3)) or set(specs)&set(controls)
            or set(specs)|set(controls) != set(ids) or set(sidecars) != set(ids)):
        raise ValueError('FIXED_HISTORY_CASE_DOMAINS_REQUIRED')
    hashes = manifest.get('artifact_hashes', {})
    if not {'cases.json', 'relations.restricted.json'} <= set(hashes):
        raise ValueError('FIXED_HISTORY_ARTIFACT_BINDING_REQUIRED')
    for name, expected in hashes.items():
        path = (directory/name).resolve()
        if not path.is_relative_to(directory) or relations.sha(path.read_bytes()) != expected:
            raise ValueError('FIXED_HISTORY_FIXTURE_CHANGED')
    semantic = 0
    for item in cases:
        qid = item['qid']; case = item['case']; ref = sidecars[qid]
        path = (directory/ref['path']).resolve()
        if not path.is_relative_to(directory) or relations.sha(path.read_bytes()) != ref['sha256']:
            raise ValueError('FIXED_HISTORY_SIDECAR_BINDING_MISMATCH')
        sidecar = _load(path); validate_history(sidecar, qid=qid, question=case['question'],
            query_derivation=manifest.get('query_derivations',{}).get(qid),
            expected_partition='FINAL_HOLDOUT' if final_partition else 'DEV')
        expected_class = 'SOURCE_CONTEXT_CAPTURE' if qid in specs else 'CONTROL_NO_RETRIEVAL'
        if sidecar.get('control_class') != expected_class:
            raise ValueError('FIXED_HISTORY_CONTROL_RELABEL_FORBIDDEN')
        semantic += sidecar['item_kind'] == 'session_turn'
        if qid in specs:
            spec = specs[qid]
            if spec.get('profile') != relations.PROFILE or spec.get('lineage_id') != manifest['lineage_id']:
                raise ValueError('FIXED_HISTORY_RETRIEVAL_PROFILE_MISMATCH')
            sources = {}
            for sid, source in spec['sources'].items():
                source_path = (directory/source['relative_path']).resolve()
                if not source_path.is_relative_to(directory): raise ValueError('FIXED_HISTORY_SOURCE_ESCAPE')
                sources[sid] = source_path.read_bytes()
            relations.validate_spec(case, spec, sources)
        elif (case.get('seed_ids') or controls[qid].get('expected_status') != 'INSUFFICIENT_EVIDENCE'
                or controls[qid].get('expected_provider_requests') != 0):
            raise ValueError('FIXED_HISTORY_CONTROL_INVALID')
    if not final_partition and semantic != 6: raise ValueError('FIXED_HISTORY_6_ACTUAL_14_EMPTY_REQUIRED')
    if final_partition and semantic: raise ValueError('FIXED_HISTORY_FINAL_EMPTY_HISTORY_REQUIRED')
    counts={'retrieval_questions':len(specs),'business_controls':len(controls),
        'actual_history_questions':semantic,'empty_history_questions':20-semantic}
    if ((final_partition or 'case_domain_counts' in manifest)
            and manifest.get('case_domain_counts') != counts):
        raise ValueError('FIXED_HISTORY_FROZEN_DOMAIN_COUNTS_MISMATCH')
    if manifest.get('partition') == 'FINAL_HOLDOUT':
        relations._verify_final_snapshot(manifest, specs, review_policy=review_policy)
        if any(a['review']['status'] not in relations.REVIEW_POLICIES[review_policy]
                for spec in specs.values() for a in spec['atoms']):
            raise ValueError('RELATIONS_FINAL_ATOMS_REVIEW_POLICY_NOT_MET')
    return {'profile': PROFILE, 'history_profile': HISTORY_PROFILE, 'lineage_id': manifest['lineage_id'],
        'questions': 20, **counts,
        'partition':manifest['partition'], 'review_policy':review_policy,
        'artifact_hashes': {p.relative_to(directory).as_posix(): relations.sha(p.read_bytes())
            for p in sorted(directory.rglob('*')) if p.is_file()},
        'production_qualified': False, 'admission_status':'FIXTURE_VALIDATED_NOT_EXECUTION_EVIDENCE'}


def validate_capture_history(case, capture, sidecar, *, query_derivation=None):
    history = validate_history(sidecar,qid=sidecar['item_id'],question=case['question'],query_derivation=query_derivation)
    expected = history + [{'role':'user','content':case['question']}]
    if (capture.get('evaluator_profile') != PROFILE or capture.get('history_profile') != HISTORY_PROFILE
            or capture.get('qid') != sidecar['item_id']
            or capture.get('base_question_sha256') != sidecar['question_sha256']
            or capture.get('current_question_sha256') != relations.sha(case['question'].encode())
            or capture.get('query_derivation_sha256') != digest(query_derivation)
            or capture.get('history_sha256') != digest(history) or capture.get('sidecar_sha256') != digest(sidecar)
            or capture.get('business_input_messages') != expected
            or capture.get('business_input_messages_sha256') != digest(expected)):
        raise ValueError('FIXED_HISTORY_DELIVERY_BINDING_MISMATCH')
    return history


def assess(case, capture, spec, source_bytes, *, sidecar, query_derivation=None):
    """Only retrieved spans establish facts; history is independently bound."""
    try:
        history = validate_capture_history(case,capture,sidecar,query_derivation=query_derivation)
        if (capture.get('evaluator_profile') != PROFILE or capture.get('history_profile') != HISTORY_PROFILE
                or capture.get('history_sha256') != digest(history)
                or capture.get('sidecar_sha256') != digest(sidecar)
                or capture.get('request_calls') != 1
                or capture.get('provider_delivery_status') != 'PROVIDER_REQUEST_CAPTURED'
                or capture.get('messages', [])[1:] != history + [{'role':'user', 'content':case['question']}]):
            raise ValueError('FIXED_HISTORY_DELIVERY_BINDING_MISMATCH')
    except (KeyError, TypeError, IndexError, ValueError, OSError):
        return {'profile':PROFILE, 'status':'UNKNOWN', 'atoms':[], 'reason':'FIXED_HISTORY_DELIVERY_UNVERIFIED'}
    # Historical messages are deliberately excluded from source-fact evaluation.
    projection = deepcopy(capture); projection['messages'] = capture['messages'][:1]
    result = relations.assess(case, projection, spec, source_bytes)
    return {**result, 'profile':PROFILE, 'history_is_retrieved_support':False}


def business_control(capture):
    """A terminal no-call observation is never a source fact or QA answer."""
    outcome = capture.get('business_outcome', {})
    terminal = capture.get('terminal_events', [])
    valid = (capture.get('status') == 'OK' and type(capture.get('request_calls')) is int
        and capture.get('request_calls') == 0 and capture.get('messages') == []
        and capture.get('provider_delivery_status') == 'NO_PROVIDER_REQUEST'
        and outcome.get('answer_mode') == 'INSUFFICIENT_EVIDENCE'
        and outcome.get('terminal_event') == 'done' and outcome.get('provider_calls') == 0
        and isinstance(terminal,list) and len(terminal) == 1 and terminal[0].get('event') == 'done'
        and terminal[0].get('data',{}).get('answer_mode') == 'INSUFFICIENT_EVIDENCE'
        and all(terminal[0].get('data',{}).get(k) == outcome.get(k)
                for k in ('retrieval_status','generation_status'))
        and capture.get('context') == '' and capture.get('members') == [])
    return {'domain':'BUSINESS_BRANCH_CONTROL', 'status':'PRESENT' if valid else 'UNKNOWN',
        'retrieval_atoms':[], 'provider_requests':capture.get('request_calls'), 'context_chars':0 if valid else None}


def fixed_slot_accounting(rows, qids, *, expected_counts=None):
    if len(qids) != 20 or len(set(qids)) != 20 or [r.get('qid') for r in rows] != list(qids):
        raise ValueError('FIXED_HISTORY_20_SLOTS_REQUIRED')
    controls = [r for r in rows if r.get('domain') == 'BUSINESS_BRANCH_CONTROL']
    expected_controls=3 if expected_counts is None else expected_counts['business_controls']
    if (len(controls) != expected_controls
            or any(r.get('chars_A') != 0 or r.get('chars_B') != 0 for r in controls)
            or any(r.get('domain') not in {'BUSINESS_BRANCH_CONTROL','RETRIEVAL_SOURCE_FACT'} for r in rows)):
        raise ValueError('FIXED_HISTORY_ZERO_CHAR_CONTROLS_REQUIRED')
    return {'total_questions':20, 'retrieval_questions':20-len(controls), 'business_controls':len(controls),
        'control_character_ties':len(controls), 'dropped_questions':0, 'statistical_predicates_changed':False}


def require_retrieval_clusters(clusters):
    """Business controls cannot supply the existing eight-group independence gate."""
    if len(set(clusters.values())) < 8:
        raise ValueError('INSUFFICIENT_RETRIEVAL_SOURCE_GROUPS')



def preparation_input(item, sidecar, lineage_id, query_derivation=None):
    from .experiment import executor
    validate_history(sidecar, qid=item['qid'], question=item['case']['question'],query_derivation=query_derivation)
    return {'profile':PROFILE, 'case':executor.checked_case(item['case']),
        'history_sidecar':sidecar, 'history_sha256':digest(sidecar['history']),
        'sidecar_sha256':digest(sidecar), 'qid':item['qid'], 'lineage_id':lineage_id,
        'base_question_sha256':sidecar['question_sha256'],
        'current_question_sha256':relations.sha(item['case']['question'].encode()),
        'query_derivation_sha256':digest(query_derivation)}


def validate_prepared(snapshot, envelope):
    if (snapshot.get('evaluator_profile') != PROFILE or snapshot.get('history_profile') != HISTORY_PROFILE
            or snapshot.get('fixed_history') != envelope['history_sidecar']['history']
            or any(snapshot.get(k) != envelope[k] for k in ('history_sha256','sidecar_sha256','qid','lineage_id','base_question_sha256','current_question_sha256','query_derivation_sha256'))
            or any(k in snapshot for k in ('history_sidecar','prefix_evidence','current_input','audit_binding'))
            or snapshot.get('question') != envelope['case']['question']):
        raise ValueError('FIXED_HISTORY_PREPARATION_MISMATCH')
    return snapshot


def _receipt(policy, path, *, manifest, previous_sequence, **kwargs):
    from .execution_evidence import check_receipt
    if not Path(path).is_file(): raise ValueError('FIXED_HISTORY_RECEIPT_REQUIRED')
    runtime = check_receipt(policy, path, **kwargs)
    receipt = _load(path); binding = manifest['execution_context']
    input_value = _load(kwargs['input_path'])
    capture = _load(kwargs['capture_path'])
    history_binding = {'evaluator_profile':PROFILE,'history_profile':HISTORY_PROFILE,
        **{k:input_value[k] for k in ('history_sha256','sidecar_sha256','qid','lineage_id','base_question_sha256','current_question_sha256','query_derivation_sha256')}}
    if (receipt.get('schema') != 'rsi-execution-evidence/v2.3'
            or receipt.get('stage') != kwargs['stage'] or receipt.get('exit_code') != 0
            or receipt.get('reason') is not None
            or any(receipt.get(k) != value or capture.get(k) != value for k,value in history_binding.items())
            or receipt.get('capture_sha256') != relations.sha(Path(kwargs['capture_path']).read_bytes())):
        raise ValueError('FIXED_HISTORY_RECEIPT_HISTORY_MISMATCH')
    sequence = receipt.get('sequence'); job = binding.get('capsule_job_id')
    if (not isinstance(job,str) or len(job) != 32
            or any(c not in '0123456789abcdef' for c in job)
            or any(receipt.get(k) != binding.get(k) for k in
                ('capsule_job_id','launch_run_id','launch_plan_sha256'))
            or type(sequence) is not int or sequence != previous_sequence + 1
            or receipt.get('container_name') != 'rsi-v22-'+job[:10]+'-'+str(sequence)):
        raise ValueError('FIXED_HISTORY_RECEIPT_RUN_MISMATCH')
    return runtime, sequence


def verify_run_plan(manifest, run, fixture, policy):
    """Recheck the actual hash-bound launch, including its execution directory."""
    from .launch import read_binding, fixture_identity
    plan = read_binding(manifest['launch_plan'])
    context = manifest['execution_context']; phase = manifest['launch_phase']
    run, fixture = Path(run).resolve(), Path(fixture).resolve()
    permitted = Path(plan.get('run_directory','')).resolve()
    if (manifest['launch_plan']['sha256'] != context.get('launch_plan_sha256')
            or plan.get('schema') != 'rsi-launch/v1' or plan.get('run_id') != context.get('launch_run_id')
            or phase not in plan.get('allowed_phases',[])
            or plan.get('evaluator_profile') != PROFILE
            or plan.get('execution_policy') != policy.to_dict()
            or plan.get('fixtures',{}).get(str(fixture)) != fixture_identity(fixture)
            or not (run == permitted or run.is_relative_to(permitted))):
        raise ValueError('FIXED_HISTORY_LAUNCH_RUN_BINDING_MISMATCH')
    return plan


def replay_artifacts(capsule, fixture, artifacts, output, *, deadline, schema=PROFILE, scope='fixed actual-history replay'):
    import time
    from .experiment import executor, ROOT
    from .locking import atomic_write
    from .events import canonical
    from .execution_evidence import resolve_policy
    from .launch import file_binding
    fixture, output = Path(fixture), Path(output)
    freeze = check_fixture(fixture)
    specs = _load(fixture/'relations.restricted.json')
    retrieval_cases = [i for i in _load(fixture/'cases.json') if i['qid'] in specs]
    require_retrieval_clusters(relations.family_clusters(retrieval_cases,specs))
    if set(artifacts) != {'A','B'}: raise ValueError('TWO_ARTIFACT_ARMS_REQUIRED')
    policy = resolve_policy(getattr(capsule,'evidence_policy',None))
    context = capsule.context_identity()
    if not context.get('launch_run_id') or not context.get('launch_plan_sha256'):
        raise ValueError('FIXED_HISTORY_LAUNCH_BINDING_REQUIRED')
    output.mkdir(parents=True,exist_ok=False)
    manifest = {'schema':schema, 'evaluator_profile':PROFILE, 'history_profile':HISTORY_PROFILE,
        'fixture':freeze, 'scope':scope, 'fixed':executor.FIXED, 'arms':{},
        'execution_policy':policy.to_dict(), 'execution_context':context,
        'launch_plan':file_binding(capsule.launch_context.plan_path),
        'launch_phase':capsule.launch_context.phase}
    verify_run_plan(manifest,output,fixture,policy)
    for arm, artifact in artifacts.items():
        atomic_write(output/(arm+'_worker.py'),artifact['worker'],exclusive=True)
        atomic_write(output/(arm+'.json'),artifact['config'],exclusive=True)
        manifest['arms'][arm] = {'candidate_id':artifact['candidate_id'],
            'worker_sha256':relations.sha(artifact['worker']),'sha256':relations.sha(artifact['config'])}
    atomic_write(output/'manifest.json',canonical(manifest),exclusive=True)
    config = _load(fixture/'manifest.json'); rows = []
    for index,item in enumerate(_load(fixture/'cases.json'),1):
        if time.monotonic() >= deadline: break
        directory = output/f'case-{index:03}'; directory.mkdir()
        sidecar = _load(fixture/config['history_sidecars'][item['qid']]['path'])
        envelope = preparation_input(item, sidecar, config['lineage_id'],config.get('query_derivations',{}).get(item['qid']))
        atomic_write(directory/'input.json',canonical(envelope),exclusive=True)
        capsule.deadline = min(deadline,time.monotonic()+300)
        raw = capsule.child('prepare',[directory/'input.json',directory/'snapshot.json'],directory,
            worker=ROOT/'scripts/rsi_fixed_history_worker_v1.py',business_root=capsule.business_root)
        snapshot = executor.execution_view(validate_prepared(raw,envelope))
        atomic_write(directory/'arm-input.json',canonical(snapshot),exclusive=True)
        for arm in ('A','B'):
            arm_dir = directory/arm; arm_dir.mkdir()
            capture = capsule.child('deliver',[directory/'arm-input.json',output/(arm+'.json'),arm_dir/'capture.json'],
                arm_dir,worker=output/(arm+'_worker.py'),business_root=capsule.business_root)
            if capture.get('upstream_sha256') != executor.fingerprint(snapshot):
                raise ValueError('FIXED_HISTORY_UPSTREAM_MISMATCH')
        rows.append({'qid':item['qid'],'status':'PAIRED'})
    atomic_write(output/'results.json',canonical({'rows':rows}),exclusive=True)
    b = manifest['arms']['B']
    return read_decision(output,fixture,b['candidate_id'],{'angle':'P2_BUDGET','proposal_id':schema},
        b['worker_sha256'],b['sha256'],evidence=policy)


def read_decision(run_directory, fixture_directory, candidate_id, proposal, worker_hash, config_hash, *, evidence=None):
    from .experiment import executor, ROOT
    from .execution_evidence import resolve_policy
    from .adjudicate import evaluate_candidate
    run, fixture = Path(run_directory), Path(fixture_directory)
    freeze = check_fixture(fixture); manifest = _load(run/'manifest.json'); policy = resolve_policy(evidence)
    verify_run_plan(manifest,run,fixture,policy)
    if (manifest.get('evaluator_profile') != PROFILE or manifest.get('history_profile') != HISTORY_PROFILE
            or manifest.get('fixture') != freeze or manifest.get('fixed') != executor.FIXED
            or manifest.get('execution_policy') != policy.to_dict()
            or manifest['arms']['B'] != {'candidate_id':candidate_id,'worker_sha256':worker_hash,'sha256':config_hash}):
        raise ValueError('FIXED_HISTORY_PAIR_BINDING_MISMATCH')
    for arm, identity in manifest['arms'].items():
        if (relations.sha((run/(arm+'_worker.py')).read_bytes()) != identity['worker_sha256']
                or relations.sha((run/(arm+'.json')).read_bytes()) != identity['sha256']):
            raise ValueError('FIXED_HISTORY_ARM_ARTIFACT_CHANGED')
        from .fixed_history_worker import build_worker
        worker = (run/(arm+'_worker.py')).read_bytes()
        if build_worker(worker) != worker:
            raise ValueError('FIXED_HISTORY_WORKER_SEAM_CHANGED')
    cases = _load(fixture/'cases.json'); specs = _load(fixture/'relations.restricted.json')
    fixture_manifest = _load(fixture/'manifest.json')
    if _load(run/'results.json').get('rows') != [{'qid':i['qid'],'status':'PAIRED'} for i in cases]:
        raise ValueError('FIXED_HISTORY_PARTIAL_REPLAY')
    retrieval_cases = [i for i in cases if i['qid'] in specs]
    clusters = relations.family_clusters(retrieval_cases,specs)
    require_retrieval_clusters(clusters)
    runtime = None; sequence = manifest['execution_context']['sequence']; rows = []
    for index,item in enumerate(cases,1):
        qid = item['qid']; case = item['case']; directory = run/f'case-{index:03}'
        sidecar = _load(fixture/fixture_manifest['history_sidecars'][qid]['path'])
        envelope = preparation_input(item,sidecar,fixture_manifest['lineage_id'],fixture_manifest.get('query_derivations',{}).get(qid))
        if _load(directory/'input.json') != envelope: raise ValueError('FIXED_HISTORY_PREPARE_INPUT_CHANGED')
        runtime, sequence = _receipt(policy,directory/'execution-receipt.json',manifest=manifest,previous_sequence=sequence,
            stage='prepare',worker_hash=relations.sha((ROOT/'scripts/rsi_fixed_history_worker_v1.py').read_bytes()),
            input_path=directory/'input.json',capture_path=directory/'snapshot.json',runtime_identity=runtime)
        snapshot = executor.execution_view(validate_prepared(_load(directory/'snapshot.json'),envelope))
        if snapshot != _load(directory/'arm-input.json'): raise ValueError('FIXED_HISTORY_PREPARE_OUTPUT_CHANGED')
        control = qid not in specs
        row = {'qid':qid, 'domain':'BUSINESS_BRANCH_CONTROL' if control else 'RETRIEVAL_SOURCE_FACT',
            'source_group':('BUSINESS_CONTROL::'+qid) if control else clusters[qid],
            'pair_status':'PAIRED','pair_identity':True,'execution':'COMPLETED',
            'parent_faults':[],'candidate_faults':[],'fault_origin':'NONE',
            'upstream_hash':executor.fingerprint(snapshot)}
        for arm,prefix in [('A','parent'),('B','candidate')]:
            cap = directory/arm/'capture.json'
            runtime, sequence = _receipt(policy,cap.parent/'execution-receipt.json',manifest=manifest,previous_sequence=sequence,
                stage='deliver',worker_hash=manifest['arms'][arm]['worker_sha256'],input_path=directory/'arm-input.json',
                capture_path=cap,config_path=run/(arm+'.json'),canonical_config_hash=manifest['arms'][arm]['sha256'],runtime_identity=runtime)
            capture = _load(cap)
            if (capture.get('upstream_sha256') != row['upstream_hash']
                    or any(capture.get(k) != snapshot.get(k) for k in
                        ('evaluator_profile','history_profile','history_sha256','sidecar_sha256','qid','lineage_id'))):
                raise ValueError('FIXED_HISTORY_CAPTURE_BINDING_MISMATCH')
            expected_input = sidecar['history'] + [{'role':'user','content':case['question']}]
            if (capture.get('business_input_messages') != expected_input
                    or capture.get('business_input_messages_sha256') != digest(expected_input)):
                raise ValueError('FIXED_HISTORY_ACTUAL_BUSINESS_INPUT_CHANGED')
            structural = executor.assess(case,capture,{'facts':[],'forbidden':[]},arm)
            if control:
                result = business_control(capture)
                facts = {'BUSINESS_EXECUTION_ASSERTION::INSUFFICIENT_EVIDENCE':result['status']}
                row[prefix+'_business_control'] = result
            else:
                spec = specs[qid]
                sources = {sid:(fixture/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
                result = assess(case,capture,spec,sources,sidecar=sidecar,query_derivation=fixture_manifest.get('query_derivations',{}).get(qid))
                facts = {a['atom_id']:a['status'] for a in result['atoms']}
            row[prefix+'_facts'] = facts; row[prefix+'_faults'] = structural['faults']
            row['provenance_'+arm] = result['status']; row['chars_'+arm] = structural['context_chars']
            row['context_'+arm+'_sha256'] = structural['context_sha256']
        if row['candidate_faults']: row['fault_origin'] = 'CANDIDATE'
        rows.append(row)
    accounting = fixed_slot_accounting(rows,[i['qid'] for i in cases],expected_counts=freeze)
    decision = evaluate_candidate({'angle':proposal['angle'],'proposal_id':proposal['proposal_id'],
        'candidate_id':candidate_id,'worker_hash':worker_hash,'config_hash':config_hash},rows,
        [i['qid'] for i in cases],total_questions=20)
    decision.update(evaluator_profile=PROFILE, history_profile=HISTORY_PROFILE,
        evidence_mode=policy.mode,real_eligible=policy.real_eligible,execution_policy=policy.to_dict(),
        fixture=freeze, fixed_slot_accounting=accounting,
        business_assertions_are_source_facts=False, production_qualified=False,
        execution_evidence_hash=digest({p.relative_to(run).as_posix():relations.sha(p.read_bytes())
            for p in run.rglob('execution-receipt.json')}))
    return decision

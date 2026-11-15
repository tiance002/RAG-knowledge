"""Integrated six-role query-profile rounds; no production or self-code mutation.

Callers own the registry writer lock, parent qualification and cumulative budget.
Every eligible proposal gets independent ordinary replay, even duplicate bytes.
"""
from copy import deepcopy
from pathlib import Path
import time
import ast

from .adjudicate import SLOTS, adjudicate_round
from .errors import StageFailure
from .contracts import validate_proposal
from .events import EventLog
from .experiment import bytes_hash, load, read_decision
from .foundation import check_split
from .baseline_qualification import adapters as profile_adapters
from .family_decision import make_family, validate_family, selected_receipt
from .holdout import replay_artifacts
from .locking import atomic_write, restrict_directory
from .postround import run_advisory
from .query_aware import PROFILE, apply_patch, invariant_probe_source
from .redact import validate_handbook_summaries
from .role_profiles import LEGACY,profile_manifest,preserves_selection_order,roles_for
from .state import canonical, digest, StageJournal
from .workflow import execute_round
from run_rsi_loop import selector


def adapters(fixture):
    """Select only the explicit versioned profile; retain the legacy evaluator."""
    manifest = Path(fixture) / 'manifest.json'
    if manifest.is_file() and load(manifest).get('profile') == 'source-relations/v1':
        return profile_adapters(fixture)
    return check_split, replay_artifacts, read_decision


def implementation_hashes():
    """Declared v2.3 compatibility surface: runtime rsik and RSI entry modules.

    This excludes tests, docs, external dependencies, and arbitrary system code;
    external runtime/source identities are separately bound by execution evidence.
    """
    package = Path(__file__).resolve().parent
    root = package.parent
    paths = [p for p in package.rglob('*.py') if 'tests' not in p.relative_to(package).parts]
    scripts = root / 'scripts'
    paths += [p for p in scripts.glob('*.py') if p.name.startswith(('rsi_', 'run_rsi_'))
              or p.name in {'replay_source_preparation.py','qualify_rsi_baseline.py','prepare_rsi_v23.py','recover_rsi_learning.py'}]
    return {p.relative_to(root).as_posix(): bytes_hash(p.read_bytes()) for p in sorted(paths)}


def bind_budget_path(query_registry, budget_ledger):
    expected = Path(query_registry).resolve().parent.parent / 'task-model-budget.sqlite'
    if Path(budget_ledger).resolve() != expected or not expected.is_file():
        raise ValueError('ORIGINAL_TASK_BUDGET_LEDGER_REQUIRED')
    return expected


def read_only_quarantine_check(registry, cid):
    """Reuse event-chain validation without EventLog's schema-creating constructor."""
    import sqlite3
    path = Path(registry.directory) / 'policy.sqlite'
    def connect():
        db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        return db
    log = object.__new__(EventLog)
    log.path = path
    log.connect = connect
    if any(e['event_type'] == 'QUERY_CANDIDATE_QUARANTINED' and e['payload']['candidate_id'] == cid for e in log.read()):
        raise ValueError('QUERY_PARENT_QUARANTINED')


def resume_query_round(*, directory, registry, fixtures, evidence=None):
    """Read-only completed recovery; incomplete external actions need reconciliation."""
    directory = Path(directory)
    completion = load(directory / 'completion.json')
    if completion['implementation'] != implementation_hashes():
        raise ValueError('QUERY_COMPLETED_IMPLEMENTATION_CHANGED')
    for name, expected in completion['files'].items():
        path = (directory / name).resolve()
        if not path.is_relative_to(directory.resolve()) or bytes_hash(path.read_bytes()) != expected:
            raise ValueError('QUERY_COMPLETED_ARTIFACT_CHANGED')
    record = load(directory / 'round.json')
    if record.get('execution_state') != 'COMPLETED':
        raise ValueError('QUERY_COMPLETED_ROUND_REQUIRED')
    parent = registry.get(record['parent_id'])
    read_only_quarantine_check(registry, parent['candidate_id'])
    if bytes_hash(parent['worker']) != record['parent_worker_sha256'] or bytes_hash(parent['config']) != record['parent_config_sha256']:
        raise ValueError('QUERY_COMPLETED_PARENT_CHANGED')
    freezes = {str(Path(f).resolve()): adapters(f)[0](f, expected_count=20) for f in fixtures}
    if freezes != record['fixture_freezes']:
        raise ValueError('QUERY_COMPLETED_FIXTURE_CHANGED')
    public = load(directory / 'public-context.json')
    if bytes_hash(canonical(public)) != record['public_context_sha256']:
        raise ValueError('QUERY_COMPLETED_PUBLIC_CHANGED')
    profile = record['role_profile']['version']
    if record['role_profile'] != profile_manifest(profile) or record.get('proposal_roles') != roles_for(profile):
        raise ValueError('QUERY_COMPLETED_ROLE_PROFILE_CHANGED')
    raw = record.get('raw_role_proposals', {})
    if set(raw) != set(record.get('proposal_instances', {})):
        raise ValueError('QUERY_COMPLETED_ROLE_PROPOSALS_CHANGED')
    for slot, proposal in raw.items():
        actual = roles_for(profile)[slot]
        validate_proposal(proposal, public, actual)
        normalized = deepcopy(proposal)
        normalized['angle'] = slot
        if normalized != record['proposal_instances'][slot]:
            raise ValueError('QUERY_COMPLETED_ROLE_PROPOSALS_CHANGED')
    expected_binding = strategy_input_binding(public, freezes, profile,
        record.get('compatibility_fingerprint'), record.get('experience_frontier'),
        proposal_protocol_profile=record['seam_profile'],models=record['role_models'])
    if record.get('strategy_input_binding') != expected_binding:
        raise ValueError('QUERY_COMPLETED_STRATEGY_INPUT_CHANGED')
    decisions = []
    for old in record.get('candidate_decisions', []):
        slot = old['angle']
        if slot not in record['qualified']:
            decisions.append(deepcopy(old))
            continue
        output = directory / slot
        proposal = record['proposal_instances'][slot]
        if load(output / 'proposal.json') != proposal:
            raise ValueError('QUERY_COMPLETED_PROPOSAL_CHANGED')
        worker = apply_patch(parent['worker'], proposal['worker_patch'])
        binding = load(output / 'binding.json')
        child = registry.get(binding['candidate_id'])
        read_only_quarantine_check(registry, child['candidate_id'])
        if (child['worker'] != worker or child['config'] != parent['config']
                or child['record']['parent_id'] != parent['candidate_id']
                or load(output / 'probes/capture.json').get('status') != 'PASS'
                or (output / 'probes/worker.py').read_bytes() != invariant_probe_source(worker)):
            raise ValueError('QUERY_COMPLETED_WORKER_CHANGED')
        suite = []
        for index, fixture in enumerate(fixtures, 1):
            run = output / f'suite-{index}'
            manifest = load(run / 'manifest.json')
            for arm, artifact in (('A', parent), ('B', child)):
                spec = manifest['arms'][arm]
                if (spec['candidate_id'] != artifact['candidate_id'] or spec['worker_sha256'] != bytes_hash(artifact['worker'])
                        or spec['sha256'] != bytes_hash(artifact['config'])):
                    raise ValueError('QUERY_COMPLETED_LINEAGE_CHANGED')
            decision = adapters(fixture)[2](run, fixture, child['candidate_id'],
                {'angle': 'P2_BUDGET', 'proposal_id': 'rsi-query-aware-engineering/v1'},
                bytes_hash(worker), bytes_hash(parent['config']), evidence=evidence)
            if decision != load(output / f'decision-{index}.json'):
                raise ValueError('QUERY_COMPLETED_EVALUATION_CHANGED')
            if slot == 'P3_ORDER' and preserves_selection_order(record.get('role_profile', {}).get('version', LEGACY)):
                assert_order_angle(run)
            suite.append(decision)
        current = summarize_suites(suite, slot, proposal['proposal_id'])
        if current != load(output / 'decision.json'):
            raise ValueError('QUERY_COMPLETED_DECISION_CHANGED')
        decisions.append(current)
    if 'candidate_decisions' in record:
        outcome = adjudicate_round(decisions, all_qualified_terminal=set(record['qualified']) == set(record['attempted_replays']))
        for key in ('verdict', 'adopted_candidate_id', 'adopted_proposal_id', 'candidate_decisions', 'family_size'):
            if outcome.get(key) != record.get(key):
                raise ValueError('QUERY_COMPLETED_JUDGMENT_CHANGED')
        family = make_family(decisions, enabled_slots=record['enabled_slots'],
            all_qualified_terminal=set(record['qualified']) == set(record['attempted_replays']),
            role_profile=profile,strategy_binding=expected_binding)
        if family != load(directory / 'family-decision.json') or record.get('family_id') != family['family_id']:
            raise ValueError('QUERY_COMPLETED_FAMILY_CHANGED')
        if record.get('adopted_candidate_id'):
            slot = next(d['angle'] for d in outcome['candidate_decisions']
                        if d.get('candidate_id') == record['adopted_candidate_id'])
            archived = load(directory / slot / 'round.json')
            expected = selected_receipt(family, candidate_id=record['adopted_candidate_id'], selected_slot=slot)
            if (load(directory / slot / 'family-decision.json') != family
                    or any(archived.get(k) != v for k, v in expected.items())):
                raise ValueError('QUERY_COMPLETED_SELECTED_FAMILY_CHANGED')
    return record


def strategy_input_binding(public, freezes, role_profile, compatibility_fingerprint, frontier, *, proposal_protocol_profile, models):
    return {'schema': 'rsi-query-strategy-input/v1',
            'fixture_freezes_sha256': digest(freezes),
            'evaluator_hashes': next(iter(freezes.values()), {}).get('protected', {}),
            'seam_profile': PROFILE, 'role_profile': profile_manifest(role_profile),
            'proposal_protocol_profile': proposal_protocol_profile, 'models': deepcopy(models),
            'compatibility_fingerprint': compatibility_fingerprint,
            'public_semantics_sha256': digest(public_semantics(public)),
            'experience_frontier': deepcopy(frontier)}


def summarize_suites(suite, slot, proposal_id):
    """Preserve primary statistics and safe cross-suite feedback separately."""
    current = deepcopy(suite[0])
    current.update(angle=slot, proposal_id=proposal_id)
    failed = [d for d in suite if not all(d['gates']['G' + str(i)] is True for i in range(1, 5))]
    current['feedback_counts'] = {
        'failed_suites': len(failed),
        'primary_questions': suite[0].get('eligible_count', len(suite[0].get('per_question', []))),
        'auxiliary_cases': sum(d.get('eligible_count', len(d.get('per_question', []))) for d in suite[1:]),
        'fact_loss_cases': sum(any(v == 'ABSENT' for v in row.get('candidate_facts', {}).values())
                               for d in suite for row in d.get('per_question', []))}
    if failed:
        current.update(verdict='REJECTED' if any(d['verdict'] == 'REJECTED' for d in failed) else 'INCONCLUSIVE',
                       gates_passed=False, round_stop=any(d.get('round_stop') for d in suite))
        current['reason_codes'] = list(dict.fromkeys(current['reason_codes'] +
            [code for d in failed for code in d.get('reason_codes', [])] + ['SOURCE_SUITE_HARD_GATE_FAILED']))
    return current


def public_semantics(public):
    """Remove only random aliases, preserving all strategy-bearing public input."""
    value = deepcopy(public)
    for name in ('round_id', 'parent_alias', 'observation_refs'):
        value.pop(name, None)
    value['evidence_quotes'] = sorted(value.get('evidence_quotes', {}).values())
    for observation in value.get('observations', []):
        observation.pop('observation_ref', None)
        for evidence in observation.get('safe_evidence', []):
            evidence.pop('evidence_ref', None)
    return value


def assert_order_angle(run):
    """P3 must preserve the parent's complete selected sequence on every case."""
    rows = load(Path(run) / 'results.json')['rows']
    for index, row in enumerate(rows, 1):
        if row['status'] != 'PAIRED':
            continue  # The ordinary judge classifies incomplete pairs.
        directory = Path(run) / f'case-{index:03}'
        a = load(directory / 'A/capture.json')['members']
        b = load(directory / 'B/capture.json')['members']
        if a != b:
            raise StageFailure('SEAM_ERROR', 'ANGLE_VIOLATION')


def run_query_round(*, public, roles, advisory_roles, parent, fixtures, registry,
                    directory, capsule_factory, scanner, handbook=None,
                    compatibility_fingerprint=None, features=None, format='OTHER',
                    enabled_slots=SLOTS, deadline=None, guard=lambda: None, role_profile=LEGACY, experience_session=None, launch=None, evidence=None):
    """Run Diagnosis/P1/P2/P3, ordinary four-suite replay, then Curator/Meta.

    advisory_roles must use the legacy read-only profile, sharing the same Budget.
    The selected_training_directory uses the existing query verification format.
    It is training adoption only; fresh sealed verification remains mandatory.
    """
    from .launch import guard_real,validate_launch
    from .execution_evidence import ExecutionEvidence
    expected_models={r:getattr(roles,'models',{}).get(r,'gpt-6-luna') for r in ('diagnose',)+tuple(roles_for(role_profile).values())}
    expected_models.update({r:getattr(advisory_roles,'models',{}).get(r,'gpt-6-luna') for r in ('curator','meta')})
    if evidence is None:
        validate_launch(launch,candidate=parent,fixtures=fixtures,directory=directory,models=expected_models,role_profile=role_profile)
    else:
        guard_real(evidence,launch,candidate=parent,fixtures=fixtures,directory=directory,models=expected_models,role_profile=role_profile)
    if hasattr(roles,'launch'):roles.launch=launch
    if hasattr(advisory_roles,'launch'):advisory_roles.launch=launch
    original_guard = guard
    def guard():
        original_guard()
        if experience_session is not None:
            if handbook is None or experience_session.handbook is not handbook:
                raise ValueError('EXPERIENCE_SESSION_HANDBOOK_MISMATCH')
            experience_session.verify()
    guard()
    if getattr(roles, 'role_profile', role_profile) != role_profile:
        raise ValueError('ROLE_PROFILE_CONFIGURATION_MISMATCH')
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    restrict_directory(directory)
    deadline = deadline if deadline is not None else time.monotonic() + 4500
    fixtures = [Path(f) for f in fixtures]
    if len(fixtures) != 4:
        raise ValueError('FOUR_QUERY_SUITES_REQUIRED')
    guard()
    registry.require_not_quarantined(parent['candidate_id'])
    if registry.get(parent['candidate_id']) != parent:
        raise ValueError('QUERY_PARENT_CHANGED')
    if public.get('parent_code') != selector(parent['worker'].decode())[1]:
        raise ValueError('QUERY_PUBLIC_PARENT_CODE_CHANGED')
    freezes = {str(f.resolve()): adapters(f)[0](f, expected_count=20) for f in fixtures}
    public = deepcopy(public)
    if handbook is not None:
        if not compatibility_fingerprint:
            raise ValueError('HANDBOOK_COMPATIBILITY_REQUIRED')
        entries = []
        for section in ('dedup', 'budget', 'order', 'format'):
            entries.extend(handbook.retrieve(section, format, features or {}, compatibility_fingerprint))
        entries = sorted({e['entry_id']: e for e in entries}.values(), key=lambda e: e['entry_id'])[:3]
        public['handbook_entries'] = entries
        public['handbook_entry_ids'] = [e['entry_id'] for e in entries]
        validate_handbook_summaries(entries, public['handbook_entry_ids'])
    scanner.check(public)
    frontier = experience_session.verify() if experience_session is not None else None
    models = {r: getattr(roles, 'models', {}).get(r, 'gpt-6-luna')
              for r in ('diagnose',) + tuple(roles_for(role_profile).values())}
    binding = strategy_input_binding(public, freezes, role_profile, compatibility_fingerprint, frontier,
        proposal_protocol_profile=getattr(roles, 'seam_profile', 'SYNTHETIC'),models=models)
    atomic_write(directory / 'public-context.json', canonical(public), exclusive=True)
    atomic_write(directory / 'fixture-freezes.restricted.json', canonical(freezes), exclusive=True)
    artifacts = {}
    observed_outputs = {}
    journal = StageJournal(directory / 'stages')

    def review(proposal):
        guard()
        slot = proposal['angle']
        output = directory / slot
        output.mkdir()
        atomic_write(output / 'proposal.json', canonical(proposal), exclusive=True)
        try:
            worker = apply_patch(parent['worker'], proposal['worker_patch'])
            compile(worker, '<query-proposal>', 'exec')
            if worker == parent['worker']:
                raise ValueError('EMPTY_PATCH')
        except (ValueError, SyntaxError) as error:
            atomic_write(output / 'static-error.restricted.json', canonical({'type': type(error).__name__, 'message': str(error)}), exclusive=True)
            from .autonomous import static_patch_reason
            raise StageFailure('SEAM_ERROR', static_patch_reason(parent['worker'], proposal['worker_patch'], error)) from error
        atomic_write(output / 'worker.py', worker, exclusive=True)
        artifact = {'worker': worker, 'output': output}
        artifacts[slot] = artifact
        return artifact

    def replay(proposal, artifact):
        output, worker = artifact['output'], artifact['worker']
        guard()
        capsule = capsule_factory(output / 'capsule')
        capsule.launch_context=launch
        decisions = []
        try:
            capsule.setup()
            probes = output / 'probes'
            probes.mkdir()
            atomic_write(probes / 'worker.py', invariant_probe_source(worker), exclusive=True)
            atomic_write(probes / 'input.json', b'{}', exclusive=True)
            capsule.deadline = min(deadline, time.monotonic() + 90)
            try:
                unit = capsule.child('prepare', [probes / 'input.json', probes / 'capture.json'], probes,
                    worker=probes / 'worker.py', business_root=capsule.business_root)
            except ValueError as error:
                log = probes / 'worker.log'
                if log.exists() and 'QUERY_' in log.read_text(encoding='utf-8', errors='replace'):
                    raise StageFailure('SEAM_ERROR', 'QUERY_UNIT_FAILED') from error
                raise StageFailure('REPLAY_ERROR', 'QUERY_UNIT_INFRA_FAILURE') from error
            if unit.get('status') != 'PASS':
                raise StageFailure('SEAM_ERROR', 'QUERY_UNIT_FAILED')
            try:
                evidence = {
                        'round_id': public['round_id'], 'slot': proposal['angle'],
                        'proposal_sha256': bytes_hash((output / 'proposal.json').read_bytes()),
                        'fixture_freezes_sha256': bytes_hash(canonical(freezes))}
                expected = digest({'schema': PROFILE, 'parent_id': parent['candidate_id'],
                    'origin': 'OPENAI_MODEL_PROPOSAL', 'worker_sha256': bytes_hash(worker),
                    'config_sha256': bytes_hash(parent['config']), 'evidence': evidence, 'production_changed': False})
                def register(_):
                    return {'candidate_id': registry.put(worker, parent['config'], parent_id=parent['candidate_id'],
                        origin='OPENAI_MODEL_PROPOSAL', evidence=evidence)}
                def registered(_):
                    try: existing = registry.get(expected)
                    except (FileNotFoundError, KeyError): return {'status': 'ABSENT', 'safe_retry': True}
                    return {'status': 'MATCHED', 'output': {'candidate_id': existing['candidate_id']}}
                registered_result, _ = journal.execute('registry-' + proposal['angle'],
                    {'parent_id': parent['candidate_id'], 'worker_sha256': bytes_hash(worker), 'evidence': evidence},
                    register, registered)
                cid = registered_result['candidate_id']
                child = registry.get(cid)
            except (ValueError, OSError) as error:
                raise StageFailure('REGISTRY_ERROR', 'QUERY_REGISTRY_FAILURE') from error
            atomic_write(output / 'binding.json', canonical({'candidate_id': cid,
                'parent_id': parent['candidate_id'], 'proposal_id': proposal['proposal_id'],
                'angle': proposal['angle'], 'worker_sha256': bytes_hash(worker)}), exclusive=True)
            atomic_write(output / 'fixture-freezes.restricted.json', canonical(freezes), exclusive=True)
            for index, fixture in enumerate(fixtures, 1):
                guard()
                if adapters(fixture)[0](fixture, expected_count=20) != freezes[str(fixture.resolve())]:
                    raise StageFailure('STORAGE_ERROR', 'FIXTURE_CHANGED_DURING_PROPOSAL')
                run = output / f'suite-{index}'
                def ordinary(_):
                    return adapters(fixture)[1](capsule, fixture, {'A': parent, 'B': child}, run,
                        deadline=deadline, schema='rsi-query-aware-engineering/v1',
                        scope='ordinary query training; auxiliary suites are fact gates, not independent samples')
                def reconciled(_):
                    if not run.exists(): return {'status': 'ABSENT', 'safe_retry': True}
                    if not (run / 'results.json').is_file() or not (run / 'manifest.json').is_file():
                        return {'status': 'UNKNOWN'}
                    audited = adapters(fixture)[2](run, fixture, cid,
                        {'angle': 'P2_BUDGET', 'proposal_id': 'rsi-query-aware-engineering/v1'},
                        bytes_hash(worker), bytes_hash(parent['config']), evidence=capsule.evidence_policy)
                    return {'status': 'MATCHED', 'output': audited}
                decision, _ = journal.execute(proposal['angle'] + '-suite-' + str(index),
                    {'fixture': freezes[str(fixture.resolve())], 'parent_id': parent['candidate_id'],
                     'candidate_id': cid}, ordinary, reconciled)
                if proposal['angle'] == 'P3_ORDER' and preserves_selection_order(role_profile):
                    assert_order_angle(run)
                decisions.append(decision)
                atomic_write(output / f'decision-{index}.json', canonical(decision), exclusive=True)
                for row in decision.get('per_question', []):
                    if not row.get('context_B_sha256') or not row.get('upstream_hash'):
                        continue
                    key = (bytes_hash(worker), index, row['qid'], row['upstream_hash'])
                    value = (row.get('context_B_sha256'), row.get('chars_B'), row.get('provenance_B'),
                             row.get('execution_environment_hash'))
                    if key in observed_outputs and observed_outputs[key] != value:
                        raise StageFailure('JUDGE_ERROR', 'DUPLICATE_PATCH_NONDETERMINISM')
                    observed_outputs[key] = value
            primary = summarize_suites(decisions, proposal['angle'], proposal['proposal_id'])
            artifact.update(child=child, suite_decisions=decisions)
            atomic_write(output / 'decision.json', canonical(primary), exclusive=True)
            return primary
        except StageFailure:
            raise
        except OSError as error:
            raise StageFailure('STORAGE_ERROR', 'QUERY_STORAGE_FAILURE') from error
        except (ValueError, RuntimeError, TimeoutError) as error:
            atomic_write(output / 'replay-error.restricted.json', canonical({'type': type(error).__name__, 'message': str(error)}), exclusive=True)
            raise StageFailure('REPLAY_ERROR', 'QUERY_REPLAY_FAILURE') from error
        finally:
            capsule.close()

    class GuardedRoles:
        def __getattr__(self, name):
            return getattr(roles, name)
        def diagnose(self, context):
            guard()
            result = roles.diagnose(context)
            guard()
            return result
        def propose(self, slot, context, diagnosis):
            guard()
            result = roles.propose(slot, context, diagnosis)
            guard()
            return result
    result = execute_round(public, GuardedRoles(), review, replay, enabled_slots=enabled_slots, deadline=deadline, role_profile=role_profile)
    evidence_modes = sorted({d.get('evidence_mode', 'UNKNOWN') for a in artifacts.values() for d in a.get('suite_decisions', [])})
    result['evidence_mode'] = evidence_modes[0] if len(evidence_modes) == 1 else 'UNKNOWN' if evidence_modes else 'NOT_EXECUTED'
    result['real_eligible'] = result['evidence_mode'] == 'REAL_REPLAY' and all(d.get('real_eligible') is True for a in artifacts.values() for d in a.get('suite_decisions', []))
    if 'candidate_decisions' in result:
        result['candidate_decisions'].sort(key=lambda d: SLOTS.index(d['angle']))
    result.update(launch_run_id=launch.run_id if launch else None,launch_plan_sha256=launch.plan_sha256 if launch else None,schema='rsi-query-multi-role/v2.3', parent_id=parent['candidate_id'],
                  role_profile=profile_manifest(role_profile),
                  compatibility_fingerprint=compatibility_fingerprint,
                  experience_frontier=frontier, strategy_input_binding=binding,
                  static_proposal_instances=len(artifacts),
                  ast_unique_strategy_count=len({digest(ast.dump(ast.parse(a['worker']), include_attributes=False)) for a in artifacts.values()}),
                  independent_holdout=False, production_changed=False,
                  enabled_slots=list(enabled_slots), fixture_freezes=freezes,
                  parent_worker_sha256=bytes_hash(parent['worker']),
                  parent_config_sha256=bytes_hash(parent['config']),
                  public_context_sha256=bytes_hash(canonical(public)),
                  public_semantics_sha256=bytes_hash(canonical(public_semantics(public))),
                  role_models=models,
                  seam_profile=getattr(roles, 'seam_profile', 'SYNTHETIC'))
    if 'candidate_decisions' in result and set(d['angle'] for d in result['candidate_decisions']) == set(SLOTS):
        # Family input is the pre-Holm decisions archived by each replay. Reusing
        # adjusted items as inputs would erase their evidence provenance.
        inputs = [load(artifacts[d['angle']]['output'] / 'decision.json')
                  if d['angle'] in artifacts and (artifacts[d['angle']]['output'] / 'decision.json').exists()
                  else deepcopy(d) for d in result['candidate_decisions']]
        family = make_family(inputs, enabled_slots=enabled_slots,
            all_qualified_terminal=set(result['qualified']) == set(result['attempted_replays']),role_profile=role_profile,strategy_binding=binding)
        # The pure coordinator and archival family must agree before any adoption.
        for key in ('verdict', 'adopted_candidate_id', 'adopted_proposal_id', 'candidate_decisions', 'family_size'):
            if family['outcome'].get(key) != result.get(key):
                raise StageFailure('JUDGE_ERROR', 'FAMILY_COORDINATOR_MISMATCH')
        atomic_write(directory / 'family-decision.json', canonical(family), exclusive=True)
        result['family_id'] = family['family_id']
    if result.get('adopted_candidate_id'):
        slot = next(s for s, a in artifacts.items() if a.get('child', {}).get('candidate_id') == result['adopted_candidate_id'])
        selected = artifacts[slot]
        receipt = selected_receipt(family, candidate_id=selected['child']['candidate_id'], selected_slot=slot)
        receipt.update(candidate_id=selected['child']['candidate_id'], parent_id=parent['candidate_id'],
            suite_decisions=selected['suite_decisions'], suite_expected=4,
            coordinator_round_id=public['round_id'], coordinator_selected_slot=slot,
            coordinator_directory=str(directory.resolve()),
            independent_holdout=False, production_changed=False)
        atomic_write(selected['output'] / 'family-decision.json', canonical(family), exclusive=True)
        atomic_write(selected['output'] / 'round.json', canonical(receipt), exclusive=True)
        result['selected_training_directory'] = str(selected['output'].resolve())
    guard()
    atomic_write(directory / 'round.json', canonical(result), exclusive=True)
    if handbook is not None:
        from .query_learning import record_query_round_learning
        def record_learning():
            return record_query_round_learning(handbook=handbook, record=result, public=public,
                                              compatibility_fingerprint=compatibility_fingerprint)
        if experience_session is not None:
            experience_session.learn_round(record_path=directory/'round.json',public_path=directory/'public-context.json',compatibility_fingerprint=compatibility_fingerprint)
        else:
            record_learning()
    # Separate role profile: completed_projection is intentionally legacy-shaped.
    advisory_failures = []
    class IndependentAdvisory:
        scanner = advisory_roles.scanner
        def call(self, role, context):
            try:
                guard()
                return advisory_roles.call(role, context)
            except StageFailure as error:
                if error.global_stop:
                    raise
                advisory_failures.append({'role': role, 'reason_code': error.reason_code})
                return {'status': 'ABSTAIN', 'result': None, 'reason': error.reason_code}
            except TimeoutError:
                advisory_failures.append({'role': role, 'reason_code': 'AGENT_TIMEOUT'})
                return {'status': 'ABSTAIN', 'result': None, 'reason': 'AGENT_TIMEOUT'}
    advisory = run_advisory(IndependentAdvisory(), [directory], directory / 'advisory')
    advisory['role_failures'] = advisory_failures
    atomic_write(directory / 'advisory-summary.json', canonical(advisory), exclusive=True)
    atomic_write(directory / 'completion.json', canonical({'implementation': implementation_hashes(),
        'files': {p.relative_to(directory).as_posix(): bytes_hash(p.read_bytes())
                  for p in sorted(directory.rglob('*')) if p.is_file()}}), exclusive=True)
    return result

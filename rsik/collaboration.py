"""Offline paired collaboration runs using the production coordinator and synthetic adapters."""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
from threading import local

from . import query_workflow
from .adjudicate import SLOTS
from .analytics.reader import read_event_log
from .analytics.projections import project_sources
from .analytics.metrics import compute_metrics
from .budgets import Budget
from .execution_evidence import ExecutionEvidence
from .family_decision import POLICY, validate_family
from .handbook.store import Handbook
from .locking import atomic_write
from .openai_roles import OpenAIRoles, runtime_profile_identity
from .query_aware import QueryRegistry
from .query_learning import ExperienceSession
from .redact import LeakScanner
from .role_profiles import M1_SPECIALIZED_V1, M1_INDEPENDENT_GENERALIST_V1, roles_for
from .state import canonical, digest

PROFILES = (M1_SPECIALIZED_V1, M1_INDEPENDENT_GENERALIST_V1)
SEAM = 'query-autonomous-v1'
MODE = 'SYNTHETIC_CONTROL'
SCHEMA = 'rsi-collaboration-pair/v1'


def _files(directory):
    return {p.relative_to(directory).as_posix(): query_workflow.bytes_hash(p.read_bytes())
            for p in sorted(directory.rglob('*')) if p.is_file()}


def role_models(profile, slot_models):
    if set(slot_models) != {'diagnose', 'curator', 'meta', *SLOTS}:
        raise ValueError('PAIR_MODEL_SLOTS_REQUIRED')
    return {roles_for(profile).get(slot, slot): model for slot, model in slot_models.items()}


def freeze_pair(*, worker, config, public, fixtures, slot_models, budget_usd, memory=(), memory_features=None):
    """Freeze exact shared inputs and effective role identities, without launching."""
    paths = tuple(Path(p).resolve(strict=True) for p in fixtures)
    if len(paths) != 4 or len(set(paths)) != 4 or not all(p.is_dir() for p in paths):
        raise ValueError('PAIR_REQUIRES_FOUR_DISTINCT_FIXTURES')
    from decimal import Decimal
    budget = Decimal(budget_usd)
    if not budget.is_finite() or not 0 < budget <= 5:
        raise ValueError('PAIR_INVALID_BUDGET')
    shared = {
        'worker_sha256': query_workflow.bytes_hash(worker),
        'config_sha256': query_workflow.bytes_hash(config),
        'public_sha256': digest(public),
        'initial_memory_sha256': digest(memory),
        'memory_features_sha256': digest(memory_features or {}),
        'fixture_sha256': [digest(_files(p)) for p in paths],
        'slot_models': dict(slot_models), 'budget_usd': str(budget),
        'family_policy_sha256': digest(POLICY), 'implementation': query_workflow.implementation_hashes(),
    }
    value = {'schema': SCHEMA, 'evidence_mode': MODE, 'shared': shared,
             'profiles': {p: runtime_profile_identity(p, role_models(p, slot_models), SEAM)
                          for p in PROFILES}}
    return {**value, 'freeze_sha256': digest(value)}


class _ScriptedRoles(OpenAIRoles):
    """In-process receipt transport. The private harness selects replies by role."""
    def __init__(self, budget, directory, scanner, script, **kwargs):
        self._script, self._role = script, local()
        super().__init__(budget, directory, directory / 'no-provider-key', scanner,
                         transport=self._receipt, **kwargs)

    def call(self, role, public, diagnosis=None):
        self._role.value = role
        return super().call(role, public, diagnosis)

    def _receipt(self, body, *_):
        context = json.loads(body['input'][1]['content'])
        value = self._script(self._role.value, context, deepcopy(body))
        return {'transport_status': 'RECEIVED', 'http_status': 200, 'response': {
            'model': body['model'], 'status': 'completed',
            'usage': {'input_tokens': 100, 'output_tokens': 20},
            'output': [{'type': 'message', 'content': [
                {'type': 'output_text', 'text': canonical(value).decode()}]}]}}


def _arm_summary(arm, result, budget, observer):
    ledgers = [read_event_log(observer.path), read_event_log(budget.log.path)]
    records = project_sources(ledgers)
    events = observer.log.read()
    proposals = [r for r in records if r['projection_type'] == 'Proposal']
    valid = [r for r in proposals if r['payload'].get('schema_valid') is True]
    qualified = [r for r in valid if r['payload'].get('static_pass') is True]
    ast_ids = [r['payload'].get('normalized_ast_sha256') for r in qualified]
    known_asts = all(v not in (None, 'UNKNOWN') for v in ast_ids)
    family_path = arm / 'round' / 'family-decision.json'
    family = json.loads(family_path.read_bytes()) if family_path.is_file() else None
    if family:
        validate_family(family)
    attempted = result.get('attempted_replays', [])
    completed = [slot for slot in attempted if all((arm/'round'/slot/f'decision-{n}.json').is_file() for n in range(1,5))]
    invocations = [e['payload'] for e in events if e['event_type'] == 'INVOCATION_FINISHED']
    proposer_roles = set(result.get('proposal_roles', {}).values())
    attempts = sum(e['event_type'] == 'INVOCATION_STARTED' and
                   e['payload'].get('agent_role') in proposer_roles for e in events)
    return {'execution_state': result['execution_state'], 'round_id': result['round_id'],
            'proposal_schema_valid': {'numerator': len(valid), 'denominator': attempts},
            'proposal_static_pass': {'numerator': len(qualified), 'denominator': len(valid)},
            'worker_ast_diversity': {'numerator': len(set(ast_ids)) if known_asts else None,
                                     'denominator': len(qualified)},
            'worker_ast_duplicates': {'numerator': len(ast_ids)-len(set(ast_ids)) if known_asts else None,
                                       'denominator': len(qualified)},
            'independent_proposal_instances': len(proposals),
            'replay_completed': {'numerator': len(completed), 'denominator': len(attempted)},
            'training_adoption': {'numerator': int(bool(result.get('adopted_candidate_id'))),
                                  'denominator': len(completed) if family and family['all_qualified_terminal'] else None},
            'family_id': family['family_id'] if family else None,
            'family_slots': family['slots'] if family else [],
            'independent_holdout': 'UNKNOWN',
            'invocations_by_role': dict(Counter(p.get('agent_role') for p in invocations)),
            'failures': result.get('errors', []), 'invocation_outcomes': dict(Counter(p['outcome'] for p in invocations)),
            'mock_cost': budget.snapshot(), 'sources': [s['sha256'] for s in ledgers],
            'analytics': compute_metrics(records)}


def run_offline_pair(*, directory, frozen, worker, config, public, fixtures, slot_models,
                     budget_usd, response_script, capsule_factory, memory=(), memory_features=None):
    """Compare two synthetic arms using a shared frozen input and separate state.

    The response script gets (profile, statistical_slot_or_advisor, context, body).
    A synthetic capsule and fixture adapter provide deterministic replay evidence.
    """
    fixtures, memory = tuple(fixtures), tuple(memory)
    inputs = dict(worker=worker, config=config, public=public, fixtures=fixtures,
                  slot_models=slot_models, budget_usd=budget_usd, memory=memory, memory_features=memory_features)
    def check_frozen():
        if freeze_pair(**inputs) != frozen:
            raise ValueError('PAIR_FREEZE_CHANGED')
    check_frozen()
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=False)
    atomic_write(directory/'freeze.json', canonical(frozen), exclusive=True)
    scanner = LeakScanner()
    scanner.check(public)
    summary = {}
    diagnoses = []
    public_hashes = []
    for profile in PROFILES:
        check_frozen()
        arm = directory / profile
        registry = QueryRegistry(arm/'registry')
        parent_id = registry.put(worker, config, parent_id=None, origin=MODE, evidence={})
        parent = registry.get(parent_id)
        budget = Budget(arm/'mock-budget.sqlite', limit_usd=budget_usd)
        book = Handbook(arm/'handbook', scanner=scanner, evidence_mode=MODE)
        for index, (delta, context) in enumerate(memory):
            book.commit_delta(deepcopy(delta), deepcopy(context), idempotency_key=f'initial-{index}',
                              created_at='2000-01-01T00:00:00+00:00')
        session = ExperienceSession(arm/'learning', book)
        mapped = role_models(profile, slot_models)
        slots = {role: slot for slot, role in roles_for(profile).items()}
        def script(role, context, body):
            return response_script(profile, slots.get(role, role), context, body)
        roles = _ScriptedRoles(budget, arm/'requests', scanner, script, role_profile=profile,
                               seam_profile=SEAM, models=mapped, runtime_identity=frozen['profiles'][profile])
        advisory = _ScriptedRoles(budget, arm/'advisory', scanner, script, role_profile=profile,
                                  seam_profile=SEAM, models=mapped, runtime_identity=frozen['profiles'][profile])
        context = deepcopy(public)
        context['round_id'] = 'round-' + digest([public['round_id'], profile])[:32]
        result = query_workflow.run_query_round(
            public=context, roles=roles, advisory_roles=advisory, parent=parent, fixtures=fixtures,
            registry=registry, directory=arm/'round', capsule_factory=capsule_factory, scanner=scanner,
            handbook=book, experience_session=session, features=memory_features,
            compatibility_fingerprint=digest('paired-memory/v1'),
            role_profile=profile, runtime_identity=frozen['profiles'][profile],
            evidence=ExecutionEvidence.synthetic_control())
        diagnoses.append(result.get('diagnosis'))
        public_hashes.append(result['public_semantics_sha256'])
        summary[profile] = _arm_summary(arm, result, budget, roles.observer)
    check_frozen()
    match = (None not in diagnoses and diagnoses[0] == diagnoses[1]
             and public_hashes[0] == public_hashes[1])
    report = {'schema': SCHEMA, 'evidence_mode': MODE, 'freeze_sha256': frozen['freeze_sha256'],
              'paired_input_matched': match, 'status': 'SYNTHETIC_ONLY' if match else 'INPUT_MISMATCH',
              'public_semantics_sha256': dict(zip(PROFILES, public_hashes)),
              'diagnosis_sha256': {p: digest(d) for p, d in zip(PROFILES, diagnoses) if d is not None},
              'profiles': summary, 'real_model_calls': 0}
    atomic_write(directory/'report.json', canonical(report), exclusive=True)
    return report

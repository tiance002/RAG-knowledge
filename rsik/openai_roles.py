"""Cost-first OpenAI roles with private receipts and post-usage billing.

Only explicitly projected public context is accepted. A known, billed malformed
response can be repaired once; transport uncertainty never causes a resend.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import subprocess
import sqlite3
import re
import sys
import time
import threading
from copy import deepcopy

from .contracts import (ANGLES, ContractError, role_schema, unwrap_response,
                        validate_diagnosis, validate_proposal, validate_readonly,validate_completed_candidate)
from .errors import StageFailure
from .http_worker import request_wire_bytes
from .locking import atomic_write
from .state import canonical, digest
from .response_audit import (INCOMPLETE_PATCH, INCOMPLETE_STRUCTURED_OUTPUT,
                             inspect as audit_response, patch_output_incomplete)
from .token_budget import LEGACY as LEGACY_TOKEN_BUDGET, APPROVED_DEV_V0, profile_identity, role_limit


POLICY = '''You are one isolated role in a deterministic Context Selection experiment.
Only aggregate counts and the permitted parent function/interface are available. Do not infer
unseen per-question answers, claim experiments ran, invent quotes or source facts.
Use only supplied aliases and exact evidence strings. Confidence is recorded only.
Return the strict JSON envelope. A proposal is a falsifiable experiment, not a
claim of correctness or improvement. Deterministic replay checks necessary facts.
Do not require advance access to hidden necessity labels before proposing a
structural ablation; explicitly record risk and falsification. ABSTAIN is correct
if the edit violates the seam or is unsupported even as an experiment.
Only select_member_indices function body may change. No imports,
globals, mutation of arguments, indirect calls, or new attributes except get/append.
Allowed calls: require, duplicate_key, any, all, range, len, enumerate, list, tuple,
set, sorted, min, max, sum, abs. Append only to a fresh private list. Keep all seed
members. Binary arithmetic permits only + and -. Multiplication, division and
floor division are forbidden AST nodes: write len(x) + len(x), never 2 * len(x).
Handle empty members and empty seed lists without raising: use max(values or [0]).
Never assign through a subscript or attribute, even on a new private dictionary.
Use one longest-seed reference computed by a list comprehension, not a mutable map.
Keep hypothesis/change/impact/risk concise, at most two sentences each.
Preserve original membership order: the frozen evaluator rejects reorder.
Members/chunks/files are existing input mappings; never hardcode dataset identity.
Do not add unsupported fields or repeat any hidden identifiers.
'''
TASKS = {'diagnose': 'Identify the earliest supported observation and a falsifiable code-policy hypothesis; separate a hypothesis from a proven defect. Nonseed overhead is an observed count, not proven waste. You may identify a testable policy limitation from the supplied selector even without per-member labels: report missing detail in required_data and state uncertainty. Do not require proof of a gain before proposing a hypothesis; only the later frozen replay can establish that. If fixable in SELECTION, set the seam flag accordingly. ABSTAIN remains valid when no admissible hypothesis is available.',
         'P1_DEDUP': 'Deduplication only: preserve independent evidence and seed identity. Do not remove near-duplicates without evidence. The supplied parent_selection identifies the actual active config. You may test using the existing trusted duplicate_key equality predicate more consistently across admitted configurations; this is a hypothesis, not proof that duplicates occur in the unseen fixture. All experiments face frozen replay and may correctly show NO_GAIN.',
         'P2_BUDGET': 'Budget allocation only. Follow experiment_intent: SEED_ONLY_ABLATION tests retaining all seeds and no nonseeds; SHORT_NONSEED_BUDGET tests retaining seeds plus nonseeds <=200 content chars; BALANCED_NEIGHBOR_BUDGET tests a general per-seed neighbor budget; SUBSTANTIAL_NONSEED_BUDGET tests preserving substantial neighboring members while filtering short nonseed tails, using a general reference such as half the longest seed content length (compare doubled length rather than division, which AST disallows). Never assume nonseeds are unnecessary: adjacent members can contain required facts. These are diagnostic experiments, not asserted safe optimizations: explain possible necessary-fact loss and require rejection on any loss. Do not ask to see Gold or hardcode case identities. STRUCTURAL_POLICY_SEARCH allows a justified general policy. Preserve original order and all seeds. A patch can fail; the deterministic judge, not you, establishes facts and gain.',
         'P3_ORDER': 'Ordering only, with invariant selected-member set. Frozen evaluator also requires original relative order. If no admissible meaningful edit exists, abstain; do not silently change selection.',
         'curator': 'Read-only completed-experiment curation. Generate a Handbook Delta only from traceable completed candidate experiments. Aggregate no-candidate/ABSTAIN counts cannot support a selection action: ABSTAIN in that case. Never assign helpful/harmful/state, invent VERIFIED status or create a causal rule from absence of evidence. No evaluator edits.',
         'meta': 'Read-only audit. Summarize only what aggregate counts support. Mark missing distribution, currency and false-pass evidence UNKNOWN. Never infer exact causes from UNKNOWN, assert answer quality, inspect holdout answers, or modify fixtures. Findings are advisory and do not change deterministic judgments.'}
TASKS['P2_BUDGET'] += ' For THREE_QUARTER_NONSEED_BUDGET, test a 3/4 longest-seed reference instead of 1/2: retain seeds, or compare four added copies of nonseed length against three added copies of the global longest seed length. Only + and - are legal; do not use multiplication/division or mutable dictionaries. This predefined alternative is selected on training and layout stress, before any independent holdout outcomes.'
TASKS['P2_BUDGET'] += ' For NINE_TENTHS_NONSEED_BUDGET, raise the actual current threshold to 9/10: compare sum([length for _ in range(10)]) >= sum([longest_seed for _ in range(9)]), never multiplication. For FULL_SEED_NONSEED_BUDGET use nonseed length >= longest seed length. For PRECEDING_NEIGHBOR_BUDGET retain all seeds and only preceding nonseeds that the current parent policy would keep: determine preceding from original_index and same file, never from chunk-id patterns. Preserve every existing parent restriction, do not reintroduce previously filtered members. These hypotheses may fail facts or statistics and must be reported as failures rather than forced through.'
TASKS['curator'] += ' When the supplied public code is a verified short-tail budget policy, the finite action FILTER_SHORT_NONSEED_TAILS has target NONSEED_MEMBERS and params reference=LONGEST_SEED_CONTENT_LENGTH,numerator,denominator. Record only the ratio implemented by that code. Scope to OTHER format and same_source=true; no unknown features. Copy supplied compatibility_fingerprint exactly and use supplied evidence refs as support_refs. No causal success-rate or answer-quality claim.'
TASKS['curator'] += ' completed_candidate, when present, is a trusted workflow attestation binding the displayed code hash, implemented action, evidence aliases and completed VERIFIED/robustness status. The aliases have private observation/candidate/parent/run/source-group receipts. You may propose one appropriately limited ADD Delta from this completed evidence; you do not set ACTIVE or helpful counts.'


from .role_profiles import LEGACY, DEPENDENCY, DEPENDENCY_TASK, roles_for, profile_manifest

ALLOWED_MODELS = frozenset({'gpt-6-luna', 'gpt-6.1-sol', 'gpt-6.2-sol'})
MODEL_REASONING_EFFORT = {'gpt-6-luna': 'none', 'gpt-6.1-sol': 'low', 'gpt-6.2-sol': 'medium'}


def reasoning_effort(model):
    try:
        return MODEL_REASONING_EFFORT[model]
    except KeyError as exc:
        raise ValueError('MODEL_NOT_ALLOWED') from exc


def _validate_decode_overrides(models, overrides, *, launch=None, max_attempts=2):
    """Validate bounded DEV decode controls without opening the formal launch path."""
    if type(max_attempts) is not int or max_attempts not in (1, 2):
        raise ValueError('MODEL_ATTEMPT_LIMIT_NOT_ALLOWED')
    overrides = dict(overrides or {})
    if launch is not None and (overrides or max_attempts != 2):
        raise ValueError('DEV_DECODE_OVERRIDES_NOT_ALLOWED_WITH_FORMAL_LAUNCH')
    if set(overrides) - set(TASKS):
        raise ValueError('MODEL_DECODE_OVERRIDE_NOT_ALLOWED')
    for role, settings in overrides.items():
        if not isinstance(settings, dict) or not settings or set(settings) - {'reasoning_effort', 'max_output_tokens'}:
            raise ValueError('MODEL_DECODE_OVERRIDE_NOT_ALLOWED')
        model = models.get(role, 'gpt-6-luna')
        if 'reasoning_effort' in settings and (model, settings['reasoning_effort']) != ('gpt-6-luna', 'max'):
            raise ValueError('MODEL_DECODE_OVERRIDE_NOT_ALLOWED')
        if 'max_output_tokens' in settings and type(settings['max_output_tokens']) is not int:
            raise ValueError('MODEL_DECODE_OVERRIDE_NOT_ALLOWED')
        if 'max_output_tokens' in settings and settings['max_output_tokens'] not in {1024, 2048, 3072, 4096}:
            raise ValueError('MODEL_DECODE_OVERRIDE_NOT_ALLOWED')
    return overrides
TASKS['P3_DEPENDENCY'] = DEPENDENCY_TASK
from .role_profiles import M1_ADAPTIVE_V1, M1_INDEPENDENT_GENERALIST_V1, M1_PROFILES, M1_SAFETY_POLICY, m1_tasks
for _profile in M1_PROFILES:
    for _role, _task in m1_tasks(_profile).items():
        if _role not in TASKS: TASKS[_role] = _task
TASKS['curator'] += ' A trusted completed_candidate may attest REUSE_VERIFIED_SELECTOR with code_sha256. This is an association scoped to that exact selector and compatible evidence, never proof of a causal or general algorithmic rule.'

def single_request(body, key_file, timeout, *, response_path=None):
    """A process, not a cancellable Future, bounds the local HTTP request."""
    process = subprocess.Popen([sys.executable, '-B', '-m', 'rsik.http_worker'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[1])
    try:
        output, _ = process.communicate(json.dumps({'body': body, 'key_file': str(key_file),
            'timeout': timeout,'response_path':str(Path(response_path).resolve()) if response_path else None},
            ensure_ascii=True).encode('ascii'), timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=5)
        if response_path and Path(response_path).is_file():
            return json.loads(Path(response_path).read_bytes())
        return {'transport_status': 'TIMEOUT', 'local_process_terminated': process.poll() is not None}
    if response_path and Path(response_path).is_file():
        return json.loads(Path(response_path).read_bytes())
    if process.returncode != 0 or len(output) > 2_100_000:
        return {'transport_status': 'UNKNOWN', 'local_exit_code': process.returncode}
    try:
        return json.loads(output)
    except (ValueError, TypeError):
        return {'transport_status': 'UNKNOWN'}


RECENT_HISTORY_REFERENCE = (
    'A history object containing recent_history_index refers to the zero-based item in search_history. '
    'The memory entry carries the selector identity, attempt count and round range.'
)


class OpenAIRoles:
    def __init__(self, budget, directory, key_file, scanner, *, deadline=None, transport=single_request, models=None, decode_overrides=None, max_attempts=2, service_tier=None, request_guard=None, seam_profile='legacy', before_request=None, role_profile=LEGACY, strategy_context=None, launch=None, observer=None, runtime_identity=None, token_budget_profile=LEGACY_TOKEN_BUDGET):
        self.budget, self.directory = budget, Path(directory)
        self.key_file, self.scanner = Path(key_file), scanner
        self.deadline, self.transport = deadline, transport
        self.before_request=before_request
        if seam_profile not in {'legacy','query-aware-v1','query-autonomous-v1'}:raise ValueError('UNKNOWN_SEAM_PROFILE')
        self.seam_profile=seam_profile
        self.role_profile=role_profile
        self.strategy_context=strategy_context
        self.launch=launch
        roles_for(role_profile)
        self.models = dict(models or {})
        if set(self.models)-set(TASKS) or any(model not in ALLOWED_MODELS for model in self.models.values()):
            raise ValueError('MODEL_NOT_ALLOWED')
        self.max_attempts = max_attempts
        self.decode_overrides = _validate_decode_overrides(
            self.models, decode_overrides, launch=launch, max_attempts=max_attempts)
        if service_tier not in (None, 'default') or (launch is not None and service_tier is not None):
            raise ValueError('DEV_SERVICE_TIER_NOT_ALLOWED_WITH_FORMAL_LAUNCH')
        self.service_tier = service_tier
        if request_guard is not None and (not callable(request_guard) or launch is not None):
            raise ValueError('DEV_REQUEST_GUARD_NOT_ALLOWED_WITH_FORMAL_LAUNCH')
        self.request_guard = request_guard
        self.token_budget = profile_identity(token_budget_profile)
        if token_budget_profile != LEGACY_TOKEN_BUDGET and (
                launch is not None or service_tier != 'default' or request_guard is None or self.decode_overrides):
            raise ValueError('TOKEN_BUDGET_PROFILE_REQUIRES_EXPLICIT_PREFLIGHT')
        if token_budget_profile == APPROVED_DEV_V0 and max_attempts != 1:
            raise ValueError('TOKEN_BUDGET_PROFILE_FORBIDS_REPAIR')
        self.runtime_identity = None
        if role_profile in M1_PROFILES:
            self.runtime_identity = (runtime_profile_identity(role_profile, self.models, seam_profile,
                                                               self.decode_overrides, self.service_tier, token_budget_profile)
                                     if runtime_identity is None else deepcopy(runtime_identity))
            validate_runtime_profile_identity(self.runtime_identity, role_profile, self.models, seam_profile,
                                              self.decode_overrides, self.service_tier, token_budget_profile)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._observer = observer
        self._observer_lock = threading.RLock()
        self._observation_local = threading.local()

    @property
    def observer(self):
        # Workflows attach their shared observer after constructing role clients.
        # Delay private storage until a standalone client actually needs it.
        with self._observer_lock:
            if self._observer is None:
                from .observability import Observer
                self._observer = Observer(self.directory / "observations.sqlite")
            return self._observer

    @observer.setter
    def observer(self, value):
        with self._observer_lock:
            self._observer = value

    @property
    def observation_invocation(self):
        """Latest attempt on this thread; never part of model-visible input."""
        return dict(getattr(self._observation_local, "invocation", {}))

    def diagnose(self, public):
        return self.call('diagnose', public)

    def propose(self, angle, public, diagnosis, *, assignment=None):
        if assignment is None:
            return self.call(angle, public, diagnosis)
        return self.call(angle, public, diagnosis, assignment=assignment)

    def call(self, role, public, diagnosis=None, *, assignment=None):
        try:
            return self._call(role, public, diagnosis, assignment=assignment)
        except TimeoutError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise StageFailure('STORAGE_ERROR', 'MODEL_EVIDENCE_STORAGE_UNAVAILABLE') from error

    def _call(self, role, public, diagnosis=None, *, assignment=None):
        self._observation_local.invocation = {}
        if self.role_profile in M1_PROFILES:
            validate_runtime_profile_identity(self.runtime_identity, self.role_profile, self.models, self.seam_profile,
                                              self.decode_overrides, self.service_tier, self.token_budget['version'])
        if (self.role_profile in M1_PROFILES or role.endswith('_GENERALIST')) and role not in (
                'diagnose', 'curator', 'meta', *roles_for(self.role_profile).values()):
            raise ValueError('ROLE_PROFILE_REQUIRED')
        if role == 'P3_DEPENDENCY' and self.role_profile not in (DEPENDENCY, *M1_PROFILES):
            raise ValueError('ROLE_PROFILE_REQUIRED')
        if role not in TASKS:
            raise ValueError('unsupported role')
        allowed = {'round_id', 'parent_alias', 'observations', 'observation_refs', 'evidence_quotes',
                   'parent_code', 'handbook_entry_ids'}
        extra={'search_history','search_memory'} if self.seam_profile=='query-autonomous-v1' else set()
        if not isinstance(public, dict) or set(public)-allowed-{'parent_selection','handbook_entries','selector_contract','experiment_intent','compatibility_fingerprint','completed_candidate'}-extra or not allowed<=set(public):
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        from .selector_contract import interface_contract,INTENTS
        if public.get('experiment_intent','').startswith('QUERY_') and self.seam_profile not in {'query-aware-v1','query-autonomous-v1'}:
            raise ContractError('QUERY_PROFILE_REQUIRED')
        if self.seam_profile in {'query-aware-v1','query-autonomous-v1'}:
            from .query_aware import interface_contract
        if self.seam_profile=='query-autonomous-v1' and not (self.role_profile in M1_PROFILES and role in {'curator','meta'}):
            from .query_aware import autonomy_interface_contract as interface_contract
            from .autonomy_contract import validate_history,validate_search_memory
            if public.get('experiment_intent')!='STRUCTURAL_POLICY_SEARCH':raise ContractError('PRESELECTED_STRATEGY_FORBIDDEN_IN_AUTONOMY_TEST')
            validate_history(public.get('search_history',[]))
            if 'search_memory' in public:
                validate_search_memory(public['search_memory'], recent=public.get('search_history',[]))
        if ('selector_contract' in public and public['selector_contract']!=interface_contract()
                or 'experiment_intent' in public and public['experiment_intent'] not in INTENTS):
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        if 'compatibility_fingerprint' in public and (not isinstance(public['compatibility_fingerprint'],str)
                or not re.fullmatch(r'[0-9a-f]{64}',public['compatibility_fingerprint'])):
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        if 'completed_candidate' in public:validate_completed_candidate(public['completed_candidate'],public,role)
        if (not re.fullmatch(r'parent-[0-9a-f]{32}',public['parent_alias'])
                or not re.fullmatch(r'round-[0-9a-f]{32}',public['round_id'])):
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        if 'parent_selection' in public and public['parent_selection'] not in {'all','exact_duplicate'}:
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        # Reconstruct and recursively validate the allowlist at the final send
        # boundary; checking only the top level would permit nested leakage.
        from .redact import make_visible
        rebuilt = make_visible(public['observations'], parent_code=public['parent_code'],
            round_id=public['round_id'], handbook_entry_ids=public['handbook_entry_ids'], scanner=self.scanner)
        if 'handbook_entries' in public:
            from .redact import validate_handbook_summaries
            validate_handbook_summaries(public['handbook_entries'],public['handbook_entry_ids'])
        ids=public['handbook_entry_ids']
        if (not isinstance(ids,list) or len(ids)>3 or len(ids)!=len(set(ids))
                or any(not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{64}',v) for v in ids)
                or set(ids)!={e['entry_id'] for e in public.get('handbook_entries',[])}):
            raise ContractError('UNAUTHORIZED_HANDBOOK_REFERENCE')
        if (public['observation_refs'] != rebuilt['observation_refs']
                or public['evidence_quotes'] != rebuilt['evidence_quotes']):
            raise ContractError('UNAUTHORIZED_PROJECTION_FIELD')
        if diagnosis is not None:
            validate_diagnosis(diagnosis, public, adaptive=self.role_profile == M1_ADAPTIVE_V1)
        if assignment is not None:
            from .adaptive_dispatch import validate_assignment
            if self.role_profile != M1_ADAPTIVE_V1 or role not in roles_for(self.role_profile).values():
                raise ContractError('UNAUTHORIZED_ASSIGNMENT')
            validate_assignment(assignment, public, diagnosis)
            expected_role = roles_for(self.role_profile)[assignment['slot']]
            if role != expected_role or assignment['task_type'] == 'ABSTAIN':
                raise ContractError('UNAUTHORIZED_ASSIGNMENT')
        elif self.role_profile == M1_ADAPTIVE_V1 and role in roles_for(self.role_profile).values():
            raise ContractError('ADAPTIVE_ASSIGNMENT_REQUIRED')
        for attempt in range(self.max_attempts):
            if self.transport is single_request:
                from .launch import validate_launch
                options = ({'role_profile': self.role_profile, 'runtime_identity': self.runtime_identity}
                           if self.role_profile in M1_PROFILES else {})
                validate_launch(self.launch, boundary="MODEL_REQUEST", **options)
            if self.before_request:self.before_request()
            context = {'public': deepcopy(public), 'diagnosis': diagnosis}
            if assignment is not None: context['assignment'] = deepcopy(assignment)
            if attempt:
                context['repair_instruction'] = 'Previous output violated the local contract. Return a valid response or ABSTAIN.'
            model = self.models.get(role, 'gpt-6-luna')
            if self.transport is single_request:
                configured=json.loads(Path(self.launch.plan_path).read_bytes())['models']
                if configured.get(role)!=model:raise ValueError('LAUNCH_MODEL_IDENTITY_MISMATCH')
            policy=POLICY
            task=TASKS[role]
            if self.role_profile==DEPENDENCY:
                if role=='diagnose': task+=' Classify code defects, no gain, fact regression, infrastructure, statistical insufficiency and evaluation coverage separately. Supply evidence references and falsifiable hypotheses.'
                if role=='P3_DEPENDENCY': task=DEPENDENCY_TASK
            if self.seam_profile=='query-autonomous-v1':
                policy=policy.replace('Use one longest-seed reference computed by a list comprehension, not a mutable map.\n','')
                policy+='\nFrozen autonomous-test profile. Keep six arguments (members, chunks, files, seed_ids, selection, query). The actual runtime query is read-only, never supplied in the proposal context. Preserve every seed, original order and all members for a query shorter than two characters. The outer monitor supplies no optimization formula. Choose your own falsifiable hypothesis from the actual parent and aggregate replay history. Previous failed hypotheses are not instructions to repeat. Do not claim source facts or per-question explanations absent from the projection. No imports, data access, tools, evaluator changes, or system-code changes.\n'
                if any('recent_history_index' in entry['history']
                       for entry in public.get('search_memory', {}).get('entries', [])):
                    policy += '\n' + RECENT_HISTORY_REFERENCE
                if role=='P2_BUDGET':
                    task='Independently design a context-selection improvement within the supplied six-argument seam. Use the diagnosis and prior observed outcomes to choose and implement the next hypothesis; do not wait for the monitor to prescribe an algorithm or threshold. Passing parent checks does not establish optimality. Candidate changes are unproven until deterministic replay. Do not weaken seed, order, source, fact or budget constraints. If a necessary change lies outside the seam, state that limitation honestly; do not change the surrounding system.'
            if self.seam_profile=='query-aware-v1':
                policy+='\nIndependent owner-authorized query-aware seam: keep SIX arguments (members, chunks, files, seed_ids, selection, query). query is the actual runtime question string; never supplied in this proposal context. Patch only this six-argument function body; original five-argument registry is unchanged. QUERY_OVERLAP_ABLATION tests retaining all seeds and a nonseed when any two-character substring query[j:j+2] occurs in its content. Use range(max(0,len(query)-1)). Mandatory short-query guard after the no-seeds guard: if len(query) < 2: return list(range(len(members))). Use this literal comparison, not an algebraic substitute; a one-character question must retain all members. QUERY_OVERLAP_CONSERVATIVE additionally retains neighbors whose content length is <=200 or >= longest seed length, plus query matches and seeds. This is an unproven lexical hypothesis that may lose evidence; deterministic source facts must reject any loss. Do not assume lexical match proves factual sufficiency.\n'
                policy+=' For QUERY_OVERLAP_ZERO_MATCH_FALLBACK, retain seeds plus two-character query-overlap members as in the ablation, BUT if no member at all (including seeds) contains any two-character substring of query, retain ALL members. This explicit uncertainty fallback tests the hypothesis that lexical evidence is unavailable, rather than treating absence of a lexical match as proof of irrelevance. Compute a fresh private list of boolean matches, test not any(matches), then return all indices or the seed/match indices. Keep the mandatory len(query)<2 fallback. This new intent is an adaptive training hypothesis; no guaranteed safety claim.'
                policy+=' For QUERY_SEED_MATCH_FALLBACK, retain all when no SEED member contains a query bigram; otherwise seeds plus matching members. For QUERY_NONSEED_MATCH_FALLBACK, retain all when no NONSEED member contains a query bigram; otherwise seeds plus matching members. For QUERY_TRIGRAM_ZERO_MATCH_FALLBACK use three-character substrings, keep all if len(query)<3 or no member matches any trigram, otherwise seeds plus matching members. For QUERY_BIGRAM_RATIO count distinct query bigrams per member, retain all if every score is zero, otherwise seeds plus members whose score added to itself is at least the highest seed score AND whose score is positive. Always keep all seeds, all members for len(query)<2, and original order. These are separate diagnostic hypotheses, not combinations unless the named intent says so.'
            if self.role_profile in M1_PROFILES:
                prompt_profile = (M1_INDEPENDENT_GENERALIST_V1 if assignment is not None
                    and assignment['assignment_reason'] == 'EXPLICIT_FROZEN_GENERALIST_FALLBACK' else self.role_profile)
                policy, task = effective_role_prompt(prompt_profile, role, self.seam_profile)
            body = {'model': model, 'store': False, **decode_settings(
                        role, model, self.decode_overrides.get(role), token_budget_profile=self.token_budget['version'],
                        repair=bool(attempt)),
                    'input': [{'role': 'system', 'content': policy + '\n' + task},
                              {'role': 'user', 'content': canonical(context).decode('utf-8')}],
                    'text': {'format': {'type': 'json_schema', 'name': 'rsi_role',
                                       'strict': True, 'schema': role_schema(role, adaptive=self.role_profile == M1_ADAPTIVE_V1)}}}
            if self.service_tier is not None:
                body['service_tier'] = self.service_tier
            projection=None
            initial_history = initial_memory = None
            if self.seam_profile=='query-autonomous-v1':
                visible=context['public']
                initial_history=len(visible.get('search_history',[]))
                initial_memory=len(visible.get('search_memory',{}).get('entries',[]))
                # Only optional model-authored history is projected down. Keep
                # current parent, diagnosis, observations and all safety rules.
                while len(request_wire_bytes(body))>16_384:
                    recent=visible.get('search_history',[])
                    memory=visible.get('search_memory',{}).get('entries',[])
                    if len(recent)>1:
                        referenced={entry['history']['recent_history_index'] for entry in memory
                            if isinstance(entry.get('history'),dict)
                            and set(entry['history'])=={'recent_history_index'}}
                        removable=next((index for index in range(len(recent)) if index not in referenced),None)
                        if removable is None:
                            if memory:memory.pop()
                            else:recent.pop(0)
                        else:
                            recent.pop(removable)
                            for entry in memory:
                                history_ref=entry.get('history')
                                if (isinstance(history_ref,dict) and set(history_ref)=={'recent_history_index'}
                                        and history_ref['recent_history_index']>removable):
                                    history_ref['recent_history_index']-=1
                    elif memory:memory.pop()
                    elif recent:recent.pop(0)
                    else:break
                    body['input'][1]['content']=canonical(context).decode('utf-8')
                if self.seam_profile=='query-autonomous-v1':
                    from .autonomy_contract import validate_history,validate_search_memory
                    validate_history(visible.get('search_history',[]))
                    if 'search_memory' in visible:
                        validate_search_memory(visible['search_memory'],recent=visible.get('search_history',[]))
                if (initial_history!=len(visible.get('search_history',[]))
                        or initial_memory!=len(visible.get('search_memory',{}).get('entries',[]))):
                    projection={'schema':'rsi-request-history-projection/v1',
                        'history_before':initial_history,'history_sent':len(visible.get('search_history',[])),
                        'memory_before':initial_memory,'memory_sent':len(visible.get('search_memory',{}).get('entries',[])),
                        'full_local_memory_deleted':False,'mandatory_context_changed':False,
                        'body_bytes':len(canonical(body))}
            self.scanner.check(body)
            wire_body=request_wire_bytes(body)
            if len(wire_body) > 16_384:
                raise ContractError('INPUT_TOO_LARGE')
            if self.request_guard is not None:
                guarded = deepcopy(body)
                self.request_guard(guarded, role)
                if guarded != body:
                    raise ContractError('REQUEST_GUARD_MUTATED_BODY')
            key = public['round_id'] + '-' + role + '-' + str(attempt)
            path = self.directory / (key + '.json')
            request_path = self.directory / (key + '.request.json')
            input_receipt_path = self.directory / (key + '.input-receipt.json')
            request_hash = digest(body)
            search_history = context['public'].get('search_history')
            search_memory = context['public'].get('search_memory')
            search_context = (canonical({'search_history':search_history,
                'search_memory':search_memory}) if self.seam_profile=='query-autonomous-v1' else None)
            input_receipt = canonical({
                'schema':'rsi-request-input-receipt/v1','request_key':key,'role':role,'attempt':attempt,
                'request_hash':request_hash,'request_file':request_path.name,'request_wire_file':request_path.name,
                'request_wire_sha256':hashlib.sha256(wire_body).hexdigest(),
                'request_body_bytes':len(wire_body),
                'search_history_items_before_pruning':initial_history,
                'search_history_items_in_request':len(search_history) if search_history is not None else None,
                'search_memory_entries_before_pruning':initial_memory,
                'search_memory_entries_in_request':len(search_memory.get('entries',[])) if search_memory is not None else None,
                'search_context_canonical_bytes':len(search_context) if search_context is not None else None,
                'user_message_utf8_bytes':len(body['input'][1]['content'].encode('utf-8')),
                'wire_encoding':'UTF-8 JSON','measurement_scope':'EXACT_HTTP_REQUEST_BODY'})
            if input_receipt_path.exists():
                if input_receipt_path.read_bytes()!=input_receipt:
                    raise ContractError('REQUEST_INPUT_RECEIPT_CHANGED')
            elif not request_path.exists() and not path.exists():
                atomic_write(input_receipt_path,input_receipt,exclusive=True)
            if projection:
                projected=self.directory/(key+'.projection.json');data=canonical(projection)
                if projected.exists():
                    if projected.read_bytes()!=data:raise ContractError('REQUEST_PROJECTION_CHANGED')
                else:atomic_write(projected,data,exclusive=True)
            version_path=self.directory/(key+'.strategy.json')
            version_data=canonical({'role_profile':profile_manifest(self.role_profile),'role':role,'prompt_sha256':digest(body['input'][0]),'handbook_entry_ids':list(public['handbook_entry_ids']),'learning_frontier':self.strategy_context() if self.strategy_context else None,'model':model,'seam_profile':self.seam_profile,'request_hash':digest(body)})
            if version_path.exists():
                if version_path.read_bytes()!=version_data:raise ContractError('REQUEST_STRATEGY_CHANGED')
            else:atomic_write(version_path,version_data,exclusive=True)
            observation = {'invocation_id': digest({'directory': str(self.directory.resolve()), 'request_key': key}),
                           'request_key': key, 'cost_request_key': key,
                           'cost_source': digest(str(self.budget.log.path.resolve())), 'round_id': public['round_id'],
                           'agent_role': role, 'attempt': attempt, 'model': model,
                           'prompt_hash': digest(body['input'][0]), 'role_profile': self.role_profile,
                           'role_profile_hash': (self.runtime_identity['sha256'] if self.runtime_identity
                                                 else digest(profile_manifest(self.role_profile))),
                           'request_hash': digest(body), 'seam_profile': self.seam_profile,
                           'requested_output_tokens': body['max_output_tokens'],
                           'token_budget_profile': self.token_budget['version'],
                           'token_budget_sha256': self.token_budget['sha256']}
            observation['attempt_id'] = observation['invocation_id']
            if attempt:
                observation['repair_of'] = public['round_id'] + '-' + role + '-0'
            self._observation_local.invocation = dict(observation)
            with self.observer.invocation(observation) as observed:
                local_timeout = False
                remaining = 90 if self.deadline is None else min(90, self.deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError('AGENT_TIMEOUT')
                if path.exists():
                    starts = [e['payload'] for e in self.budget.log.read() if e['event_type']=='MODEL_REQUEST_STARTED'
                              and e['payload']['request_key']==key]
                    if (not request_path.is_file() or json.loads(request_path.read_text(encoding='utf-8'))!=body
                            or len(starts)!=1 or starts[0]['request_hash']!=digest(body)):
                        raise StageFailure('RECOVERY_ERROR', 'REQUEST_INPUT_CHANGED', global_stop=True)
                    if input_receipt_path.exists() and request_path.read_bytes() != wire_body:
                        raise StageFailure('RECOVERY_ERROR', 'REQUEST_WIRE_CHANGED', global_stop=True)
                    receipt = json.loads(path.read_text(encoding='utf-8'))
                    observed.resume(receipt)
                else:
                    if request_path.exists():
                        raise StageFailure('RECOVERY_ERROR', 'REQUEST_STATUS_UNKNOWN', global_stop=True)
                    # Require durable invocation evidence before any ledger start
                    # or dispatch. A failed START must not create a paid attempt.
                    observed.start()
                    # No fees reserved. Wait for earlier requests near USD5.
                    while True:
                        try:
                            self.budget.start(key, role, body['model'], digest(body))
                            break
                        except RuntimeError as error:
                            if str(error) != 'BUDGET_SERIALIZE_REQUIRED':
                                if str(error)=='BUDGET_EXHAUSTED':
                                    raise StageFailure('AGENT_ERROR','BUDGET_EXHAUSTED',global_stop=True) from None
                                raise StageFailure('AGENT_ERROR', 'BILLING_OR_REQUEST_STATUS_UNKNOWN', global_stop=True) from None
                            if self.deadline is not None and time.monotonic() >= self.deadline:
                                raise TimeoutError('AGENT_TIMEOUT')
                            time.sleep(.05)
                    atomic_write(request_path, wire_body, exclusive=True)
                    try:
                        receipt = (self.transport(body,self.key_file,remaining,response_path=self.directory/(key+'.transport.json'))
                                   if self.transport is single_request else self.transport(body,self.key_file,remaining))
                    except StageFailure:
                        raise
                    except TimeoutError:
                        receipt = {'transport_status': 'UNKNOWN'}
                        local_timeout = True
                        observed.outcome, observed.failure_origin = 'TIMEOUT', 'TRANSPORT'
                    except Exception:
                        receipt = {'transport_status': 'UNKNOWN'}
                    # Persist before billing receipt; recovery can reconcile the
                    # actual saved usage without sending another request.
                    atomic_write(path, canonical(receipt), exclusive=True)
                response = receipt.get('response')
                response = response if isinstance(response, dict) else {}
                usage = response.get('usage') if receipt.get('transport_status') == 'RECEIVED' else None
                output, value = audit_response(receipt)
                if local_timeout:
                    output['output_classification'] = 'TIMEOUT'
                output_kind = output['output_classification']
                state = self.budget.complete(key, usage, status=receipt['transport_status'],
                                             provider_request_id=receipt.get('provider_request_id'))
                # Record actual usage before optional downstream processing can
                # fail. Telemetry failure must not turn a saved response into
                # unknown/free usage or trigger another network request.
                observed.received(receipt, output)
                if self.before_request:self.before_request()
                if receipt.get('transport_status') == 'RECEIVED' and output_kind not in {'COMPLETE', INCOMPLETE_STRUCTURED_OUTPUT}:
                    observed.output_failure(output_kind)
                if state['unbounded_usage_requests']:
                    raise StageFailure('AGENT_ERROR', 'BILLING_OR_REQUEST_STATUS_UNKNOWN', global_stop=True)
                if response.get('model') != model and not str(response.get('model', '')).startswith(model+'-'):
                    observed.outcome, observed.failure_origin = 'API_ERROR', 'PROVIDER'
                    raise StageFailure('AGENT_ERROR', 'MODEL_NOT_ALLOWED', global_stop=True)
                if output_kind not in {'COMPLETE', INCOMPLETE_STRUCTURED_OUTPUT}:
                    raise StageFailure('TIMEOUT' if output_kind == 'TIMEOUT' else 'AGENT_ERROR',
                                       output_kind, global_stop=False)
                if output_kind == 'COMPLETE' and role in ANGLES and patch_output_incomplete(value):
                    observed.output_failure(INCOMPLETE_PATCH, origin='CONTRACT')
                    raise StageFailure('AGENT_ERROR', INCOMPLETE_PATCH, global_stop=False)
                try:
                    if output_kind == INCOMPLETE_STRUCTURED_OUTPUT:
                        raise ContractError(INCOMPLETE_STRUCTURED_OUTPUT)
                    self.scanner.check(value)
                    result = unwrap_response(value)
                    if result is not None:
                        if role == 'diagnose': validate_diagnosis(result, public, adaptive=self.role_profile == M1_ADAPTIVE_V1)
                        elif role in ANGLES: validate_proposal(result, public, role)
                        else: validate_readonly(result, public, role)
                    observed.validated(True, abstained=value.get('status') == 'ABSTAIN')
                    return value
                except (ContractError, ValueError, TypeError, KeyError):
                    observed.validated(False)
                    if output_kind == INCOMPLETE_STRUCTURED_OUTPUT:
                        observed.output_failure(INCOMPLETE_STRUCTURED_OUTPUT, origin='CONTRACT')
                    if attempt + 1 == self.max_attempts:
                        if output_kind == INCOMPLETE_STRUCTURED_OUTPUT:
                            raise StageFailure('AGENT_ERROR', INCOMPLETE_STRUCTURED_OUTPUT, global_stop=False)
                        reason = 'CONTRACT_ERROR_AFTER_ONE_REPAIR' if self.max_attempts == 2 else 'CONTRACT_ERROR_NO_REPAIR'
                        return {'status': 'ABSTAIN', 'result': None, 'reason': reason}
        raise AssertionError('unreachable')


def effective_role_prompt(profile, role, seam_profile):
    """Exact M1 system prompt components used at the request boundary."""
    if seam_profile not in {'legacy','query-aware-v1','query-autonomous-v1'}:
        raise ValueError('UNKNOWN_SEAM_PROFILE')
    tasks = m1_tasks(profile)
    if role not in tasks: raise ValueError('ROLE_PROFILE_REQUIRED')
    policy = M1_SAFETY_POLICY
    if seam_profile == 'query-autonomous-v1':
        policy += '\n' + RECENT_HISTORY_REFERENCE
    return policy, tasks[role]


def decode_settings(role, model, overrides=None, *, token_budget_profile=LEGACY_TOKEN_BUDGET, repair=False):
    overrides = dict(overrides or {})
    if set(overrides) - {'reasoning_effort', 'max_output_tokens'}:
        raise ValueError('MODEL_DECODE_OVERRIDE_NOT_ALLOWED')
    effort = reasoning_effort(model)
    if token_budget_profile != LEGACY_TOKEN_BUDGET and model == 'gpt-6-luna' and role in ANGLES and not repair:
        effort = 'max'
    if 'reasoning_effort' in overrides:
        if (model, overrides['reasoning_effort']) != ('gpt-6-luna', 'max'):
            raise ValueError('MODEL_REASONING_OVERRIDE_NOT_ALLOWED')
        effort = overrides['reasoning_effort']
    value = {'reasoning': {'effort': effort},
             'max_output_tokens': overrides.get('max_output_tokens', role_limit(token_budget_profile, role, repair=repair))}
    if (type(value['max_output_tokens']) is not int
            or ('max_output_tokens' in overrides and value['max_output_tokens'] not in {1024, 2048, 3072, 4096})):
        raise ValueError('MODEL_DECODE_OVERRIDE_NOT_ALLOWED')
    if model == 'gpt-6-luna' and effort != 'max': value['temperature'] = 0
    return value


def runtime_profile_identity(profile, models, seam_profile, decode_overrides=None, service_tier=None,
                             token_budget_profile=LEGACY_TOKEN_BUDGET):
    """Freeze effective prompts/tasks/decode/schema/models/seam/implementation for M1.

    Suitable for launch and paired-run locks; no private context or credentials.
    """
    from .query_workflow import implementation_hashes
    roles = ('diagnose',) + tuple(roles_for(profile).values()) + ('curator', 'meta')
    if profile not in M1_PROFILES: raise ValueError('M1_ROLE_PROFILE_REQUIRED')
    if service_tier not in (None, 'default'):
        raise ValueError('DEV_SERVICE_TIER_NOT_ALLOWED')
    actual_models = {r:models.get(r, 'gpt-6-luna') for r in roles}
    if set(models) - set(roles) or any(m not in ALLOWED_MODELS for m in actual_models.values()):
        raise ValueError('MODEL_NOT_ALLOWED')
    effective_overrides = _validate_decode_overrides(actual_models, decode_overrides)
    specs = {}
    for role in roles:
        policy, task = effective_role_prompt(profile, role, seam_profile)
        specs[role] = {'policy':policy, 'task':task, 'system_prompt':policy + '\n' + task,
                       'prompt_sha256': digest(policy + '\n' + task), 'task_sha256': digest(task),
                       'model':actual_models[role], 'decode':decode_settings(role,actual_models[role],effective_overrides.get(role), token_budget_profile=token_budget_profile),
                       'response_schema':role_schema(role, adaptive=profile == M1_ADAPTIVE_V1)}
    binding = {'schema':'rsi-m1-runtime-profile/v1', 'role_profile':profile_manifest(profile),
               'seam_profile':seam_profile, 'models':actual_models, 'roles':specs,
               'implementation':implementation_hashes(), 'token_budget':profile_identity(token_budget_profile)}
    if service_tier is not None:
        binding['service_tier'] = service_tier
    if profile == M1_ADAPTIVE_V1:
        from .adaptive_dispatch import catalog_identity
        binding['dispatcher'] = catalog_identity()
        fallback_specs = {}
        for role in roles_for(profile).values():
            policy, task = effective_role_prompt(M1_INDEPENDENT_GENERALIST_V1, role, seam_profile)
            fallback_specs[role] = {'policy': policy, 'task': task,
                'system_prompt': policy + '\n' + task, 'prompt_sha256': digest(policy + '\n' + task),
                'model': actual_models[role], 'decode': decode_settings(role, actual_models[role], effective_overrides.get(role), token_budget_profile=token_budget_profile),
                'response_schema': role_schema(role), 'capability': 'fixed_selection_seam_only'}
        binding['fallback'] = {'profile': 'M1_ADAPTIVE_FROZEN_GENERALIST_FALLBACK_V1',
            'roles': fallback_specs, 'activation': ['DISPATCH_UNAVAILABLE'],
            'diagnosis_schema': role_schema('diagnose', adaptive=True)}
    return {**binding, 'sha256':digest(binding)}


def validate_runtime_profile_identity(binding, profile, models, seam_profile, decode_overrides=None, service_tier=None,
                                      token_budget_profile=LEGACY_TOKEN_BUDGET):
    expected = runtime_profile_identity(profile, models, seam_profile, decode_overrides, service_tier, token_budget_profile)
    if binding != expected: raise ValueError('M1_RUNTIME_PROFILE_IDENTITY_CHANGED')
    return expected

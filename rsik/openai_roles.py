"""Cost-first OpenAI roles with private receipts and post-usage billing.

Only explicitly projected public context is accepted. A known, billed malformed
response can be repaired once; transport uncertainty never causes a resend.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import re
import sys
import time
import threading
from copy import deepcopy

from .contracts import (ANGLES, ContractError, role_schema, unwrap_response,
                        validate_diagnosis, validate_proposal, validate_readonly,validate_completed_candidate)
from .errors import StageFailure
from .locking import atomic_write
from .state import canonical, digest


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
TASKS['P3_DEPENDENCY'] = DEPENDENCY_TASK
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


class OpenAIRoles:
    def __init__(self, budget, directory, key_file, scanner, *, deadline=None, transport=single_request, models=None, seam_profile='legacy', before_request=None, role_profile=LEGACY, strategy_context=None, launch=None, observer=None):
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
        if set(self.models)-set(TASKS) or any(model not in {'gpt-6-luna','gpt-6.1-sol'} for model in self.models.values()):
            raise ValueError('MODEL_NOT_ALLOWED')
        self.directory.mkdir(parents=True, exist_ok=True)
        from .observability import Observer
        self.observer = observer if observer is not None else Observer(self.directory / "observations.sqlite")
        self._observation_local = threading.local()

    @property
    def observation_invocation(self):
        """Latest attempt on this thread; never part of model-visible input."""
        return dict(getattr(self._observation_local, "invocation", {}))

    def diagnose(self, public):
        return self.call('diagnose', public)

    def propose(self, angle, public, diagnosis):
        return self.call(angle, public, diagnosis)

    def call(self, role, public, diagnosis=None):
        self._observation_local.invocation = {}
        if role=='P3_DEPENDENCY' and self.role_profile!=DEPENDENCY:raise ValueError('ROLE_PROFILE_REQUIRED')
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
        if self.seam_profile=='query-autonomous-v1':
            from .query_aware import autonomy_interface_contract as interface_contract
            from .autonomy_contract import validate_history,validate_search_memory
            if public.get('experiment_intent')!='STRUCTURAL_POLICY_SEARCH':raise ContractError('PRESELECTED_STRATEGY_FORBIDDEN_IN_AUTONOMY_TEST')
            validate_history(public.get('search_history',[]))
            if 'search_memory' in public:validate_search_memory(public['search_memory'])
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
            validate_diagnosis(diagnosis, public)
        for attempt in range(2):
            if self.transport is single_request:
                from .launch import validate_launch
                validate_launch(self.launch,boundary="MODEL_REQUEST")
            if self.before_request:self.before_request()
            context = {'public': deepcopy(public), 'diagnosis': diagnosis}
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
                if role=='P2_BUDGET':
                    task='Independently design a context-selection improvement within the supplied six-argument seam. Use the diagnosis and prior observed outcomes to choose and implement the next hypothesis; do not wait for the monitor to prescribe an algorithm or threshold. Passing parent checks does not establish optimality. Candidate changes are unproven until deterministic replay. Do not weaken seed, order, source, fact or budget constraints. If a necessary change lies outside the seam, state that limitation honestly; do not change the surrounding system.'
            if self.seam_profile=='query-aware-v1':
                policy+='\nIndependent owner-authorized query-aware seam: keep SIX arguments (members, chunks, files, seed_ids, selection, query). query is the actual runtime question string; never supplied in this proposal context. Patch only this six-argument function body; original five-argument registry is unchanged. QUERY_OVERLAP_ABLATION tests retaining all seeds and a nonseed when any two-character substring query[j:j+2] occurs in its content. Use range(max(0,len(query)-1)). Mandatory short-query guard after the no-seeds guard: if len(query) < 2: return list(range(len(members))). Use this literal comparison, not an algebraic substitute; a one-character question must retain all members. QUERY_OVERLAP_CONSERVATIVE additionally retains neighbors whose content length is <=200 or >= longest seed length, plus query matches and seeds. This is an unproven lexical hypothesis that may lose evidence; deterministic source facts must reject any loss. Do not assume lexical match proves factual sufficiency.\n'
                policy+=' For QUERY_OVERLAP_ZERO_MATCH_FALLBACK, retain seeds plus two-character query-overlap members as in the ablation, BUT if no member at all (including seeds) contains any two-character substring of query, retain ALL members. This explicit uncertainty fallback tests the hypothesis that lexical evidence is unavailable, rather than treating absence of a lexical match as proof of irrelevance. Compute a fresh private list of boolean matches, test not any(matches), then return all indices or the seed/match indices. Keep the mandatory len(query)<2 fallback. This new intent is an adaptive training hypothesis; no guaranteed safety claim.'
                policy+=' For QUERY_SEED_MATCH_FALLBACK, retain all when no SEED member contains a query bigram; otherwise seeds plus matching members. For QUERY_NONSEED_MATCH_FALLBACK, retain all when no NONSEED member contains a query bigram; otherwise seeds plus matching members. For QUERY_TRIGRAM_ZERO_MATCH_FALLBACK use three-character substrings, keep all if len(query)<3 or no member matches any trigram, otherwise seeds plus matching members. For QUERY_BIGRAM_RATIO count distinct query bigrams per member, retain all if every score is zero, otherwise seeds plus members whose score added to itself is at least the highest seed score AND whose score is positive. Always keep all seeds, all members for len(query)<2, and original order. These are separate diagnostic hypotheses, not combinations unless the named intent says so.'
            body = {'model': model, 'store': False, 'reasoning': {'effort': 'none' if model=='gpt-6-luna' else 'low'},
                    'max_output_tokens': 1024 if role == 'diagnose' else 3072 if role in ANGLES else 2048,
                    'input': [{'role': 'system', 'content': policy + '\n' + task},
                              {'role': 'user', 'content': canonical(context).decode('utf-8')}],
                    'text': {'format': {'type': 'json_schema', 'name': 'rsi_role',
                                       'strict': True, 'schema': role_schema(role)}}}
            if model == 'gpt-6-luna': body['temperature'] = 0
            projection=None
            if self.seam_profile=='query-autonomous-v1':
                visible=context['public']
                initial_history=len(visible.get('search_history',[]))
                initial_memory=len(visible.get('search_memory',{}).get('entries',[]))
                # Only optional model-authored history is projected down. Keep
                # current parent, diagnosis, observations and all safety rules.
                while len(canonical(body))>16_384:
                    recent=visible.get('search_history',[])
                    memory=visible.get('search_memory',{}).get('entries',[])
                    if len(recent)>1:recent.pop(0)
                    elif len(memory)>4:memory.pop()
                    elif recent:recent.pop(0)
                    elif memory:memory.pop()
                    else:break
                    body['input'][1]['content']=canonical(context).decode('utf-8')
                if (initial_history!=len(visible.get('search_history',[]))
                        or initial_memory!=len(visible.get('search_memory',{}).get('entries',[]))):
                    projection={'schema':'rsi-request-history-projection/v1',
                        'history_before':initial_history,'history_sent':len(visible.get('search_history',[])),
                        'memory_before':initial_memory,'memory_sent':len(visible.get('search_memory',{}).get('entries',[])),
                        'full_local_memory_deleted':False,'mandatory_context_changed':False,'body_bytes':len(canonical(body))}
            self.scanner.check(body)
            # UTF-8 byte count is a conservative bound on byte-level token count.
            if len(canonical(body)) > 16_384:
                raise ContractError('INPUT_TOO_LARGE')
            key = public['round_id'] + '-' + role + '-' + str(attempt)
            path = self.directory / (key + '.json')
            request_path = self.directory / (key + '.request.json')
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
                           'role_profile_hash': digest(profile_manifest(self.role_profile)),
                           'request_hash': digest(body), 'seam_profile': self.seam_profile}
            observation['attempt_id'] = observation['invocation_id']
            self._observation_local.invocation = dict(observation)
            with self.observer.invocation(observation) as observed:
                remaining = 90 if self.deadline is None else min(90, self.deadline - time.monotonic())
                if remaining <= 0:
                    raise TimeoutError('AGENT_TIMEOUT')
                if path.exists():
                    starts = [e['payload'] for e in self.budget.log.read() if e['event_type']=='MODEL_REQUEST_STARTED'
                              and e['payload']['request_key']==key]
                    if (not request_path.is_file() or json.loads(request_path.read_text(encoding='utf-8'))!=body
                            or len(starts)!=1 or starts[0]['request_hash']!=digest(body)):
                        raise StageFailure('RECOVERY_ERROR', 'REQUEST_INPUT_CHANGED', global_stop=True)
                    observed.resume()
                    receipt = json.loads(path.read_text(encoding='utf-8'))
                else:
                    if request_path.exists():
                        raise StageFailure('RECOVERY_ERROR', 'REQUEST_STATUS_UNKNOWN', global_stop=True)
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
                    observed.start()
                    atomic_write(request_path, canonical(body), exclusive=True)
                    try:
                        receipt = (self.transport(body,self.key_file,remaining,response_path=self.directory/(key+'.transport.json'))
                                   if self.transport is single_request else self.transport(body,self.key_file,remaining))
                    except TimeoutError:
                        receipt = {'transport_status': 'UNKNOWN'}
                        observed.outcome, observed.failure_origin = 'TIMEOUT', 'TRANSPORT'
                    except Exception:
                        receipt = {'transport_status': 'UNKNOWN'}
                    # Persist before billing receipt; recovery can reconcile the
                    # actual saved usage without sending another request.
                    atomic_write(path, canonical(receipt), exclusive=True)
                observed.received(receipt)
                response = receipt.get('response', {})
                usage = response.get('usage') if receipt.get('transport_status') == 'RECEIVED' else None
                state = self.budget.complete(key, usage, status=receipt['transport_status'],
                                             provider_request_id=receipt.get('provider_request_id'))
                if self.before_request:self.before_request()
                if state['unbounded_usage_requests']:
                    raise StageFailure('AGENT_ERROR', 'BILLING_OR_REQUEST_STATUS_UNKNOWN', global_stop=True)
                if response.get('model') != model and not str(response.get('model', '')).startswith(model+'-'):
                    observed.outcome, observed.failure_origin = 'API_ERROR', 'PROVIDER'
                    raise StageFailure('AGENT_ERROR', 'MODEL_NOT_ALLOWED', global_stop=True)
                try:
                    if response.get('status') != 'completed':
                        raise ContractError('INCOMPLETE_RESPONSE')
                    messages = response.get('output', [])
                    parts = [part['text'] for message in messages if message.get('type') == 'message'
                             and message.get('phase') in (None, 'final_answer')
                             for part in message.get('content', []) if part.get('type') == 'output_text']
                    if len(parts) != 1:
                        raise ContractError('CONTRACT_ERROR')
                    value = json.loads(parts[0])
                    self.scanner.check(value)
                    result = unwrap_response(value)
                    if result is not None:
                        if role == 'diagnose': validate_diagnosis(result, public)
                        elif role in ANGLES: validate_proposal(result, public, role)
                        else: validate_readonly(result, public, role)
                    observed.validated(True, abstained=value.get('status') == 'ABSTAIN')
                    return value
                except (ContractError, ValueError, TypeError, KeyError):
                    observed.validated(False)
                    if attempt:
                        return {'status': 'ABSTAIN', 'result': None, 'reason': 'CONTRACT_ERROR_AFTER_ONE_REPAIR'}
        raise AssertionError('unreachable')

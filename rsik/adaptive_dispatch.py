"""Pure, evidence-gated M1-C1 assignments; fixed slots are statistical identities.

No file reads, model calls, candidate algorithms, or automatic error fallback live here.
Catalog contents are public capability constraints, safe to bind into a capsule.
"""
from copy import deepcopy
import re

from .adjudicate import SLOTS
from .contracts import ContractError, validate_diagnosis
from .redact import COUNTS, CATEGORIES, FORMATS
from .state import digest

VERSION = 'M1_ADAPTIVE_DISPATCH_V1'
SCHEMA_VERSION = 'rsi-adaptive-dispatch/v1'
TASK_TYPES = ('DEDUP', 'BUDGET', 'DEPENDENCY', 'GENERALIST', 'ABSTAIN')
FALLBACK_REASONS = ('DISPATCH_UNAVAILABLE', 'DIAGNOSIS_UNAVAILABLE')
FALLBACK_PROFILE = 'M1_ADAPTIVE_FROZEN_GENERALIST_FALLBACK_V1'
_FORBIDDEN = ['change_evaluator', 'read_private_data', 'change_seam_signature',
              'mutate_inputs', 'remove_seeds', 'reorder_members', 'share_peer_proposals']
_SCOPES = {
    'DEDUP': 'Investigate independent deduplication hypotheses; duplicate appearance does not prove dispensability.',
    'BUDGET': 'Investigate independent budget hypotheses; nonseed overhead does not prove waste.',
    'DEPENDENCY': 'Investigate independent cross-member dependency and conservative fallback hypotheses.',
    'GENERALIST': 'Independently choose a falsifiable hypothesis within the frozen selection seam.',
    'ABSTAIN': 'Do not propose or execute a candidate when no admissible seam experiment is supported.',
}


class DispatchUnavailable(RuntimeError):
    """Explicit operational unavailability; malformed input is never a fallback trigger."""
    code = 'DISPATCH_UNAVAILABLE'

    def __init__(self):
        super().__init__(self.code)


def task_catalog():
    """Return a fresh JSON-safe copy; caller mutation never changes catalog identity."""
    return {kind: {'task_type': kind, 'scope': _SCOPES[kind],
                   'preconditions': ['public_evidence_only', 'fixed_selection_seam', 'independent_assignment'],
                   'forbidden_actions': list(_FORBIDDEN),
                   'falsification_requirement': 'State a concrete observation that refutes the hypothesis; retain all fact, source, replay and statistical gates.'}
            for kind in TASK_TYPES}


def catalog_identity():
    return digest({'dispatcher_version': VERSION, 'catalog': task_catalog()})


def _error(code='DISPATCH_INPUT_INVALID'):
    raise ContractError(code)


def _public_evidence(public):
    """Bind evidence refs to actual allowlisted numeric public observations."""
    if not isinstance(public, dict):
        _error()
    observations = public.get('observations', [])
    quotes = public.get('evidence_quotes', {})
    if not isinstance(observations, list) or not isinstance(quotes, dict):
        _error()
    bound = {}
    refs = []
    for obs in observations:
        if (not isinstance(obs, dict) or set(obs) != {'observation_ref', 'category', 'stage', 'format',
                'problem_layer', 'counts', 'member_refs', 'safe_evidence'}
                or obs['stage'] != 'CONTEXT_SELECTION' or obs['problem_layer'] != 'SELECTION'
                or obs['category'] not in CATEGORIES or obs['format'] not in FORMATS
                or obs['member_refs'] != [] or not isinstance(obs['counts'], dict)
                or set(obs['counts']) - COUNTS or not isinstance(obs['safe_evidence'], list)
                or any(type(v) is not int or v < 0 for v in obs['counts'].values())):
            _error()
        if (not isinstance(obs['observation_ref'], str)
                or not re.fullmatch(r'observation-[0-9a-f]{32}', obs['observation_ref'])
                or obs['observation_ref'] in refs):
            _error()
        refs.append(obs['observation_ref'])
        quote = '; '.join(f'{key}={obs["counts"][key]}' for key in sorted(obs['counts']))
        for entry in obs['safe_evidence']:
            if (not isinstance(entry, dict) or set(entry) != {'evidence_ref', 'text'}
                    or not isinstance(entry['evidence_ref'], str) or not entry['evidence_ref']
                    or not re.fullmatch(r'evidence-[0-9a-f]{32}', entry['evidence_ref'])
                    or entry['evidence_ref'] in bound or entry['text'] != quote
                    or quotes.get(entry['evidence_ref']) != quote):
                _error()
            bound[entry['evidence_ref']] = dict(obs['counts'])
    if set(quotes) != set(bound) or public.get('observation_refs', []) != refs:
        _error()
    return bound


def _supports(kind, counts):
    if kind == 'DEDUP':
        return counts.get('exact_content_duplicate_pairs', 0) > 0 and counts.get('duplicate_key_eligible_members', 0) > 0
    if kind == 'BUDGET':
        return counts.get('nonseed_members', 0) > 0 or counts.get('nonseed_content_chars', 0) > 0
    return counts.get('dependency_edges', 0) > 0 or counts.get('related_member_pairs', 0) > 0


def _diagnosis(value, evidence, public):
    if value is None:
        return None
    if not isinstance(value, dict):
        _error()
    if 'status' in value:
        if set(value) != {'status', 'reason', 'result'} or value['status'] not in {'OK', 'ABSTAIN'}:
            _error()
        if value['status'] == 'ABSTAIN':
            if value['result'] is not None:
                _error()
            return None
        value = value['result']
    allowed = {'schema_version', 'observation_refs', 'problem_layer', 'first_deviation', 'evidence_refs',
               'evidence_quote', 'hypothesis', 'fixable_within_seam', 'required_data', 'confidence', 'task_signals'}
    if (not isinstance(value, dict) or set(value) - allowed
            or type(value.get('fixable_within_seam')) is not bool
            or not isinstance(value.get('evidence_refs'), list)
            or any(not isinstance(r, str) or r not in evidence for r in value['evidence_refs'])
            or len(set(value['evidence_refs'])) != len(value['evidence_refs'])
            or not isinstance(value.get('task_signals', []), list)):
        _error()
    validate_diagnosis(value, public, adaptive=True)
    for signal in value.get('task_signals', []):
        if (not isinstance(signal, dict) or set(signal) != {'task_type', 'evidence_refs', 'disposition'}
                or signal['task_type'] not in TASK_TYPES[:3]
                or signal['disposition'] not in {'SUPPORT', 'CONFLICT'}
                or not isinstance(signal['evidence_refs'], list)
                or not signal['evidence_refs']
                or any(not isinstance(r, str) or r not in value['evidence_refs'] for r in signal['evidence_refs'])
                or len(set(signal['evidence_refs'])) != len(signal['evidence_refs'])):
            _error()
    return value


def _plan(public, diagnosis, kinds, supports, reason, *, mode='ADAPTIVE', fallback_reason=None):
    catalog = task_catalog()
    diagnosis_hash = digest(diagnosis)
    assignments = []
    for index, slot in enumerate(SLOTS):
        kind = kinds[index % len(kinds)]
        contract = catalog[kind]
        assignment = {'slot': slot, 'task_type': kind, 'task_contract_hash': digest(contract),
                      'evidence_refs': sorted(supports.get(kind, [])),
                      'preconditions': list(contract['preconditions']),
                      'forbidden_actions': list(contract['forbidden_actions']),
                      'falsification_requirement': contract['falsification_requirement'],
                      'assignment_reason': ('REPEATED_SUPPORTED_TASK_INDEPENDENT_SEARCH' if index >= len(kinds)
                                            and kind in TASK_TYPES[:3] else reason)}
        assignment['task_id'] = 'assignment-' + digest({'round_id': public.get('round_id'),
            'parent_alias': public.get('parent_alias'), 'diagnosis_sha256': diagnosis_hash,
            'catalog_sha256': catalog_identity(), 'assignment': assignment})[:24]
        assignments.append(assignment)
    result = {'schema_version': SCHEMA_VERSION, 'dispatcher_version': VERSION,
              'catalog_sha256': catalog_identity(), 'diagnosis_sha256': diagnosis_hash,
              'mode': mode, 'fallback_reason': fallback_reason,
              'fallback_profile': FALLBACK_PROFILE if fallback_reason else None,
              'fallback_version': 'v1' if fallback_reason else None,
              'assignments': assignments}
    result['dispatch_sha256'] = digest(result)
    return result


def build_dispatch_plan(public, diagnosis, *, catalog=None):
    """Allocate all three independent slots using structured evidence, never confidence."""
    if catalog is not None and catalog != task_catalog():
        _error('DISPATCH_CATALOG_INVALID')
    evidence = _public_evidence(public)
    value = _diagnosis(diagnosis, evidence, public)
    if value is None or not value['fixable_within_seam'] or value.get('required_data'):
        return _plan(public, diagnosis, ['ABSTAIN'], {}, 'NO_ADMISSIBLE_SEAM_EXPERIMENT')
    if value.get('problem_layer', 'SELECTION') != 'SELECTION':
        return _plan(public, diagnosis, ['GENERALIST'], {}, 'NO_SUPPORTED_SPECIALIST')
    supports = {}
    conflicts = {s['task_type'] for s in value.get('task_signals', []) if s['disposition'] == 'CONFLICT'}
    for signal in value.get('task_signals', []):
        kind = signal['task_type']
        if signal['disposition'] != 'SUPPORT' or kind in conflicts:
            continue
        actual = [ref for ref in signal['evidence_refs'] if _supports(kind, evidence[ref])]
        if actual:
            supports.setdefault(kind, set()).update(actual)
    kinds = [kind for kind in TASK_TYPES[:3] if kind in supports]
    return _plan(public, diagnosis, kinds or ['GENERALIST'], supports,
                 'SUPPORTED_PUBLIC_STRUCTURAL_SIGNAL' if kinds else 'NO_SUPPORTED_SPECIALIST')


def generalist_fallback(public, diagnosis=None, *, reason):
    """Explicit operator/runtime fallback; dispatcher failures never invoke it silently."""
    evidence = _public_evidence(public)
    value = _diagnosis(diagnosis, evidence, public)
    if reason not in FALLBACK_REASONS:
        _error()
    admissible = value is not None and value['fixable_within_seam'] and not value.get('required_data')
    return _plan(public, diagnosis, ['GENERALIST'] if admissible else ['ABSTAIN'], {},
                 'EXPLICIT_FROZEN_GENERALIST_FALLBACK' if admissible else 'NO_ADMISSIBLE_SEAM_EXPERIMENT',
                 mode='FROZEN_GENERALIST_FALLBACK', fallback_reason=reason)


def validate_dispatch_plan(plan, public=None, diagnosis=None):
    """Check catalog and canonical hashes, optionally reproduce the complete decision."""
    try:
        fields = {'schema_version', 'dispatcher_version', 'catalog_sha256', 'diagnosis_sha256',
                  'mode', 'fallback_reason', 'fallback_profile', 'fallback_version', 'assignments', 'dispatch_sha256'}
        if (not isinstance(plan, dict) or set(plan) != fields or plan['schema_version'] != SCHEMA_VERSION
                or plan['dispatcher_version'] != VERSION or plan['catalog_sha256'] != catalog_identity()
                or digest({k: v for k, v in plan.items() if k != 'dispatch_sha256'}) != plan['dispatch_sha256']
                or not isinstance(plan['assignments'], list) or len(plan['assignments']) != 3):
            _error('DISPATCH_PLAN_INVALID')
        if plan['mode'] not in {'ADAPTIVE', 'FROZEN_GENERALIST_FALLBACK'}:
            _error('DISPATCH_PLAN_INVALID')
        if (plan['fallback_reason'] not in FALLBACK_REASONS if plan['mode'] == 'FROZEN_GENERALIST_FALLBACK'
                else plan['fallback_reason'] is not None):
            _error('DISPATCH_PLAN_INVALID')
        if (plan['fallback_profile'] != (FALLBACK_PROFILE if plan['fallback_reason'] else None)
                or plan['fallback_version'] != ('v1' if plan['fallback_reason'] else None)):
            _error('DISPATCH_PLAN_INVALID')
        for slot, item in zip(SLOTS, plan['assignments'], strict=True):
            if set(item) != {'task_id', 'slot', 'task_type', 'task_contract_hash', 'evidence_refs',
                             'preconditions', 'forbidden_actions', 'falsification_requirement', 'assignment_reason'}:
                _error('DISPATCH_PLAN_INVALID')
            contract = task_catalog()[item['task_type']]
            if (item['slot'] != slot or item['task_contract_hash'] != digest(contract)
                    or any(item[k] != contract[k] for k in ('preconditions', 'forbidden_actions', 'falsification_requirement'))
                    or not isinstance(item['evidence_refs'], list)
                    or any(not isinstance(ref, str) for ref in item['evidence_refs'])
                    or item['evidence_refs'] != sorted(set(item['evidence_refs']))
                    or not isinstance(item['task_id'], str) or not re.fullmatch(r'assignment-[0-9a-f]{24}', item['task_id'])
                    or item['assignment_reason'] not in {'REPEATED_SUPPORTED_TASK_INDEPENDENT_SEARCH',
                        'NO_ADMISSIBLE_SEAM_EXPERIMENT', 'NO_SUPPORTED_SPECIALIST',
                        'SUPPORTED_PUBLIC_STRUCTURAL_SIGNAL', 'EXPLICIT_FROZEN_GENERALIST_FALLBACK'}
                    or (item['task_type'] in TASK_TYPES[:3] and not item['evidence_refs'])
                    or (plan['mode'] == 'FROZEN_GENERALIST_FALLBACK' and item['task_type'] not in {'GENERALIST', 'ABSTAIN'})):
                _error('DISPATCH_PLAN_INVALID')
        if len({item['task_id'] for item in plan['assignments']}) != 3:
            _error('DISPATCH_PLAN_INVALID')
        if public is not None:
            expected = (generalist_fallback(public, reason=plan['fallback_reason'], diagnosis=diagnosis)
                        if plan['mode'] == 'FROZEN_GENERALIST_FALLBACK' else build_dispatch_plan(public, diagnosis))
            if plan != expected:
                _error('DISPATCH_PLAN_INVALID')
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError('DISPATCH_PLAN_INVALID') from None
    return deepcopy(plan)


def validate_assignment(assignment, public, diagnosis):
    """At send time accept only an exactly reproducible public assignment."""
    # The frozen helper is explicit in the assignment reason and has a separate
    # catalog-bound identity; it cannot masquerade as an adaptive specialization.
    if isinstance(assignment, dict) and assignment.get('assignment_reason') == 'EXPLICIT_FROZEN_GENERALIST_FALLBACK':
        expected = generalist_fallback(public, reason='DISPATCH_UNAVAILABLE', diagnosis=diagnosis)['assignments']
    else:
        expected = build_dispatch_plan(public, diagnosis)['assignments']
    if assignment not in expected:
        _error('DISPATCH_PLAN_INVALID')
    return deepcopy(assignment)

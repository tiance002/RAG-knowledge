"""Host-owned objective profiles and stable measurement definitions."""
from copy import deepcopy
from datetime import datetime, timezone
import math
import re

from .locking import atomic_write
from .state import canonical, digest

CONTRACT_VERSION = 'rsi-context-metrics/v1'
PAIRS = 'Complete, identity-verified A/B pairs on the same frozen evaluation cases'
CONTRACTS = {
    'context_chars': ('characters', 'min', 'Sum of candidate context Unicode character counts', PAIRS),
    'parent_chars': ('characters', 'min', 'Sum of parent context Unicode character counts', PAIRS),
    'candidate_chars': ('characters', 'min', 'Sum of candidate context Unicode character counts', PAIRS),
    'shorter_cases': ('cases', 'max', 'Count of pairs with shorter candidate context', PAIRS),
    'longer_cases': ('cases', 'min', 'Count of pairs with longer candidate context', PAIRS),
    'unchanged_cases': ('cases', 'max', 'Count of pairs with equal context length', PAIRS),
    'necessary_fact_status': ('status', 'max', 'Presence of required facts in candidate context', 'Audited fact labels for eligible cases'),
    'provenance_status': ('status', 'max', 'Presence of required source provenance', 'Audited evidence for eligible cases'),
    'replay_seconds': ('seconds', 'min', 'Sum of candidate replay receipt durations', 'Complete, comparable, hash-bound real execution receipts'),
    'answer_quality': ('ratio', 'max', 'Final answers meeting the frozen quality rubric / eligible answers', 'Real final answers and bound grading receipts'),
    'recall': ('ratio', 'max', 'Expected relevant sources retrieved / expected relevant sources', 'Frozen relevance labels and real retrieval receipts'),
    'ttft': ('seconds', 'min', 'Median time from start to first answer token', 'Comparable live timing receipts'),
    'answer_tokens': ('tokens', 'min', 'Sum of observed final answer input and output tokens', 'Complete provider usage receipts for final RAG answers'),
}
METRIC_CONTRACTS = {
    metric: dict(metric_id=metric, metric_contract_version=CONTRACT_VERSION, unit=unit,
                 direction=direction, measurement_method=method, evidence_requirements=evidence,
                 evaluator_identity=('rsi-context-replay' if metric in {
                     'context_chars','parent_chars','candidate_chars','shorter_cases','longer_cases',
                     'unchanged_cases','necessary_fact_status','provenance_status','replay_seconds'
                 } else 'UNAVAILABLE'))
    for metric, (unit, direction, method, evidence) in CONTRACTS.items()
}
METRIC_CONTRACTS['answer_quality'].update(numerator='correct final answers', denominator='eligible answers')
METRIC_CONTRACTS['recall'].update(numerator='retrieved expected sources', denominator='expected sources')

PROFILE_FIELDS = {
    'objective_profile_id', 'version', 'description', 'primary_metric', 'direction',
    'hard_constraints', 'secondary_metrics', 'tie_breakers', 'required_metric_contracts', 'components',
}


def validate_profile(profile):
    """Validate trusted configuration. Return a standalone snapshot."""
    def require(condition):
        if not condition:
            raise ValueError('INVALID_OBJECTIVE_PROFILE')

    require(isinstance(profile, dict) and set(profile) == PROFILE_FIELDS)
    require(isinstance(profile['objective_profile_id'], str)
            and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', profile['objective_profile_id']))
    require(type(profile['version']) is int and profile['version'] >= 1)
    require(isinstance(profile['description'], str) and 0 < len(profile['description'].strip()) <= 256)
    primary = profile['primary_metric']
    require(isinstance(primary, str) and primary in METRIC_CONTRACTS)
    require(METRIC_CONTRACTS[primary]['unit'] != 'status'
            and profile['direction'] == METRIC_CONTRACTS[primary]['direction'])
    needed = {primary}
    for field in ('secondary_metrics', 'tie_breakers', 'components'):
        values = profile[field]
        require(isinstance(values, list) and len(values) <= 8
                and all(isinstance(value, str) and re.fullmatch(r'[a-z][a-z0-9_]{0,63}', value) for value in values)
                and len(set(values)) == len(values))
        if field != 'components':
            require(primary not in values and all(value in METRIC_CONTRACTS and
                    METRIC_CONTRACTS[value]['unit'] != 'status' for value in values))
            needed.update(values)
    constraints = profile['hard_constraints']
    require(isinstance(constraints, list) and len(constraints) <= 8)
    for constraint in constraints:
        require(isinstance(constraint, dict) and set(constraint) == {'metric_id', 'operator', 'value'})
        metric, operator, value = constraint['metric_id'], constraint['operator'], constraint['value']
        require(isinstance(metric, str) and metric in METRIC_CONTRACTS)
        if METRIC_CONTRACTS[metric]['unit'] == 'status':
            require(operator == '==' and value in ('PASS', 'FAIL'))
        else:
            require(operator in ('<=', '>=', '==', '<', '>') and type(value) in (float, int)
                    and 0 <= value <= 10**15 and math.isfinite(value))
        needed.add(metric)
    required = profile['required_metric_contracts']
    require(isinstance(required, dict) and set(required) == needed
            and all(version == METRIC_CONTRACTS[metric]['metric_contract_version']
                    for metric, version in required.items()))
    return deepcopy(profile)


def objective_scope(profile):
    profile = validate_profile(profile)
    metric = profile['primary_metric']
    return {'primary_metric': metric, 'direction': profile['direction'],
            'metric_contract_version': profile['required_metric_contracts'][metric]}


def profile_change(previous, current, *, reason, source_reference):
    """Create a deterministic transition record from explicit owner configuration."""
    previous = validate_profile(previous) if previous is not None else None
    current = validate_profile(current)
    if not all(isinstance(item, str) and item.strip() and len(item) <= 512
               for item in (reason, source_reference)):
        raise ValueError('OBJECTIVE_CHANGE_SOURCE_REQUIRED')
    if previous is not None and previous['objective_profile_id'] == current['objective_profile_id']:
        if current != previous and current['version'] <= previous['version']:
            raise ValueError('OBJECTIVE_VERSION_MUST_ADVANCE')
    changed = sorted(field for field in PROFILE_FIELDS if previous is None or previous[field] != current[field])
    return {'schema': 'rsi-objective-change/v1', 'previous_objective_profile': previous,
            'current_objective_profile': current, 'previous_sha256': digest(previous) if previous else None,
            'current_sha256': digest(current), 'change_reason': reason, 'changed_fields': changed,
            'primary_changed': previous is not None and objective_scope(previous) != objective_scope(current),
            'source_reference': source_reference}


def freeze_objective(directory, current, *, previous=None, reason, source_reference):
    change = profile_change(previous, current, reason=reason, source_reference=source_reference)
    record = {**change, 'timestamp': datetime.now(timezone.utc).isoformat()}
    atomic_write(directory / 'objective-profile.json', canonical(record), exclusive=True)
    return record


def validate_transition(transition):
    if not isinstance(transition, dict) or set(transition) != {'previous', 'current', 'reason', 'source_reference'}:
        raise ValueError('INVALID_OBJECTIVE_TRANSITION')
    return profile_change(transition['previous'], transition['current'], reason=transition['reason'],
                          source_reference=transition['source_reference'])


def prompt_objective(profile):
    """Return only the structured instructions needed by proposal roles."""
    profile = validate_profile(profile)
    return {key: value for key, value in profile.items() if key != 'description'}


def validate_prompt_objective(value):
    if not isinstance(value, dict) or set(value) != PROFILE_FIELDS - {'description'}:
        raise ValueError('INVALID_OBJECTIVE_PROMPT')
    validate_profile({**value, 'description': 'Structured objective from frozen configuration'})

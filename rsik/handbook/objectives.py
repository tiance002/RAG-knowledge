"""Host-bound objective applicability; Curator output never defines scope."""
from copy import deepcopy

from ..contracts import ContractError
from ..events import digest


OBJECTIVE_FIELDS = ('objective_profile_id', 'objective_version', 'applicability_scope', 'metric_effects')
EFFECT_STATUSES = {'IMPROVED', 'REGRESSED', 'UNCHANGED', 'UNKNOWN'}


def objective_metadata(context, support_refs):
    profile = context.get('objective_profile')
    if profile is None:
        if context.get('metric_effects') or context.get('objective_applicability'):
            raise ContractError('OBJECTIVE_PROFILE_REQUIRED')
        return {}
    from ..objectives import validate_profile, objective_scope
    validate_profile(profile)
    applicability = deepcopy(context.get('objective_applicability', {}))
    kind = applicability.pop('kind', 'SUCCESS')
    if kind not in {'SUCCESS', 'FAILURE', 'GENERAL_CONSTRAINT'}:
        raise ContractError('INVALID_OBJECTIVE_APPLICABILITY')
    failure_type = applicability.pop('failure_type', None)
    authority = applicability.pop('authority', None)
    if applicability:
        raise ContractError('INVALID_OBJECTIVE_APPLICABILITY')
    scope = {**objective_scope(profile), 'objective_profile_sha256': digest(profile), 'components': list(profile['components']), 'kind': kind}
    if kind == 'FAILURE':
        if failure_type not in {'INTERFACE_CONSTRAINT', 'CONDITIONAL_REGRESSION', 'NO_GAIN', 'INFRASTRUCTURE', 'PARENT_FAILURE', 'UNKNOWN'}:
            raise ContractError('INVALID_FAILURE_TYPE')
        scope['failure_type'] = failure_type
    if kind == 'GENERAL_CONSTRAINT' or failure_type == 'INTERFACE_CONSTRAINT':
        if authority not in {'DETERMINISTIC_HOST', 'EXPLICIT_USER'}:
            raise ContractError('HOST_CONSTRAINT_AUTHORITY_REQUIRED')
        scope['authority'] = authority
    effects = deepcopy(context.get('metric_effects', {}))
    for metric, effect in effects.items():
        if (not isinstance(metric, str) or not isinstance(effect, dict)
                or set(effect) != {'status', 'evidence_refs'}
                or effect['status'] not in EFFECT_STATUSES
                or not isinstance(effect['evidence_refs'], list) or not effect['evidence_refs']
                or set(effect['evidence_refs']) - set(context['refs'])
                or not set(effect['evidence_refs']) & set(support_refs)):
            raise ContractError('UNTRACEABLE_METRIC_EFFECT')
        effect['evidence_refs'] = [ref for ref in effect['evidence_refs'] if ref in support_refs]
    return {'objective_profile_id': profile['objective_profile_id'], 'objective_version': profile['version'],
            'applicability_scope': scope, 'metric_effects': effects}


def scoped_entry(entry, context):
    # validate_entry rejects these model-supplied fields before this host step.
    return {**entry, **objective_metadata(context, entry['support_refs'])}


def objective_matches(entry, profile, component=None):
    if profile is None:
        return not entry.get('applicability_scope')
    from ..objectives import objective_scope
    scope = entry.get('applicability_scope')
    if not scope:
        return False
    components = scope.get('components', [])
    if component is not None and component not in components:
        return False
    if not set(components) & set(profile['components']):
        return False
    same_target = (entry.get('objective_profile_id') == profile['objective_profile_id']
                   and all(scope.get(key) == value for key, value in objective_scope(profile).items()))
    kind = scope.get('kind', 'SUCCESS')
    if kind == 'SUCCESS':
        return same_target
    if kind == 'GENERAL_CONSTRAINT':
        return scope.get('authority') in {'DETERMINISTIC_HOST', 'EXPLICIT_USER'}
    failure = scope.get('failure_type')
    if failure == 'INTERFACE_CONSTRAINT':
        return scope.get('authority') in {'DETERMINISTIC_HOST', 'EXPLICIT_USER'}
    if failure == 'CONDITIONAL_REGRESSION':
        return bool(entry['conditions'])
    return failure == 'NO_GAIN' and same_target


def objective_reuse_summary(entry, profile):
    """Project only measured directions. A relative effect cannot prove an absolute limit."""
    from ..objectives import canonical_metric, context_selection_measures

    scope = entry.get('applicability_scope', {})
    if profile is None or scope.get('kind', 'SUCCESS') != 'SUCCESS':
        return {}
    hard_metrics = {constraint['metric_id'] for constraint in profile['hard_constraints']}
    secondary = set(profile['secondary_metrics'])
    stored = entry.get('metric_effects', {})
    effects = {}
    for metric in sorted(hard_metrics | secondary | {profile['primary_metric']}):
        equivalent = {item['status'] for key, item in stored.items()
                      if canonical_metric(key) == canonical_metric(metric)}
        effects[metric] = (equivalent.pop() if len(equivalent) == 1 and context_selection_measures(metric)
                           else 'UNKNOWN')
    hard_status = {metric: 'CONFLICT' if effects[metric] == 'REGRESSED' else 'UNKNOWN'
                   for metric in sorted(hard_metrics)}
    reuse = reuse_status(effects, hard_status)
    return {'objective_applicability': reuse, 'metric_effects': effects, 'hard_constraint_statuses': hard_status}


def reuse_status(effects, hard_status):
    if 'CONFLICT' in hard_status.values():
        return 'HARD_CONSTRAINT_CONFLICT'
    if hard_status or 'UNKNOWN' in effects.values():
        return 'COMPATIBILITY_UNKNOWN'
    if 'REGRESSED' in effects.values():
        return 'TRADEOFF'
    return 'DIRECT_REUSE'

"""Pure parent scheduling for the query seam; scheduling grants no admission."""
from collections import Counter
from copy import deepcopy
import math

from .objectives import (METRIC_CONTRACTS, canonical_metric, constraint_evidence_status,
                         validate_context_selection_objective, validate_profile)
from .state import digest


SCHEMA = 'rsi-query-parent-selection/v1'
MODES = {'latest', 'all_eligible', 'pareto', 'seeded_multi_branch'}
ELIGIBLE_KINDS = {'QUALIFIED_ROOT', 'TRAINING_EXPLORATION'}
RELATIVE_AXES = {'parent_chars', 'shorter_cases', 'longer_cases', 'unchanged_cases'}


def validate_parent_selection(spec, objective):
    """An absent policy preserves legacy scheduling; explicit policies need a Profile."""
    if spec is None:
        return None
    if (not isinstance(spec, dict) or set(spec) != {'schema', 'mode', 'seed'}
            or spec['schema'] != SCHEMA or not isinstance(spec['mode'], str) or spec['mode'] not in MODES
            or type(spec['seed']) is not int or spec['seed'] < 0):
        raise ValueError('INVALID_QUERY_PARENT_SELECTION')
    profile = validate_profile(objective)
    validate_context_selection_objective(profile)
    if canonical_metric(profile['primary_metric']) != 'context_chars':
        raise ValueError('QUERY_PARENT_PRIMARY_MUST_BE_ABSOLUTE')
    return deepcopy(spec)


def _complete_rows(decision):
    if not isinstance(decision, dict):
        return None
    rows = decision.get('per_question')
    count = decision.get('eligible_count')
    if (decision.get('schema_version') != 'rsi-candidate-decision/v2.2'
            or type(count) is not int or count <= 0 or not isinstance(rows, list)
            or len(rows) != count or not all(isinstance(row, dict) for row in rows)):
        return None
    qids = [row.get('qid') for row in rows]
    if (not all(isinstance(qid, str) and qid for qid in qids) or len(set(qids)) != count
            or not all(row.get('pair_status') == 'PAIRED' and row.get('pair_identity') is True
                       and row.get('execution') == 'COMPLETED'
                       and all(type(row.get(key)) is int and row[key] >= 0
                               for key in ('chars_A', 'chars_B')) for row in rows)):
        return None
    return rows


def _observed_status(values):
    if not values:
        return 'UNKNOWN'
    if any(value in ('ABSENT', 'MISSING', 'FAIL') for value in values):
        return 'FAIL'
    return 'PASS' if all(value in ('PRESENT', 'PASS') for value in values) else 'UNKNOWN'


def measure_parent(suite_decisions, *, arm='B'):
    """Measure absolute primary size and all-suite audited fact/provenance coverage."""
    if arm not in ('A', 'B'):
        raise ValueError('INVALID_QUERY_PARENT_ARM')
    metrics = {metric: None for metric in METRIC_CONTRACTS}
    metrics.update(necessary_fact_status='UNKNOWN', provenance_status='UNKNOWN')
    result = {'metrics': metrics, 'cohort_id': None}
    if not isinstance(suite_decisions, (list, tuple)) or not suite_decisions:
        return result
    primary = _complete_rows(suite_decisions[0])
    if primary is not None:
        metrics['context_chars'] = metrics['candidate_chars'] = sum(row['chars_' + arm] for row in primary)
        identities = [(row['qid'], row.get('upstream_hash'), row.get('execution_environment_hash'))
                      for row in primary]
        if all(isinstance(value, str) and value not in ('', 'UNKNOWN')
               for identity in identities for value in identity):
            result['cohort_id'] = digest(sorted(identities))
    fact_values, provenance_values = [], []
    for decision in suite_decisions:
        rows = _complete_rows(decision)
        if rows is None:
            fact_values.append('UNKNOWN')
            provenance_values.append('UNKNOWN')
            continue
        for row in rows:
            parent, candidate = row.get('parent_facts'), row.get('candidate_facts')
            if (not isinstance(parent, dict) or not parent or not isinstance(candidate, dict)
                    or set(parent) != set(candidate)):
                fact_values.append('UNKNOWN')
            else:
                fact_values.extend((parent if arm == 'A' else candidate).values())
            provenance_values.append(row.get('provenance_' + arm))
    metrics['necessary_fact_status'] = _observed_status(fact_values)
    metrics['provenance_status'] = _observed_status(provenance_values)
    return result


def _numeric(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _value(metrics, metric):
    if metric in RELATIVE_AXES:
        return None
    if metric in ('context_chars', 'candidate_chars'):
        context, candidate = metrics.get('context_chars'), metrics.get('candidate_chars')
        if context is not None and candidate is not None and context != candidate:
            return None
        return context if context is not None else candidate
    return metrics.get(metric)


def select_parent(records, spec, objective, previous_selected_ids=()):
    """Return a deterministic, reasoned schedule over already admitted parent records."""
    spec = validate_parent_selection(spec, objective)
    mode = spec['mode'] if spec else 'latest'
    profile = validate_profile(objective) if objective is not None else None
    if profile is not None:
        validate_context_selection_objective(profile)
        if canonical_metric(profile['primary_metric']) != 'context_chars':
            raise ValueError('QUERY_PARENT_PRIMARY_MUST_BE_ABSOLUTE')
    records = list(records)
    identifiers = [record.get('candidate_id') for record in records]
    if (not all(isinstance(value, str) and value for value in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        raise ValueError('QUERY_PARENT_DUPLICATE_OR_INVALID_ID')
    counts = Counter(previous_selected_ids)
    population = []
    primary_metric = profile['primary_metric'] if profile else 'context_chars'
    for record in records:
        metrics = record.get('metrics') or {}
        reasons = []
        if record.get('excluded_reason'):
            reasons.append(record['excluded_reason'])
        if record.get('kind') not in ELIGIBLE_KINDS:
            reasons.append('PARENT_KIND_NOT_ELIGIBLE')
        if type(record.get('admission_round')) is not int or record['admission_round'] < 0:
            reasons.append('INVALID_ADMISSION_ROUND')
        primary = _value(metrics, primary_metric)
        if not _numeric(primary):
            reasons.append('PRIMARY_METRIC_UNKNOWN')
        constraints = []
        for constraint in profile['hard_constraints'] if profile else ():
            value = _value(metrics, constraint['metric_id'])
            known = (value in ('PASS', 'FAIL') if METRIC_CONTRACTS[constraint['metric_id']]['unit'] == 'status'
                     else _numeric(value))
            status = (constraint_evidence_status(constraint, measured_value=value) if known else 'UNKNOWN')
            constraints.append({'metric_id': constraint['metric_id'], 'status': status})
        constraint_status = ('FAIL' if any(item['status'] == 'CONFIRMED_FAIL' for item in constraints)
                             else 'UNKNOWN' if any(item['status'] != 'PASS' for item in constraints) else 'PASS')
        if constraint_status != 'PASS':
            reasons.append('HARD_CONSTRAINT_' + constraint_status)
        prior_reasons = record.get('reason_codes', [])
        prior_reasons = prior_reasons if isinstance(prior_reasons, list) else []
        population.append({**deepcopy(record), 'eligible': not reasons,
                           'reason_codes': list(dict.fromkeys([*prior_reasons, *reasons])),
                           'constraint_status': constraint_status, 'constraint_results': constraints,
                           'primary_metric_value': primary if _numeric(primary) else None,
                           'selection_count': counts[record['candidate_id']],
                           'pareto_status': 'UNKNOWN', 'dominated_by': []})
    eligible = [row for row in population if row['eligible']]
    axes = [primary_metric, *(profile['secondary_metrics'] if profile else ())]
    axes = list(dict.fromkeys(metric for metric in axes if metric not in RELATIVE_AXES))
    for row in eligible:
        cohort = row.get('cohort_id')
        peers = [other for other in eligible if other.get('cohort_id') == cohort]
        if not isinstance(cohort, str) or not cohort:
            continue
        if not all(_numeric(_value(peer['metrics'], metric)) for peer in peers for metric in axes):
            continue
        def dominates(other):
            left = [_value(other['metrics'], metric) * (1 if METRIC_CONTRACTS[metric]['direction'] == 'min' else -1)
                    for metric in axes]
            right = [_value(row['metrics'], metric) * (1 if METRIC_CONTRACTS[metric]['direction'] == 'min' else -1)
                     for metric in axes]
            return all(a <= b for a, b in zip(left, right)) and any(a < b for a, b in zip(left, right))
        row['dominated_by'] = sorted(other['candidate_id'] for other in peers if dominates(other))
        row['pareto_status'] = 'DOMINATED' if row['dominated_by'] else 'FRONTIER'
    frontier = sorted(row['candidate_id'] for row in eligible if row['pareto_status'] == 'FRONTIER')
    stable = lambda row: (row['admission_round'], row['candidate_id'])
    selected = None
    if eligible:
        if mode == 'latest':
            selected = max(eligible, key=stable)
        elif mode == 'all_eligible':
            selected = min(eligible, key=lambda row: (row['selection_count'], *stable(row)))
        elif mode == 'seeded_multi_branch':
            selected = min(eligible, key=lambda row: (row['selection_count'],
                digest([spec['seed'], row['candidate_id'], len(previous_selected_ids)]), *stable(row)))
        else:
            candidates = [row for row in eligible if row['pareto_status'] != 'DOMINATED']
            # Pick a cohort without comparing its numeric measurements with another
            # cohort. Each absent cohort is its own incomparable singleton.
            anchor = min(candidates, key=lambda row: (row['selection_count'], *stable(row)))
            cohort = anchor.get('cohort_id')
            candidates = ([row for row in candidates if row.get('cohort_id') == cohort]
                          if isinstance(cohort, str) and cohort else [anchor])
            # Unknown optional axes remain unknown; omit them from tie breaking.
            common = [metric for metric in (profile['tie_breakers'] if profile else ())
                      if metric not in RELATIVE_AXES
                      and all(_numeric(_value(row['metrics'], metric)) for row in candidates)]
            def preference(row):
                values = [_value(row['metrics'], metric) * (1 if METRIC_CONTRACTS[metric]['direction'] == 'min' else -1)
                          for metric in [primary_metric, *common]]
                return (*values, row['selection_count'], *stable(row))
            selected = min(candidates, key=preference)
    return {'selected_candidate_id': selected['candidate_id'] if selected else None,
            'selection_mode': mode, 'population': population, 'pareto_candidate_ids': frontier}

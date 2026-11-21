"""Deterministic paired F5 gate; no source authentication or I/O occurs here.

The caller must reconstruct rows from trusted live evidence bound to the exact
frozen panel and configuration profile. These calculations alone prove neither
that provenance nor eligibility. Missing grades remain UNKNOWN.
"""

from collections.abc import Mapping, Sequence
from math import comb, fsum, isfinite
from numbers import Real
from random import Random


_GRADES = {'INCORRECT': 0, 'CORE_ONLY': 1, 'COMPLETE_CORRECT': 2}
_GRADE_METRICS = {'complete_correct', 'core_correct'}
_DIRECTIONS = {'maximize', 'minimize'}


def _number(value, name):
    if isinstance(value, bool) or not isinstance(value, Real):
        raise ValueError(f'{name} must be a finite number')
    try:
        number = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f'{name} must be a finite number') from exc
    if not isfinite(number):
        raise ValueError(f'{name} must be a finite number')
    return number


def _identifier(value, name):
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f'{name} must be a nonempty canonical string')
    return value


def _strings(value, name):
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f'{name} must be a sequence of strings')
    values = [_identifier(item, name) for item in value]
    if len(set(values)) != len(values):
        raise ValueError(f'{name} contains duplicates')
    return values


def _metric_value(row, side, metric):
    grade = row[f'{side}_grade']
    if metric == 'complete_correct':
        return float(grade == 'COMPLETE_CORRECT')
    if metric == 'core_correct':
        return float(grade in ('CORE_ONLY', 'COMPLETE_CORRECT'))
    value = row[f'{side}_metrics'].get(metric)
    return float(value) if value is not None else None


def _effect(rows, metric, direction):
    measured = []
    for row in rows:
        parent = _metric_value(row, 'parent', metric)
        candidate = _metric_value(row, 'candidate', metric)
        if parent is not None and candidate is not None:
            measured.append((parent, candidate))
    result = {'metric': metric, 'direction': direction,
              'paired_count': len(measured), 'required_count': len(rows),
              'parent_mean': None, 'candidate_mean': None,
              'raw_difference': None, 'improvement': None, 'paired_bootstrap_ci95': None}
    if measured:
        # Scale before summation to avoid overflowing otherwise finite means.
        count = len(measured)
        parent_mean = fsum(parent / count for parent, _ in measured)
        candidate_mean = fsum(candidate / count for _, candidate in measured)
        difference = candidate_mean - parent_mean
        if not isfinite(difference):
            raise ValueError(f'{metric} aggregate difference is not finite')
        result.update(parent_mean=parent_mean, candidate_mean=candidate_mean,
                      raw_difference=difference,
                      improvement=difference if direction == 'maximize' else -difference)
        if count > 1 and metric not in _GRADE_METRICS:
            # Resample whole pairs. The interval describes candidate minus parent.
            deltas = [candidate / count - parent / count for parent, candidate in measured]
            rng = Random(0)
            replicates = sorted(fsum(rng.choices(deltas, k=count)) for _ in range(1000))
            result['paired_bootstrap_ci95'] = [replicates[25], replicates[974]]
    return result


def adjudicate_pairs(*, phase, rows, objective):
    """Evaluate trusted paired rows against an explicit predeclared objective.

    DEV60 requires 54/60 valid grade pairs; DEV200 requires 190/200. Every
    explicitly declared critical stratum requires ten valid pairs. Metrics must
    be measured on *all* valid grade pairs, including protected metrics. An
    insufficient panel/measurement is INCONCLUSIVE, while an observed objective
    or protection failure is REJECTED. Schema errors raise ValueError.
    """
    if type(phase) is int and phase in (60, 200):
        phase = f'DEV{phase}'
    if not isinstance(phase, str) or phase not in ('DEV60', 'DEV200'):
        raise ValueError('phase must be DEV60 or DEV200')
    expected = 60 if phase == 'DEV60' else 200
    required = 54 if phase == 'DEV60' else 190
    if not isinstance(objective, Mapping):
        raise ValueError('objective must be a mapping')
    required_fields = {'metric', 'direction', 'minimum_effect',
                       'maximum_grade_regression_rate', 'critical_strata',
                       'protected_metrics', 'accept_quality_tradeoff'}
    if not required_fields.issubset(objective):
        raise ValueError('objective requires all predeclared fields')
    metric = _identifier(objective['metric'], 'metric')
    direction = objective['direction']
    if not isinstance(direction, str) or direction not in _DIRECTIONS:
        raise ValueError('direction must be maximize or minimize')
    minimum = _number(objective['minimum_effect'], 'minimum_effect')
    maximum_rate = _number(objective['maximum_grade_regression_rate'],
                           'maximum_grade_regression_rate')
    if minimum <= 0 or not 0 <= maximum_rate <= 1:
        raise ValueError('minimum_effect must be positive and regression rate within [0,1]')
    critical = _strings(objective['critical_strata'], 'critical_strata')
    if type(objective['accept_quality_tradeoff']) is not bool:
        raise ValueError('accept_quality_tradeoff must be an explicit boolean')
    protected = objective['protected_metrics']
    if not isinstance(protected, Mapping):
        raise ValueError('protected_metrics must be a mapping')
    protections = {}
    for name, rule in protected.items():
        _identifier(name, 'protected metric')
        if not isinstance(rule, Mapping) or not {'direction', 'maximum_regression'}.issubset(rule):
            raise ValueError('protected metric requires direction and maximum_regression')
        if not isinstance(rule['direction'], str) or rule['direction'] not in _DIRECTIONS:
            raise ValueError('protected metric direction must be maximize or minimize')
        limit = _number(rule['maximum_regression'], 'maximum_regression')
        if limit < 0:
            raise ValueError('maximum_regression must be nonnegative')
        protections[name] = (rule['direction'], limit)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise ValueError('rows must be a sequence of mappings')
    if len(rows) > expected:
        raise ValueError('rows exceeds the frozen panel size')
    seen = set()
    valid = []
    unknown = []
    for row in rows:
        if not isinstance(row, Mapping) or not {
                'qid', 'strata', 'parent_grade', 'candidate_grade',
                'parent_metrics', 'candidate_metrics'}.issubset(row):
            raise ValueError('row requires qid, strata, grades and metric mappings')
        qid = _identifier(row['qid'], 'qid')
        if qid in seen:
            raise ValueError('duplicate qid')
        seen.add(qid)
        _strings(row['strata'], 'strata')
        for side in ('parent', 'candidate'):
            grade = row[f'{side}_grade']
            if grade is not None and (not isinstance(grade, str) or grade not in _GRADES):
                raise ValueError('grade must be COMPLETE_CORRECT, CORE_ONLY, INCORRECT or None')
            metrics = row[f'{side}_metrics']
            if not isinstance(metrics, Mapping):
                raise ValueError('metrics must be mappings of finite numbers')
            for name, value in metrics.items():
                _identifier(name, 'metric name')
                if name in _GRADE_METRICS:
                    raise ValueError('grade metrics must be derived from grades')
                _number(value, name)
        if row['parent_grade'] is None or row['candidate_grade'] is None:
            unknown.append(qid)
        else:
            valid.append(row)

    count = len(valid)
    wins = sum(_GRADES[row['candidate_grade']] > _GRADES[row['parent_grade']] for row in valid)
    losses = sum(_GRADES[row['candidate_grade']] < _GRADES[row['parent_grade']] for row in valid)
    strata_counts = {name: sum(name in row['strata'] for row in valid) for name in critical}
    primary = _effect(valid, metric, direction)
    protected_effects = {name: _effect(valid, name, rule[0]) for name, rule in protections.items()}
    incomplete = []
    failures = []
    if len(rows) != expected:
        incomplete.append('INCOMPLETE_PANEL')
    if count < required:
        incomplete.append('INSUFFICIENT_VALID_PAIRS')
    for name, paired_count in strata_counts.items():
        if paired_count < 10:
            incomplete.append(f'INSUFFICIENT_CRITICAL_STRATUM:{name}')
    if count == 0 or primary['paired_count'] != count:
        incomplete.append('PRIMARY_METRIC_INCOMPLETE')
    elif primary['improvement'] < minimum:
        failures.append('PRIMARY_EFFECT_BELOW_MINIMUM')
    for name, effect in protected_effects.items():
        if count == 0 or effect['paired_count'] != count:
            incomplete.append(f'PROTECTED_METRIC_INCOMPLETE:{name}')
        elif effect['improvement'] < -protections[name][1]:
            failures.append(f'PROTECTED_METRIC_REGRESSION:{name}')
    regression_rate = losses / count if count else None
    if count and regression_rate > maximum_rate:
        failures.append('GRADE_REGRESSION_RATE_EXCEEDED')
    if losses > wins and not objective['accept_quality_tradeoff']:
        failures.append('QUALITY_TRADEOFF_NOT_ACCEPTED')
    decision = ('INCONCLUSIVE' if incomplete else 'REJECTED' if failures else
                'ADVANCE' if phase == 'DEV60' else 'ADMITTED')
    quality_effects = {}
    for quality_metric in sorted(_GRADE_METRICS):
        effect = _effect(valid, quality_metric, 'maximize')
        quality_wins = sum(_metric_value(row, 'candidate', quality_metric) >
                           _metric_value(row, 'parent', quality_metric) for row in valid)
        quality_losses = sum(_metric_value(row, 'candidate', quality_metric) <
                             _metric_value(row, 'parent', quality_metric) for row in valid)
        discordant = quality_wins + quality_losses
        # Exact two-sided conditional binomial test, reported without gating.
        probability = (min(1.0, 2 * sum(comb(discordant, index)
                        for index in range(min(quality_wins, quality_losses) + 1)) /
                        (2 ** discordant)) if discordant else 1.0)
        effect.update(paired_wins=quality_wins, paired_losses=quality_losses,
                      discordant_pairs=discordant, mcnemar_exact_two_sided_p=probability)
        quality_effects[quality_metric] = effect
    return {'phase': phase, 'decision': decision,
            'reason_codes': incomplete + failures,
            'expected_pairs': expected, 'observed_pairs': len(rows),
            'minimum_valid_pairs': required, 'valid_pairs': count,
            'unknown_pairs': len(unknown), 'unknown_qids': unknown,
            'missing_pairs': expected - len(rows), 'critical_strata_pairs': strata_counts,
            'grade_wins': wins, 'grade_losses': losses, 'grade_ties': count - wins - losses,
            'grade_regression_rate': regression_rate,
            'primary_effect': primary, 'protected_effects': protected_effects,
            'quality_effects': quality_effects}

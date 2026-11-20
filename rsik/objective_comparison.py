"""Hash-bound, read-only historical objective comparisons; never admission decisions."""
from collections import defaultdict
from pathlib import Path
import math
import operator

from .analytics.reader import canonical, digest, read_artifact
from .analytics.projections import project_sources
from .locking import atomic_write
from .objectives import METRIC_CONTRACTS, validate_profile


_NUMERIC = ('parent_chars', 'candidate_chars', 'shorter_cases', 'longer_cases', 'unchanged_cases')
_OPERATORS = {'==': operator.eq, '!=': operator.ne, '<=': operator.le,
              '>=': operator.ge, '<': operator.lt, '>': operator.gt,
              'eq': operator.eq, 'ne': operator.ne, 'lte': operator.le,
              'gte': operator.ge, 'lt': operator.lt, 'gt': operator.gt}


def _status(values):
    if not values or any(value not in {'PRESENT', 'ABSENT', 'MISSING', 'FAIL', 'PASS'} for value in values):
        return None
    return 'FAIL' if any(value in {'ABSENT', 'MISSING', 'FAIL'} for value in values) else 'PASS'


def _measure(decision, projected):
    """Use S1 aggregates only after proving an exact, unique complete pair set."""
    values = {metric: None for metric in METRIC_CONTRACTS}
    statuses = {metric: 'UNKNOWN' for metric in METRIC_CONTRACTS}
    rows = decision.get('per_question') or []
    eligible = decision.get('eligible_count')
    measurements = projected.get('context_measurements', {})
    complete = (decision.get('schema_version') == 'rsi-candidate-decision/v2.2'
                and type(eligible) is int and eligible > 0 and len(rows) == eligible
                and measurements.get('status') == 'KNOWN'
                and len({row.get('qid') for row in rows}) == eligible
                and all(isinstance(row.get('qid'), str) and row['qid'] for row in rows)
                and all(row.get('pair_status') == 'PAIRED' and row.get('pair_identity') is True
                        and row.get('execution') == 'COMPLETED'
                        and all(type(row.get(key)) is int and row[key] >= 0
                                for key in ('chars_A', 'chars_B')) for row in rows))
    if not complete:
        return values, statuses, False
    for metric in _NUMERIC:
        values[metric] = measurements[metric]
        statuses[metric] = 'KNOWN'
    values['context_chars'] = values['candidate_chars']
    statuses['context_chars'] = 'KNOWN'
    facts = [row.get('candidate_facts') for row in rows]
    if all(isinstance(fact, dict) and fact
           and isinstance(row.get('parent_facts'), dict) and row['parent_facts']
           and set(fact) == set(row['parent_facts']) for row, fact in zip(rows, facts)):
        values['necessary_fact_status'] = _status([value for fact in facts for value in fact.values()])
    values['provenance_status'] = _status([row.get('provenance_B') for row in rows])
    for metric in ('necessary_fact_status', 'provenance_status'):
        if values[metric] is not None:
            statuses[metric] = 'KNOWN'
    # A duration in arbitrary metadata is not an execution receipt. Historical
    # rounds do not bind a complete timing receipt, so replay_seconds stays UNKNOWN.
    return values, statuses, True


def _cohort(data, decision, artifact):
    rows = decision.get('per_question') or []
    row_scope = sorted(((row.get('qid'), row.get('upstream_hash'), row.get('source_group'),
                        row.get('execution_environment_hash')) for row in rows), key=canonical)
    scope = {'pairs': row_scope, 'decision_schema': decision.get('schema_version'),
             'evidence_mode': data.get('evidence_mode', 'UNKNOWN'),
             'evaluator_version': data.get('evaluator_version', 'UNKNOWN'),
             'seam_profile': data.get('seam_profile', 'UNKNOWN'),
             'fixture_freezes': data.get('fixture_freezes', {})}
    # Missing evaluation/evidence identities cannot establish comparability across
    # archives. Same-archive candidates still share the frozen evaluator context.
    missing_pair_identity = any(not isinstance(row.get(key), str) or row[key] in {'', 'UNKNOWN'}
                                for row in rows for key in
                                ('qid', 'upstream_hash', 'source_group', 'execution_environment_hash'))
    if missing_pair_identity or any(scope[key] == 'UNKNOWN' for key in ('evidence_mode', 'evaluator_version')):
        scope['unknown_scope_archive_sha256'] = artifact['sha256']
    return scope


def _constraint_status(profile, values, statuses):
    results = []
    for constraint in profile['hard_constraints']:
        metric = constraint['metric_id']
        if statuses.get(metric) != 'KNOWN':
            results.append('UNKNOWN')
            continue
        comparison = _OPERATORS.get(constraint['operator'])
        if comparison is None:
            raise ValueError('OBJECTIVE_COMPARISON_OPERATOR_UNSUPPORTED')
        try:
            results.append('PASS' if comparison(values[metric], constraint['value']) else 'FAIL')
        except TypeError:
            results.append('UNKNOWN')
    return 'FAIL' if 'FAIL' in results else 'UNKNOWN' if 'UNKNOWN' in results else 'PASS'


def _numeric(value):
    return type(value) in (int, float) and math.isfinite(value)


def _dominates(left, right, metrics, directions):
    """Same weak/strict Pareto relation used by ParentPool, for frozen axes."""
    left_values = [left['metric_values'][metric] * (1 if directions[metric] == 'min' else -1)
                   for metric in metrics]
    right_values = [right['metric_values'][metric] * (1 if directions[metric] == 'min' else -1)
                    for metric in metrics]
    return all(a <= b for a, b in zip(left_values, right_values)) and any(
        a < b for a, b in zip(left_values, right_values))


def compare_historical(profile, artifacts, *, metric_contracts=None):
    """Compare public round artifact specs; ranks are local to compatible cohorts.

    `artifacts` contains explicit path/sha256/kind records as in the S1 historical
    manifest. No referenced worker, database, restricted source, or receipt is read.
    Unknown metrics never receive numeric defaults. Required contract definitions
    are frozen in the result, independently of objective preference.
    """
    profile = validate_profile(profile)
    contracts = METRIC_CONTRACTS if metric_contracts is None else metric_contracts
    requested = list(dict.fromkeys([profile['primary_metric'], *profile['secondary_metrics'],
                                   *profile['tie_breakers'], *profile['required_metric_contracts'],
                                   *(item['metric_id'] for item in profile['hard_constraints'])]))
    for metric in requested:
        if metric not in contracts:
            raise ValueError('OBJECTIVE_COMPARISON_CONTRACT_MISSING:' + metric)
    loaded = []
    for spec in artifacts:
        if spec['kind'] != 'round':
            raise ValueError('OBJECTIVE_COMPARISON_ROUND_REQUIRED')
        loaded.append(read_artifact(spec['path'], spec['sha256'], spec['kind']))
    # Reuse all archive identity checks and S1 complete-pair aggregation.
    projections = project_sources([], loaded)
    projected = {record['id']: record['payload'] for record in projections
                 if record['projection_type'] == 'Candidate'}
    results, seen = [], set()
    directions = {metric: contracts[metric]['direction'] for metric in requested}
    directions[profile['primary_metric']] = profile['direction']
    axes = list(dict.fromkeys([profile['primary_metric'], *profile['secondary_metrics']]))
    ranking = list(dict.fromkeys([profile['primary_metric'], *profile['tie_breakers'],
                                 *profile['secondary_metrics']]))
    for artifact in loaded:
        data = artifact['data']
        for decision in data.get('candidate_decisions', []):
            identity = artifact['source_id'] + ':candidate:' + decision['angle']
            if identity in seen:
                continue
            seen.add(identity)
            values, statuses, complete = _measure(decision, projected[identity])
            contract_statuses = {}
            for metric in requested:
                supplied = contracts[metric]
                known = METRIC_CONTRACTS.get(metric)
                expected_version = profile['required_metric_contracts'].get(
                    metric, supplied.get('metric_contract_version'))
                supported = (known == supplied and supplied.get('metric_contract_version') == expected_version)
                contract_statuses[metric] = 'MATCH' if supported else 'MISMATCH'
                if not supported:
                    values[metric], statuses[metric] = None, 'CONTRACT_MISMATCH'
            constraint_status = _constraint_status(profile, values, statuses)
            primary = profile['primary_metric']
            primary_known = statuses.get(primary) == 'KNOWN' and _numeric(values.get(primary))
            comparable = (bool(decision.get('candidate_id')) and complete and primary_known
                          and constraint_status == 'PASS')
            scope = _cohort(data, decision, artifact)
            scope['metric_contracts'] = {metric: digest(contracts[metric]) for metric in requested}
            scope['primary_direction'] = profile['direction']
            proposal = next((proposal for proposal in data.get('proposal_instances', {}).values()
                             if proposal.get('proposal_id') == decision.get('proposal_id')), {})
            result = {'candidate_id': decision.get('candidate_id'), 'parent_id': data.get('parent_id', proposal.get('parent_id')),
                      'proposal_id': decision.get('proposal_id'), 'round_id': data['round_id'],
                      'lineage_id': data.get('lineage_id'), 'comparison_id': identity,
                      'objective_profile_id': profile['objective_profile_id'], 'objective_version': profile['version'],
                      'metric_values': values, 'metric_statuses': statuses, 'metric_contract_statuses': contract_statuses,
                      'constraint_status': constraint_status, 'primary_metric_value': values.get(profile['primary_metric']),
                      'comparison_status': 'COMPARABLE' if comparable else 'CONSTRAINT_FAILED' if constraint_status == 'FAIL' else 'NOT_COMPARABLE',
                      'complete_pair_evidence': complete, 'cohort_id': digest(scope), 'cohort': scope,
                      'rank': None, 'pareto_status': 'UNKNOWN', 'dominated_by': [],
                      'historical_verdict': decision.get('verdict', 'UNKNOWN'),
                      'historical_gates_passed': decision.get('gates_passed', 'UNKNOWN'),
                      'historical_adopted': projected[identity].get('adopted', 'UNKNOWN'),
                      'admission_effect': 'NONE',
                      'evidence_refs': [{key: artifact[key] for key in ('source_id', 'path', 'sha256')}],
                      'candidate_measurement_sha256': digest(decision)}
            results.append(result)
    groups = defaultdict(list)
    for result in results:
        if result['comparison_status'] == 'COMPARABLE':
            groups[result['cohort_id']].append(result)
    for cohort in groups.values():
        # Missing optional axes are retained, but cannot enter numeric comparisons.
        complete_axes = all(all(item['metric_statuses'].get(metric) == 'KNOWN'
                                and _numeric(item['metric_values'].get(metric)) for metric in axes)
                            for item in cohort)
        ranked_axes = [metric for metric in ranking if all(
            item['metric_statuses'].get(metric) == 'KNOWN' and _numeric(item['metric_values'].get(metric))
            for item in cohort)]
        for item in cohort:
            item['ranking_metrics'] = ranked_axes
            if complete_axes:
                item['dominated_by'] = sorted(other['comparison_id'] for other in cohort
                                              if _dominates(other, item, axes, directions))
                item['pareto_status'] = 'DOMINATED' if item['dominated_by'] else 'FRONTIER'
        def metric_key(item):
            return tuple(item['metric_values'][metric] * (1 if directions[metric] == 'min' else -1)
                         for metric in ranked_axes)
        ordered = sorted(cohort, key=lambda item: metric_key(item) + (item['candidate_id'], item['comparison_id']))
        previous, rank = None, None
        for position, item in enumerate(ordered, 1):
            key = metric_key(item)
            if key != previous:
                rank = position
            item['rank'] = rank
            previous = key
    result = {'schema_version': 'rsi-objective-comparison/v1', 'objective_profile': profile,
              'metric_contracts': {metric: contracts[metric] for metric in requested},
              'comparison_scope': 'WITHIN_COHORT_ONLY', 'admission_effect': 'NONE',
              'candidates': sorted(results, key=lambda item: item['comparison_id']),
              'cohort_count': len(groups), 'ranked_count': sum(map(len, groups.values()))}
    # Canonical roundtrip freezes caller-owned mutable structures too.
    import json
    return json.loads(canonical(result))


def write_comparison(result, path):
    """Create one immutable comparison artifact; never overwrite an existing file."""
    if result.get('schema_version') != 'rsi-objective-comparison/v1':
        raise ValueError('OBJECTIVE_COMPARISON_SCHEMA_INVALID')
    payload = canonical(result)
    atomic_write(Path(path), payload, exclusive=True)
    return {'path': str(Path(path).resolve()), 'sha256': digest(result)}



def main(argv=None):
    """Compare explicit archived inputs from the command line without model calls."""
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', required=True, type=Path, help='Frozen Objective Profile JSON')
    parser.add_argument('--manifest', required=True, type=Path, help='Hash-bound artifact manifest JSON')
    parser.add_argument('--output', required=True, type=Path, help='New immutable comparison JSON path')
    arguments = parser.parse_args(argv)
    try:
        profile = json.loads(arguments.profile.read_text(encoding='utf-8-sig'))
        manifest_path = arguments.manifest.resolve(strict=True)
        manifest = json.loads(manifest_path.read_text(encoding='utf-8-sig'))
        artifacts = []
        for item in manifest['artifacts']:
            item = dict(item)
            path = Path(item['path'])
            item['path'] = str(path if path.is_absolute() else manifest_path.parent / path)
            artifacts.append(item)
        result = compare_historical(profile, artifacts)
        receipt = write_comparison(result, arguments.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.exit(2, 'OBJECTIVE_COMPARISON_FAILED: ' + str(error) + '\n')
    print(json.dumps({**receipt, 'ranked_count': result['ranked_count'],
                      'cohort_count': result['cohort_count']}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

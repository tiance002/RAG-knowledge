"""Offline three-arm accounting; never generates answers or fills missing arms.

Each row is one question/arm, with totals across *all* its attempts. The optional
planned_qids manifest is the denominator, including questions never dispatched.
evaluation_identity names the frozen experiment (including the model pair);
comparison_input_hash hashes identical delivered messages/evidence/prior context.
"""
import argparse
from collections import Counter
from itertools import combinations
import json
import math
from pathlib import Path
import statistics

ARMS = ('CHEAP_ONLY', 'DYNAMIC', 'EXPENSIVE_ONLY')
GRADES = ('PASS', 'PARTIAL', 'FAIL', 'UNKNOWN')


def _number(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def _grade(row):
    value = row.get('core_correctness')
    if value is None:
        value = 'UNKNOWN'
    if value not in GRADES:
        raise ValueError('invalid core_correctness; use PASS/PARTIAL/FAIL/UNKNOWN')
    return value


def summarize(rows, planned_qids=None):
    rows = list(rows)
    slots = {}
    versions = set()
    for row in rows:
        key = row['arm'], row['qid']
        if key in slots or row['arm'] not in ARMS:
            raise ValueError('duplicate comparison slot or invalid arm')
        if not isinstance(row['qid'], str) or not row['qid'].strip():
            raise ValueError('qid must be a nonempty string')
        identity = row.get('evaluation_identity')
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError('evaluation_identity must be a nonempty string')
        _grade(row)
        slots[key] = row
        versions.add(identity)
    if len(versions) > 1:
        raise ValueError('incomparable dataset/code/model/prompt/index/rubric identity')
    observed = {r['qid'] for r in rows}
    if planned_qids is None:
        planned = observed
        basis = 'OBSERVED_UNION_NOT_FROZEN_PLAN'
    else:
        manifest = list(planned_qids)
        if any(not isinstance(q, str) or not q.strip() for q in manifest):
            raise ValueError('planned_qids must contain nonempty strings')
        planned = set(manifest)
        if len(planned) != len(manifest) or not observed <= planned:
            raise ValueError('duplicate planned qid or observed qid outside plan')
        basis = 'FROZEN_QID_MANIFEST'
    result = {}
    for arm in ARMS:
        selected = [r for r in rows if r['arm'] == arm]
        n = len(planned)
        absent = n - len(selected)
        counts = Counter({g: 0 for g in GRADES})
        counts.update(_grade(r) for r in selected)
        counts['UNKNOWN'] += absent
        known = sum(counts[x] for x in GRADES[:3])
        execution = Counter(r.get('execution_status') or 'UNKNOWN' for r in selected)
        execution['NOT_RUN'] += absent
        latency = [r['latency_ms'] for r in selected if _number(r.get('latency_ms'))]
        def total(key):
            values = [r.get(key) for r in selected]
            return sum(values) if len(values) == n and n and all(_number(v) for v in values) else None
        missingness = {}
        for key in ('input_tokens', 'output_tokens', 'cost', 'latency_ms'):
            valid = sum(_number(r.get(key)) for r in selected)
            missingness[key] = {'valid_n': valid, 'missing_or_invalid_n': n - valid,
                                'invalid_n': sum(r.get(key) is not None and not _number(r[key]) for r in selected)}
        upgraded = sum(r.get('upgraded') is True for r in selected)
        upgrade_unknown = absent + sum(not isinstance(r.get('upgraded'), bool) for r in selected)
        currencies = [r.get('cost_currency') for r in selected]
        currency_known = (bool(currencies) and all(isinstance(c, str) and c.strip() for c in currencies)
                          and len(set(currencies)) == 1)
        cost = total('cost') if currency_known else None
        result[arm] = {
            'status': 'OBSERVED' if selected else 'NOT_RUN',
            'n_planned': n, 'denominator_basis': basis, 'n_observed': len(selected),
            'n_missing': absent, 'n_reviewed': known, 'n_unknown': counts['UNKNOWN'],
            'core_counts': dict(counts), 'execution_counts': dict(execution),
            'generation_counts': dict(Counter(r.get('generation_status') or 'UNKNOWN' for r in selected)),
            'finish_reason_counts': dict(Counter(r.get('finish_reason') or 'UNKNOWN' for r in selected)),
            'strict_core_pass_per_reviewed': counts['PASS'] / known if known else None,
            'confirmed_pass_per_planned': counts['PASS'] / n if n else None,
            'upgraded': upgraded, 'upgrade_unknown': upgrade_unknown,
            'upgrade_rate_per_planned': upgraded / n if n and not upgrade_unknown else None,
            'input_tokens': total('input_tokens'), 'output_tokens': total('output_tokens'),
            'cost': cost, 'cost_currency': currencies[0] if currency_known else None,
            'cost_status': 'UNKNOWN' if cost is None else 'REPORTED_NOT_BILL_VERIFIED',
            'metric_missingness': missingness,
            'latency_n': len(latency), 'p50_ms': statistics.median(latency) if latency else None,
            'p95_ms_nearest_rank': sorted(latency)[math.ceil(.95 * len(latency))-1] if latency else None,
            'latency_scope': 'observed per-question wall time including all attempts and upgrade',
            'sample_limitation': 'Small sample quantiles do not establish service SLO.'}
    by_arm = {a: {r['qid'] for r in rows if r['arm'] == a} for a in ARMS}
    shared = set.intersection(*by_arm.values())
    mismatch, unknown = [], []
    for qid in sorted(shared):
        hashes = [slots[a, qid].get('comparison_input_hash') for a in ARMS]
        if any(not isinstance(h, str) or not h.strip() for h in hashes):
            unknown.append(qid)
        elif len(set(hashes)) != 1:
            mismatch.append(qid)
    complete = bool(planned) and all(by_arm[a] == planned for a in ARMS)
    status = ('NOT_RUN' if not all(by_arm.values()) else
              'INCOMPARABLE' if mismatch else 'PARTIAL' if not complete else
              'METADATA_UNKNOWN' if unknown else 'AVAILABLE')
    paired = {}
    rank = {'FAIL': 0, 'PARTIAL': 1, 'PASS': 2}
    for left, right in combinations(ARMS, 2):
        pairs = by_arm[left] & by_arm[right]
        counts = Counter({'left_better': 0, 'right_better': 0, 'same_coverage': 0,
                          'unknown_quality': 0, 'input_unknown': 0, 'input_mismatch': 0})
        for qid in pairs:
            lrow, rrow = slots[left, qid], slots[right, qid]
            hashes = [r.get('comparison_input_hash') for r in (lrow, rrow)]
            if any(not isinstance(h, str) or not h.strip() for h in hashes):
                counts['input_unknown'] += 1
            elif hashes[0] != hashes[1]:
                counts['input_mismatch'] += 1
            elif _grade(lrow) not in rank or _grade(rrow) not in rank:
                counts['unknown_quality'] += 1
            else:
                delta = rank[_grade(lrow)] - rank[_grade(rrow)]
                counts['left_better' if delta > 0 else 'right_better' if delta < 0 else 'same_coverage'] += 1
        paired[left + '_vs_' + right] = {'n_shared': len(pairs), **counts}
    result['comparison'] = {
        'status': status, 'denominator_basis': basis, 'shared_qids': sorted(shared),
        'missing_qids_by_arm': {a: sorted(planned - by_arm[a]) for a in ARMS},
        'input_mismatch_qids': mismatch, 'input_unknown_qids': unknown,
        'paired_core_coverage': paired, 'cost_savings': None,
        'note': 'Coverage is not full factuality. Costs require complete attempts, currency/pricing identity and billing review before savings claims.'}
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--records', required=True, type=Path)
    parser.add_argument('--planned-qids', type=Path, help='Frozen JSON array of all planned qids')
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    rows = [json.loads(line) for line in args.records.read_text(encoding='utf8').splitlines() if line.strip()]
    planned = json.loads(args.planned_qids.read_text(encoding='utf8')) if args.planned_qids else None
    if args.planned_qids and not isinstance(planned, list):
        parser.error('--planned-qids must contain a JSON array')
    report = summarize(rows, planned)
    with args.output.open('x', encoding='utf8') as out:
        json.dump(report, out, ensure_ascii=False, indent=2, allow_nan=False)


if __name__ == '__main__':
    main()

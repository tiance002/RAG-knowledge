"""Summarize independently reviewed paired results without imputing missing data."""
import argparse
from collections import Counter
import json
from pathlib import Path

KNOWN = {'PASS', 'PARTIAL', 'FAIL'}


def glm_only_usage(pairs, records):
    """Attribute single-generation replay tokens using the matching provider response."""
    by_slot = {(r['qid'], r['arm']): r.get('outcome', {}) for r in records}
    def confirmed(row, qid, arm):
        outcome = by_slot.get((qid, arm), {})
        actual = outcome.get('actual_model')
        if (row.get('answer_sha256') and outcome.get('answer_sha256') == row['answer_sha256']
                and actual == row.get('model') and actual == outcome.get('model')):
            return actual
        return None

    glm, cheap = 'zai-org/GLM-5.3-Flash', 'Qwen/Qwen3.5-4B'
    fields = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    sums = {k: [0, 0] for k in fields}
    included = dynamic_glm = 0
    for qid, dynamic, baseline in pairs:
        dynamic_model = confirmed(dynamic, qid, 'AFFINITY_V2')
        if confirmed(baseline, qid, 'GLM_ONLY') != glm or dynamic_model not in (cheap, glm):
            continue
        if not all(type(r.get(k)) is int and r[k] >= 0 for r in (dynamic, baseline) for k in fields):
            continue
        included += 1
        uses_glm = dynamic['model'] == glm
        dynamic_glm += int(uses_glm)
        for key in fields:
            sums[key][0] += dynamic[key] if uses_glm else 0
            sums[key][1] += baseline[key]
    return {'n': included, 'excluded_missing_identity_or_usage': len(pairs) - included,
            'dynamic_glm_questions': dynamic_glm, 'dynamic_cheap_questions': included - dynamic_glm,
            'model': glm, 'usage': {
                k: {'dynamic': a, 'all_glm': b, 'reduction_percent': 100 * (b - a) / b if b else None}
                for k, (a, b) in sums.items()}}


def summarize(rows, records=()):
    if len({r['qid'] for r in rows}) != len(rows):
        raise ValueError('duplicate qid')
    metrics = {'planned': len(rows), 'states': {}, 'quality': {}, 'exclusions': Counter()}
    paired_quality, paired_tokens, completed_pairs = [], [], []
    for arm in ('dynamic', 'glm'):
        metrics['states'][arm] = dict(Counter(r[arm].get('state', 'NOT_RUN') for r in rows))
        metrics['quality'][arm] = dict(Counter(r[arm].get('core', 'UNKNOWN') for r in rows))
    for row in rows:
        a, b = row['dynamic'], row['glm']
        if not row.get('same_messages_confirmed'):
            metrics['exclusions']['input_identity_unknown_or_mismatch'] += 1
            continue
        if a.get('state') != 'COMPLETED' or b.get('state') != 'COMPLETED':
            metrics['exclusions']['incomplete_generation'] += 1
            continue
        completed_pairs.append((row['qid'], a, b))
        if a.get('core') in KNOWN and b.get('core') in KNOWN:
            paired_quality.append((a['core'], b['core']))
        else:
            metrics['exclusions']['quality_unknown'] += 1
        ta, tb = a.get('total_tokens'), b.get('total_tokens')
        if type(ta) is int and ta >= 0 and type(tb) is int and tb >= 0:
            paired_tokens.append((ta, tb))
        else:
            metrics['exclusions']['usage_unknown'] += 1
    n = len(paired_quality)
    ac = sum(a == 'PASS' for a, _ in paired_quality)
    bc = sum(b == 'PASS' for _, b in paired_quality)
    dyn = sum(a for a, _ in paired_tokens)
    glm = sum(b for _, b in paired_tokens)
    metrics['paired_quality'] = {'n': n, 'dynamic_pass': ac, 'glm_pass': bc,
        'dynamic_accuracy': ac / n if n else None, 'glm_accuracy': bc / n if n else None,
        'accuracy_drop_percentage_points': 100 * (bc - ac) / n if n else None}
    metrics['paired_tokens'] = {'n': len(paired_tokens), 'dynamic_total': dyn,
        'glm_total': glm, 'dynamic_reduction_percent': 100 * (glm - dyn) / glm if glm else None}
    metrics['paired_glm_only_tokens'] = glm_only_usage(completed_pairs, records)
    metrics['exclusions'] = dict(metrics['exclusions'])
    metrics['definition'] = 'PASS/(PASS+PARTIAL+FAIL); same-question matched cohorts, UNKNOWN excluded; negative reduction means an increase'
    return metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('reviewed_json', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--generation-records', type=Path, help='Read provider identities from the paired replay export')
    args = parser.parse_args()
    data = json.loads(args.reviewed_json.read_text(encoding='utf-8-sig'))
    records = json.loads(args.generation_records.read_text(encoding='utf-8-sig')) if args.generation_records else ()
    result = summarize(data['rows'], records)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))

"""Summarize independently reviewed paired results without imputing missing data."""
import argparse
from collections import Counter
import json
from pathlib import Path

KNOWN = {'PASS', 'PARTIAL', 'FAIL'}


def summarize(rows):
    if len({r['qid'] for r in rows}) != len(rows):
        raise ValueError('duplicate qid')
    metrics = {'planned': len(rows), 'states': {}, 'quality': {}, 'exclusions': Counter()}
    paired_quality, paired_tokens = [], []
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
    metrics['exclusions'] = dict(metrics['exclusions'])
    metrics['definition'] = 'PASS/(PASS+PARTIAL+FAIL); same-question matched cohorts, UNKNOWN excluded; negative reduction means an increase'
    return metrics


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('reviewed_json', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.reviewed_json.read_text(encoding='utf-8-sig'))
    result = summarize(data['rows'])
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))

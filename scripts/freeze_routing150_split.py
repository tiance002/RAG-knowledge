"""Freeze a label-blind development/holdout split before provider generation."""
import collections
import hashlib
import json
from pathlib import Path


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def freeze(manifest):
    groups = collections.defaultdict(list)
    for case in manifest['cases']:
        gold = case['gold']
        fmt = gold.get('format') or ('pdf' if gold.get('expected_source_sha256') else 'text')
        identity = gold.get('source_sha256') or gold.get('expected_source_sha256')
        method = 'SOURCE_FILE_SHA256'
        if not identity:
            # Original text corpora are indexed by QA source item, not a verified
            # globally disjoint document graph. Do not claim no shared passages.
            identity = gold.get('original_case', {}).get('qid') or case['qid']
            method = 'ORIGINAL_QA_SOURCE_ITEM_NOT_CONTENT_DISJOINT'
        key = fmt + ':' + str(identity)
        groups[key].append((case['qid'], fmt, method))
    ordered = sorted(groups, key=lambda key: hashlib.sha256(key.encode()).hexdigest())
    selected = set()
    targets = {'pdf': 18, 'text': 16, 'docx': 8, 'xlsx': 8}
    counts = collections.Counter()
    for key in ordered:
        rows = groups[key]
        fmt = rows[0][1]
        if counts[fmt] + len(rows) <= targets.get(fmt, 0):
            selected.add(key)
            counts[fmt] += len(rows)
    for key in ordered:
        if key not in selected and sum(counts.values()) + len(groups[key]) <= 50:
            selected.add(key)
            counts[groups[key][0][1]] += len(groups[key])
    records = [dict(qid=qid, format=fmt, source_group_sha256=hashlib.sha256(key.encode()).hexdigest(),
                    grouping_method=method, split='HOLDOUT' if key in selected else 'DEVELOPMENT')
               for key in ordered for qid, fmt, method in groups[key]]
    return dict(manifest_sha256=hashlib.sha256(canonical(manifest)).hexdigest(),
                policy='label-blind-source-group-split-v1', created_before_generation=True,
                no_content_overlap_guarantee=False,
                records=records, counts=dict(collections.Counter(row['split'] for row in records)),
                holdout_by_format=dict(counts))


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('manifest', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    result = freeze(json.loads(args.manifest.read_text(encoding='utf8')))
    with args.output.open('x', encoding='utf8') as output:
        json.dump(result, output, ensure_ascii=False, indent=2)
    print(json.dumps({key: value for key, value in result.items() if key != 'records'}))

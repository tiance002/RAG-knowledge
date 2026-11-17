"""Replay frozen records without reconstructing missing stage observations."""
import argparse
import json
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.evaluation import score_record, compare_records

def replay(record, version):
    arm = record.get('arm')
    version = f'{version}:{arm}' if arm is not None else version
    # Only explicit matching identifier granularity may be compared.
    gold = record.get('expected_chunk_ids') or []
    observed = record.get('sources_at_10')
    top10 = None if observed is None else [x['chunk_id'] for x in observed if x.get('chunk_id')]
    if observed is not None and any(not x.get('chunk_id') for x in observed):
        top10 = None
    verdict = record.get('verdict')
    included = record.get('included_in_answer_accuracy')
    correctness = {'correct': 1, 'incorrect': 0}.get(verdict) if included is True else None
    answer = record.get('answer')
    finish = record.get('finish_reason')
    scores = {'core_correctness': correctness,
              'empty': (not bool(answer.strip())) if isinstance(answer, str) else (True if verdict == 'empty' else None),
              'skipped': True if verdict == 'skipped' else None,
              'truncated': (finish == 'length') if finish is not None else None,
              'latency_ms': record.get('latency_ms'), 'tokens': record.get('tokens'), 'cost': record.get('cost')}
    result = score_record(record['qid'], version, gold, {}, scores)
    # top10 is an observed historical output, but its relation to context/rerank is unspecified.
    result['observed_top10_chunk_ids'] = top10
    return result

def summarize(records):
    states = {stage: dict(Counter(r['evidence'][stage] for r in records)) for stage in ('raw', 'merged', 'reranked', 'context', 'cited')}
    return {'records': len(records), 'stage_states': states,
            'top10_observed': sum(r['observed_top10_chunk_ids'] is not None for r in records)}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('input', type=Path)
    parser.add_argument('--experiment-version', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--summary', type=Path)
    args = parser.parse_args()
    with args.input.open(encoding='utf-8') as src:
        records = [replay(json.loads(line), args.experiment_version) for line in src if line.strip()]
    compare_records(records)
    with args.output.open('w', encoding='utf-8') as dst:
        for record in records:
            dst.write(json.dumps(record, ensure_ascii=False) + '\n')
    if args.summary:
        args.summary.write_text(json.dumps(summarize(records), ensure_ascii=False, indent=2), encoding='utf-8')

if __name__ == '__main__':
    main()

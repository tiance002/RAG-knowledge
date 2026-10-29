"""Join immutable P0 observations and evidence scores without model calls."""
import argparse
from collections import defaultdict
import json
from pathlib import Path


def read_rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]


def summarize(runtime):
    events = defaultdict(list)
    for row in read_rows(runtime / 'events.jsonl'):
        events[row['trace_id']].append(row)
    results = read_rows(runtime / 'results.jsonl')
    scored = {r['trace_id']: r for r in read_rows(runtime / 'p0-evidence-scores.jsonl')}
    if len({r['trace_id'] for r in results}) != len(results):
        raise ValueError('Duplicate result trace')
    output = []
    for result in results:
        trace_id = result['trace_id']
        record = scored[trace_id]
        responses = [e['metadata'] for e in events[trace_id]
                     if e.get('stage') == 'llm.response' and e.get('event') == 'stage']
        ends = [e for e in events[trace_id] if e.get('stage') == 'p0.smoke.question' and e.get('event') == 'end']
        # This runner has exactly one generation. Do not silently sum partial Agent usage.
        if len(responses) != 1 or len(ends) != 1:
            raise ValueError('Unexpected smoke response/root count')
        response = responses[0]
        scores = record['scores']
        scores['empty'] = result['answer_chars'] == 0 if 'answer_chars' in result else None
        scores['skipped'] = False if result.get('status') == 'COMPLETED' else None
        scores['truncated'] = response.get('finish_reason') == 'length' if response.get('finish_reason') else None
        scores['latency_ms'] = ends[0]['latency_ms']
        scores['tokens'] = response.get('usage')
        scores['evidence_recall'] = record['coverage']['context']
        record['status'] = result['status']
        record['token_scope'] = 'generation_only'
        output.append(record)
    if len(output) != len(scored):
        raise ValueError('Evidence/result trace mismatch')
    summary = {'records': len(output),
               'empty': sum(r['scores']['empty'] is True for r in output),
               'skipped': sum(r['scores']['skipped'] is True for r in output),
               'truncated': sum(r['scores']['truncated'] is True for r in output),
               'generation_tokens': {key: sum((r['scores']['tokens'] or {}).get(key, 0) for r in output)
                                     for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
               'answer_correctness': 'UNKNOWN', 'monetary_cost': 'UNKNOWN',
               'limitations': 'generation tokens only; no answer-body judge; no formal citation execution'}
    return output, summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', required=True, type=Path)
    args = parser.parse_args()
    output, summary = summarize(args.runtime)
    (args.runtime / 'p0-scored-records.jsonl').write_text(
        ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in output), encoding='utf-8')
    (args.runtime / 'p0-validation-summary.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()

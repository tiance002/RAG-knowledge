"""Read-only import of explicitly selected historical reviews; never runs a model."""
import argparse
import hashlib
import json
from pathlib import Path


def import_row(row, root, ai_review=None):
    root = Path(root).resolve()
    path = (root / row['sse']).resolve()
    if not path.is_relative_to(root / 'var' / 'e1-20261029'):
        raise ValueError('SSE outside historical E1 directory')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != row['sse_sha256']:
        raise ValueError('SSE hash mismatch')
    text = raw.decode('utf-8').replace('\r\n', '\n')
    chunks, done, error = [], False, False
    for block in text.split('\n\n'):
        event = None
        data = []
        for line in block.splitlines():
            if line.startswith('event:'): event = line[6:].strip()
            if line.startswith('data:'): data.append(line[5:].lstrip())
        if event == 'answer_chunk':
            content = json.loads('\n'.join(data)).get('content')
            if not isinstance(content, str): raise ValueError('invalid answer chunk')
            chunks.append(content)
        elif event == 'done': done = True
        elif event == 'error': error = True
    if not chunks or not done or error: raise ValueError('SSE answer incomplete or error')
    traces = row['result_case'].get('trace_ids') or []
    if len(traces) != 1 or not traces[0]: raise ValueError('ambiguous trace association')
    verdict = row['human_core']
    reviewer = 'USER'
    evaluation_source = 'historical_user_review_import'
    evidence_scope = 'historical user core review; verified SSE answer; other quality fields unresolved'
    ai_basis = None
    if verdict == 'UNKNOWN' and ai_review is not None:
        if ai_review.get('qid') != row['qid'] or ai_review.get('sse_sha256') != row['sse_sha256'] or ai_review.get('assessor') != 'AI delegated core review; not a human judgment':
            raise ValueError('AI review provenance mismatch')
        ai_basis = ai_review.get('core_judgment')
        verdict = {'CORRECT': 'PASS', 'CORRECT_UNDER_USER_PROXY': 'PASS', 'CORRECT_UNDER_USER_RUBRIC': 'PASS', 'INCORRECT': 'FAIL'}.get(ai_basis)
        reviewer = 'AI_DELEGATED'
        evaluation_source = 'historical_ai_review_import'
        evidence_scope = 'AI delegated core review with original user rubric or proxy where specified; verified SSE answer; other quality fields unresolved'
    if verdict not in ('PASS', 'FAIL', 'PARTIAL'):
        raise ValueError('no explicit supported core verdict')
    metrics = {'eval.core_correctness': verdict, 'eval.faithfulness': 'UNKNOWN', 'eval.citation_support': 'UNKNOWN', 'eval.answer_completeness': 'UNKNOWN', 'eval.abstention': 'UNKNOWN'}
    if verdict in ('PASS', 'FAIL'): metrics['eval.core_pass'] = verdict == 'PASS'
    return {
        'qid': row['qid'], 'trace_id': traces[0],
        'answer_sha256': hashlib.sha256(''.join(chunks).encode('utf-8')).hexdigest(),
        'reviewer_type': reviewer, 'rubric_version': 'e11-historical-import-v1',
        'evaluation_source': evaluation_source, 'evidence_scope': evidence_scope,
        'original_ai_core_judgment': ai_basis,
        'historical_rubric': 'NOT_CAPTURED_AS_VERSION', 'source_sse_sha256': row['sse_sha256'],
        'metrics': metrics,
    }



def historical_local_binding(row, root):
    """Verify immutable case association, including the recorded request span identity."""
    root = Path(root).resolve()
    base = root / 'var' / 'e1-20261029'
    paths = {}
    for field, digest in (('sse', 'sse_sha256'), ('events_file', 'events_sha256'), ('result', 'result_sha256')):
        path = (root / row[field]).resolve()
        if not path.is_relative_to(base): raise ValueError('historical path outside E1')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != row[digest]: raise ValueError(field + ' hash mismatch')
        paths[field] = (path, raw)
    sse_path, events_path, result_path = (paths[k][0] for k in ('sse', 'events_file', 'result'))
    if sse_path.parent != events_path.parent or sse_path.parent.parent != result_path.parent:
        raise ValueError('case file association mismatch')
    results = json.loads(paths['result'][1])['cases']
    matches = [(i, case) for i, case in enumerate(results) if case.get('qid') == row['qid']]
    if len(matches) != 1: raise ValueError('ambiguous result case')
    index, case = matches[0]
    if case != row['result_case'] or sse_path.parent.name != f'case-{index:02d}' or not case.get('sse_complete'):
        raise ValueError('case index, result or terminal mismatch')
    events = [json.loads(line) for line in paths['events_file'][1].decode('utf8').splitlines() if line.strip()]
    roots = [e for e in events if e.get('stage') == 'qa.request' and e.get('event') in ('start', 'end')]
    ids = {(e.get('trace_id'), e.get('span_id')) for e in roots if e.get('parent_id') is None}
    if len(roots) != 2 or any(e.get('parent_id') is not None for e in roots) or len(ids) != 1 or {e.get('event') for e in roots} != {'start', 'end'}:
        raise ValueError('root request event not uniquely bound')
    trace, span = next(iter(ids))
    if not span or case.get('trace_ids') != [trace]: raise ValueError('result/event trace mismatch')
    imported = import_row(row, root)
    if imported['trace_id'] != trace: raise ValueError('SSE/trace linkage mismatch')
    return {'qid': row['qid'], 'trace_id': trace, 'root_observation_id': span,
            'answer_sha256': imported['answer_sha256'], 'events_sha256': row['events_sha256'],
            'result_sha256': row['result_sha256'], 'sse_sha256': row['sse_sha256']}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--ai-review', type=Path, help='Original independent AI review JSONL')
    parser.add_argument('--ai-review-sha256', help='Frozen expected SHA256 for AI review file')
    parser.add_argument('--qid', action='append', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    selected = set(args.qid)
    if len(selected) != len(args.qid): raise ValueError('duplicate qid')
    rows = [json.loads(line) for line in args.ledger.read_text(encoding='utf8').splitlines() if line.strip()]
    rows = [row for row in rows if row['qid'] in selected]
    if len(rows) != len(selected) or {row['qid'] for row in rows} != selected:
        raise ValueError('missing or duplicate selected qid')
    ai_rows = {}
    if args.ai_review:
        raw = args.ai_review.read_bytes()
        if not args.ai_review_sha256 or hashlib.sha256(raw).hexdigest() != args.ai_review_sha256:
            raise ValueError('AI review file hash mismatch')
        for line in raw.decode('utf8').splitlines():
            if line.strip():
                review = json.loads(line)
                if review['qid'] in ai_rows: raise ValueError('duplicate AI review qid')
                ai_rows[review['qid']] = review
    records = [import_row(row, args.root, ai_rows.get(row['qid'])) for row in rows]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists(): raise FileExistsError(args.output)
    with args.output.open('x', encoding='utf8') as out:
        for row in records: out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + '\n')
    print(json.dumps({'records': len(records), 'output': str(args.output)}))

if __name__ == '__main__': main()

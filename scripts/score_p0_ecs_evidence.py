"""Score recorded P0 smoke traces using frozen source/page identity; no service calls."""
import argparse
import hashlib
import json
from collections import defaultdict
from pathlib import Path
import sys


def rows(path):
    with path.open(encoding='utf-8') as f:
        return [json.loads(line) for line in f if line.strip()]


def build_mapping(prior):
    control = rows(prior / 'control-sql.jsonl')
    old_kbs = {r['value']['name'].removeprefix('prod-'): r['value']['id'] for r in control if r['kind'] == 'kb'}
    mapping = {}
    for row in control:
        if row['kind'] == 'chunk':
            v = row['value']
            mapping[v['id']] = (v['file_id'], [v['page_number']] if v['page_number'] is not None else [])
    for p in rows(prior / 'profiles.jsonl'):
        profile, arm = p['profile'], p['arm']
        kb = old_kbs[profile] if profile in old_kbs else 'v200-' + profile + ('-' + arm if profile.startswith('pdf-') else '')
        fid = kb + '-' + hashlib.sha256(p['source_id'].encode()).hexdigest()
        for i, c in enumerate(p['chunks']):
            cid = fid + '-' + str(i)
            if cid in mapping:
                raise ValueError('Duplicate chunk ID: ' + cid)
            mapping[cid] = (p['source_id'], [x for x in (c.get('source_pages') or [c['page_number']]) if x is not None])
    return mapping


def wanted(case):
    if case.get('positive_document_ids'):
        return 'source_document', set(case['positive_document_ids'])
    if case.get('expected_pages') and case.get('expected_source_sha256'):
        return 'pdf_page', {case['expected_source_sha256'] + ':' + str(p) for p in case['expected_pages']}
    return 'none', set()


def evaluate_trace(qid, version, events, case, mapping, scorer):
    unit, gold = wanted(case)
    observed = defaultdict(list)
    for event in events:
        if event.get('event') != 'stage':
            continue
        observed[event['stage']].append(event.get('metadata') or {})
    def ids(stage):
        entries = observed.get(stage)
        if not entries:
            return None
        result = set()
        for meta in entries:
            candidates = meta.get('candidates')
            if not isinstance(candidates, list) or any(not isinstance(c, dict) or not c.get('id') for c in candidates):
                return None
            result.update(c['id'] for c in candidates)
        return result
    def context_ids():
        entries = observed.get('retrieval.context_expansion')
        if not entries:
            return None
        result = set()
        for meta in entries:
            if not meta.get('chunk_id') or any(not isinstance(meta.get(k), list) for k in ('sibling_ids', 'previous_ids', 'next_ids')):
                return None
            # Historical sibling_ids include siblings omitted by the length cap.
            # Only new included_sibling_ids prove which parent text survived.
            if meta.get('parent_id') and 'included_sibling_ids' not in meta:
                return None
            if not meta.get('parent_id'):
                result.add(meta['chunk_id'])
            for k in ('included_sibling_ids', 'previous_ids', 'next_ids'):
                result.update(meta.get(k, []))
        return result
    vector, bm25 = ids('retrieval.vector'), ids('retrieval.bm25')
    stages = {'raw': None if vector is None or bm25 is None else vector | bm25,
              'fusion': ids('retrieval.fusion'), 'merged': ids('retrieval.after_dedup'),
              'reranked': ids('rerank.filtered'), 'context': context_ids(), 'cited': None}
    def labels(ids_):
        if ids_ is None or any(cid not in mapping for cid in ids_):
            return None
        result = set()
        for cid in ids_:
            source, pages = mapping[cid]
            if unit == 'pdf_page' and not pages:
                return None
            result.update({source} if unit == 'source_document' else {source + ':' + str(page) for page in pages})
        return result
    mapped = {stage: labels(value) for stage, value in stages.items()}
    result = scorer(qid, version, gold, mapped)
    result['trace_id'] = events[0]['trace_id']
    result['evidence_unit'] = unit
    result['candidate_counts'] = {k: None if v is None else len(v) for k, v in stages.items()}
    result['fusion_evidence'] = scorer(qid, version, gold, {'raw': mapped['fusion']})['evidence']['raw']
    result['fusion_coverage'] = scorer(qid, version, gold, {'raw': mapped['fusion']})['coverage']['raw']
    result['mapping_unknown_counts'] = {k: None if v is None else sum(cid not in mapping for cid in v) for k, v in stages.items()}
    return result


def main():
    parser = argparse.ArgumentParser()
    for name in ('runtime', 'prior', 'dataset', 'backend'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.backend.resolve()))
    from app.services.evaluation import score_record
    manifest = json.loads((args.runtime / 'manifest.json').read_text(encoding='utf-8'))
    freeze = json.loads((args.dataset / 'freeze.json').read_text(encoding='utf-8'))
    for name, digest in freeze['files'].items():
        if hashlib.sha256((args.dataset / name).read_bytes()).hexdigest() != digest:
            raise ValueError('Frozen dataset hash mismatch: ' + name)
    if hashlib.sha256((args.dataset / 'freeze.json').read_bytes()).hexdigest() != manifest['dataset_freeze_sha256']:
        raise ValueError('Manifest dataset mismatch')
    for file, key in [('profiles.jsonl', 'profiles_sha256'), ('control-sql.jsonl', 'control_sha256')]:
        if hashlib.sha256((args.prior / file).read_bytes()).hexdigest() != manifest[key]:
            raise ValueError('Manifest prior mismatch: ' + file)
    cases = {r['qid']: r for r in rows(args.dataset / 'gold100.jsonl')}
    mapping = build_mapping(args.prior)
    traces = defaultdict(list)
    for e in rows(args.runtime / 'events.jsonl'):
        traces[e['trace_id']].append(e)
    allowed = {x['qid'] for x in manifest['inputs']}
    output = []
    for trace in traces.values():
        starts = [e for e in trace if e.get('stage') == 'p0.smoke.question' and e.get('event') == 'start']
        if not starts:
            continue
        if len(starts) != 1:
            raise ValueError('Ambiguous trace root')
        qid = starts[0]['metadata']['qid']
        if qid not in allowed or qid not in cases:
            raise ValueError('Unexpected qid: ' + qid)
        output.append(evaluate_trace(qid, manifest['experiment_version'], trace, cases[qid], mapping, score_record))
    output_path = args.runtime / 'p0-evidence-scores.jsonl'
    with output_path.open('w', encoding='utf-8') as f:
        for item in output:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')
    print(json.dumps({'records': len(output), 'expected': len(allowed), 'output': str(output_path),
                      'limitations': 'cited unknown; rerank.filtered is all observed candidates; missing events and unresolved IDs unknown'}))

if __name__ == '__main__':
    main()

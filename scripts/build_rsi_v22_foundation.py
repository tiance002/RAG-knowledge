"""Build source-bound Context Selection fixtures from existing reviewed QA items.

Copies only selected, hash-verified small source files; never opens a live DB or
changes the source pool. Reviews are SELF_CHECKED, never forged human approvals.
The original executor schema/evaluator and a separate provenance sidecar coexist.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from rsi_candidates import canonical, sha
from run_rsi_offline import EVALUATOR, FIXED


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('xb') as stream:
        stream.write(canonical(value))


def eligible(pool, catalog):
    result, used = [], set()
    for item in pool['items']:
        content = item['content']
        evidence = content.get('evidence', [])
        hashes = {e.get('source_sha256') for e in evidence}
        if item.get('split') != 'UNASSIGNED' or len(hashes) != 1 or not evidence:
            continue
        digest = next(iter(hashes))
        if digest in used or digest not in catalog:
            continue
        source = catalog[digest]
        if not source['name'].lower().endswith('.md') or source['source_bytes'] > 100_000:
            continue
        if item.get('content_review', {}).get('decision') != 'PASS':
            continue
        if not all(isinstance(e.get('quote'), str) and e['quote'].strip()
                   and type(e.get('original_location', {}).get('char_start_0based')) is int
                   and type(e.get('original_location', {}).get('char_end_exclusive_0based')) is int
                   for e in evidence):
            continue
        used.add(digest)
        result.append((item, source))
    return result


def fetch_sources(items, host, identity):
    # Pass paths as Python data on stdin, never interpolate paths into shell code.
    refs = [{'path': source['source_path'], 'sha256': source['sha256']} for _, source in items]
    script = '''import base64,hashlib,json
from pathlib import Path
refs = REFS
out=[]
for ref in refs:
    path=Path(ref['path']).resolve()
    if not path.is_relative_to(Path('/opt/amrag/backend/storage/uploads')):
        raise ValueError('source escapes authorized upload root')
    data=path.read_bytes()
    if len(data)>100000 or hashlib.sha256(data).hexdigest()!=ref['sha256']:
        raise ValueError('source version mismatch')
    out.append({'sha256':ref['sha256'],'base64':base64.b64encode(data).decode()})
print(json.dumps(out))
'''.replace('REFS', repr(refs))
    proc = subprocess.run(['ssh', '-i', str(identity), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
                           host, 'python3 -'], input=script, text=True, capture_output=True, timeout=60)
    if proc.returncode:
        raise RuntimeError('SOURCE_READ_FAILED; SSH exit=' + str(proc.returncode))
    sources = {}
    for row in json.loads(proc.stdout):
        data = base64.b64decode(row['base64'], validate=True)
        if sha(data) != row['sha256']:
            raise ValueError('source transfer digest mismatch')
        sources[row['sha256']] = data
    if set(sources) != {r['sha256'] for r in refs}:
        raise ValueError('source transfer is incomplete')
    return sources


def segment(text, intervals, target=400):
    """Contiguous source slices, never split a necessary quoted interval.

    This is a NEW documented R0 layout, not a claim to reuse a historical index.
    """
    cuts, position = [0], 0
    while position < len(text):
        end = min(position + target, len(text))
        while True:
            crossing = [right for left, right in intervals if left < end < right]
            if not crossing:
                break
            end = max(crossing)
        if end <= position:
            raise ValueError('invalid source segmentation')
        cuts.append(end)
        position = end
    return list(zip(cuts, cuts[1:]))


def build_split(items, sources, out, split, reusable_sources=None):
    cases, evaluation, provenance, inventory = [], {}, {}, []
    for index, (item, source) in enumerate(items):
        content = item['content']
        data = sources[source['sha256']]
        # Evidence coordinates were defined against UTF-8 decoded code points.
        text = data.decode('utf-8')
        intervals = []
        for evidence in content['evidence']:
            loc = evidence['original_location']
            start, end = loc['char_start_0based'], loc['char_end_exclusive_0based']
            if not (0 <= start < end <= len(text) and text[start:end] == evidence['quote']):
                raise ValueError('SOURCE_QUOTE_MISMATCH: ' + content['qid'])
            intervals.append((start, end))
        qid = f'{split.lower()}-{index+1:03}'
        file_id, kb_id = source['file_id'], source['kb_id']
        chunks, member_spans, seed_ids = [], {}, []
        for number, (start, end) in enumerate(segment(text, intervals)):
            # The protected evidence_text returns strip(). Freeze trimmed source
            # intervals instead of weakening the worker's exact-byte guard.
            while start < end and text[start].isspace():
                start += 1
            while start < end and text[end-1].isspace():
                end -= 1
            if start == end:
                continue
            cid = f'{qid}-member-{number:03}'
            body = text[start:end]
            chunks.append({'id': cid, 'file_id': file_id, 'content': body,
                           'content_sha256': sha(body.encode()), 'original_index': number,
                           'content_type': 'text', 'heading_text': text.splitlines()[0][:160],
                           'source_locator': {'record_start': text.count('\n', 0, start) + 1,
                                              'record_end': text.count('\n', 0, end - 1) + 1}})
            member_spans[cid] = {'source_sha256': source['sha256'], 'start': start, 'end': end}
            if any(start <= left and right <= end for left, right in intervals):
                seed_ids.append(cid)
        # Seeds are evidence-anchored frozen inputs. No retrieval quality claim.
        case = {'question': content['question'], 'allowed_kb_ids': [kb_id],
                'files': [{'id': file_id, 'kb_id': kb_id, 'name': source['name']}],
                'chunks': chunks, 'seed_ids': seed_ids, 'expected_seed_count': len(seed_ids)}
        facts, predicates = [], []
        for number, evidence in enumerate(content['evidence']):
            start, end = intervals[number]
            supports = [cid for cid, loc in member_spans.items() if loc['start'] <= start < end <= loc['end']]
            if not supports:
                raise ValueError('necessary evidence has no source member')
            # A whole reviewed clause is retained. Do not reduce a relation to a number.
            facts.append({'kind': 'relation', 'file_id': file_id, 'text': evidence['quote']})
            predicates.append({'fact_id': f'fact-{number+1}', 'kind': 'relation', 'file_id': file_id,
                               'kb_id': kb_id, 'source_sha256': source['sha256'], 'text': evidence['quote'],
                               'start': start, 'end': end, 'supporting_chunk_ids': supports,
                               'normalization': 'identity-unicode-codepoints-v1',
                               'rule': 'exact_clause_in_source_bound_member'})
        cases.append({'qid': qid, 'case': case})
        evaluation[qid] = {'status': 'PAIRED', 'facts': facts, 'forbidden': []}
        provenance[qid] = {'source_group': source['sha256'], 'origin_qid': content['qid'],
                           'origin_core_sha256': item['core_sha256'], 'source_sha256': source['sha256'],
                           'source_relative_path': 'sources/' + source['sha256'] + '.md',
                           'file_id': file_id, 'kb_id': kb_id, 'member_spans': member_spans,
                           'facts': predicates, 'prior_review': item['content_review'],
                           'review': {'kind': 'SELF_CHECKED_WITH_SOURCE',
                                      'reviewer': 'Codex deterministic quote/span/hash validation',
                                      'human_review': False,
                                      'limitations': 'Reuses prior AI semantic review; not authenticated human Gold.'}}
        destination = out / 'sources' / (source['sha256'] + '.md')
        destination.parent.mkdir(parents=True, exist_ok=True)
        reusable = (reusable_sources or {}).get(source['sha256'])
        if reusable:
            os.link(reusable, destination)
        else:
            with destination.open('xb') as stream:
                stream.write(data)
        inventory.append({'source_sha256': source['sha256'], 'source_group': source['sha256'],
                          'bytes': len(data), 'original_path': source['source_path'],
                          'ownership_source': 'pinned historical source manifest + unchanged raw source',
                          'live_database_ownership': 'NOT_CHECKED', 'public_status': 'existing public QA corpus',
                          'agent_raw_text_export': False})
    save(out / 'cases.json', cases)
    save(out / 'evaluation.json', evaluation)
    save(out / 'provenance.restricted.json', provenance)
    save(out / 'sources.manifest.json', inventory)
    save(out / 'all.json', {'selection': 'all'})
    manifest = {'experiment_id': 'rsi-v22-' + split.lower() + '-r0', 'status': 'READY_DEV_ONLY',
                'base_commit': '90d86ce87e8c76c8c45f5f296160ddf53de71d54',
                'evaluator_version': EVALUATOR, 'fixed': FIXED,
                'cases': {'path': 'cases.json', 'sha256': sha(canonical(cases))},
                'evaluation': {'path': 'evaluation.json', 'sha256': sha(canonical(evaluation))},
                'arms': {'A': {'candidate_id': 'v22-root-A', 'parent_id': None,
                               'path': 'all.json', 'sha256': sha(canonical({'selection': 'all'}))},
                         'B': {'candidate_id': 'v22-root-B', 'parent_id': 'v22-root-A',
                               'path': 'all.json', 'sha256': sha(canonical({'selection': 'all'}))}},
                'budget': {'max_questions': len(cases), 'external_calls': 0, 'attempts_per_arm': 1},
                'stop_conditions': ['untrusted source', 'missing required fact', 'invalid pair'],
                'v22_scope': {'mode': 'B', 'split': split, 'source_groups': len(inventory),
                              'layout': '400-codepoint source slices; boundary whitespace trimmed as required by frozen evidence_text; evidence spans unsplit',
                              'seeds': 'reviewed evidence-anchored; not a retrieval evaluation',
                              'quality_review': 'SELF_CHECKED; no fabricated human approval'}}
    save(out / 'manifest.json', manifest)
    return {'questions': len(cases), 'groups': len(inventory),
            'cases_sha256': sha(canonical(cases)), 'evaluator': EVALUATOR,
            'raw_source_bytes': sum(s['bytes'] for s in inventory), 'execution': 'NOT_RUN'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qa-root', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--ssh-host', required=True)
    parser.add_argument('--ssh-identity', type=Path, required=True)
    parser.add_argument('--reuse-foundation', type=Path,
                        help='Reuse unchanged selected raw files via hard links; do not download them again')
    parser.add_argument('--exclude-holdout-sources', type=Path,
                        help='Private pre-execution source-independence exclusions; never candidate-result based')
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError('never overwrite an existing foundation')
    pool_path = args.qa_root / 'portable/reviewed-standalone-draft.json'
    pool = load(pool_path)
    catalog = {}
    for path in sorted(args.qa_root.glob('terra_ecs_batch*/source_manifest.json')):
        for source in load(path).get('files', []):
            if source.get('sha256'):
                catalog[source['sha256']] = source
    selected = eligible(pool, catalog)
    if len(selected) < 30:
        raise ValueError('insufficient real independent source candidates')
    # Holdout candidates are not printed or passed to any proposing/curating agent.
    train = selected[:20]
    excluded = set(load(args.exclude_holdout_sources)) if args.exclude_holdout_sources else set()
    candidates = selected[40:] if len(selected) >= 50 else selected[20:]
    holdout = [(item, source) for item, source in candidates if source['sha256'] not in excluded][:10]
    if len(holdout) != 10:
        raise ValueError('insufficient independent holdout sources after pre-execution exclusions')
    hashes_train = {s['sha256'] for _, s in train}
    if hashes_train & {s['sha256'] for _, s in holdout}:
        raise ValueError('source overlap between splits')
    reusable = {}
    if args.reuse_foundation:
        for split in ('train', 'holdout_restricted'):
            for path in (args.reuse_foundation / split / 'sources').glob('*.md'):
                if sha(path.read_bytes()) != path.stem:
                    raise ValueError('reusable source hash mismatch')
                reusable[path.stem] = path
        required = {source['sha256'] for _, source in train + holdout}
        sources = {key: reusable[key].read_bytes() for key in required & set(reusable)}
        missing = [(item, source) for item, source in train + holdout if source['sha256'] not in reusable]
        if missing:
            sources.update(fetch_sources(missing, args.ssh_host, args.ssh_identity))
    else:
        sources = fetch_sources(train + holdout, args.ssh_host, args.ssh_identity)
    args.out.mkdir(parents=True)
    training = build_split(train, sources, args.out / 'train', 'DEV', reusable)
    sealed = build_split(holdout, sources, args.out / 'holdout_restricted', 'HOLDOUT', reusable)
    # A newly assembled source-separated holdout is NOT automatically certified
    # independent of every historical exposure; that policy is checked separately.
    summary = {'schema': 'rsi-v22-foundation-build/v1', 'pool_raw_sha256': sha(pool_path.read_bytes()),
               'training': training, 'holdout': {'questions': sealed['questions'], 'groups': sealed['groups'],
                    'status': 'SEALED_PENDING_INDEPENDENCE_AUDIT', 'exposed_to_rsi_roles': False},
               'copied_source_files': len(set(sources) - set(reusable)),
               'copied_source_bytes': sum(len(data) for key, data in sources.items() if key not in reusable),
               'reused_hardlinked_sources': len(set(sources) & set(reusable)),
               'original_data_writes': 0, 'model_calls': 0,
               'limits': ['Prior review is AI, not human.', 'No response quality or retrieval evaluation.',
                          'Source hash separation alone is not semantic independence.',
                          'Sources remain local/restricted; no original text sent to model.']}
    save(args.out / 'build-summary.json', summary)
    print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == '__main__':
    main()

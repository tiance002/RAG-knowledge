"""Offline compact freeze: 138 remaining initial cases and 28 actual followups.

No provider imports/calls, credentials, response-based selection or corpus copies.
Messages are lazily reconstructed from hash-bound existing read-only sources.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3

from dynamic_router_freeze12 import canonical, sha, selected_citations


CHEAP = 'Qwen/Qwen3.5-4B'
POLICY = 'EXCLUDE_PRIOR12_THEN_CORE_EXCLUDED_THEN_LEXICOGRAPHICALLY_LAST_INITIAL_NO_OUTPUT_FILTER'


def read_json(path):
    return json.loads(Path(path).read_bytes())


def select_cases(base, follow, prior):
    """Selection accesses IDs and original exclusion flags only, never answers."""
    seen = {c['qid'] for c in prior['cases']}
    remaining = [c for c in base['cases'] if 'initial:' + c['qid'] not in seen]
    assert len(remaining) == 140
    excluded = [{'qid': 'initial:' + c['qid'], 'reason': 'ORIGINAL_EXCLUDED_FROM_CORE_ACCURACY'}
                for c in remaining if c.get('excluded_from_core_accuracy')]
    assert len(excluded) == 1
    valid = sorted(c['qid'] for c in remaining if not c.get('excluded_from_core_accuracy'))
    excluded.append({'qid': 'initial:' + valid[-1],
                     'reason': 'PREDECLARED_LEXICOGRAPHICALLY_LAST_VALID_QID_TO_HONOR_OWNER_138_COUNT'})
    omitted = {x['qid'] for x in excluded}
    chosen = [('initial:' + q, 'initial') for q in valid if 'initial:' + q not in omitted]
    chosen += [('followup:' + f['id'], 'followup') for f in follow['cases']
               if 'followup:' + f['id'] not in seen]
    assert Counter(r for _, r in chosen) == {'initial': 138, 'followup': 28}
    return chosen, excluded


def read_attempts(path):
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    try:
        return {(q, r): (s, json.loads(i))
                for q, r, s, i in db.execute('select qid,role,state,info from attempts')}
    finally:
        db.close()


def sources(root):
    previous = root / 'followup-routing-20261102'
    paths = dict(base=root / 'dynamic-routing-20261101/gold150/manifest.json',
                 split=root / 'dynamic-routing-20261101/gold150/split.json',
                 prior=root / 'dynamic-router-v1-20261102/frozen_cases.json',
                 ledger=previous / 'full180-v2/ledger.sqlite3',
                 pipeline=previous / 'full180_pipeline.py')
    for path in sorted((previous / 'input').glob('*.json')):
        obj = read_json(path)
        if obj.get('version') == 'followup30-draft-v1':
            paths['follow'] = path
        if obj.get('dataset_version') == 'dynamic-routing-150-question-gold-candidate-v2':
            paths['overlay'] = path
    assert {'follow', 'overlay'} <= paths.keys()
    return paths


def materialize(paths, chosen):
    base, follow, overlay, split = (read_json(paths[k]) for k in ('base', 'follow', 'overlay', 'split'))
    assert sha(canonical(base)) == overlay['base_frozen_manifest_sha256']
    records = {c['qid']: c for c in base['cases']}
    follows = {f['id']: f for f in follow['cases']}
    splitrows = {s['qid']: s for s in split['records']}
    edits = {c['qid']: c['candidate'] for c in overlay['changes']}
    # Exact full180_pipeline office-year override, including its update precedence.
    for n in list(range(26, 36)) + list(range(41, 50)):
        qid = 'office50:%03d' % n
        prefix = 'For fiscal year 2018, ' if n <= 35 else 'According to the 2022 data, '
        edits[qid] = {'question': prefix + records[qid]['question']}
    attempts = read_attempts(paths['ledger'])
    cases = []
    for number, (qid, role) in enumerate(chosen):
        ident = qid.split(':', 1)[1]
        f = follows[ident] if role == 'followup' else None
        c = records[f['previous_qid'] if f else ident]
        provenance = {'original_qid': c['qid'], 'delivered_qid': qid, 'delivered_role': role,
                      'ledger': str(paths['ledger']), 'base_manifest': str(paths['base']),
                      'pipeline': str(paths['pipeline']), 'context_source_path': c['context_source_path'],
                      'context_sha256': c['context_sha256']}
        reason = None
        if f:
            initial_q = edits.get(c['qid'], {}).get('question', c['question'])
            pq, pr = (('initial:' + c['qid'], 'initial') if f['previous_question'] == initial_q
                      else ('prior:' + f['id'], 'prior'))
            state, info = attempts.get((pq, pr), ('MISSING', {}))
            prior_messages = [c['messages'][0], {'role': 'user', 'content': f['previous_question']}]
            if state != 'COMPLETED' or info.get('finish_reason') != 'stop' or not info.get('text'):
                reason = 'NOT_RUN_MISSING_OR_INCOMPLETE_ACTUAL_PARENT'
            elif info.get('messages_sha256') != sha(canonical(prior_messages)):
                reason = 'NOT_RUN_PARENT_DELIVERED_HASH_MISMATCH'
            history = f.get('history_messages', [])[:-2] if f.get('history_messages') else []
            assert all(set(m) == {'role', 'content'} and m['role'] in ('user', 'assistant') for m in history)
            # Missing parents remain explicit NOT_RUN; never manufacture an answer.
            if not reason:
                history += [{'role': 'user', 'content': f['previous_question']},
                            {'role': 'assistant', 'content': info['text']}]
            question = f['followup_question']
            gold = {'answer_points': [f['gold_answer']], 'expected_chunk_ids': f.get('expected_chunk_ids', []),
                    'kb_scope': c['gold'].get('kb_scope'), 'source_record': f}
            gold_status = 'EXISTING_FOLLOWUP_CANDIDATE_UNPROMOTED'
            provenance.update(prior_qid=pq, prior_role=pr, prior_state=state,
                              prior_messages_sha256=info.get('messages_sha256'),
                              prior_answer_sha256=sha(info['text'].encode()) if info.get('text') else None,
                              prior_provider_response_id=info.get('provider_request_id'),
                              history_reconstruction='EXACT_FULL180_PIPELINE_REAL_LEDGER_REPLY')
        else:
            question = edits.get(ident, {}).get('question', c['question'])
            history = []
            gold = deepcopy(c['gold'])
            candidate = edits.get(ident, {})
            if 'answer_points' in candidate:
                gold['answer_points'] = candidate['answer_points']
            gold_status = 'OWNER_EXISTING_OVERLAY_PRESERVED' if ident in edits else 'ORIGINAL_FROZEN_GOLD'
        messages = [c['messages'][0]] + history + [{'role': 'user', 'content': question}]
        mh = sha(canonical(messages))
        sent_state, sent_info = attempts.get((qid, role), ('MISSING', {}))
        if not reason and sent_info.get('messages_sha256') != mh:
            reason = 'NOT_RUN_DELIVERED_MESSAGES_HASH_MISMATCH'
        context, citations = selected_citations(messages[0]['content'])
        assert sha(context.encode()) == c['context_sha256'], 'context mismatch: ' + qid
        assert sha(canonical(c['retrieved_top10'])) == c['retrieved_top10_sha256']
        provenance.update(delivered_state=sent_state, delivered_messages_sha256=sent_info.get('messages_sha256'))
        case = dict(qid=qid, original_qid=c['qid'], stratum='actual_history' if f else 'initial_remaining',
                    question=question, messages=messages, messages_sha256=mh, history_messages=history,
                    citations=citations, kb_scope=gold.get('kb_scope'),
                    document_scope=c['gold'].get('original_case', {}).get('document_ids'),
                    retrieval_status='SUCCESS', retrieval_provenance='FROZEN_DELIVERED_CONTEXT_RECOVERED_NO_NEW_RETRIEVAL',
                    live_retrieval_status='UNKNOWN_NOT_MEASURED', selected_chunk_mapping_status='UNKNOWN',
                    evidence_completeness_status='UNKNOWN_NOT_INFERRED_FROM_GOLD',
                    retrieved_top10=c['retrieved_top10'], retrieved_top10_sha256=c['retrieved_top10_sha256'],
                    gold=gold, gold_status=gold_status, gold_sha256=sha(canonical(gold)),
                    source_group=c['source_group'], historical_split=splitrows.get(c['qid']),
                    provenance=provenance, source_status='NOT_RUN' if reason else 'DELIVERED_MESSAGES_HASH_VERIFIED',
                    not_run_reason=reason, arm_rotation=number % 3,
                    cheap_baseline={'state': sent_state, 'info': sent_info, 'source_ledger': str(paths['ledger']),
                                    'source_qid': qid, 'source_role': role})
        comparison = {k: case[k] for k in ('messages', 'citations', 'history_messages', 'kb_scope', 'document_scope', 'retrieval_status')}
        case['comparison_input_hash'] = sha(canonical(comparison))
        cases.append(case)
    return cases


def compact(case):
    keys = ('qid', 'original_qid', 'stratum', 'messages_sha256', 'comparison_input_hash', 'gold_sha256',
            'gold_status', 'source_status', 'not_run_reason', 'historical_split')
    out = {k: case[k] for k in keys}
    out['question_sha256'] = sha(case['question'].encode())
    b = case['cheap_baseline']
    out['historical_cheap'] = {'state': b['state'], 'source_qid': b['source_qid'],
                               'source_role': b['source_role'], 'model': b['info'].get('actual_model'),
                               'messages_sha256': b['info'].get('messages_sha256'),
                               'info_sha256': sha(canonical(b['info']))}
    return out


def load_frozen_cases(manifest):
    """Validate immutable source bytes and every compact case, then hydrate.

    Accept a freeze dict, freeze path, or an execution contract referring to the
    freeze by case_freeze_path/case_freeze_sha256. Does not write any source.
    """
    if isinstance(manifest, (str, Path)):
        manifest = read_json(manifest)
    if 'case_freeze_path' in manifest:
        path = Path(manifest['case_freeze_path'])
        assert sha(path.read_bytes()) == manifest['case_freeze_sha256'], 'freeze hash mismatch'
        manifest = read_json(path)
    paths = {}
    for name, entry in manifest['sources'].items():
        path = Path(entry['path'])
        assert sha(path.read_bytes()) == entry['byte_sha256'], 'source bytes changed: ' + name
        paths[name] = path
    base, follow, prior = (read_json(paths[k]) for k in ('base', 'follow', 'prior'))
    chosen, excluded = select_cases(base, follow, prior)
    assert excluded == manifest['unselected_initial_cases']
    cases = materialize(paths, chosen)
    assert [compact(c) for c in cases] == manifest['cases'], 'case reconstruction mismatch'
    return cases


def freeze(root, out):
    paths = sources(root)
    source_hashes = {k: {'path': str(p), 'byte_sha256': sha(p.read_bytes()), 'bytes': p.stat().st_size}
                     for k, p in paths.items()}
    chosen, excluded = select_cases(*(read_json(paths[k]) for k in ('base', 'follow', 'prior')))
    cases = materialize(paths, chosen)
    manifest = dict(schema_version='dynamic-router-compact166-v1', dataset_version='initial138-followup28-20261102',
                    baseline_model=CHEAP, quality_check='NONE', execution_arms=['DYNAMIC'], approved_max_calls=166,
                    selection_policy=POLICY, selection_uses_provider_responses=False,
                    source_read_policy='HASH_BOUND_READ_ONLY_EXISTING_INPUTS_NO_CORPUS_INDEX_COPY_NO_CALLS',
                    gold_policy='EXISTING_OWNER_OVERLAY_AND_UNPROMOTED_FOLLOWUP_PRESERVED_NEVER_PROMPTED',
                    sources=source_hashes, unselected_initial_cases=excluded,
                    prior_excluded_qids=[c['qid'] for c in read_json(paths['prior'])['cases']],
                    cases=[compact(c) for c in cases],
                    counts=dict(Counter(c['stratum'] for c in cases)),
                    not_run_count=sum(bool(c['not_run_reason']) for c in cases),
                    comparison_input_hash_keys=['messages', 'citations', 'history_messages', 'kb_scope', 'document_scope', 'retrieval_status'])
    os.umask(0o077)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with out.open('xb') as handle:
        handle.write(canonical(manifest) + b'\n')
    os.chmod(out, 0o400)
    hydrated = load_frozen_cases(manifest)
    report = dict(status='PASS', cases=len(cases), counts=manifest['counts'],
                  not_run_count=manifest['not_run_count'], source_hashes_verified=len(paths),
                  delivered_hash_verified=sum(c['source_status'] == 'DELIVERED_MESSAGES_HASH_VERIFIED' for c in hydrated),
                  manifest=str(out), byte_sha256=sha(out.read_bytes()), manifest_bytes=out.stat().st_size,
                  calls=0, raw_corpus_copies=0, unselected_initial_cases=excluded,
                  cheap_states=dict(Counter(c['cheap_baseline']['state'] for c in cases)))
    print(json.dumps(report, ensure_ascii=False))
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/opt/amrag/experiments'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    freeze(args.root, args.out)

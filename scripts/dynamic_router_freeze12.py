"""Freeze twelve existing delivered inputs; offline, read-only historical inputs.

No provider, database credential, corpus/index copy or Gold revision is used.
JSON hashes use sorted compact UTF-8; file hashes use original bytes.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode('utf-8')


def sha(value):
    return hashlib.sha256(value).hexdigest()


def selected_citations(system):
    """Read only reference blocks actually present, never zip them with top10."""
    start = system.index('参考资料：\n') + len('参考资料：\n')
    end = system.rindex('\n要求：')
    context = system[start:end]
    matches = list(re.finditer(r'(?m)^\[(\d+)\] 来源: ([^\n]+)\n', context))
    assert matches, 'no recoverable delivered reference blocks'
    citations = []
    for i, match in enumerate(matches):
        content = context[match.end():matches[i + 1].start() if i + 1 < len(matches) else len(context)]
        # Keep separators in the slice; stripping would change actual input text.
        citations.append({'citation_label': match.group(1), 'source_file': match.group(2),
                          'content': content, 'content_sha256': sha(content.encode('utf-8')),
                          'identity_status': 'SOURCE_FILENAME_OBSERVED_CHUNK_MAPPING_UNKNOWN',
                          'score_status': 'UNKNOWN_NOT_CAPTURED',
                          'source_metadata': {'source_format': Path(match.group(2)).suffix.lstrip('.')}})
    return context, citations


def freeze(root, out):
    previous = root / 'followup-routing-20261102'
    basepath = root / 'dynamic-routing-20261101/gold150/manifest.json'
    sources = {}

    def read(path):
        data = path.read_bytes()
        sources[str(path)] = {'byte_sha256': sha(data), 'canonical_json_sha256': sha(canonical(json.loads(data)))}
        return json.loads(data)

    base = read(basepath)
    split = read(basepath.with_name('split.json'))
    source = {c['qid']: c for c in base['cases']}
    ordinal = {c['gold'].get('ordinal'): c['qid'] for c in base['cases'] if c['gold'].get('ordinal')}
    inputs = [(p, read(p)) for p in sorted((previous / 'input').glob('*.json'))]
    followpath, follow = next((p, x) for p, x in inputs if x.get('version') == 'followup30-draft-v1')
    overlaypath, overlay = next((p, x) for p, x in inputs if x.get('dataset_version') == 'dynamic-routing-150-question-gold-candidate-v2')
    assert sha(canonical(base)) == overlay['base_frozen_manifest_sha256']
    edits = {x['qid']: x['candidate'] for x in overlay['changes']}
    for n in list(range(26, 36)) + list(range(41, 50)):
        qid = 'office50:%03d' % n
        prefix = 'For fiscal year 2018, ' if n <= 35 else 'According to the 2022 data, '
        edits[qid] = {'question': prefix + source[qid]['question']}
    fullscript = previous / 'full180_pipeline.py'
    sources[str(fullscript)] = {'byte_sha256': sha(fullscript.read_bytes())}
    ledgerpath = previous / 'full180-v2/ledger.sqlite3'
    sources[str(ledgerpath)] = {'byte_sha256': sha(ledgerpath.read_bytes())}
    db = sqlite3.connect('file:' + str(ledgerpath) + '?mode=ro', uri=True)
    attempts = {(q, r): (s, json.loads(i)) for q, r, s, i in db.execute('select qid,role,state,info from attempts')}
    db.close()
    # Chosen by task form and explicit original unanswerable label, before outputs.
    selection = [
        ('simple', 'office50:001'), ('simple', 'office50:024'),
        ('simple', ordinal[82]), ('simple', ordinal[86]),
        ('field_compare_calc', 'dataset200-mml:0223'),
        ('field_compare_calc', 'hotpotqa:5a745a6055429974ef308be4'),
        ('field_compare_calc', 'hotpotqa:5ab672165542995eadeeffe0'),
        ('field_compare_calc', ordinal[84]),
        ('actual_history', 'followup-c:003'), ('actual_history', 'followup-c:006'),
        ('insufficient', ordinal[112]), ('insufficient', 'dataset200-mml:0220')]
    splitrows = {r['qid']: r for r in split['records']}
    followrows = {f['id']: f for f in follow['cases']}
    cases = []
    for number, (stratum, ident) in enumerate(selection):
        provenance = {'base_manifest': str(basepath), 'pipeline': str(fullscript), 'ledger': str(ledgerpath)}
        if stratum == 'actual_history':
            f = followrows[ident]
            c = source[f['previous_qid']]
            initial_q = edits.get(c['qid'], {}).get('question', c['question'])
            prior_qid, prior_role = ('initial:' + c['qid'], 'initial') if f['previous_question'] == initial_q else ('prior:' + f['id'], 'prior')
            state, info = attempts[(prior_qid, prior_role)]
            assert state == 'COMPLETED' and info['finish_reason'] == 'stop'
            prior_messages = [c['messages'][0], {'role': 'user', 'content': f['previous_question']}]
            assert sha(canonical(prior_messages)) == info['messages_sha256']
            history = f.get('history_messages', [])[:-2] if f.get('history_messages') else []
            history += [{'role': 'user', 'content': f['previous_question']}, {'role': 'assistant', 'content': info['text']}]
            question = f['followup_question']
            messages = [c['messages'][0]] + history + [{'role': 'user', 'content': question}]
            qid, role = 'followup:' + ident, 'followup'
            gold = {'answer_points': [f['gold_answer']], 'expected_chunk_ids': f.get('expected_chunk_ids', []),
                    'kb_scope': c['gold'].get('kb_scope'), 'source_record': f}
            gold_status = 'EXISTING_FOLLOWUP_CANDIDATE_UNPROMOTED'
            provenance.update(followup_input=str(followpath), prior_qid=prior_qid, prior_role=prior_role,
                              prior_answer_sha256=sha(info['text'].encode('utf-8')),
                              prior_messages_sha256=info['messages_sha256'],
                              prior_provider_response_id=info.get('provider_request_id'),
                              history_reconstruction='EXACT_FULL180_PIPELINE_WITH_REAL_LEDGER_REPLY_NOT_SYNTHETIC_REFERENCE')
        else:
            c = source[ident]
            assert ident not in {x['qid'] for x in overlay['changes']}, 'do not promote Gold overlay'
            question = edits.get(ident, {}).get('question', c['question'])
            messages = [c['messages'][0], {'role': 'user', 'content': question}]
            history = []
            qid, role = 'initial:' + ident, 'initial'
            gold = c['gold']
            gold_status = 'ORIGINAL_FROZEN_NO_GOLD_OVERLAY'
        sent_state, sent_info = attempts[(qid, role)]
        mh = sha(canonical(messages))
        assert mh == sent_info['messages_sha256'], 'actual delivery hash mismatch: ' + qid
        context, citations = selected_citations(messages[0]['content'])
        assert sha(context.encode('utf-8')) == c['context_sha256'], 'context extraction mismatch: ' + qid
        provenance.update(original_qid=c['qid'], delivered_qid=qid, delivered_role=role,
                          delivered_state=sent_state, delivered_messages_sha256=sent_info['messages_sha256'],
                          context_source_path=c['context_source_path'], context_sha256=c['context_sha256'])
        case = dict(qid=qid, original_qid=c['qid'], stratum=stratum, question=question, messages=messages,
                    messages_sha256=mh, citations=citations, history_messages=history,
                    kb_scope=gold.get('kb_scope'), document_scope=c['gold'].get('original_case', {}).get('document_ids'),
                    retrieval_status='SUCCESS', retrieval_provenance='FROZEN_DELIVERED_CONTEXT_RECOVERED_NO_NEW_RETRIEVAL',
                    live_retrieval_status='UNKNOWN_NOT_MEASURED', selected_chunk_mapping_status='UNKNOWN',
                    evidence_completeness_status='UNKNOWN_NOT_INFERRED_FROM_GOLD',
                    retrieved_top10=c['retrieved_top10'], retrieved_top10_sha256=c['retrieved_top10_sha256'],
                    gold=gold, gold_status=gold_status, gold_sha256=sha(canonical(gold)),
                    source_group=c['source_group'], historical_split=splitrows.get(c['qid']),
                    provenance=provenance, source_status='DELIVERED_MESSAGES_HASH_VERIFIED', arm_rotation=number % 3)
        comparison = {k: case[k] for k in ('messages', 'citations', 'history_messages', 'kb_scope', 'document_scope', 'retrieval_status')}
        case['comparison_input_hash'] = sha(canonical(comparison))
        cases.append(case)
    assert len(cases) == 12 and len({c['qid'] for c in cases}) == 12
    manifest = dict(schema_version='dynamic-router-freeze12-v1', dataset_version='existing-full180-stratified12-20261102',
                    selection_policy='RETROSPECTIVE_TASK_FORM_NO_OUTPUT_FILTER_NO_BLIND_TEST_CLAIM',
                    source_hashes=sources, original_manifest_source_hashes=base['source_hashes'],
                    source_read_policy='READ_ONLY_EXISTING_CORPUS_INDEX_INPUTS_NO_CREDENTIALS_NO_MODEL_CALLS',
                    gold_policy='ORIGINAL_INITIAL_GOLD_AND_EXISTING_UNPROMOTED_FOLLOWUP_CANDIDATE_PRESERVED',
                    comparison_input_hash_keys=['messages', 'citations', 'history_messages', 'kb_scope', 'document_scope', 'retrieval_status'],
                    cases=cases)
    os.umask(0o077)
    out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with out.open('xb') as handle:
        handle.write(canonical(manifest) + b'\n')
    print(json.dumps({'status': 'PASS', 'cases': len(cases), 'manifest': str(out), 'byte_sha256': sha(out.read_bytes()),
                      'calls': 0, 'delivered_hash_verified': len(cases), 'actual_history': 2,
                      'followup_gold_unpromoted': 2, 'score_status': 'UNKNOWN_NOT_CAPTURED'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/opt/amrag/experiments'))
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    freeze(args.root, args.out)

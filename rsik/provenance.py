"""Additional source/position checks; never replaces the protected evaluator."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def assess_provenance(case, capture, specification, source_bytes):
    """Pure source-bound exact-clause predicates against actual delivery offsets.

    Whole reviewed clauses retain object/unit/condition context. This establishes
    only those frozen predicates, not semantic completeness of an answer.
    """
    chunks = {c['id']: c for c in case['chunks']}
    files = {f['id']: f for f in case['files']}
    members = capture.get('members', [])
    proofs = capture.get('proof', [])
    context = capture.get('context', '')
    messages = capture.get('messages', [])
    source_hash = specification['source_sha256']
    if hashlib.sha256(source_bytes).hexdigest() != source_hash:
        return {'status': 'UNKNOWN', 'reason': 'SOURCE_VERSION_MISMATCH', 'facts': []}
    try:
        source = source_bytes.decode('utf-8')
    except UnicodeError:
        return {'status': 'UNKNOWN', 'reason': 'SOURCE_DECODE_ERROR', 'facts': []}
    if (len(members) != len(proofs) or len({m['id'] for m in members}) != len(members)
            or not messages or capture.get('status') != 'OK'):
        return {'status': 'UNKNOWN', 'reason': 'DELIVERY_PROOF_INCOMPLETE', 'facts': []}
    valid_members = {}
    for member, proof in zip(members, proofs, strict=True):
        chunk = chunks.get(member.get('id'))
        loc = specification['member_spans'].get(member.get('id'))
        owner = files.get(member.get('file_id'))
        if not chunk or not loc or not owner:
            return {'status': 'UNKNOWN', 'reason': 'SOURCE_MEMBER_UNBOUND', 'facts': []}
        content = member.get('content')
        try:
            trustworthy = (
                proof['id'] == member['id'] and member['content'] == chunk['content']
                and member['file_id'] == chunk['file_id'] == specification['file_id']
                and member['kb_id'] == owner['kb_id'] == specification['kb_id']
                and owner['kb_id'] in case['allowed_kb_ids']
                and loc['source_sha256'] == source_hash
                and source[loc['start']:loc['end']] == content
                and hashlib.sha256(content.encode()).hexdigest() == chunk['content_sha256']
                and context[proof['context_start']:proof['context_end']] == content
                and messages[0]['content'][proof['prompt_start']:proof['prompt_end']] == content)
        except (KeyError, TypeError, ValueError):
            trustworthy = False
        if not trustworthy:
            return {'status': 'UNKNOWN', 'reason': 'SOURCE_OR_DELIVERY_MISMATCH', 'facts': []}
        valid_members[member['id']] = member
    facts = []
    for predicate in specification['facts']:
        if (predicate['rule'] != 'exact_clause_in_source_bound_member'
                or predicate['normalization'] != 'identity-unicode-codepoints-v1'
                or predicate['source_sha256'] != source_hash
                or source[predicate['start']:predicate['end']] != predicate['text']):
            return {'status': 'UNKNOWN', 'reason': 'UNVERIFIABLE_PREDICATE', 'facts': []}
        hits = []
        for cid in predicate['supporting_chunk_ids']:
            loc = specification['member_spans'].get(cid)
            if (cid in valid_members and loc and loc['source_sha256'] == source_hash
                    and loc['start'] <= predicate['start'] < predicate['end'] <= loc['end']
                    and predicate['text'] in valid_members[cid]['content']
                    and valid_members[cid]['file_id'] == predicate['file_id']
                    and valid_members[cid]['kb_id'] == predicate['kb_id']):
                hits.append(cid)
        facts.append({'fact_id': predicate['fact_id'], 'status': 'PRESENT' if hits else 'ABSENT',
                      'supporting_member_ids': hits})
    if not facts:
        return {'status': 'UNKNOWN', 'reason': 'NO_FROZEN_FACTS', 'facts': []}
    return {'status': 'PRESENT' if all(f['status'] == 'PRESENT' for f in facts) else 'ABSENT',
            'facts': facts, 'source_sha256': source_hash}


def audit_run(run_directory, fixture_directory):
    run, fixture = Path(run_directory), Path(fixture_directory)
    cases = json.loads((fixture / 'cases.json').read_text(encoding='utf-8'))
    provenance = json.loads((fixture / 'provenance.restricted.json').read_text(encoding='utf-8'))
    results = json.loads((run / 'results.json').read_text(encoding='utf-8'))
    stored = {r['qid']: r for r in results['rows']}
    rows = []
    for index, item in enumerate(cases, 1):
        qid, case = item['qid'], item['case']
        spec = provenance[qid]
        source_path = (fixture / spec['source_relative_path']).resolve()
        if not source_path.is_relative_to(fixture.resolve()):
            raise ValueError('source evidence escapes fixture directory')
        source_bytes = source_path.read_bytes()
        row = {'qid': qid, 'source_group': spec['source_group'], 'arms': {},
               'replay_status': stored.get(qid, {}).get('status', 'MISSING')}
        for arm in ('A', 'B'):
            path = run / f'case-{index:03}' / arm / 'capture.json'
            if not path.is_file():
                row['arms'][arm] = {'status': 'UNKNOWN', 'reason': 'CAPTURE_MISSING', 'facts': []}
            else:
                row['arms'][arm] = assess_provenance(case, json.loads(path.read_text(encoding='utf-8')), spec, source_bytes)
        rows.append(row)
    return {'schema': 'rsi-source-position-check/v1', 'protected_evaluator': 'UNCHANGED',
            'questions': len(cases), 'source_groups': len({r['source_group'] for r in rows}),
            'complete': len(stored) == len(cases) and all(r['replay_status'] == 'PAIRED' for r in rows),
            'all_required_present': all(r['arms'][a]['status'] == 'PRESENT' for r in rows for a in ('A', 'B')),
            'rows': rows}

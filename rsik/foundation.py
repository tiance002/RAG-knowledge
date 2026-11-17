"""Freeze the new real-data substrate without modifying protected interfaces."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unicodedata


ROOT = Path(__file__).resolve().parents[1]
PROTECTED = {
    'scripts/rsi_candidates.py': '2f537753f740ce832790f25b2af2de6d3e96563e6b18d50b2d5108d25beb36a4',
    'scripts/run_rsi_offline.py': '9b58b19149c08768ea865314fc0431d1ec0bbd8f46291ba2099ce18dfbe8c81f',
    'scripts/rsi_feedback.py': '5e16f1d153ac936cff3a54530f87b1bc8708544cd2481ea3b359a254383942cb',
    'scripts/rsi_delivery_worker.py': 'a5f793b9f8f6ad7908c78580a670c0cc5c909898dc8e4c36d05fdbc2a330d3da',
    'scripts/replay_source_preparation.py': '484274d899761eb0641a2ccc15886b8ae6b70dd242a0fb53f6f5adef9c49c020',
    'scripts/rsi_decision.py': 'b8f0d28211aca4ffb01b9f0c3bb66bc4ffd4669193430ac49a5acd86a1512fa1',
}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def verify_boundary():
    for name, expected in PROTECTED.items():
        if digest((ROOT / name).read_bytes()) != expected:
            raise ValueError('PROTECTED_BOUNDARY_CHANGED: ' + name)
    return dict(PROTECTED)


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def check_split(directory, *, expected_count):
    directory = Path(directory)
    manifest = load(directory / 'manifest.json')
    cases, evaluation = load(directory / 'cases.json'), load(directory / 'evaluation.json')
    source_map = load(directory / 'provenance.restricted.json')
    neighbor_stress=manifest.get('v22_scope',{}).get('mode')=='SOURCE_LAYOUT_STRESS'
    query_stress=manifest.get('v22_scope',{}).get('mode')=='SOURCE_QUERY_STRESS'
    if query_stress:
        if digest((directory/'qa_origins.restricted.json').read_bytes())!=manifest['v22_scope']['qa_origins_sha256']:
            raise ValueError('QUERY_ORIGINS_TAMPERED')
        origins=load(directory/'qa_origins.restricted.json')
    if len(cases) != expected_count or len({item['qid'] for item in cases}) != len(cases):
        raise ValueError('INSUFFICIENT_REAL_CASES')
    ids = {item['qid'] for item in cases}
    if set(evaluation) != ids or set(source_map) != ids:
        raise ValueError('UNPAIRED_FIXTURE')
    for name, value in [('cases', cases), ('evaluation', evaluation)]:
        if digest(canonical(value)) != manifest[name]['sha256']:
            raise ValueError('FIXTURE_TAMPERED')
    if len({p['source_group'] for p in source_map.values()}) < 8:
        raise ValueError('INSUFFICIENT_SOURCE_GROUPS')
    for item in cases:
        qid, case = item['qid'], item['case']
        expected, proof = evaluation[qid], source_map[qid]
        if query_stress:
            if case['seed_ids']!=proof['query_stress_original_seed_ids'] or digest(canonical({k:v for k,v in case.items() if k!='question'}))!=proof['selector_input_sha256']:
                raise ValueError('QUERY_STRESS_SELECTOR_INPUT_CHANGED')
            if qid in origins and (origins[qid]['core_sha256']!=proof['origin_core_sha256'] or origins[qid]['content']['question']!=case['question']
                    or [e['quote'] for e in origins[qid]['content']['evidence']]!=[f['text'] for f in expected['facts']]):
                raise ValueError('QUERY_ORIGIN_BINDING_MISMATCH')
        if neighbor_stress:
            from scripts.build_rsi_neighbor_stress import neighbor_seed
            old=proof.get('stress_original_seed_ids')
            direction=manifest.get('v22_scope',{}).get('seed_shift_direction','NEXT_ELSE_PREVIOUS')
            singleton_policy=manifest.get('v22_scope',{}).get('single_chunk_policy','ERROR')
            if singleton_policy not in {'ERROR','KEEP_ORIGINAL'}:raise ValueError('INVALID_SINGLE_CHUNK_STRESS_POLICY')
            if not isinstance(old,list) or not old or case['seed_ids']!=neighbor_seed(case,old,direction,singleton_policy=='KEEP_ORIGINAL'):
                raise ValueError('STRESS_SEED_TRANSFORM_MISMATCH')
        if set(expected) != {'status', 'facts', 'forbidden'} or expected['status'] != 'PAIRED':
            raise ValueError('PREDETERMINED_OR_UNSUPPORTED_EVALUATION')
        if not expected['facts'] or len(expected['facts']) != len(proof['facts']):
            raise ValueError('MISSING_SOURCE_PREDICATES')
        path = (directory / proof['source_relative_path']).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError('SOURCE_ESCAPES_FIXTURE')
        data = path.read_bytes()
        if digest(data) != proof['source_sha256']:
            raise ValueError('SOURCE_VERSION_MISMATCH')
        text = data.decode('utf-8')
        for chunk in case['chunks']:
            loc = proof['member_spans'][chunk['id']]
            if (type(chunk['original_index']) is not int or chunk['original_index'] < 0
                    or not chunk['content'] or chunk['content'].strip() != chunk['content']
                    or text[loc['start']:loc['end']] != chunk['content']
                    or digest(chunk['content'].encode()) != chunk['content_sha256']):
                raise ValueError('CHUNK_INCOMPATIBLE_WITH_PROTECTED_WORKER')
        for fact, predicate in zip(expected['facts'], proof['facts'], strict=True):
            if fact['text'] != predicate['text'] or text[predicate['start']:predicate['end']] != fact['text']:
                raise ValueError('FACT_SOURCE_MISMATCH')
            anchored_ids=proof['stress_original_seed_ids'] if neighbor_stress else case['seed_ids']
            if not query_stress and not any(cid in anchored_ids for cid in predicate['supporting_chunk_ids']):
                raise ValueError('EVIDENCE_ANCHORED_SEED_MISSING')
    return {'questions': len(cases), 'source_groups': len({p['source_group'] for p in source_map.values()}),
            'pre_blocked': 0, 'eligible': len(cases),
            'artifact_hashes': {name: digest((directory / name).read_bytes()) for name in
                                ('manifest.json', 'cases.json', 'evaluation.json', 'provenance.restricted.json',
                                 'sources.manifest.json', 'all.json')},
            'protected': verify_boundary(), 'character_rule': 'len(actual_capture.context); identity Unicode code points',
            'human_review': False, 'review_policy': 'owner-authorized self-check + existing AI semantic review'}


def source_separation(training, holdout):
    """Private duplicate screen; return no holdout text, titles or source IDs."""
    def normal(text):
        return ''.join(c for c in unicodedata.normalize('NFKC', text).casefold() if c.isalnum())
    def read_sources(directory):
        result = []
        for path in sorted((Path(directory) / 'sources').glob('*.md')):
            text = path.read_text(encoding='utf-8')
            heading = next((line for line in text.splitlines() if line.strip()), '')
            body = normal(text)
            result.append((path.stem, normal(heading), {body[i:i+8] for i in range(max(0, len(body)-7))}))
        return result
    a, b = read_sources(training), read_sources(holdout)
    collisions, maximum = 0, 0.0
    for aid, atitle, ag in a:
        for bid, btitle, bg in b:
            overlap = len(ag & bg) / max(1, min(len(ag), len(bg)))
            maximum = max(maximum, overlap)
            if aid == bid or (atitle and atitle == btitle) or overlap >= .5:
                collisions += 1
    return {'status': 'SOURCE_SEPARATED' if not collisions else 'BLOCKED', 'train_sources': len(a),
            'holdout_sources': len(b), 'collisions': collisions, 'max_8gram_containment': maximum,
            'limitations': ['Duplicate screen does not prove semantic independence.',
                           'Existing AI-reviewed public source pool; not a claim of never-seen source data.']}

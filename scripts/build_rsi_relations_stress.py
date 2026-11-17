"""Build offline, reviewed QUESTION PARAPHRASE and seed stress fixtures.

This is fixture engineering only: no runtime query rewrite, model call, search,
qualification or admission evidence. Review input is private and hash-bound.
"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.curate_rsi_relations_dev import build as curate
from rsik.events import canonical, digest
from rsik.locking import restrict_directory


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_geometry(row, seeds, direction=None):
    """All required supports reachable at fixed window=1; real nonseed edge."""
    positions = {}
    for member in row['members']:
        origin = member['origin_qid']
        positions[member['key']] = (origin, sum(v[0] == origin for v in positions.values()))
    if not seeds or len(set(seeds)) != len(seeds) or set(seeds) - set(positions):
        raise ValueError('STRESS_SEED_SET_INVALID')
    supports = {s['member'] for a in row['atoms'] for s in a['supports']}
    for key in supports:
        origin, index = positions[key]
        if not any(positions[s][0] == origin and abs(index - positions[s][1]) <= 1 for s in seeds):
            raise ValueError('STRESS_SUPPORT_OUTSIDE_FIXED_WINDOW')
    for atom in row['atoms']:
        by_id = {s['support_id']: s['member'] for s in atom['supports']}
        for alternative in atom['support_alternatives']:
            if not {by_id[s] for s in alternative} - set(seeds):
                raise ValueError('STRESS_NONSEED_NECESSITY_LOST')
    if direction:
        sign = 1 if direction == 'forward' else -1
        nonseed = supports - set(seeds)
        offsets = [[positions[key][1] - positions[s][1] for s in seeds
                    if positions[key][0] == positions[s][0]
                    and abs(positions[key][1] - positions[s][1]) <= 1] for key in nonseed]
        if not nonseed or any(not values or any(v != sign for v in values) for values in offsets):
            raise ValueError('STRESS_DIRECTION_NOT_ESTABLISHED')


def prepare_plans(base, reviews):
    """Keep atoms unchanged; require explicit reviewed geometry, never fallback."""
    if reviews.get('schema') != 'rsi-relations-stress-review/v1' or reviews.get('review_status') != 'AI_REVIEWED':
        raise ValueError('STRESS_AI_REVIEW_REQUIRED')
    if reviews.get('base_curation_digest') != digest(base):
        raise ValueError('STRESS_REVIEW_BASE_CHANGED')
    if not reviews.get('reviewer') or not reviews.get('reviewed_at') or not reviews.get('lineage_id'):
        raise ValueError('STRESS_REVIEW_IDENTITY_REQUIRED')
    rows = reviews['cases']
    if len(rows) != len(base['cases']) or {r['qid'] for r in rows} != {r['qid'] for r in base['cases']}:
        raise ValueError('STRESS_ORIGIN_SET_CHANGED')
    by_qid = {r['qid']: r for r in rows}
    plans = {}
    for suite in ('primary', 'question-paraphrase', 'forward-seed', 'backward-seed'):
        plan = deepcopy(base)
        plan.update(lineage_id=reviews['lineage_id'], reviewer=reviews['reviewer'], reviewed_at=reviews['reviewed_at'])
        for row in plan['cases']:
            review = by_qid[row['qid']]
            subject = {k: v for k, v in review.items() if k != 'subject_sha256'}
            if review.get('subject_sha256') != digest(subject) or not review.get('equivalence_rationale'):
                raise ValueError('STRESS_REVIEW_BINDING_INVALID')
            expected_scope = {'original_question':row['question'], 'atoms_digest':digest(row['atoms']),
                              'source_member_intervals_digest':digest(row['members'])}
            if review.get('semantic_review_scope') != expected_scope:
                raise ValueError('STRESS_SEMANTIC_REVIEW_SCOPE_CHANGED')
            replacements = review.get('member_replacements', {})
            original_members = {m['key']: m for m in row['members']}
            if set(replacements) - {m['key'] for m in row['members']}:
                raise ValueError('STRESS_REPLACEMENT_MEMBER_UNKNOWN')
            row['members'] = [deepcopy(replacements.get(m['key'], m)) for m in row['members']]
            if any(k != m['key'] for k, m in replacements.items()):
                raise ValueError('STRESS_REPLACEMENT_KEY_CHANGED')
            # Additional members are reviewed exact source intervals, not facts.
            row['members'].extend(deepcopy(review.get('additional_members', [])))
            if len({m['key'] for m in row['members']}) != len(row['members']):
                raise ValueError('STRESS_MEMBER_KEY_DUPLICATED')
            new_members = {m['key']:m for m in row['members']}
            if {m['origin_qid'] for m in new_members.values()} != {m['origin_qid'] for m in original_members.values()}:
                raise ValueError('STRESS_SOURCE_ORIGIN_SET_CHANGED')
            for origin in {m['origin_qid'] for m in new_members.values()}:
                ordered = [m['start'] for m in new_members.values() if m['origin_qid'] == origin]
                if ordered != sorted(ordered):
                    raise ValueError('STRESS_SOURCE_ORDER_CHANGED')
            for atom in row['atoms']:
                for support in atom['supports']:
                    old = original_members[support['member']]
                    key = review.get('support_member_remap', {}).get(support['member'], support['member'])
                    new = new_members.get(key)
                    if (not new or new['origin_qid'] != old['origin_qid']
                            or not new['start'] <= old['start'] < old['end'] <= new['end']):
                        raise ValueError('STRESS_EXACT_SUPPORT_IDENTITY_LOST')
                    support['member'] = key
            row['seeds'] = list(review.get('primary_seed', row['seeds']))
            if suite == 'question-paraphrase':
                question = review['question_paraphrase']
                if not isinstance(question, str) or not question.strip() or question == row['question']:
                    raise ValueError('STRESS_PARAPHRASE_MUST_CHANGE')
                row['question'] = question
            elif suite in {'forward-seed', 'backward-seed'}:
                row['seeds'] = list(review[suite.replace('-', '_')])
                if set(row['seeds']) == set(review.get('primary_seed', next(r['seeds'] for r in base['cases'] if r['qid'] == row['qid']))):
                    raise ValueError('STRESS_SEED_SET_MUST_CHANGE_FROM_PRIMARY')
            validate_geometry(row, row['seeds'], suite.split('-')[0] if suite.endswith('-seed') else None)
        plans[suite] = plan
    return plans


def directionality(plan):
    """Report geometry independently of names and unchanged/fallback counts."""
    result = []
    for row in plan['cases']:
        positions = {}
        for m in row['members']:
            origin = m['origin_qid']
            positions[m['key']] = (origin, sum(v[0] == origin for v in positions.values()))
        nonseed = sorted({s['member'] for a in row['atoms'] for s in a['supports']} - set(row['seeds']))
        details = []
        for key in nonseed:
            origin, index = positions[key]
            offsets = [index - positions[s][1] for s in row['seeds']
                       if positions[s][0] == origin and abs(index - positions[s][1]) <= 1]
            details.append({'member':key, 'reaching_seed_offsets':offsets,
                            'only_preceding_support':bool(offsets) and all(v < 0 for v in offsets),
                            'only_following_support':bool(offsets) and all(v > 0 for v in offsets)})
        result.append({'qid':row['qid'], 'nonseed_supports':details})
    return result


def static_opportunity(cases, specs):
    """Frozen geometry/removal audit, explicitly not an execution capture."""
    rows = []
    for item in cases:
        case = item['case']; spec = specs[item['qid']]
        chunks = {m['id']:m for m in case['chunks']}
        seeds = [chunks[mid] for mid in case['seed_ids']]
        available = {m['id'] for m in chunks.values() if any(
            m['file_id'] == s['file_id'] and abs(m['original_index'] - s['original_index']) <= 1 for s in seeds)}
        removable = []
        for mid in available - set(case['seed_ids']):
            retained = available - {mid}
            if all(any(all(next(s['member_id'] for s in atom['supports'] if s['support_id'] == sid) in retained
                           for sid in alternative) for alternative in atom['support_alternatives']) for atom in spec['atoms']):
                removable.append(mid)
        rows.append({'qid':item['qid'],'window_reachable_members':len(available),
                     'mandatory_seeds':len(seeds),'nonseed_members':len(available-set(case['seed_ids'])),
                     'individually_removable_nonseeds':sorted(removable),
                     'reachable_content_chars':sum(len(chunks[mid]['content']) for mid in available),
                     'maximum_member_chars':max(len(chunks[mid]['content']) for mid in available)})
    return {'method':'STATIC_FIXED_WINDOW_AND_EXACT_ATOM_ALTERNATIVES; not actual prepare/deliver execution',
            'rows':rows,'removable_nonseed_total':sum(len(r['individually_removable_nonseeds']) for r in rows),
            'max_reachable_content_chars':max(r['reachable_content_chars'] for r in rows),
            'all_reachable_content_under_4000':all(r['reachable_content_chars'] <= 4000 for r in rows)}


def build(source, curation, review, output, families=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    base, reviews = load(curation), load(review)
    plans = prepare_plans(base, reviews)
    if output.exists():
        raise ValueError('FRESH_STRESS_DIRECTORY_REQUIRED')
    output.mkdir(parents=True); restrict_directory(output)
    primary = {r['qid']: r for r in base['cases']}
    summaries = {}
    for suite, plan in plans.items():
        plan_path = output / (suite + '.curation.restricted.json')
        plan_path.write_bytes(canonical(plan))
        directory = output / suite
        receipt = curate(source, plan_path, directory, families, 'primary')
        # The legacy curator uses whole-member supports. Restore every original
        # exact support interval/text/binding inside the reviewed expanded member.
        specs = load(directory / 'relations.restricted.json')
        for row in base['cases']:
            qid = row['qid']; spec = specs[qid]
            reviewed = next(r for r in reviews['cases'] if r['qid'] == qid)
            original_members = {m['key']: m for m in row['members']}
            for definition, atom in zip(row['atoms'], spec['atoms'], strict=True):
                for original, support in zip(definition['supports'], atom['supports'], strict=True):
                    member = original_members[original['member']]
                    key = reviewed.get('support_member_remap', {}).get(original['member'], original['member'])
                    mid = qid + '-' + key; loc = spec['member_spans'][mid]
                    src = spec['sources'][loc['source_id']]
                    text = (directory / src['relative_path']).read_bytes().decode('utf-8')
                    support.update(member_id=mid, source_id=loc['source_id'], start=member['start'],
                                   end=member['end'], text=text[member['start']:member['end']])
                atom['review']['subject_sha256'] = digest({k:v for k,v in atom.items() if k != 'review'})
        (directory / 'relations.restricted.json').write_bytes(canonical(specs))
        origins = load(directory / 'origins.restricted.json')
        cases = {r['qid']: r['case'] for r in load(directory / 'cases.json')}
        for origin in origins:
            qid = origin['qid']; row = next(r for r in plan['cases'] if r['qid'] == qid)
            primary_row = next(r for r in plans['primary']['cases'] if r['qid'] == qid)
            origin.update(variant=suite, seed_changed=row['seeds'] != primary_row['seeds'],
                          query_variant='AI_REVIEWED_QUESTION_PARAPHRASE' if suite == 'question-paraphrase' else 'UNCHANGED',
                          original_seed_keys=primary[qid]['seeds'], primary_seed_keys=primary_row['seeds'], reviewed_seed_keys=row['seeds'],
                          stress_review_sha256=sha(review))
        (directory / 'origins.restricted.json').write_bytes(canonical(origins))
        manifest = load(directory / 'manifest.json'); manifest['suite_variant'] = suite
        manifest['stress_review_sha256'] = sha(review)
        manifest['source_layout_profile'] = reviews.get('source_layout_profile', 'unchanged-source-members/v1')
        manifest['artifact_hashes']['relations.restricted.json'] = sha(directory / 'relations.restricted.json')
        (directory / 'manifest.json').write_bytes(canonical(manifest))
        # Manifest mutation is explicit; update its freeze identity, not stale proof.
        from rsik.source_relations import check_fixture
        receipt['freeze'] = check_fixture(directory, expected_count=20, review_policy='research-source-family/v1')
        receipt['stress_review_sha256'] = sha(review)
        (directory / 'build-receipt.json').write_bytes(canonical(receipt))
        summaries[suite] = {'questions':len(cases),
            'changed_seed_cases':sum(r['seed_changed'] for r in origins),
            'changed_questions':sum(cases[q]['question'] != primary[q]['question'] for q in cases),
            'fallback_cases':sum(not r['seed_changed'] for r in origins) if suite.endswith('-seed') else 0,
            'directionality':directionality(plan),
            'static_opportunity':static_opportunity(load(directory / 'cases.json'), specs),
            'fixture_hashes':receipt['freeze']['artifact_hashes']}
    summary = {'schema':'rsi-relations-stress-build/v1','suites':summaries,
        'base_curation_sha256':sha(curation),'review_sha256':sha(review),
        'independent_question_origins':len(primary),'additional_independent_samples':0,
        'execution_status':'NOT_EXECUTED','production_qualified':False,
        'limitations':reviews.get('limitations', []),
        'claim':'Static reviewed fixture validity only; actual prepare/A/A and removal replay still required.'}
    (output / 'build-summary.restricted.json').write_bytes(canonical(summary))
    return summary


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'curation', 'review', 'output'):
        p.add_argument('--' + name, required=True)
    p.add_argument('--families'); a = p.parse_args()
    print(json.dumps(build(a.source, a.curation, a.review, a.output, a.families), ensure_ascii=True))

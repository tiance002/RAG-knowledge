"""Build a separate reviewed DEV MAIN layout with real frozen neighbors.

No candidate policy, model request, service, dynamic execution or qualification.
The exact source-relations atoms stay unchanged; original unused neighbor text
may provide an opportunity, which is audited rather than guaranteed.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.build_rsi_relations_stress import build as build_stress
from scripts.build_rsi_relations_stress import load, prepare_plans, sha, static_opportunity
from scripts.curate_rsi_relations_dev import build as curate
from rsik.events import canonical, digest
from rsik.source_relations import check_fixture


def prepare_main(source, base, review):
    if review.get('schema') != 'rsi-relations-main-review/v1':
        raise ValueError('MAIN_REVIEW_SCHEMA_REQUIRED')
    provenance = load(Path(source) / 'provenance.restricted.json')
    old_cases = {r['qid']:r['case'] for r in load(Path(source) / 'cases.json')}
    base_rows = {r['qid']:r for r in base['cases']}
    for row in review['cases']:
        for evidence in row.get('original_neighbor_evidence', []):
            p = provenance[evidence['origin_qid']]
            loc = p['member_spans'].get(evidence['original_member_id'])
            if (not loc or (loc['start'], loc['end']) != (evidence['start'], evidence['end'])
                    or p['source_sha256'] != evidence['source_sha256']):
                raise ValueError('MAIN_ORIGINAL_NEIGHBOR_IDENTITY_CHANGED')
            original_chunk = next((m for m in old_cases[evidence['origin_qid']]['chunks']
                                   if m['id']==evidence['original_member_id']),None)
            raw = (Path(source) / p['source_relative_path']).read_bytes()
            if (hashlib.sha256(raw).hexdigest()!=p['source_sha256'] or not original_chunk
                    or original_chunk['file_id']!=p['file_id']
                    or original_chunk['content']!=raw.decode('utf-8')[loc['start']:loc['end']]):
                raise ValueError('MAIN_NEIGHBOR_NOT_IN_FROZEN_CASE')
            member = next((m for m in row['additional_members'] if m['key'] == evidence['member_key']), None)
            if (not member or member['origin_qid'] != evidence['origin_qid']
                    or (member['start'], member['end']) != (loc['start'], loc['end'])):
                raise ValueError('MAIN_NEIGHBOR_MUST_BE_EXACT_FROZEN_CHUNK')
        if {e['member_key'] for e in row.get('original_neighbor_evidence', [])} != {m['key'] for m in row['additional_members']}:
            raise ValueError('MAIN_ALL_ADDED_NEIGHBORS_REQUIRE_ORIGINAL_PROOF')
        original = base_rows[row['qid']]
        for origin in original['origin_qids']:
            members = [row.get('member_replacements', {}).get(m['key'],m) for m in original['members'] if m['origin_qid']==origin]
            boundary = members[-1]['end']
            for added in [m for m in row['additional_members'] if m['origin_qid']==origin]:
                if added['start'] != boundary:
                    raise ValueError('MAIN_ORIGINAL_NEIGHBOR_MUST_BE_CONTIGUOUS')
                boundary = added['end']
    # Reuse the exact-support, source identity, nonseed and exclusive-direction
    # validators; only primary is materialized, under its own MAIN lineage.
    plan = prepare_plans(base, {**review, 'schema':'rsi-relations-stress-review/v1'})['primary']
    for row in plan['cases']:
        positions = {}
        for member in row['members']:
            origin = member['origin_qid']
            positions[member['key']] = (origin,sum(p[0]==origin for p in positions.values()))
        reachable = [m for m in row['members'] if any(positions[m['key']][0]==positions[s][0]
                    and abs(positions[m['key']][1]-positions[s][1])<=1 for s in row['seeds'])]
        if sum(m['end']-m['start'] for m in reachable)>4000:
            raise ValueError('MAIN_FIXED_CHARACTER_CAP_EXCEEDED')
    return plan


def build(source, curation, review_path, output, families=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    base, review = load(curation), load(review_path)
    plan = prepare_main(source, base, review)
    if output.exists():
        raise ValueError('FRESH_MAIN_DIRECTORY_REQUIRED')
    # Curator creates and restricts the fresh directory. Its input plan is kept
    # alongside the output as a reviewable private derivation, never overwritten.
    plan_path = output.with_name(output.name + '.curation.restricted.json')
    if plan_path.exists():
        raise ValueError('FRESH_MAIN_PLAN_REQUIRED')
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_bytes(canonical(plan))
    receipt = curate(source, plan_path, output, families, 'primary')
    specs = load(output / 'relations.restricted.json')
    reviewed = {r['qid']:r for r in review['cases']}
    for row in base['cases']:
        qid = row['qid']; spec = specs[qid]; members = {m['key']:m for m in row['members']}
        for definition, atom in zip(row['atoms'], spec['atoms'], strict=True):
            for original, support in zip(definition['supports'], atom['supports'], strict=True):
                old = members[original['member']]
                key = reviewed[qid].get('support_member_remap', {}).get(original['member'], original['member'])
                mid = qid + '-' + key; loc = spec['member_spans'][mid]
                text = (output / spec['sources'][loc['source_id']]['relative_path']).read_bytes().decode('utf-8')
                support.update(member_id=mid, source_id=loc['source_id'], start=old['start'], end=old['end'],
                               text=text[old['start']:old['end']])
            atom['review']['subject_sha256'] = digest({k:v for k,v in atom.items() if k != 'review'})
    (output / 'relations.restricted.json').write_bytes(canonical(specs))
    manifest = load(output / 'manifest.json')
    manifest.update(suite_variant='main-real-neighbors', source_layout_profile=review['source_layout_profile'],
                    main_review_sha256=sha(review_path))
    manifest['artifact_hashes']['relations.restricted.json'] = sha(output / 'relations.restricted.json')
    (output / 'manifest.json').write_bytes(canonical(manifest))
    receipt['freeze'] = check_fixture(output, expected_count=20, review_policy='research-source-family/v1')
    receipt.update(main_review_sha256=sha(review_path), execution_status='NOT_EXECUTED')
    (output / 'build-receipt.json').write_bytes(canonical(receipt))
    cases = load(output / 'cases.json')
    summary = {'schema':'rsi-main-neighbor-build/v1','fixture':receipt['freeze'],
        'independent_question_origins':20,'additional_independent_samples':0,
        'atoms':sum(len(s['atoms']) for s in specs.values()),
        'static_opportunity':static_opportunity(cases, specs),
        'original_neighbor_members':sum(len(r['original_neighbor_evidence']) for r in review['cases']),
        'execution_status':'NOT_EXECUTED','production_qualified':False,
        'limitations':review.get('limitations', []),
        'claim':'Real-source fixture engineering only; no safety, replay, statistical or compression result.'}
    (output / 'main-build-summary.restricted.json').write_bytes(canonical(summary))
    return summary


def build_matched_suites(source, curation, review_path, output, families=None):
    """Materialize four coherent suites only after actual-neighbor proof checks."""
    output = Path(output).resolve()
    base, review = load(curation), load(review_path)
    prepare_main(source, base, review)
    if output.exists():
        raise ValueError('FRESH_MAIN_DIRECTORY_REQUIRED')
    derivation = output.with_name(output.name + '.review-derivation.restricted.json')
    if derivation.exists():
        raise ValueError('FRESH_MAIN_REVIEW_DERIVATION_REQUIRED')
    derived = {**review, 'schema':'rsi-relations-stress-review/v1',
               'derived_from_main_review_sha256':sha(review_path),
               'derivation_rule':'SCHEMA_ADAPTER_ONLY; all per-case source-neighbor evidence and review bindings retained'}
    derivation.parent.mkdir(parents=True,exist_ok=True)
    derivation.write_bytes(canonical(derived))
    summary = build_stress(source,curation,derivation,output,families)
    matched = {'schema':'rsi-main-matched-suites-build/v1','main_review_sha256':sha(review_path),
               'derivation_sha256':sha(derivation),'lineage_id':review['lineage_id'],
               'source_layout_profile':review['source_layout_profile'],
               'original_neighbor_members':sum(len(r['original_neighbor_evidence']) for r in review['cases']),
               'suites':summary['suites'],'execution_status':'NOT_EXECUTED',
               'independent_question_origins':20,'additional_independent_samples':0,
               'production_qualified':False}
    (output/'main-matched-summary.restricted.json').write_bytes(canonical(matched))
    return matched


if __name__ == '__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('source','curation','review','output'):p.add_argument('--'+name,required=True)
    p.add_argument('--families');p.add_argument('--matched-suites',action='store_true');a=p.parse_args()
    function=build_matched_suites if a.matched_suites else build
    print(json.dumps(function(a.source,a.curation,a.review,a.output,a.families), ensure_ascii=True))

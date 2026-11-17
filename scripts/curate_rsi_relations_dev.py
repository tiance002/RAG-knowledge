"""Build a restricted, reviewed DEV lineage from frozen source intervals.

No model, service, holdout or legacy evaluator access. The private curation
manifest is an explicit input, never embedded in this distributable script.
This produces controls to execute; it does not manufacture execution evidence.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rsik.events import canonical, digest
from rsik.source_relations import check_fixture
from rsik.locking import restrict_directory


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def build(source, curation, output, families=None, variant='primary'):
    source, output = Path(source).resolve(), Path(output).resolve()
    plan = load(curation)
    if plan['partition'] != 'DEV' or source.name != 'train':
        raise ValueError('EXPLICIT_LEGACY_TRAIN_DEV_ONLY')
    if output.exists():
        raise ValueError('FRESH_LINEAGE_DIRECTORY_REQUIRED')
    provenance = load(source/'provenance.restricted.json')
    old_cases = {r['qid']: r['case'] for r in load(source/'cases.json')}
    family_map = load(families) if families else {}
    output.mkdir(parents=True); restrict_directory(output)
    (output/'sources').mkdir()
    cases, specs, controls, origins, resources = [], {}, [], [], {}
    for row in plan['cases']:
        qid = row['qid']; chunks, files, sources, spans, mids = [], {}, {}, {}, {}
        for block in row['members']:
            origin = block['origin_qid']; prov = provenance[origin]
            h = prov['source_sha256']; sid = 'legacy-dev-'+h
            raw = (source/prov['source_relative_path']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != h:
                raise ValueError('LEGACY_SOURCE_CHANGED')
            text = raw.decode('utf-8'); start, end = block['start'], block['end']
            excerpt = text[start:end]
            if not excerpt or hashlib.sha256(excerpt.encode()).hexdigest() != block['text_sha256']:
                raise ValueError('CURATED_INTERVAL_CHANGED')
            target = output/'sources'/(h+'.md')
            if not target.exists():
                # Small selected immutable originals only, required by adapter confinement.
                try:
                    os.link(source/prov['source_relative_path'], target)
                    storage = 'HARDLINK_IMMUTABLE_SOURCE'
                except OSError:
                    shutil.copyfile(source/prov['source_relative_path'], target)
                    storage = 'COPY_CROSS_VOLUME_FALLBACK'
                resources[h] = {'source':str(source/prov['source_relative_path']),
                    'bytes':len(raw),'storage':storage,'reason':'source adapter requires fixture-contained exact original',
                    'cleanup':'retain frozen evidence for reproducible DEV qualification'}
            original_file = next(f for f in old_cases[origin]['files'] if f['id']==prov['file_id'])
            files[original_file['id']] = original_file
            metadata = family_map.get(h, {})
            sources[sid] = {'document_id':prov['file_id'],'version':'sha256:'+h,
                'content_sha256':h,'source_family_id':metadata.get('source_family_id','provisional-origin:'+prov['file_id']),
                'relation':metadata.get('relation','UNKNOWN'),
                'related_source_ids':metadata.get('related_source_ids',[]),
                'exposure_status':metadata.get('exposure_status','EXPOSURE_UNKNOWN'),
                'review_status':metadata.get('review_status','AI_REVIEWED'),
                'file_id':prov['file_id'],'kb_id':prov['kb_id'],
                'relative_path':'sources/'+h+'.md'}
            mid = qid+'-'+block['key']; mids[block['key']] = mid
            line_start = text.count('\n',0,start)+1; line_end = text.count('\n',0,end)+1
            chunks.append({'id':mid,'file_id':prov['file_id'],'content':excerpt,
                'content_sha256':block['text_sha256'],'content_type':'text',
                'original_index':sum(c['file_id']==prov['file_id'] for c in chunks),
                'heading_text':text.splitlines()[0],
                'source_locator':{'record_start':line_start,'record_end':line_end}})
            spans[mid] = {'source_id':sid,'start':start,'end':end}
        case = {'question':row['question'],'files':list(files.values()),'chunks':chunks,
            'allowed_kb_ids':sorted({f['kb_id'] for f in files.values()}),
            'seed_ids':[mids[k] for k in row['seeds']], 'expected_seed_count':len(row['seeds'])}
        original_seeds = list(case['seed_ids'])
        if variant in {'forward-seed','backward-seed'}:
            proposal = []
            for fid in files:
                file_chunks = [c for c in chunks if c['file_id']==fid]
                proposal.append(file_chunks[-1 if variant=='forward-seed' else 0]['id'])
            # A stress variant must still have a required nonseed support in every atom.
            index_by_mid = {c['id']:(c['file_id'],c['original_index']) for c in chunks}
            support_mids = {mids[s['member']] for a in row['atoms'] for s in a['supports']}
            reachable = all(any(index_by_mid[mid][0] == index_by_mid[seed][0]
                and abs(index_by_mid[mid][1]-index_by_mid[seed][1]) <= 1 for seed in proposal)
                for mid in support_mids)
            if reachable and all(any(mids[s['member']] not in proposal for s in a['supports']) for a in row['atoms']):
                case['seed_ids'] = proposal; case['expected_seed_count'] = len(proposal)
        atoms = []
        for definition in row['atoms']:
            atom = {k:v for k,v in definition.items() if k not in {'supports','remove_members'}}
            atom['equivalence_rule'] = 'EXACT_REVIEWED_SUPPORT_ALTERNATIVES'
            atom['supports'] = []
            for s in definition['supports']:
                mid = mids[s['member']]; loc = spans[mid]
                chunk = next(c for c in chunks if c['id']==mid)
                atom['supports'].append({'support_id':s['support_id'],'member_id':mid,
                    'source_id':loc['source_id'],'start':loc['start'],'end':loc['end'],
                    'text':chunk['content'],'binds':s['binds']})
            atom['review'] = {'status':'AI_REVIEWED','reviewer':plan['reviewer'],
                'reviewed_at':plan['reviewed_at'],'subject_sha256':digest(atom)}
            atoms.append(atom)
            preferred = [mids[k] for k in definition['remove_members'] if mids[k] not in case['seed_ids']]
            removable = preferred or [s['member_id'] for s in atom['supports'] if s['member_id'] not in case['seed_ids']][:1]
            if not removable:
                raise ValueError('NONSEED_REMOVAL_CONTROL_REQUIRED')
            controls.append({'qid':qid,'atom_id':atom['atom_id'],
                'removed_member_ids':removable,
                'operation':'REAL_DELIVER_MEMBER_REMOVAL', 'expected_positive':'PRESENT','expected_negative':'ABSENT'})
        specs[qid] = {'profile':'source-relations/v1','lineage_id':plan['lineage_id'],
            'citation_binding_profile':'numbered-delivery/v1',
            'sources':sources,'member_spans':spans,'atoms':atoms}
        cases.append({'qid':qid,'case':case})
        origins.append({'qid':qid,'origin_qids':row['origin_qids'],
            'question_origin':row['question_origin'],'curation_sha256':digest(row),
            'variant':variant,'seed_changed':case['seed_ids']!=original_seeds,
            'query_variant':'REPEATED_PRIMARY_CONTROL' if variant=='query-conservative' else 'UNCHANGED',
            'statistical_independence':'SAME_20_ORIGINS_PAIRED_STRESS_ONLY'})
    for name, value in [('cases.json',cases),('relations.restricted.json',specs),
                        ('control-spec.restricted.json',controls),('origins.restricted.json',origins)]:
        (output/name).write_bytes(canonical(value))
    manifest = {'profile':'source-relations/v1','partition':'DEV','lineage_id':plan['lineage_id'],
        'review_status':'AI_REVIEWED','production_qualified':False,
        'suite_variant':variant,'independent_question_origins':20,
        'artifact_hashes':{n:hashlib.sha256((output/n).read_bytes()).hexdigest()
            for n in ('cases.json','relations.restricted.json')},
        'family_review_status':'AUDITED_MAPPING' if families else 'PROVISIONAL_NOT_INDEPENDENCE_PROOF'}
    (output/'manifest.json').write_bytes(canonical(manifest))
    freeze = check_fixture(output,expected_count=20,review_policy='research-source-family/v1')
    receipt = {'schema':'rsi-dev-curation-build/v1','freeze':freeze,
        'curation_sha256':hashlib.sha256(Path(curation).read_bytes()).hexdigest(),
        'family_mapping_sha256':hashlib.sha256(Path(families).read_bytes()).hexdigest() if families else None,
        'legacy_manifest_sha256':hashlib.sha256((source/'manifest.json').read_bytes()).hexdigest(),
        'resource_copies':resources,'reused':'existing raw DEV and legacy origin metadata; no environment created',
        'execution_status':'NOT_EXECUTED','claim':'fixture validity only; actual positive/negative replay required'}
    (output/'build-receipt.json').write_bytes(canonical(receipt))
    return receipt


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('--source',required=True)
    p.add_argument('--curation',required=True); p.add_argument('--output',required=True)
    p.add_argument('--families'); p.add_argument('--variant',default='primary',
        choices=('primary','query-conservative','forward-seed','backward-seed')); a=p.parse_args()
    result=build(a.source,a.curation,a.output,a.families,a.variant)
    print(json.dumps({'questions':result['freeze']['questions'],'execution_status':result['execution_status']}))

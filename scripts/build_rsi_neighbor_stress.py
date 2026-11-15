"""Freeze a source-layout stress fixture; preserve original facts and source bytes.

Deterministically move each seed to its next source chunk (otherwise previous),
without reading fact contents or choosing cases by candidate outcomes. This is a
controlled neighbor dependency test, not a real retrieval or independent holdout.
"""
import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.foundation import check_split,canonical,digest


def load(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def neighbor_seed(case, seeds, direction='NEXT_ELSE_PREVIOUS', allow_singleton=False):
    if direction not in {'NEXT_ELSE_PREVIOUS','PREVIOUS_ELSE_NEXT'}:raise ValueError('INVALID_STRESS_DIRECTION')
    chunks={c['id']:c for c in case['chunks']};selected=[]
    for seed in seeds:
        current=chunks[seed]
        same=sorted([c for c in chunks.values() if c['file_id']==current['file_id']],key=lambda c:c['original_index'])
        i=next(i for i,c in enumerate(same) if c['id']==seed)
        if direction=='PREVIOUS_ELSE_NEXT':target=same[i-1] if i else same[i+1] if i+1<len(same) else None
        else:target=same[i+1] if i+1<len(same) else same[i-1] if i else None
        if target is None:
            if not allow_singleton:raise ValueError('SOURCE_HAS_NO_NEIGHBOR; never filter a case after seeing results')
            target=current
        if target['id'] not in selected:selected.append(target['id'])
    return selected


def build(source,out,direction='NEXT_ELSE_PREVIOUS'):
    source,out=Path(source).resolve(),Path(out).resolve()
    check_split(source,expected_count=20)
    out.mkdir(parents=True,exist_ok=False)
    for name in ('all.json','evaluation.json','sources.manifest.json'):
        os.link(source/name,out/name)
    (out/'sources').mkdir()
    for path in (source/'sources').iterdir():
        if path.is_file():os.link(path,out/'sources'/path.name)
    cases=deepcopy(load(source/'cases.json'));proof=deepcopy(load(source/'provenance.restricted.json'))
    for item in cases:
        case=item['case'];old=list(case['seed_ids']);case['seed_ids']=neighbor_seed(case,old,direction)
        case['expected_seed_count']=len(case['seed_ids'])
        proof[item['qid']]['stress_original_seed_ids']=old
    for name,value in [('cases.json',cases),('provenance.restricted.json',proof)]:
        (out/name).write_bytes(canonical(value))
    manifest=deepcopy(load(source/'manifest.json'))
    manifest['experiment_id']='rsi-v22-neighbor-seed-stress-'+direction.lower()+'-r0'
    manifest['cases']['sha256']=digest(canonical(cases))
    manifest['v22_scope'].update(mode='SOURCE_LAYOUT_STRESS',split='TRAIN_STRESS',
        seed_shift_direction=direction,
        seeds='next_same_source_chunk_else_previous/v1; not retrieval or independent holdout',
        source_fixture_cases_sha256=digest((source/'cases.json').read_bytes()),
        limitation='Same twenty training sources and unchanged Gold; controlled neighbor dependency only.')
    (out/'manifest.json').write_bytes(canonical(manifest))
    freeze=check_split(out,expected_count=20)
    (out/'freeze.json').write_text(json.dumps(freeze,indent=2),encoding='utf-8')
    summary={'questions':len(cases),'source_groups':freeze['source_groups'],
        'original_facts_unchanged':(source/'evaluation.json').read_bytes()==(out/'evaluation.json').read_bytes(),
        'raw_source_copy_bytes':0,'source_files_reused':len(list((out/'sources').iterdir())),
        'new_root_required':True,'independent_holdout':False,'parent_fact_verification':'NOT_YET_RUN'}
    (out/'build-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    return summary


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path);parser.add_argument('--out',required=True,type=Path)
    parser.add_argument('--direction',choices=['NEXT_ELSE_PREVIOUS','PREVIOUS_ELSE_NEXT'],default='NEXT_ELSE_PREVIOUS')
    args=parser.parse_args();print(json.dumps(build(args.source,args.out,args.direction)))

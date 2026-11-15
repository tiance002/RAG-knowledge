"""Freeze auxiliary stress over fresh sealed sources before any evaluation."""
import argparse,json,os,sys
from copy import deepcopy
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from build_rsi_neighbor_stress import neighbor_seed
from rsik.foundation import check_split,canonical,digest
from rsik.locking import restrict_directory


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
    p.add_argument('--direction',choices=('NEXT_ELSE_PREVIOUS','PREVIOUS_ELSE_NEXT'),required=True);a=p.parse_args()
    source,out=a.source,a.out;cases=deepcopy(json.loads((source/'cases.json').read_bytes()));count=len(cases)
    check_split(source,expected_count=count)
    if not 10<=count<=20:raise ValueError('SEALED_SOURCE_COUNT_OUTSIDE_RANGE')
    out.mkdir(parents=True,exist_ok=False);restrict_directory(out)
    proof=deepcopy(json.loads((source/'provenance.restricted.json').read_bytes()));moved=0
    for item in cases:
        case=item['case'];old=list(case['seed_ids']);new=neighbor_seed(case,old,a.direction,True)
        moved+=new!=old;case['seed_ids']=new;case['expected_seed_count']=len(new);proof[item['qid']]['stress_original_seed_ids']=old
    for name in ('evaluation.json','all.json','sources.manifest.json'):os.link(source/name,out/name)
    (out/'sources').mkdir()
    for path in (source/'sources').glob('*.md'):os.link(path,out/'sources'/path.name)
    (out/'cases.json').write_bytes(canonical(cases));(out/'provenance.restricted.json').write_bytes(canonical(proof))
    manifest=json.loads((source/'manifest.json').read_bytes());manifest['experiment_id']='rsi-fresh-sealed-auxiliary-'+a.direction.lower()
    manifest['cases']['sha256']=digest(canonical(cases))
    manifest['v22_scope'].update(mode='SOURCE_LAYOUT_STRESS',split='FRESH_SEALED_AUXILIARY_STRESS',seed_shift_direction=a.direction,
        single_chunk_policy='KEEP_ORIGINAL',source_fixture_cases_sha256=digest((source/'cases.json').read_bytes()),
        seeds='deterministic same-source neighbor transform; singleton unchanged; not real retrieval',
        limitation='Auxiliary hard gates within the SAME sealed source bundle; not additional independent samples.')
    (out/'manifest.json').write_bytes(canonical(manifest));freeze=check_split(out,expected_count=count)
    (out/'freeze.json').write_text(json.dumps(freeze,indent=2),encoding='utf-8')
    summary={'questions':count,'source_groups':count,'changed_seed_questions':moved,'unchanged_questions':count-moved,
        'new_raw_bytes':0,'model_calls':0,'candidate_results_used':False,'statistical_denominator':count,'auxiliary_only':True}
    (out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8');print(json.dumps(summary))


if __name__=='__main__':main()

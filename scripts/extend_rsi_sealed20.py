"""Pre-execution sealed20 power plan; deterministic source order, no outcomes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from build_rsi_v22_foundation import eligible,fetch_sources,build_split,load
from rsik.foundation import check_split,source_separation
from rsik.locking import restrict_directory


def build(qa,foundation,out,host,identity,exclude_sources=None,reuse_extra=None):
    qa,foundation,out=Path(qa),Path(foundation),Path(out)
    pool=load(qa/'portable/reviewed-standalone-draft.json');catalog={}
    for path in sorted(qa.glob('terra_ecs_batch*/source_manifest.json')):
        for source in load(path).get('files',[]):
            if source.get('sha256'):catalog[source['sha256']]=source
    excluded=set(load(exclude_sources)) if exclude_sources else set()
    choices=[pair for pair in eligible(pool,catalog) if pair[1]['sha256'] not in excluded]
    by_hash={s['sha256']:(i,s) for i,s in choices}
    train_hashes={s['source_sha256'] for s in load(foundation/'train/sources.manifest.json')}
    old=load(foundation/'holdout_restricted/sources.manifest.json')
    retained=[by_hash[s['source_sha256']] for s in old]
    used=train_hashes|{s['sha256'] for _,s in retained}
    additions=[pair for pair in choices if pair[1]['sha256'] not in used][:10]
    if len(additions)!=10:raise ValueError('INSUFFICIENT_UNUSED_SOURCES')
    out.mkdir(parents=True,exist_ok=False);restrict_directory(out)
    selected=retained+additions
    prereg={'schema_version':'rsi-sealed20-prereg/v1','purpose':'higher nonzero-cluster power before any independent verification',
        'questions':20,'retained_old':10,'added':10,'selection':'first ten eligible unused source hashes in pinned pool order',
        'pool_sha256':hashlib.sha256((qa/'portable/reviewed-standalone-draft.json').read_bytes()).hexdigest(),
        'selected_source_sha256':[s['sha256'] for _,s in selected],'used_candidate_or_holdout_outcomes':False,
        'estimated_new_raw_bytes':sum(s['source_bytes'] for _,s in additions),'owner':'Codex RSI continuation',
        'retention':'freeze and preserve for first and only sealed verification of a qualified candidate'}
    (out/'prereg.restricted.json').write_text(json.dumps(prereg,indent=2),encoding='utf-8')
    reusable={p.stem:p for split in ('train','holdout_restricted') for p in (foundation/split/'sources').glob('*.md')}
    if reuse_extra:
        reusable.update({p.stem:p for p in (Path(reuse_extra)/'sources').glob('*.md')})
    sources={key:path.read_bytes() for key,path in reusable.items()}
    missing=[pair for pair in additions if pair[1]['sha256'] not in reusable]
    if missing:sources.update(fetch_sources(missing,host,identity))
    summary=build_split(selected,sources,out/'fixture','HOLDOUT20',reusable)
    freeze=check_split(out/'fixture',expected_count=20)
    (out/'fixture/freeze.json').write_text(json.dumps(freeze,indent=2),encoding='utf-8')
    separation=source_separation(foundation/'train',out/'fixture')
    internal=source_separation(out/'fixture',out/'fixture')
    if separation['collisions'] or internal['collisions']!=20:raise ValueError('PRE_EXECUTION_SOURCE_INDEPENDENCE_FAILED')
    public={'questions':20,'source_groups':20,'retained_old_sources':10,'added_sources':10,
        'train_source_collisions':0,'internal_nonself_collisions':0,'selected_from_outcomes':False,
        'new_raw_bytes':sum(s['source_bytes'] for _,s in missing),'reused_raw_files':20-len(missing),
        'holdout_executed':False,'limitations':separation['limitations']}
    (out/'summary.json').write_text(json.dumps(public,indent=2),encoding='utf-8')
    return public


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for key in ('qa','foundation','out','identity'):p.add_argument('--'+key,required=True,type=Path)
    p.add_argument('--exclude-sources',type=Path);p.add_argument('--reuse-extra',type=Path)
    p.add_argument('--host',required=True);a=p.parse_args()
    print(json.dumps(build(a.qa,a.foundation,a.out,a.host,a.identity,a.exclude_sources,a.reuse_extra)))

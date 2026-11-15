"""Freeze only genuinely unused reviewed sources, without looking at outcomes."""
import argparse,hashlib,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'scripts'))
from build_rsi_v22_foundation import eligible,fetch_sources,build_split,load
from rsik.foundation import check_split,source_separation
from rsik.locking import restrict_directory


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('qa','train','used-holdout','exclusions','reuse-root','out','identity','previous-use-audit'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--host',required=True);a=p.parse_args()
    pool=load(a.qa/'portable/reviewed-standalone-draft.json');catalog={}
    for path in sorted(a.qa.glob('terra_ecs_batch*/source_manifest.json')):
        for source in load(path).get('files',[]):
            if source.get('sha256'):catalog[source['sha256']]=source
    used={s['source_sha256'] for f in (a.train,a.used_holdout) for s in load(f/'sources.manifest.json')}
    prior=load(a.previous_use_audit)
    if prior.get('errors'):raise ValueError('PREVIOUS_SOURCE_USE_UNKNOWN')
    used.update(prior['used_source_hashes'])
    excluded=set(load(a.exclusions))
    selected=[pair for pair in eligible(pool,catalog) if pair[1]['sha256'] not in used|excluded][:20]
    if not 10<=len(selected)<=20:raise ValueError('INSUFFICIENT_FRESH_SOURCES')
    a.out.mkdir(parents=True,exist_ok=False);restrict_directory(a.out)
    paths={}
    for path in a.reuse_root.glob('**/sources/*.md'):
        if len(path.stem)==64:paths.setdefault(path.stem,[]).append(path)
    reusable,sources,unreadable={},{},[]
    for _,source in selected:
        key=source['sha256']
        for path in paths.get(key,[]):
            try:data=path.read_bytes()
            except PermissionError:
                unreadable.append(str(path));continue
            if hashlib.sha256(data).hexdigest()!=key:raise ValueError('REUSABLE_SOURCE_HASH_MISMATCH')
            reusable[key]=path;sources[key]=data;break
    missing=[pair for pair in selected if pair[1]['sha256'] not in reusable]
    prereg={'schema':'rsi-fresh-source-sealed/v1','selection':'first eligible unused source hashes in pinned reviewed pool order',
        'pool_sha256':hashlib.sha256((a.qa/'portable/reviewed-standalone-draft.json').read_bytes()).hexdigest(),
        'source_hashes':[s['sha256'] for _,s in selected],'questions':len(selected),'used_holdout_reused':False,
        'selected_from_outcomes':False,'candidate_results_used':False,'owner':'Codex RSI query-aware continuation',
        'previous_use_audit_sha256':hashlib.sha256(a.previous_use_audit.read_bytes()).hexdigest(),
        'unreadable_existing_paths_not_modified':unreadable,
        'resource':{'purpose':'one-use source-independent sealed evaluation after training qualification',
            'estimated_new_raw_bytes':sum(s['source_bytes'] for _,s in missing),'new_database_or_index_copies':0,
            'retain':'frozen Gold, original source version map and sole usable source bytes; no download intermediate files'}}
    (a.out/'prereg.restricted.json').write_text(json.dumps(prereg,indent=2),encoding='utf-8')
    if missing:sources.update(fetch_sources(missing,a.host,a.identity))
    build_split(selected,sources,a.out/'fixture','FRESH_SEALED',reusable)
    freeze=check_split(a.out/'fixture',expected_count=len(selected))
    (a.out/'fixture/freeze.json').write_text(json.dumps(freeze,indent=2),encoding='utf-8')
    train=source_separation(a.train,a.out/'fixture');old=source_separation(a.used_holdout,a.out/'fixture')
    internal=source_separation(a.out/'fixture',a.out/'fixture')
    summary={'questions':len(selected),'source_groups':len(selected),'train_collisions':train['collisions'],
        'used_holdout_collisions':old['collisions'],'internal_nonself_collisions':internal['collisions']-len(selected),
        'new_raw_bytes':sum(s['source_bytes'] for _,s in missing),'reused_sources':len(selected)-len(missing),
        'holdout_executed':False,'model_calls':0,'candidate_results_used':False}
    (a.out/'summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    if any(summary[k] for k in ('train_collisions','used_holdout_collisions','internal_nonself_collisions')):
        raise ValueError('PRE_EXECUTION_SOURCE_SEPARATION_FAILED')
    print(json.dumps(summary))


if __name__=='__main__':main()

"""Same selector inputs, real alternate reviewed questions; no heldout sources.

The alternate choice was frozen by source coverage and core-hash order, never
by a candidate's result. This is a regression probe, not independent holdout.
"""
from copy import deepcopy
import argparse,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.foundation import canonical,digest,check_split


def load(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def build(source,pool_path,choices_path,out):
    source,out=Path(source),Path(out);out.mkdir(parents=True,exist_ok=False)
    pool=load(pool_path);items={i['core_sha256']:i for i in pool['items']}
    choices={i['qid']:i['core_sha256'] for i in load(choices_path)['choices']}
    cases=deepcopy(load(source/'cases.json'));evaluation=deepcopy(load(source/'evaluation.json'));proof=deepcopy(load(source/'provenance.restricted.json'));origins={}
    for item in cases:
        qid=item['qid'];case=item['case'];p=proof[qid]
        p['query_stress_original_seed_ids']=list(case['seed_ids'])
        p['selector_input_sha256']=digest(canonical({k:v for k,v in case.items() if k!='question'}))
        if qid not in choices:continue
        record=items[choices[qid]];content=record['content'];raw=(source/p['source_relative_path']).read_text(encoding='utf-8')
        if record['content_review']['decision']!='PASS':raise ValueError('ALTERNATE_NOT_REVIEWED')
        case['question']=content['question'];facts=[];predicates=[]
        for n,e in enumerate(content['evidence'],1):
            loc=e['original_location'];a,b=loc['char_start_0based'],loc['char_end_exclusive_0based']
            if raw[a:b]!=e['quote'] or e['source_sha256']!=p['source_sha256']:raise ValueError('ALTERNATE_SOURCE_MISMATCH')
            supports=[cid for cid,s in p['member_spans'].items() if s['start']<=a<b<=s['end']]
            if not supports:raise ValueError('UNSPLIT_ALTERNATE_EVIDENCE_REQUIRED')
            facts.append({'kind':'relation','file_id':p['file_id'],'text':e['quote']})
            predicates.append({'fact_id':'fact-'+str(n),'kind':'relation','file_id':p['file_id'],'kb_id':p['kb_id'],
                'source_sha256':p['source_sha256'],'text':e['quote'],'start':a,'end':b,'supporting_chunk_ids':supports,
                'normalization':'identity-unicode-codepoints-v1','rule':'exact_clause_in_source_bound_member'})
        evaluation[qid]={'status':'PAIRED','facts':facts,'forbidden':[]};p.update(facts=predicates,origin_qid=content['qid'],origin_core_sha256=record['core_sha256'],prior_review=record['content_review'])
        origins[qid]=record
    for name,value in [('cases.json',cases),('evaluation.json',evaluation),('provenance.restricted.json',proof),('qa_origins.restricted.json',origins)]:
        (out/name).write_bytes(canonical(value))
    for name in ('sources.manifest.json','all.json'):os.link(source/name,out/name)
    (out/'sources').mkdir()
    for path in (source/'sources').iterdir():
        if path.is_file():os.link(path,out/'sources'/path.name)
    manifest=deepcopy(load(source/'manifest.json'));manifest['experiment_id']='rsi-v22-real-alternate-query-stress'
    for name in ('cases','evaluation'):manifest[name]['sha256']=digest((out/(name+'.json')).read_bytes())
    manifest['v22_scope'].update(mode='SOURCE_QUERY_STRESS',split='TRAIN_QUERY_STRESS',
        seeds='unchanged selector inputs; real alternate reviewed queries; no real retrieval claim',
        qa_origins_sha256=digest((out/'qa_origins.restricted.json').read_bytes()),
        source_fixture_cases_sha256=digest((source/'cases.json').read_bytes()))
    (out/'manifest.json').write_bytes(canonical(manifest));freeze=check_split(out,expected_count=20)
    (out/'freeze.json').write_text(json.dumps(freeze,indent=2),encoding='utf-8')
    summary={'questions':20,'alternate_questions':len(origins),'source_groups':20,'selector_inputs_unchanged':True,
             'new_raw_source_bytes':0,'independent_holdout':False,'selected_by_candidate_outcome':False}
    (out/'build-summary.json').write_text(json.dumps(summary,indent=2),encoding='utf-8');return summary


if __name__=='__main__':
    p=argparse.ArgumentParser();
    for k in ('source','pool','choices','out'):p.add_argument('--'+k,required=True,type=Path)
    a=p.parse_args();print(json.dumps(build(a.source,a.pool,a.choices,a.out)))

import ast, hashlib, json, os
from pathlib import Path

BASE = Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
OUT = Path('/opt/amrag/experiments/dynamic-routing-20261101/gold150')
EXCLUDED = 'longbench-zh:dureader:eb1b46866411bfef383d5ab1459a45171460322c71efbb20'
def sha(b): return hashlib.sha256(b).hexdigest()
def canonical(x): return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
def rows(p): return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
def check(p, expected):
    assert sha(p.read_bytes()) == expected, str(p)
def main():
    freeze=BASE/'dataset200/freeze.json'; goldpath=BASE/'dataset200/gold100.jsonl'; officefreeze=BASE/'office50-v1/dataset250/freeze250.json'; officepath=officefreeze.parent/'office50.jsonl'
    check(freeze,'c3a0a79e6dd53b28c8c2456d08d27d0357137b80633d08fec945839d15a205f3')
    check(goldpath,json.loads(freeze.read_bytes())['files']['gold100.jsonl'])
    check(officepath,json.loads(officefreeze.read_bytes())['office50_sha256'])
    source=BASE/'run_ecs_dataset200.py'; tree=ast.parse(source.read_text(encoding='utf-8'))
    defs=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='answer_prompt']; assert len(defs)==1
    env={}; exec(compile(ast.Module(body=defs,type_ignores=[]),str(source),'exec'),env); prompt=env['answer_prompt']
    retrieval={(r['qid'],r['arm']):r for r in rows(BASE/'runtime200-r4/retrieval-results.jsonl')}
    offresults={(r['qid'],r['arm']):r for r in rows(BASE/'office50-v1/evaluation/results.jsonl')}
    retries={(r['qid'],r['arm']):r for r in rows(BASE/'office50-v1/retry429-r1/results.jsonl')}
    cases=[]
    for g in rows(goldpath)+rows(officepath):
        qid=g['qid']; isoffice=qid.startswith('office50:')
        if isoffice:
            result=offresults[(qid,'before')]; ctx=result.get('context'); path=BASE/'office50-v1/evaluation/results.jsonl'; recovered=False
            if not ctx:
                ctx=offresults[(qid,'after')].get('context'); assert ctx
                assert sha(ctx.encode())==retries[(qid,'before')]['context_sha256']
                assert result['retrieved_top10']==offresults[(qid,'after')]['retrieved_top10']
                recovered=True
            refs=result['retrieved_top10']; expected=sha(ctx.encode())
        else:
            result=retrieval[(qid,'before')]; path=BASE/'runtime200-r4/contexts'/f"{g['ordinal']}-before.txt"; ctx=path.read_text(encoding='utf-8'); expected=result['context_sha256']; assert sha(ctx.encode())==expected
            assert sha(prompt(ctx).encode())==result['prompt_sha256']
            refs=result['sources_at_10']; recovered=False
        messages=[{'role':'system','content':prompt(ctx)},{'role':'user','content':g['question']}]
        nbytes=len(canonical(messages)); assert nbytes+4096+1024<=256000
        cases.append({'qid':qid,'question':g['question'],'messages':messages,'messages_sha256':sha(canonical(messages)),'gold':g,'context_sha256':expected,'context_source_path':str(path),'context_recovered_cross_arm_hash_verified':recovered,'retrieved_top10':refs,'retrieved_top10_sha256':sha(canonical(refs)),'excluded_from_core_accuracy':qid==EXCLUDED,'expensive_eligible':qid!=EXCLUDED,'conservative_input_bytes_plus_framing':nbytes+1024,'source_group':g.get('source_sha256') or str(g.get('kb_scope'))})
    assert len(cases)==150 and len({c['qid'] for c in cases})==150 and sum(c['excluded_from_core_accuracy'] for c in cases)==1
    manifest={'dataset_version':'dataset200-gold100+office50-v1-before','authorization_id':'dynamic-routing-150-owner-20261101','mode':'frozen-context provider replay','config':{'max_cheap_calls':150,'max_expensive_calls':150,'expensive_only_reviewed_fail':True,'max_tokens':4096,'temperature':0.7,'context_limit':256000,'cheap_model':'XingChenAGI/Xing4.0-29B','expensive_model':'zai-org/GLM-5.3-Flash'},'source_hashes':{str(p):sha(p.read_bytes()) for p in [freeze,goldpath,officefreeze,officepath,source,BASE/'runtime200-r4/retrieval-results.jsonl',BASE/'office50-v1/evaluation/results.jsonl',BASE/'office50-v1/retry429-r1/results.jsonl']},'cases':cases}
    os.umask(0o077); OUT.mkdir(parents=True,exist_ok=True); p=OUT/'manifest.json'
    data=canonical(manifest)+b'\n'
    with p.open('xb') as f:f.write(data)
    print(json.dumps({'status':'PASS','cases':len(cases),'excluded':1,'eligible_max':149,'recovered_contexts':sum(c['context_recovered_cross_arm_hash_verified'] for c in cases),'input_bytes_plus_framing_total':sum(c['conservative_input_bytes_plus_framing'] for c in cases),'max_input_bytes_plus_framing':max(c['conservative_input_bytes_plus_framing'] for c in cases),'output_token_cap_each':4096,'manifest':str(p),'manifest_sha256':sha(data),'calls':0}))
if __name__=='__main__':main()

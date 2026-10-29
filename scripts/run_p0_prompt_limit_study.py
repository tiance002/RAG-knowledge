"""Bounded isolated 2x3 prompt/output diagnostic; no retriever or production mutation."""
import hashlib,json,os,pathlib,sqlite3,sys,time
import httpx
from openai import OpenAI
B=pathlib.Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
R=B/'prompt-limit-factorial-20261028-v1'
sha=lambda b:hashlib.sha256(b).hexdigest()
def dump(p,obj):
 p.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf8');os.chmod(p,0o600)
def append(p,obj):
 with p.open('a',encoding='utf8') as f:
  f.write(json.dumps(obj,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
 os.chmod(p,0o600)
def main():
 os.umask(0o077);R.mkdir(mode=0o700,exist_ok=True)
 sys.path.insert(0,str(B));from run_ecs_dataset200 import answer_prompt
 goldfile=B/'dataset200/gold100.jsonl'; freeze=B/'dataset200/freeze.json';fr=json.loads(freeze.read_bytes())
 for name,digest in fr['files'].items():assert sha((B/'dataset200'/name).read_bytes())==digest
 gold={x['ordinal']:x for x in map(json.loads,goldfile.read_text(encoding='utf8').splitlines())}
 rows=[json.loads(x) for sub in ('runtime200-r4','answers200-continuation-r1','answers200-fast-r2') for x in (B/sub/'results.jsonl').read_text(encoding='utf8').splitlines()]
 cases=[]
 for i in (63,83,137,153):
  g=gold[i]; old=next(x for x in rows if x['qid']==g['qid'] and x['arm']=='before');c=(B/'runtime200-r4/contexts'/f'{i}-before.txt').read_text(encoding='utf8');assert sha(c.encode())==old['context_sha256']
  cases.append({'gold':g,'context':c,'context_sha256':sha(c.encode()),'baseline_system_prompt':answer_prompt(c),'historical_finish_reason':old.get('finish_reason'),'historical_empty':not bool((old.get('answer') or '').strip())})
 con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);con.row_factory=sqlite3.Row
 mid=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes())['models']['llm'];m=dict(con.execute('select * from custom_models where id=?',(mid,)).fetchone());con.close()
 historical=json.loads((B/'runtime200-r4/state.json').read_bytes())['models']['llm'];assert historical=={'id':m['id'],'model':m['model_name'],'endpoint_sha256':sha(m['base_url'].encode())}
 add='\n请简洁回答，只包含直接回答问题所需的核心事实；不要省略必要的数值、单位或条件。'
 manifest={'case_ids':[x['gold']['qid'] for x in cases],'ordinals':[63,83,137,153],'dataset_freeze_sha256':sha(freeze.read_bytes()),'gold_sha256':sha(goldfile.read_bytes()),'source_runner_sha256':sha((B/'run_ecs_dataset200.py').read_bytes()),'model':historical,'temperature':0.7,'stream':False,'max_tokens':[2000,4096,8192],'prompt_variants':{'baseline':'unchanged answer_prompt(context)','concise_addendum':add},'cases':[{'qid':x['gold']['qid'],'context_sha256':x['context_sha256'],'baseline_prompt_sha256':sha(x['baseline_system_prompt'].encode()),'gold_sha256':sha(json.dumps(x['gold'],ensure_ascii=False,sort_keys=True).encode()),'historical_finish_reason':x['historical_finish_reason'],'historical_empty':x['historical_empty']} for x in cases],'max_calls':24,'retries':0,'stop_on_any_error':True,'tokenizer':'provider unspecified; unknown','sampling':'temperature=0.7; all other provider defaults identical','scope':'diagnostic, one run per cell'}
 data=json.dumps(cases,ensure_ascii=False,sort_keys=True).encode();manifest['frozen_inputs_sha256']=sha(data)
 if '--prepare' in sys.argv:
  if (R/'manifest.json').exists():raise SystemExit('manifest exists; refuse overwrite')
  (R/'inputs.json').write_bytes(data);os.chmod(R/'inputs.json',0o600);dump(R/'manifest.json',manifest)
  print(json.dumps({'status':'PREPARED_ZERO_CALLS','manifest_sha256':sha((R/'manifest.json').read_bytes()),'calls':24,'cases':manifest['cases']},ensure_ascii=False));return
 assert '--execute' in sys.argv
 assert json.loads((R/'manifest.json').read_bytes())==manifest and sha((R/'inputs.json').read_bytes())==manifest['frozen_inputs_sha256']
 if (R/'requests.jsonl').exists():raise SystemExit('journal exists; no automatic replay')
 with OpenAI(base_url=m['base_url'],api_key=m['api_key'],max_retries=0,timeout=180,http_client=httpx.Client(trust_env=False)) as api:
  for c in cases:
   for cap in (2000,4096,8192):
    for variant in ('baseline','concise'):
     qid=c['gold']['qid'];prompt=c['baseline_system_prompt']+(add if variant=='concise' else '')
     req={'qid':qid,'ordinal':c['gold']['ordinal'],'variant':variant,'max_tokens':cap,'prompt_sha256':sha(prompt.encode()),'context_sha256':c['context_sha256'],'status':'RESERVED','time':time.time()};append(R/'requests.jsonl',req)
     try:
      response=api.chat.completions.create(model=m['model_name'],messages=[{'role':'system','content':prompt},{'role':'user','content':c['gold']['question']}],temperature=.7,max_tokens=cap,stream=False)
      raw=response.model_dump(mode='json');dump(R/f"response-{c['gold']['ordinal']}-{variant}-{cap}.json",raw)
      ch=response.choices[0];res={**req,'status':'COMPLETED','finish_reason':ch.finish_reason,'empty':not bool((ch.message.content or '').strip()),'usage':raw.get('usage'),'response_model':response.model,'response_sha256':sha((R/f"response-{c['gold']['ordinal']}-{variant}-{cap}.json").read_bytes())};append(R/'results.jsonl',res);append(R/'requests.jsonl',{**req,'status':'HTTP_SUCCESS'});print(json.dumps(res,ensure_ascii=False),flush=True)
     except Exception as e:
      out={**req,'status':'UNKNOWN_OR_FAILED','error_type':type(e).__name__};append(R/'requests.jsonl',out);dump(R/'completion.json',{'status':'STOPPED','last':out});print(json.dumps(out),flush=True);return
     time.sleep(5)
 dump(R/'completion.json',{'status':'COMPLETED','calls':24})
if __name__=='__main__':main()


\n
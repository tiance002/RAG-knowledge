"""Continue missing paired answers on ECS, retaining the prior request journal."""
import hashlib,json,os,pathlib,sqlite3,sys,time
from openai import OpenAI,RateLimitError
import httpx

base=pathlib.Path(sys.argv[1]).resolve();prior=base/'runtime200-r4';out=base/'answers200-continuation-r1'
out.mkdir(mode=0o700,exist_ok=True)
sys.path.insert(0,str(base));from run_ecs_dataset200 import answer_prompt
from ecs_benchmark200_protocol import template_sha
def sha(value):return hashlib.sha256(value).hexdigest()
def write(path,value):
 tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(path)
def append(name,value):
 with (out/name).open('a',encoding='utf-8') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
freeze=json.loads((base/'dataset200/freeze.json').read_bytes())
for name,value in freeze['files'].items():assert sha((base/'dataset200'/name).read_bytes())==value
assert template_sha(str(base/'run_ecs_dataset200.py'))=='7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94'
cases=[json.loads(l) for l in (base/'dataset200/gold100.jsonl').open(encoding='utf-8')]
old=[json.loads(l) for l in (prior/'results.jsonl').open(encoding='utf-8')];assert len(old)==40
completed={(x['qid'],x['arm']) for x in old};assert len(completed)==40
retrieval={(x['qid'],x['arm']):x for x in (json.loads(l) for l in (prior/'retrieval-results.jsonl').open(encoding='utf-8'))}
plan=[(c,a) for c in cases for a in ('before','after') if (c['qid'],a) not in completed]
assert len(plan)==160
prior_state=json.loads((prior/'state.json').read_bytes());assert prior_state['status']=='UNKNOWN_STOP'
con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);con.row_factory=sqlite3.Row
model_id=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes())['models']['llm']
model=dict(con.execute('select * from custom_models where id=?',(model_id,)).fetchone());con.close()
assert prior_state['models']['llm']=={'id':model['id'],'model':model['model_name'],'endpoint_sha256':sha(model['base_url'].encode())}
plan_digest=sha(json.dumps([(c['qid'],a) for c,a in plan]).encode())
write(out/'manifest.json',{'prior_runtime':str(prior),'prior_results_sha256':sha((prior/'results.jsonl').read_bytes()),
 'plan_sha256':plan_digest,'prior_successes':40,'planned_answers':160,'model':model['model_name'],
 'temperature':.7,'max_tokens':2000,'prompt_template_sha256':template_sha(str(base/'run_ecs_dataset200.py')),
 'retry_policy':'429 rejected requests: at most 3 attempts with 60s backoff; other failures recorded as skipped; no automatic replay of unknown responses'})
if (out/'results.jsonl').exists() or (out/'requests.jsonl').exists():raise RuntimeError('Refuse to overwrite continuation evidence')
if '--preflight' in sys.argv:
 print(json.dumps({'status':'PREPARED_ZERO_CALLS','prior_completed':40,'planned':len(plan),'model':model['model_name']}));sys.exit(0)
success=0;skipped=0
for i,(case,arm) in enumerate(plan,1):
 row=retrieval[(case['qid'],arm)];context=(prior/'contexts'/f'{case["ordinal"]}-{arm}.txt').read_text(encoding='utf-8')
 assert sha(context.encode())==row['context_sha256']
 answer=None;reason='unknown'
 for attempt in range(1,4):
  req={'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'RESERVED','context_sha256':row['context_sha256'],'at':time.time()}
  append('requests.jsonl',req)
  try:
   with OpenAI(base_url=model['base_url'],api_key=model['api_key'],max_retries=0,timeout=120,http_client=httpx.Client(trust_env=False)) as api:
    result=api.chat.completions.create(model=model['model_name'],messages=[{'role':'system','content':answer_prompt(context)},
       {'role':'user','content':case['question']}],temperature=.7,max_tokens=2000,stream=False)
   answer={**row,'question':case['question'],'answer':result.choices[0].message.content,
      'finish_reason':result.choices[0].finish_reason,'usage':result.usage.model_dump() if result.usage else None,
      'response_model':result.model,'status':'COMPLETED'}
   append('requests.jsonl',{'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'HTTP_SUCCESS','at':time.time()});break
  except RateLimitError as exc:
   reason='rate_limited';append('requests.jsonl',{'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'REJECTED_429','at':time.time()})
   if attempt<3:time.sleep(60)
  except Exception as exc:
   reason=type(exc).__name__;append('requests.jsonl',{'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'UNKNOWN_OR_FAILED','error_type':reason,'at':time.time()});break
 if answer is None:answer={'qid':case['qid'],'ordinal':case['ordinal'],'arm':arm,'status':'SKIPPED','reason':reason};skipped+=1
 else:success+=1
 append('results.jsonl',answer)
 write(out/'status.json',{'phase':'GENERATION','planned':160,'visited':i,'new_completed':success,'new_skipped':skipped,'prior_completed':40,'at':time.time()})
 if i<len(plan):time.sleep(15)
write(out/'status.json',{'phase':'COMPLETED','planned':160,'visited':len(plan),'new_completed':success,'new_skipped':skipped,'prior_completed':40,'at':time.time()})

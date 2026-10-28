"""Two paired diagnostic responses at 4096; keep original 2000-token results intact."""
import hashlib,json,pathlib,sqlite3,sys,time
import httpx
from openai import OpenAI,RateLimitError

base=pathlib.Path(sys.argv[1]);root=base/'answer-cap4096-v1';prior=base/'runtime200-r4'
sys.path.insert(0,str(base));sys.path.insert(0,str(root/'backend'))
from run_ecs_dataset200 import answer_prompt
from ecs_benchmark200_protocol import template_sha
from app.core.config import get_settings
sha=lambda value:hashlib.sha256(value).hexdigest()
assert get_settings().LLM_ANSWER_MAX_TOKENS==4096
assert template_sha(str(base/'run_ecs_dataset200.py'))=='7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94'
old=[json.loads(l) for l in (prior/'results.jsonl').open(encoding='utf-8')]
groups={}
for r in old:
 if not (r.get('answer') or '').strip() and r.get('finish_reason')=='length':groups.setdefault(r['qid'],{})[r['arm']]=r
qid=next(q for q,v in sorted(groups.items()) if set(v)=={'before','after'})
cases={x['qid']:x for x in (json.loads(l) for l in (base/'dataset200/gold100.jsonl').open(encoding='utf-8'))}
case=cases[qid];inputs=[]
for arm in ('before','after'):
 original=groups[qid][arm];context=(prior/'contexts'/f'{case["ordinal"]}-{arm}.txt').read_text(encoding='utf-8')
 assert sha(context.encode())==original['context_sha256']
 inputs.append((arm,context,original))
conn=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);conn.row_factory=sqlite3.Row
model_id=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes())['models']['llm']
model=dict(conn.execute('select * from custom_models where id=?',(model_id,)).fetchone());conn.close()
state=json.loads((prior/'state.json').read_bytes());assert state['models']['llm']=={'id':model['id'],'model':model['model_name'],'endpoint_sha256':sha(model['base_url'].encode())}
plan={'scope':'paired diagnostic, not full A/B evaluation','qid':qid,'model':model['model_name'],'temperature':.7,'max_tokens':4096,
 'prompt_sha256':template_sha(str(base/'run_ecs_dataset200.py')),'arms':[{'arm':a,'context_sha256':v['context_sha256']} for a,_,v in inputs]}
p=root/'smoke-plan.json'
if p.exists():assert json.loads(p.read_bytes())==plan
else:p.write_text(json.dumps(plan,ensure_ascii=False,indent=2))
if (root/'smoke-requests.jsonl').exists():raise RuntimeError('No automatic replay of existing requests')
if '--preflight' in sys.argv:print(json.dumps({'status':'PREPARED_ZERO_CALLS','arms':2,'max_tokens':4096}));sys.exit(0)
def append(name,value):
 with (root/name).open('a',encoding='utf-8') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
for arm,context,original in inputs:
 for attempt in range(1,3):
  append('smoke-requests.jsonl',{'qid':qid,'arm':arm,'attempt':attempt,'status':'RESERVED','at':time.time()})
  try:
   with OpenAI(base_url=model['base_url'],api_key=model['api_key'],max_retries=0,timeout=120,http_client=httpx.Client(trust_env=False)) as api:
    response=api.chat.completions.create(model=model['model_name'],messages=[{'role':'system','content':answer_prompt(context)},
      {'role':'user','content':case['question']}],temperature=.7,max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS,stream=False)
   msg=response.choices[0].message;extra=msg.model_dump();reasoning=extra.get('reasoning_content') or extra.get('reasoning') or ''
   result={'qid':qid,'arm':arm,'status':'COMPLETED','answer':msg.content,'answer_chars':len(msg.content or ''),
     'reasoning_chars':len(reasoning) if isinstance(reasoning,str) else None,'finish_reason':response.choices[0].finish_reason,
     'usage':response.usage.model_dump() if response.usage else None,'context_sha256':original['context_sha256'],'max_tokens':4096}
   append('smoke-results.jsonl',result);append('smoke-requests.jsonl',{'qid':qid,'arm':arm,'attempt':attempt,'status':'HTTP_SUCCESS','at':time.time()});break
  except RateLimitError:
   append('smoke-requests.jsonl',{'qid':qid,'arm':arm,'attempt':attempt,'status':'REJECTED_429','at':time.time()})
   if attempt<2:time.sleep(60)
   else:append('smoke-results.jsonl',{'qid':qid,'arm':arm,'status':'SKIPPED','reason':'rate_limited'})
  except Exception as e:
   append('smoke-requests.jsonl',{'qid':qid,'arm':arm,'attempt':attempt,'status':'UNKNOWN_OR_FAILED','error_type':type(e).__name__,'at':time.time()})
   append('smoke-results.jsonl',{'qid':qid,'arm':arm,'status':'SKIPPED','reason':type(e).__name__});break
 if arm=='before':time.sleep(5)
print(json.dumps({'status':'FINISHED','arms':2}))

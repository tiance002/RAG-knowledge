"""Continue only unvisited 200-set answers after a clean, journaled stop."""
import hashlib,json,pathlib,sqlite3,sys,time
import httpx
from openai import OpenAI,RateLimitError

base=pathlib.Path(sys.argv[1]).resolve();prior=base/'runtime200-r4';first=base/'answers200-continuation-r1';out=base/'answers200-fast-r2'
out.mkdir(mode=0o700,exist_ok=True);sys.path.insert(0,str(base))
from run_ecs_dataset200 import answer_prompt
from ecs_benchmark200_protocol import template_sha
sha=lambda value:hashlib.sha256(value).hexdigest()
def write(name,value):
 path=out/name;tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(path)
def append(name,value):
 with (out/name).open('a',encoding='utf-8') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
frozen=json.loads((base/'dataset200/freeze.json').read_bytes())
for name,value in frozen['files'].items():assert sha((base/'dataset200'/name).read_bytes())==value
assert template_sha(str(base/'run_ecs_dataset200.py'))=='7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94'
assert (first/'completion.json').exists(),'previous runner has not stopped'
cases=[json.loads(l) for l in (base/'dataset200/gold100.jsonl').open()]
original=[json.loads(l) for l in (prior/'results.jsonl').open()];older=[json.loads(l) for l in (first/'results.jsonl').open()]
visited={(x['qid'],x['arm']) for x in original+older};assert len(visited)==len(original)+len(older)
retrieval={(x['qid'],x['arm']):x for x in (json.loads(l) for l in (prior/'retrieval-results.jsonl').open())}
plan=[(c,a) for c in cases for a in ('before','after') if (c['qid'],a) not in visited]
con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);con.row_factory=sqlite3.Row
model_id=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes())['models']['llm']
m=dict(con.execute('select * from custom_models where id=?',(model_id,)).fetchone());con.close()
oldstate=json.loads((prior/'state.json').read_bytes())
assert oldstate['models']['llm']=={'id':m['id'],'model':m['model_name'],'endpoint_sha256':sha(m['base_url'].encode())}
if (out/'results.jsonl').exists() or (out/'requests.jsonl').exists():raise RuntimeError('Refuse to overwrite continuation')
write('manifest.json',{'status':'FROZEN','old_results_sha256':sha((first/'results.jsonl').read_bytes()),
 'previous_original_count':len(original),'previous_continuation_count':len(older),'planned':len(plan),
 'questions':[{'qid':c['qid'],'arm':a} for c,a in plan],'model':m['model_name'],'minimum_wait_seconds':2,
 'prompt_template_sha256':template_sha(str(base/'run_ecs_dataset200.py'))})
if '--preflight' in sys.argv:print(json.dumps({'status':'PREPARED_ZERO_CALLS','planned':len(plan)}));sys.exit(0)
success=skip=0
for n,(case,arm) in enumerate(plan,1):
 r=retrieval[(case['qid'],arm)];context=(prior/'contexts'/f'{case["ordinal"]}-{arm}.txt').read_text(encoding='utf-8')
 assert sha(context.encode())==r['context_sha256'];answer=None;reason='unknown'
 for attempt in range(1,4):
  append('requests.jsonl',{'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'RESERVED','at':time.time()})
  try:
   with OpenAI(base_url=m['base_url'],api_key=m['api_key'],max_retries=0,timeout=120,http_client=httpx.Client(trust_env=False)) as api:
    response=api.chat.completions.create(model=m['model_name'],messages=[{'role':'system','content':answer_prompt(context)},
        {'role':'user','content':case['question']}],temperature=.7,max_tokens=2000,stream=False)
   answer={**r,'question':case['question'],'answer':response.choices[0].message.content,
      'finish_reason':response.choices[0].finish_reason,'usage':response.usage.model_dump() if response.usage else None,'status':'COMPLETED'}
   append('requests.jsonl',{'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'HTTP_SUCCESS','at':time.time()});break
  except RateLimitError:
   reason='rate_limited';append('requests.jsonl',{'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'REJECTED_429','at':time.time()})
   if attempt<3:time.sleep(60)
  except Exception as e:
   reason=type(e).__name__;append('requests.jsonl',{'qid':case['qid'],'arm':arm,'attempt':attempt,'status':'UNKNOWN_OR_FAILED','error_type':reason,'at':time.time()});break
 if answer is None:answer={'qid':case['qid'],'ordinal':case['ordinal'],'arm':arm,'status':'SKIPPED','reason':reason};skip+=1
 else:success+=1
 append('results.jsonl',answer);write('status.json',{'phase':'GENERATION','planned':len(plan),'visited':n,'completed':success,'skipped':skip,'at':time.time()})
 if n<len(plan):time.sleep(2)
write('status.json',{'phase':'COMPLETED','planned':len(plan),'visited':len(plan),'completed':success,'skipped':skip,'at':time.time()})

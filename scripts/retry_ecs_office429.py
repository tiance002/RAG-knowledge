"""Retry Office answers whose previous provider attempts were all rejected with 429."""
import asyncio,hashlib,json,os,pathlib,sqlite3,sys,time
import httpx
from openai import OpenAI,RateLimitError

base=pathlib.Path(sys.argv[1]).resolve();root=base/'office50-v1';evaluation=root/'evaluation';out=root/'retry429-r1';out.mkdir(mode=0o700,exist_ok=True)
prior=base/'runtime200-r4';sys.path.insert(0,str(prior/'source/backend'));sys.path.insert(0,str(base))
os.environ.update(ENV='test',DATABASE_URL='sqlite:///'+str(evaluation/'isolated.db'),CHROMA_DB_DIR=str(evaluation/'chroma'),
 IMAGE_DIR=str(evaluation/'images'),UPLOAD_DIR=str(evaluation/'uploads'),ANONYMIZED_TELEMETRY='False')
os.chdir(evaluation)
from sqlmodel import Session
from app.core.database import engine
from app.models import CustomModel
from app.services.retrieval import RetrievalService
from app.services.rerank import RerankService,batch_rerank,expand_context
from ecs_benchmark200_helpers import VectorCache
from ecs_benchmark200_protocol import template_sha
from run_ecs_dataset200 import answer_prompt

sha=lambda value:hashlib.sha256(value).hexdigest()
def append(name,value):
 with (out/name).open('a',encoding='utf-8') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
def write(name,value):
 p=out/name;tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2));tmp.replace(p)
frozen=json.loads((root/'dataset250/freeze250.json').read_bytes());raw=(root/'dataset250/office50.jsonl').read_bytes()
assert sha(raw)==frozen['office50_sha256']
assert template_sha(str(base/'run_ecs_dataset200.py'))=='7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94'
cases={x['qid']:x for x in (json.loads(l) for l in raw.splitlines())}
results=[json.loads(l) for l in (evaluation/'results.jsonl').open(encoding='utf-8')]
failed=[x for x in results if x.get('status')=='SKIPPED' and x.get('reason')=='generation_failed']
errors=[json.loads(l) for l in (evaluation/'failures.jsonl').open(encoding='utf-8')]
for r in failed:
 label='generation:'+r['qid']+':'+r['arm'];a=[x for x in errors if x.get('label')==label]
 assert a and all(x.get('error_type')=='RateLimitError' for x in a)
assert len(failed)==14 and len({(r['qid'],r['arm']) for r in failed})==14
con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);con.row_factory=sqlite3.Row
ids=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes())['models']
models={role:CustomModel(**dict(con.execute('select * from custom_models where id=?',(ids[role],)).fetchone())) for role in ('embedding','rerank','llm','vlm')};con.close()
prior_state=json.loads((prior/'state.json').read_bytes())
for role in ('embedding','rerank','llm'):
 m=models[role];assert prior_state['models'][role]=={'id':m.id,'model':m.model_name,'endpoint_sha256':sha(m.base_url.encode())}
cache=VectorCache(evaluation/'vectors.db')
assert all(cache.has(cases[x['qid']]['question']) for x in failed)
if (out/'results.jsonl').exists() or (out/'requests.jsonl').exists():raise RuntimeError('Refuse to overwrite retry evidence')
write('manifest.json',{'status':'FROZEN','reason':'previous 429 rejection','planned':len(failed),
 'prior_results_sha256':sha((evaluation/'results.jsonl').read_bytes()),'office50_sha256':frozen['office50_sha256'],
 'qid_and_arm':[{'qid':x['qid'],'arm':x['arm']} for x in failed],
 'context_policy':'original failed generation did not retain context; reconstruct retrieval and rerank using the same frozen index and settings',
 'model':models['llm'].model_name,'temperature':.7,'max_tokens':2000,'minimum_wait_seconds':2})
if '--preflight' in sys.argv:print(json.dumps({'status':'PREPARED_ZERO_CALLS','planned':len(failed)}));sys.exit(0)
failures=[];sync_send=httpx.Client.send;async_send=httpx.AsyncClient.send
def reserve(request):
 body=json.loads(request.content);name=body.get('model');role=next((k for k,m in models.items() if m.model_name==name),None)
 if role not in ('rerank','llm') or not str(request.url).startswith(models[role].base_url.rstrip('/')+'/'):raise RuntimeError('Unexpected model or endpoint')
 if role=='llm' and (body.get('max_tokens')!=2000 or body.get('temperature')!=.7):raise RuntimeError('Generation parameter drift')
 rec={'role':role,'request_sha256':sha(request.content),'status':'RESERVED','at':time.time()};append('requests.jsonl',rec);return rec
def finish(rec,response=None):
 ok=response is not None and response.status_code<400
 if not ok:failures.append(rec['request_sha256'])
 append('requests.jsonl',{'role':rec['role'],'request_sha256':rec['request_sha256'],'status':'HTTP_SUCCESS' if ok else 'UNKNOWN_OR_FAILED',
  'http_status':response.status_code if response is not None else None,'at':time.time()})
def send(client,request,*args,**kwargs):
 rec=reserve(request)
 try:r=sync_send(client,request,*args,**kwargs)
 except BaseException:finish(rec);raise
 finish(rec,r);return r
async def asend(client,request,*args,**kwargs):
 rec=reserve(request)
 try:r=await async_send(client,request,*args,**kwargs)
 except BaseException:finish(rec);raise
 finish(rec,r);return r
httpx.Client.send=send;httpx.AsyncClient.send=asend
class ModelSession:
 def __init__(self,session):self.session=session
 def get(self,kind,key):
  if kind is CustomModel:return next((m for m in models.values() if m.id==key),None)
  return self.session.get(kind,key)
 def __getattr__(self,key):return getattr(self.session,key)
def build_context(case):
 service=RetrievalService();vector=cache.get(case['question'])
 async def embedded(session,kb_id,text):return vector
 service._embed_query_for_kb=embedded
 with Session(engine) as raw_session:
  session=ModelSession(raw_session);candidates,metrics=asyncio.run(service.search(session,case['question'],['office50-v1'],
    search_mode='hybrid',top_k=50,score_threshold=0,vector_weight=.7,bm25_weight=.3,use_cache=False,log_retrieval=False))
  before=len(failures);ranked=batch_rerank(case['question'],candidates,RerankService(session),models['rerank'],top_k=20,score_threshold=0)
  if not ranked or len(failures)>before:raise RuntimeError('rerank failed or empty')
  expanded=expand_context(ranked,session,window_size=1)
  return '\n\n'.join(f'[{i}] 来源: {r.get("file_name","未知")}\n{r.get("content","")}' for i,r in enumerate(expanded,1))
def do_answer(case,context):
 with OpenAI(base_url=models['llm'].base_url,api_key=models['llm'].api_key,max_retries=0,timeout=120,http_client=httpx.Client(trust_env=False)) as api:
  response=api.chat.completions.create(model=models['llm'].model_name,messages=[{'role':'system','content':answer_prompt(context)},
   {'role':'user','content':case['question']}],temperature=.7,max_tokens=2000,stream=False)
 return {'answer':response.choices[0].message.content,'finish_reason':response.choices[0].finish_reason,
  'usage':response.usage.model_dump() if response.usage else None}
done=skipped=0
for i,old in enumerate(failed,1):
 case=cases[old['qid']];context=None;answer=None;reason='unknown'
 for attempt in range(1,4):
  try:context=build_context(case);break
  except Exception as e:
   reason=type(e).__name__;append('failures.jsonl',{'qid':old['qid'],'arm':old['arm'],'stage':'retrieval','attempt':attempt,'error_type':reason})
   if attempt<3:time.sleep(10*attempt)
 if context is not None:
  for attempt in range(1,4):
   try:answer=do_answer(case,context);break
   except RateLimitError:
    reason='rate_limited';append('failures.jsonl',{'qid':old['qid'],'arm':old['arm'],'stage':'generation','attempt':attempt,'error_type':'RateLimitError'})
    if attempt<3:time.sleep(60)
   except Exception as e:
    reason=type(e).__name__;append('failures.jsonl',{'qid':old['qid'],'arm':old['arm'],'stage':'generation','attempt':attempt,'error_type':reason});break
 if answer is None:row={'qid':old['qid'],'arm':old['arm'],'status':'SKIPPED','reason':reason};skipped+=1
 else:
  row={'qid':old['qid'],'arm':old['arm'],'status':'COMPLETED','context_sha256':sha(context.encode()),**answer};done+=1
 append('results.jsonl',row);write('status.json',{'phase':'GENERATION','planned':len(failed),'visited':i,'completed':done,'skipped':skipped,'at':time.time()})
 if i<len(failed):time.sleep(2)
write('status.json',{'phase':'COMPLETED','planned':len(failed),'visited':len(failed),'completed':done,'skipped':skipped,'at':time.time()})

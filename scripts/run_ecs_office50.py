"""Independent ECS continuation for frozen Office controls; no production writes."""
import asyncio,collections,hashlib,json,multiprocessing,os,pathlib,sqlite3,sys,time,traceback
from dataclasses import asdict

base=pathlib.Path(sys.argv[1]).resolve();root=base/'office50-v1';dataset=root/'dataset250';runtime=root/'evaluation'
runtime.mkdir(mode=0o700,exist_ok=True)
def sha(value):return hashlib.sha256(value).hexdigest()
def save(name,data):
 p=runtime/name;tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8');tmp.replace(p)
def log(name,data):
 with (runtime/name).open('a',encoding='utf-8') as f:f.write(json.dumps(data,ensure_ascii=False)+'\n')
def status(phase,**extra):save('status.json',{'phase':phase,'at':time.time(),**extra})
def retry(fn,label,attempts=3):
 for i in range(1,attempts+1):
  try:return fn()
  except Exception as e:
   log('failures.jsonl',{'label':label,'attempt':i,'error_type':type(e).__name__,'message':str(e)[:300]})
   if i<attempts:time.sleep(i*5)
 return None
freeze=json.loads((dataset/'freeze250.json').read_bytes());raw=(dataset/'office50.jsonl').read_bytes()
assert sha(raw)==freeze['office50_sha256'] and sha((base/'dataset200/freeze.json').read_bytes())==freeze['old200_freeze_sha256']
cases=[json.loads(l) for l in raw.splitlines()];assert len(cases)==50
for s in freeze['sources']:assert sha(pathlib.Path(s['path']).read_bytes())==s['sha256']
prior=base/'runtime200-r4';manifest=json.loads((prior/'run-manifest.json').read_bytes())
assert manifest['prompt_template_sha256']=='7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94'
backend=prior/'source/backend';assert (backend/'app/services/parser.py').exists();sys.path.insert(0,str(backend));sys.path.insert(0,str(base))
os.environ.update(ENV='test',DATABASE_URL='sqlite:///'+str(runtime/'isolated.db'),CHROMA_DB_DIR=str(runtime/'chroma'),
 IMAGE_DIR=str(runtime/'images'),UPLOAD_DIR=str(runtime/'uploads'),ANONYMIZED_TELEMETRY='False')
os.chdir(runtime)
from app.services.parser import FileParser
status('PARSING',sources=len(freeze['sources']))
parsed=[];failed_sources=[]
for item in freeze['sources']:
 path=pathlib.Path(item['path'])
 def parse():
  tmp=runtime/('parse-'+item['sha256']+'.tmp')
  if tmp.exists():tmp.unlink()
  def worker():
   value=FileParser('office50-v1').parse(str(path),chunk_mode='custom',chunk_size=500,chunk_overlap=50,use_semantic=False,embedding_model=None)
   if not value:raise ValueError('empty parser output')
   tmp.write_text(json.dumps([asdict(c) for c in value],ensure_ascii=False),encoding='utf-8')
  process=multiprocessing.Process(target=worker);process.start();process.join(180)
  if process.is_alive():process.terminate();process.join();raise TimeoutError('file parsing exceeded 180 seconds')
  if process.exitcode!=0 or not tmp.exists():raise RuntimeError('parser subprocess failed')
  result=json.loads(tmp.read_bytes());tmp.unlink();return result
 chunks=retry(parse,'parse:'+item['sha256'])
 if chunks is None:failed_sources.append(item['sha256']);continue
 # Frozen selected Office sources contain no embedded images; text/table parsing is identical in both arms.
 if any(c['content_type']=='image' for c in chunks):
  failed_sources.append(item['sha256']);log('failures.jsonl',{'label':'parse:'+item['sha256'],'error_type':'UNEXPECTED_IMAGE'});continue
 parsed.append({**item,'name':path.name,'chunks':chunks})
 (runtime/('parse-'+item['sha256']+'.json')).write_text(json.dumps(chunks,ensure_ascii=False),encoding='utf-8')
status('WAITING_FOR_200',parsed_sources=len(parsed),failed_sources=failed_sources)
while not (prior/'completion.json').exists():time.sleep(20)
prior_completion=json.loads((prior/'completion.json').read_bytes())
status('PREPARING_INDEX',previous_200_exit_code=prior_completion['exit_code'],parsed_sources=len(parsed),failed_sources=failed_sources)
from sqlmodel import SQLModel,Session
from app.core.database import engine
from app.models import CustomModel,KnowledgeBase,FileDocument,DocumentChunk,ContentType
from app.services.embedding import EmbeddingService
from app.services.vector_store import VectorStoreService
from app.services.retrieval import RetrievalService
from app.services.rerank import RerankService,batch_rerank,expand_context
from ecs_benchmark200_helpers import bulk_bm25,VectorCache
from run_ecs_dataset200 import answer_prompt
from openai import OpenAI
import httpx
db=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);db.row_factory=sqlite3.Row
ids=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes())['models']
models={k:CustomModel(**dict(db.execute('select * from custom_models where id=?',(ids[k],)).fetchone())) for k in ('embedding','rerank','llm','vlm')};db.close()
prior_state=json.loads((prior/'state.json').read_bytes())
for role in ('embedding','rerank','llm'):
 frozen=prior_state['models'][role];m=models[role]
 assert frozen=={'id':m.id,'model':m.model_name,'endpoint_sha256':sha(m.base_url.encode())}

# Every remote request gets its own durable record; retries and skipped cases remain visible.
sync_send=httpx.Client.send;async_send=httpx.AsyncClient.send;provider_failures=[]
def reserve(request):
 body=json.loads(request.content);name=body.get('model');role=next((k for k,m in models.items() if m.model_name==name),None)
 if role not in ('embedding','rerank','llm') or not str(request.url).startswith(models[role].base_url.rstrip('/')+'/'):
  raise RuntimeError('Unexpected model or endpoint')
 if role=='llm' and (body.get('max_tokens')!=2000 or body.get('temperature')!=.7):raise RuntimeError('Prompt parameter drift')
 rec={'role':role,'request_sha256':sha(request.content),'start':time.time(),'status':'RESERVED'};log('requests.jsonl',rec);return rec
def finish(rec,response=None):
 if response is None or response.status_code>=400:provider_failures.append(rec['request_sha256'])
 log('requests.jsonl',{'request_sha256':rec['request_sha256'],'role':rec['role'],'status':'HTTP_SUCCESS' if response is not None and response.status_code<400 else 'UNKNOWN_OR_FAILED',
  'http_status':response.status_code if response is not None else None,'end':time.time()})
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
cache=VectorCache(runtime/'vectors.db');service=EmbeddingService(base_url=models['embedding'].base_url,api_key=models['embedding'].api_key,model=models['embedding'].model_name)
def embed(texts,label):
 missing=list(dict.fromkeys(t for t in texts if not cache.has(t)))
 for start in range(0,len(missing),16):
  part=missing[start:start+16]
  def batch():
   vectors=asyncio.run(service.embed_documents(part,model_id=models['embedding'].model_name))
   assert len(vectors)==len(part) and all(len(v)==1024 for v in vectors)
   return vectors
  vectors=retry(batch,label+':'+str(start))
  if vectors is None:return False
  cache.put_many(part,vectors)
 return True
usable=[]
for p in parsed:
 if embed([c['content'] for c in p['chunks']],'embed-source:'+p['sha256']):usable.append(p)
 else:failed_sources.append(p['sha256'])
SQLModel.metadata.create_all(engine);vs=VectorStoreService();kb='office50-v1';bychunk={};index=[]
with Session(engine) as s:
 s.add(KnowledgeBase(id=kb,name=kb,embedding_model=models['embedding'].id,vlm_model=models['vlm'].id));s.commit()
 for p in usable:
  fid='office-'+p['sha256'];s.add(FileDocument(id=fid,kb_id=kb,name=p['name'],local_path=p['path'],size=pathlib.Path(p['path']).stat().st_size));s.commit()
  docs=[];vectors=[]
  for i,c in enumerate(p['chunks']):
   cid=fid+'-'+str(i);text=c['content'];ct=c['content_type'].lower()
   s.add(DocumentChunk(id=cid,file_id=fid,content=text,page_number=c['page_number'],content_type=ContentType(ct),original_index=i))
   bychunk[cid]=p['sha256'];index.append((cid,text))
   docs.append({'id':cid,'content':text,'metadata':{'file_id':fid,'file_name':p['name'],'page_number':c['page_number'] or 0,'content_type':ct}});vectors.append(cache.get(text))
   if len(docs)==64:vs.add_documents(kb,docs,vectors);docs=[];vectors=[]
  if docs:vs.add_documents(kb,docs,vectors)
  s.commit()
 bulk_bm25(s,kb,index)
class ModelSession:
 def __init__(self,s):self.session=s
 def get(self,kind,key):
  if kind is CustomModel:return next((m for m in models.values() if m.id==key),None)
  return self.session.get(kind,key)
 def __getattr__(self,key):return getattr(self.session,key)
def generate(q,context):
 with OpenAI(base_url=models['llm'].base_url,api_key=models['llm'].api_key,max_retries=0,timeout=120,http_client=httpx.Client(trust_env=False)) as api:
  result=api.chat.completions.create(model=models['llm'].model_name,messages=[{'role':'system','content':answer_prompt(context)},{'role':'user','content':q}],temperature=.7,max_tokens=2000,stream=False)
  return {'answer':result.choices[0].message.content or '','finish_reason':result.choices[0].finish_reason,
          'usage':result.usage.model_dump() if result.usage else None}
def query(case,arm):
 vector=cache.get(case['question']);service=RetrievalService()
 async def query_vector(session,kb_id,text):return vector
 service._embed_query_for_kb=query_vector
 with Session(engine) as raw:
  session=ModelSession(raw)
  candidates,metrics=asyncio.run(service.search(session,case['question'],[kb],search_mode='hybrid',top_k=50,score_threshold=0,vector_weight=.7,bm25_weight=.3,use_cache=False,log_retrieval=False))
  failures_before=len(provider_failures)
  ranked=batch_rerank(case['question'],candidates,RerankService(session),models['rerank'],top_k=20,score_threshold=0)
  if not ranked or len(provider_failures)!=failures_before:raise RuntimeError('Empty rerank result or provider failure')
  context='\n\n'.join(f'[{i}] 来源: {r.get("file_name","未知")}\n{r.get("content","")}' for i,r in enumerate(expand_context(ranked,session,window_size=1),1))
  return {'qid':case['qid'],'arm':arm,'source_recall_at_10':int(any(bychunk.get(r['id'])==case['source_sha256'] for r in ranked[:10])),
    'retrieved_top10':[{'chunk_id':r['id'],'source_sha256':bychunk.get(r['id'])} for r in ranked[:10]],'context':context,'metrics':metrics}
status('EVALUATING',previous_200_exit_code=prior_completion['exit_code'],usable_sources=len(usable),failed_sources=failed_sources)
usable_ids={x['sha256'] for x in usable};results=[]
for case in cases:
 qid=case['qid']
 if case['source_sha256'] not in usable_ids or not embed([case['question']],'embed-question:'+qid):
  for arm in ('before','after'):results.append({'qid':qid,'arm':arm,'status':'SKIPPED','reason':'source_or_embedding_unavailable'})
  continue
 for arm in ('before','after'):
  found=retry(lambda:query(case,arm),'retrieval:'+qid+':'+arm)
  if found is None:results.append({'qid':qid,'arm':arm,'status':'SKIPPED','reason':'retrieval_failed'});continue
  answer=retry(lambda:generate(case['question'],found['context']),'generation:'+qid+':'+arm)
  if answer is None:results.append({'qid':qid,'arm':arm,'status':'SKIPPED','reason':'generation_failed',**{k:v for k,v in found.items() if k!='context'}});continue
  result={'status':'COMPLETED','format':case['format'],**found,**answer,'reference_answer':case['answer'],'source_location':case['expected_sources']}
  results.append(result);log('results.jsonl',result)
 status('EVALUATING',questions_visited=1+cases.index(case),completed_answers=sum(r['status']=='COMPLETED' for r in results),skipped=sum(r['status']=='SKIPPED' for r in results))
for r in results:
 if r['status']=='SKIPPED':log('results.jsonl',r)
summary={'office_questions':50,'answer_attempts_expected':100,'completed':sum(r['status']=='COMPLETED' for r in results),
 'skipped':sum(r['status']=='SKIPPED' for r in results),'old200_exit_code':prior_completion['exit_code'],
 'source_recall_by_format_and_arm':{},'answer_correctness':'PENDING_SEMANTIC_REVIEW',
 'note':'Office documents use the same non-PDF parser/index for both arms; report separately from the PDF table ablation.'}
for form in ('docx','xlsx'):
 for arm in ('before','after'):
  group=[r['source_recall_at_10'] for r in results if r.get('format')==form and r['arm']==arm and r['status']=='COMPLETED']
  summary['source_recall_by_format_and_arm'][form+'_'+arm]={'n':len(group),'mean':sum(group)/len(group) if group else None}
save('summary.json',summary);status('COMTED',**summary)

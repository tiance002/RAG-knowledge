"""One isolated real-agent run; bounded provider calls, never overwrites evidence."""
import asyncio, hashlib, json, os, shutil, sqlite3, sys, time, uuid, multiprocessing as mp, socket
from pathlib import Path

base=Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
prior=base/'runtime200-r4'; data=base/'dataset200'
backend=Path(sys.argv[1]).resolve()
out=base/('p0-agent-http-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
out.mkdir(mode=0o700)
import atexit
def scrub_credentials():
    path=out/'isolated.db'
    if path.exists():
        c=sqlite3.connect(path)
        try:
            c.execute("update custom_models set api_key=''");c.commit();c.execute('vacuum')
        finally:c.close()
atexit.register(scrub_credentials)
cloud='--cloud' in sys.argv
credentials=json.load(sys.stdin) if cloud else {}
if cloud and (not credentials.get('LANGFUSE_PUBLIC_KEY') or not credentials.get('LANGFUSE_SECRET_KEY') or credentials.get('LANGFUSE_BASE_URL','').rstrip('/')!='https://us.cloud.langfuse.com'):
    raise ValueError('Invalid cloud tracing configuration')
def sha(b): return hashlib.sha256(b).hexdigest()
def rows(p): return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
freeze=json.loads((data/'freeze.json').read_bytes())
for name,digest in freeze['files'].items():
    if sha((data/name).read_bytes())!=digest: raise ValueError('Dataset hash mismatch')
registry={r['qid']:r for r in rows(data/'registry200.jsonl')}
negative=sys.argv[2]=='empty-kb'
case=(next(r for r in rows(data/'gold100.jsonl') if r['qid']==sys.argv[2][4:]) if sys.argv[2].startswith('qid=') else sorted((r for r in rows(data/'gold100.jsonl') if registry[r['qid']]['profile']=='hotpotqa'),key=lambda r:r['qid'])[0 if negative else int(sys.argv[2])])
controls=rows(prior/'control-sql.jsonl')
kbs={r['value']['name'].removeprefix('prod-'):r['value']['id'] for r in controls if r['kind']=='kb'}
profile=registry[case['qid']]['profile']; kb=kbs.get(profile,'v200-'+profile+'-after')
source=sqlite3.connect('file:'+str(prior/'isolated.db')+'?mode=ro',uri=True)
dest=sqlite3.connect(out/'isolated.db'); source.backup(dest); source.close()
if negative:
    row=list(dest.execute('select * from knowledge_bases where id=?',(kb,)).fetchone()); row[0]=str(uuid.uuid4()); row[1]='isolated-empty-negative-control';row[5]=0
    dest.execute('insert into knowledge_bases values ('+','.join('?'*len(row))+')',row);kb=row[0]
ids=json.loads(Path('/opt/amrag/ids.json').read_bytes())['models']
prod=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True)
for role in ('llm','embedding'):
    model=prod.execute('select * from custom_models where id=?',(ids[role],)).fetchone()
    if not model: raise ValueError('Model missing: '+role)
    dest.execute('insert or replace into custom_models values ('+','.join('?'*len(model))+')', model)
    if role=='llm': llm={'model_id':model[5], 'base_url':model[3], 'api_key':model[4]}
dest.commit(); dest.close();prod.close()
shutil.copytree(prior/'chroma',out/'chroma')
os.environ.update(RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true' if cloud else 'false', RAG_SNAPSHOTS_ENABLED='false',
    RAG_OBSERVATION_LOG=str(out/'events.jsonl'), ENV='test', DATABASE_URL='sqlite:///'+str(out/'isolated.db'),
    CHROMA_DB_DIR=str(out/'chroma'), IMAGE_DIR=str(out/'images'), UPLOAD_DIR=str(out/'uploads'), ANONYMIZED_TELEMETRY='False')
if cloud: os.environ.update({k:credentials[k] for k in ('LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY','LANGFUSE_BASE_URL')})
os.chdir(out);sys.path.insert(0,str(backend))
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from app.services.agent_workflow import AgentWorkflow
from app.services.retrieval import RetrievalService
from app.services.llm import LLMService
from app.services.rerank import RerankService
from app.services.observability import span, _sdk
import httpx
original_send=httpx.AsyncClient.send
http_calls={'chat':mp.Value('i',0),'embedding':mp.Value('i',0)}
async def bounded_send(self, request, *a, **kw):
    if cloud and request.url.host=='us.cloud.langfuse.com': return await original_send(self,request,*a,**kw)
    path=request.url.path.lower()
    role='chat' if path.endswith('/chat/completions') else ('embedding' if path.endswith(('/embeddings','/embed')) else None)
    if role is None: raise RuntimeError('Unapproved network endpoint')
    with http_calls[role].get_lock():
        if http_calls[role].value>=({'chat':8,'embedding':4}[role]): raise RuntimeError('HTTP provider cap reached')
        http_calls[role].value+=1
    return await original_send(self,request,*a,**kw)
httpx.AsyncClient.send=bounded_send
def deny_rerank(*a,**kw): raise RuntimeError('Rerank disabled for bounded acceptance')
RerankService.rerank=deny_rerank
calls={'chat':mp.Value('i',0),'embedding':mp.Value('i',0)}
original_generate=ChatOpenAI._agenerate
async def capped_generate(self,*a,**kw):
    with calls['chat'].get_lock():
        if calls['chat'].value>=8: raise RuntimeError('chat provider cap reached')
        calls['chat'].value+=1
    return await original_generate(self,*a,**kw)
ChatOpenAI._agenerate=capped_generate
def safe_get(self,temperature=0.5,streaming=True,max_tokens=None):
    from app.core.config import get_settings
    return ChatOpenAI(model=self.model,openai_api_key=self.api_key,openai_api_base=self.base_url,temperature=temperature,streaming=streaming,max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS if max_tokens is None else max_tokens,max_retries=0,request_timeout=90)
LLMService.get_chat_model=safe_get
original_embed=RetrievalService._embed_query_for_kb
async def capped_embed(self,*a,**kw):
    with calls['embedding'].get_lock():
        if calls['embedding'].value>=4: raise RuntimeError('embedding provider cap reached')
        calls['embedding'].value+=1
    return await original_embed(self,*a,**kw)
RetrievalService._embed_query_for_kb=capped_embed
manifest={'qid':case['qid'],'question_sha256':sha(case['question'].encode()),'kb_id':kb,'freeze_sha256':sha((data/'freeze.json').read_bytes()),'runner_sha256':sha(Path(__file__).read_bytes()),'bounds':{'chat':8,'embedding':4,'rerank':0},'cost':'UNKNOWN','case_type':'empty_kb_negative_control' if negative else 'frozen_in_scope','client_max_retries':0,'cloud_tracing':cloud}
(out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')

from app.main import app
import uvicorn
if cloud:
    from langfuse import Langfuse
    preflight=Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'],timeout=10)
    if not preflight.auth_check(): raise RuntimeError('Cloud auth preflight failed; zero provider calls')
    preflight.shutdown()
manifest.update(model=llm['model_id'], request_mode='agent', transport='actual HTTP/SSE')
(out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
def serve():
    with (out/'server.log').open('w',encoding='utf-8') as log:
        os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
        try: uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')
        finally:
            sdk=_sdk()
            if sdk: sdk.flush()
p=mp.Process(target=serve);p.start()
result={'status':'FAILED','cost':'UNKNOWN'}
try:
    for _ in range(100):
        try:
            if httpx.get(f'http://127.0.0.1:{port}/health',timeout=1,trust_env=False).status_code==200: break
        except Exception: time.sleep(.1)
    else: raise RuntimeError('server startup failed')
    request={'messages':[{'role':'user','content':case['question']}], 'kb_ids':[kb], 'mode':'agent','model_id':ids['llm'],'search_mode':'fulltext','top_k':5,'rerank_enabled':False,'context_window':0,'use_rewrite':False}
    with httpx.Client(timeout=480,trust_env=False) as client:
        with client.stream('POST',f'http://127.0.0.1:{port}/api/v1/chat/completions',json=request) as response:
            status=response.status_code;ctype=response.headers.get('content-type','');raw=''.join(response.iter_text())
    (out/'response.sse').write_text(raw,encoding='utf-8')
    events=[]
    for block in raw.split('\n\n'):
        lines=block.splitlines(); typ=next((x[7:] for x in lines if x.startswith('event: ')),None); data=next((x[6:] for x in lines if x.startswith('data: ')),None)
        if typ and data: events.append((typ,json.loads(data)))
    p.terminate();p.join(20)
    if p.is_alive(): raise RuntimeError('server failed to shut down')
    local=rows(out/'events.jsonl')
    roots=[r for r in local if r['stage']=='qa.request' and r['event']=='start']
    if len(roots)!=1: raise RuntimeError('expected one qa.request root')
    trace_id=roots[0]['trace_id']
    checks=[]
    db=sqlite3.connect('file:'+str(out/'isolated.db')+'?mode=ro',uri=True)
    for _,d in (x for x in events if x[0]=='rag_result'):
        for c in d.get('citations',[]):
            found=db.execute('select dc.id,dc.page_number from document_chunks dc join file_documents f on f.id=dc.file_id where f.kb_id=? and f.id=? and dc.content=?',(kb,c.get('fileId'),c.get('content'))).fetchall()
            checks.append({'fileId':c.get('fileId'),'matching_chunks':found})
    db.close()
    cloud_result={'enabled':cloud}
    if cloud:
        client=Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'],timeout=10)
        try:
            for attempt in range(8):
                obs=client.api.observations.get_many(trace_id=trace_id,limit=100).data
                if obs and any(getattr(o,'name',None)=='qa.request' for o in obs): break
                time.sleep(3)
            cloud_ids={str(o.id) for o in obs}; local_ids={r['span_id'] for r in local if r['event']=='start'}
            names=[o.name for o in obs]
            cloud_result.update(trace_id=trace_id, count=len(obs),names=names,local_span_ids=sorted(local_ids),cloud_ids=sorted(cloud_ids),missing_local_span_ids=sorted(local_ids-cloud_ids),local_actual_query=sum(r['stage']=='agent.actual_query' for r in local),cloud_actual_query=names.count('agent.actual_query'))
        finally: client.shutdown()
    quality=[(i,r) for i,r in enumerate(local) if r['stage']=='agent.quality']
    queries=[(i,r) for i,r in enumerate(local) if r['stage']=='agent.actual_query']
    post=[{'score':q['metadata'].get('score'),'query_metadata':n['metadata']} for i,q in quality for j,n in queries if j>i and n['metadata'].get('retry_count',0)>q['metadata'].get('retry_count',0)]
    result.update(http_status=status,content_type=ctype,event_counts={e:sum(x==e for x,_ in events) for e,_ in events},sse_terminated=raw.endswith('\n\n'),errors=[d for e,d in events if e=='error'],citations=checks,trace_id=trace_id,post_grade_queries=post,cloud_readback=cloud_result,provider_http={k:v.value for k,v in http_calls.items()},calls={k:v.value for k,v in calls.items()})
    result['status']='COMPLETED' if status==200 and raw.endswith('\n\n') and any(e=='done' for e,_ in events) and not result['errors'] else 'FAILED'
except Exception as exc:
    result.update(error_type=type(exc).__name__,provider_http={k:v.value for k,v in http_calls.items()})
finally:
    if p.is_alive(): p.terminate();p.join(20)
    scrub_credentials()
    c=sqlite3.connect(out/'isolated.db');result['remaining_nonempty_keys']=c.execute("select count(*) from custom_models where coalesce(api_key,'')<>''").fetchone()[0];c.close()
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(out),**result},ensure_ascii=False),flush=True)
sys.exit(0 if result['status']=='COMPLETED' else 1)

\n
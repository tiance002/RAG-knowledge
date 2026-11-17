"""One isolated real-agent run; bounded provider calls, never overwrites evidence."""
import asyncio, hashlib, json, os, shutil, sqlite3, sys, time, uuid
from pathlib import Path

base=Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
prior=base/'runtime200-r4'; data=base/'dataset200'
backend=Path(sys.argv[1]).resolve()
out=base/('p0-agent-accept-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
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
case=sorted((r for r in rows(data/'gold100.jsonl') if registry[r['qid']]['profile']=='hotpotqa'),key=lambda r:r['qid'])[0 if negative else int(sys.argv[2])]
controls=rows(prior/'control-sql.jsonl')
kbs={r['value']['name'].removeprefix('prod-'):r['value']['id'] for r in controls if r['kind']=='kb'}
kb=kbs['hotpotqa']
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
http_calls={'chat':0,'embedding':0}
async def bounded_send(self, request, *a, **kw):
    if cloud and request.url.host=='us.cloud.langfuse.com': return await original_send(self,request,*a,**kw)
    path=request.url.path.lower()
    role='chat' if path.endswith('/chat/completions') else ('embedding' if path.endswith(('/embeddings','/embed')) else None)
    if role is None: raise RuntimeError('Unapproved network endpoint')
    if http_calls[role]>=({'chat':8,'embedding':4}[role]): raise RuntimeError('HTTP provider cap reached')
    http_calls[role]+=1
    return await original_send(self,request,*a,**kw)
httpx.AsyncClient.send=bounded_send
def deny_rerank(*a,**kw): raise RuntimeError('Rerank disabled for bounded acceptance')
RerankService.rerank=deny_rerank
calls={'chat':0,'embedding':0}
original_generate=ChatOpenAI._agenerate
async def capped_generate(self,*a,**kw):
    if calls['chat']>=8: raise RuntimeError('chat provider cap reached')
    calls['chat']+=1
    return await original_generate(self,*a,**kw)
ChatOpenAI._agenerate=capped_generate
def safe_get(self,temperature=0.5,streaming=True,max_tokens=None):
    from app.core.config import get_settings
    return ChatOpenAI(model=self.model,openai_api_key=self.api_key,openai_api_base=self.base_url,temperature=temperature,streaming=streaming,max_tokens=get_settings().LLM_ANSWER_MAX_TOKENS if max_tokens is None else max_tokens,max_retries=0,request_timeout=90)
LLMService.get_chat_model=safe_get
original_embed=RetrievalService._embed_query_for_kb
async def capped_embed(self,*a,**kw):
    if calls['embedding']>=4: raise RuntimeError('embedding provider cap reached')
    calls['embedding']+=1
    return await original_embed(self,*a,**kw)
RetrievalService._embed_query_for_kb=capped_embed
manifest={'qid':case['qid'],'question_sha256':sha(case['question'].encode()),'kb_id':kb,'freeze_sha256':sha((data/'freeze.json').read_bytes()),'runner_sha256':sha(Path(__file__).read_bytes()),'bounds':{'chat':8,'embedding':4,'rerank':0},'cost':'UNKNOWN','case_type':'empty_kb_negative_control' if negative else 'frozen_in_scope','client_max_retries':0,'cloud_tracing':cloud}
(out/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
async def run():
    state={'messages':[HumanMessage(content=case['question'])], 'kb_ids':[kb], 'search_params':{'rerank_enabled':False}, 'model_config':llm,'agent_steps':[], 'retry_count':0}
    return await asyncio.wait_for(AgentWorkflow().app.ainvoke(state,config={'recursion_limit':30}),timeout=480)
result={'status':'UNKNOWN_OR_FAILED','calls':calls,'http_calls':http_calls,'cost':'UNKNOWN'}
try:
    with span('p0.agent.acceptance', case_type=manifest['case_type']) as trace:
        state=asyncio.run(run())
        trace_id=trace['trace_id']
    result['trace_id']=trace_id
    if cloud:
        sdk=_sdk()
        if sdk: sdk.flush()
        import urllib.request, base64
        auth=base64.b64encode((credentials['LANGFUSE_PUBLIC_KEY']+':'+credentials['LANGFUSE_SECRET_KEY']).encode()).decode()
        request=urllib.request.Request('https://us.cloud.langfuse.com/api/public/traces/'+trace_id,headers={'Authorization':'Basic '+auth})
        try:
            with urllib.request.urlopen(request,timeout=15) as response:
                doc=json.load(response); result['cloud_readback']={'http_status':response.status,'trace_id_match':doc.get('id')==trace_id,'observation_count':len(doc.get('observations',[]))}
        except Exception as exc: result['cloud_readback']={'error_type':type(exc).__name__}
    result.update(status='COMPLETED',retry_count=state.get('retry_count'),last_score=state.get('last_score'),steps=state.get('agent_steps',[]),answer_sha256=sha(str(state['messages'][-1].content).encode()))
except Exception as exc:
    result.update(error_type=type(exc).__name__)
finally:
    c=sqlite3.connect(out/'isolated.db'); c.execute("update custom_models set api_key=''"); c.commit(); c.execute('vacuum'); result['remaining_nonempty_keys']=c.execute("select count(*) from custom_models where coalesce(api_key,'')<>''").fetchone()[0];c.close()
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(out),'status':result['status'],'calls':calls,'http_calls':http_calls,'error_type':result.get('error_type')}))
sys.exit(0 if result['status']=='COMPLETED' else 1)

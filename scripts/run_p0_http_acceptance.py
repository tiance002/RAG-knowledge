"""One bounded real HTTP SSE request against an isolated database and code snapshot."""
import asyncio, hashlib, json, multiprocessing as mp, os, shutil, socket, sqlite3, sys, time, uuid
from pathlib import Path

base = Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
code = Path(sys.argv[1]).resolve()
cloud = '--cloud' in sys.argv
if cloud:
    credentials = json.load(sys.stdin)
    names = ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')
    if not all(credentials.get(k) for k in names) or credentials['LANGFUSE_BASE_URL'].rstrip('/') != 'https://us.cloud.langfuse.com':
        raise ValueError('Missing authorized US Cloud configuration')
    os.environ.update({k: credentials[k] for k in names})
    del credentials
    from langfuse import Langfuse
    preflight_client = Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'], timeout=10)
    if not preflight_client.auth_check(): raise RuntimeError('Cloud auth preflight failed; zero generation calls')
    preflight_client.shutdown()
out = base / ('p0-http-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
out.mkdir(mode=0o700)
prior = base / 'runtime200-r4'
dataset = base / 'dataset200'
sha = lambda b: hashlib.sha256(b).hexdigest()
freeze = json.loads((dataset/'freeze.json').read_bytes())
for name, digest in freeze['files'].items():
    if sha((dataset/name).read_bytes()) != digest: raise ValueError('dataset hash mismatch: '+name)
rows = lambda p: [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
registry = {r['qid']:r for r in rows(dataset/'registry200.jsonl')}
case = sorted((r for r in rows(dataset/'gold100.jsonl') if registry[r['qid']]['profile'].startswith('pdf-')), key=lambda x:x['qid'])[0]
profile = registry[case['qid']]['profile']
control = rows(prior/'control-sql.jsonl')
old = {r['value']['name'].removeprefix('prod-'):r['value']['id'] for r in control if r['kind']=='kb'}
kb = old.get(profile, 'v200-'+profile+'-after')
source = sqlite3.connect('file:'+str(prior/'isolated.db')+'?mode=ro', uri=True)
dest = sqlite3.connect(out/'isolated.db'); source.backup(dest); source.close()
model_id = json.loads(Path('/opt/amrag/ids.json').read_bytes())['models']['llm']
production = sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro', uri=True)
model = production.execute('select * from custom_models where id=?',(model_id,)).fetchone()
if model is None: raise ValueError('model unavailable')
cols = [x[1] for x in production.execute('pragma table_info(custom_models)')]
production.close()
state = json.loads((prior/'state.json').read_bytes())['models']['llm']
record = dict(zip(cols,model))
if {'id':record['id'], 'model':record['model_name'], 'endpoint_sha256':sha(record['base_url'].encode())} != state: raise ValueError('model identity drift')
dest.execute('delete from custom_models where id=?',(model_id,))
dest.execute('insert into custom_models ('+','.join(cols)+') values ('+','.join('?' for _ in cols)+')',model); dest.commit(); dest.close()
shutil.copytree(prior/'chroma',out/'chroma')
manifest={'qid':case['qid'],'profile':profile,'kb_id':kb,'dataset_freeze_sha256':sha((dataset/'freeze.json').read_bytes()),'search_mode':'fulltext','model_id':model_id,'limits':{'generation_http':1,'embedding_http':0,'rerank_http':0},'cost':'UNKNOWN','code':str(code)}
(out/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
(out/'gold.json').write_text(json.dumps(case,ensure_ascii=False,indent=2),encoding='utf-8')
os.environ.update(ENV='test',DATABASE_URL='sqlite:///'+str(out/'isolated.db'),CHROMA_DB_DIR=str(out/'chroma'),IMAGE_DIR=str(out/'images'),UPLOAD_DIR=str(out/'uploads'),ANONYMIZED_TELEMETRY='False',AMRAG_IMAGE_MODE='off',RAG_OBSERVABILITY_ENABLED='true',LANGFUSE_ENABLED='true' if cloud else 'false',RAG_SNAPSHOTS_ENABLED='false',RAG_OBSERVATION_LOG=str(out/'events.jsonl'))
os.chdir(out); sys.path.insert(0,str(code/'backend'))
import httpx
from openai import AsyncOpenAI
from app.services.llm import LLMService
count = mp.Value('i',0)
def bounded_client(self):
    if self._client is None:
        class BoundedTransport(httpx.AsyncBaseTransport):
            def __init__(self): self.inner=httpx.AsyncHTTPTransport(retries=0)
            async def handle_async_request(self, request):
                with count.get_lock():
                    if count.value >= 1: raise RuntimeError('generation request bound reached')
                    count.value += 1
                return await self.inner.handle_async_request(request)
            async def aclose(self): await self.inner.aclose()
        self._client = AsyncOpenAI(base_url=self.base_url,api_key=self.api_key,max_retries=0,timeout=120,http_client=httpx.AsyncClient(transport=BoundedTransport(),trust_env=False))
    return self._client
LLMService._get_client=bounded_client
from app.main import app
import uvicorn
sock=socket.socket(); sock.bind(('127.0.0.1',0)); port=sock.getsockname()[1]; sock.close()
def serve():
    with (out/'server.log').open('w',encoding='utf-8') as log:
        os.dup2(log.fileno(),1); os.dup2(log.fileno(),2)
        uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')
        from app.services.observability import _sdk
        if _sdk(): _sdk().flush()
p=mp.Process(target=serve); p.start()
try:
    for _ in range(100):
        try:
            if httpx.get(f'http://127.0.0.1:{port}/health',timeout=1,trust_env=False).status_code==200: break
        except Exception: time.sleep(.1)
    else: raise RuntimeError('server startup failed')
    request={'messages':[{'role':'user','content':case['question']}], 'kb_ids':[kb], 'mode':'chat','model_id':model_id,'search_mode':'fulltext','top_k':20,'rerank_enabled':False,'context_window':0,'use_rewrite':False}
    with httpx.Client(timeout=180,trust_env=False) as client:
        with client.stream('POST',f'http://127.0.0.1:{port}/api/v1/chat/completions',json=request) as response:
            status=response.status_code; content_type=response.headers.get('content-type',''); raw=''.join(response.iter_text())
    (out/'response.sse').write_text(raw,encoding='utf-8')
    events=[]
    for block in raw.split('\n\n'):
        lines=block.splitlines(); ev=next((x[7:] for x in lines if x.startswith('event: ')),None); data=next((x[6:] for x in lines if x.startswith('data: ')),None)
        if ev and data: events.append((ev,json.loads(data)))
    answer=''.join(d.get('content','') for e,d in events if e=='answer_chunk')
    citations=[c for e,d in events if e=='rag_result' for c in d.get('citations',[])]
    db=sqlite3.connect('file:'+str(out/'isolated.db')+'?mode=ro',uri=True)
    checks=[]
    for c in citations:
        matches=db.execute('select dc.id, dc.page_number from document_chunks dc join file_documents f on f.id=dc.file_id where f.kb_id=? and f.id=? and dc.content=?',(kb,c.get('fileId'),c.get('content'))).fetchall()
        checks.append({'fileId':c.get('fileId'),'location':c.get('location'),'matching_chunks':matches})
    db.close()
    (out/'answer.txt').write_text(answer,encoding='utf-8')
    p.terminate(); p.join(20)
    if p.is_alive(): raise RuntimeError('Server failed to shut down and flush')
    cloud_result = {'enabled': cloud, 'verified': False}
    if cloud:
        event_rows = rows(out/'events.jsonl')
        trace_ids = {r['trace_id'] for r in event_rows if r.get('stage') == 'qa.request' and r.get('event') == 'start'}
        if len(trace_ids) != 1: raise RuntimeError('Expected exactly one qa.request trace')
        trace_id = trace_ids.pop()
        client = Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'], timeout=10)
        try:
            for attempt in range(8):
                try:
                    observations = client.api.observations.get_many(trace_id=trace_id, limit=100).data
                    names_found = [getattr(item, 'name', None) for item in observations]
                    if 'qa.request' in names_found:
                        cloud_result = {'enabled': True, 'verified': True, 'trace_id': trace_id, 'observation_names': names_found, 'readback_attempt': attempt + 1}
                        break
                except Exception as exc:
                    cloud_result = {'enabled': True, 'verified': False, 'trace_id': trace_id, 'error_type': type(exc).__name__, 'readback_attempt': attempt + 1}
                time.sleep(3)
        finally:
            client.shutdown()
        (out/'cloud-readback.json').write_text(json.dumps(cloud_result,ensure_ascii=False,indent=2),encoding='utf-8')
    result={'http_status':status,'content_type':content_type,'event_counts':{k:sum(e==k for e,_ in events) for k in sorted({e for e,_ in events})},'answer_chars':len(answer),'citations':checks,'generation_http_calls':count.value,'errors':[d for e,d in events if e=='error'],'sse_terminated':raw.endswith('\n\n'),'cost':'UNKNOWN'}
    result['cloud_readback'] = cloud_result
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'output':str(out),**result},ensure_ascii=False),flush=True)
    sys.exit(0 if status==200 and 'text/event-stream' in content_type and answer and citations and all(c['matching_chunks'] for c in checks) and not result['errors'] and any(e=='done' for e,_ in events) and count.value==1 and (not cloud or cloud_result['verified']) else 1)
finally:
    p.terminate(); p.join(10)
    scrub = sqlite3.connect(out/'isolated.db')
    scrub.execute("update custom_models set api_key = ''")
    scrub.commit()
    scrub.execute('pragma wal_checkpoint(truncate)')
    scrub.execute('vacuum')
    scrub.close()

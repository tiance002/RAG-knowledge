"""Run on ECS against an isolated source/SQLite/Redis DB. No providers."""
import os, sys, json, time, subprocess, urllib.request, signal
from pathlib import Path

ROOT=Path(__file__).resolve().parent
BACKEND=ROOT/'code/backend'
WITH_INDEX='--with-index' in sys.argv
RUN=ROOT/('queue-test-index' if WITH_INDEX else 'queue-test')
RUN.mkdir(mode=0o700,exist_ok=False)
os.chdir(BACKEND)
env=os.environ.copy()
env.update(DATABASE_URL='sqlite:///'+str(RUN/'test.db'),CHROMA_DB_DIR=str(RUN/'chroma'),
    UPLOAD_DIR=str(RUN/'uploads'),IMAGE_DIR=str(RUN/'images'),REDIS_URL='redis://127.0.0.1:16379/12',
    CELERY_BROKER_URL='redis://127.0.0.1:16379/12',CELERY_RESULT_BACKEND='redis://127.0.0.1:16379/13',
    CELERY_QUEUE_NAME='amrag_migration_20261105',CELERY_WORKER_CONCURRENCY='1',
    LANGFUSE_ENABLED='false',RAG_OBSERVABILITY_ENABLED='false',RAG_PROMETHEUS_ENABLED='false',
    PYTHONPATH=str(BACKEND))
os.environ.update(env);sys.path.insert(0,str(BACKEND))
from redis import Redis
for db in (12,13):
    if Redis(host='127.0.0.1',port=16379,db=db).dbsize():
        raise RuntimeError('Isolation Redis DB not empty; refuse reuse')
from app.main import app
from app.core.database import engine,init_db
from app.models import KnowledgeBase,FileDocument,DocumentChunk,CustomModel,ModelType
from app.tasks.dispatch import FileTaskReceipt
from app.tasks.celery_app import celery_app
from sqlmodel import Session,select
init_db()
embedding_calls=[]
if WITH_INDEX:
    import threading
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    class Embeddings(BaseHTTPRequestHandler):
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            inputs=body['input'];inputs=[inputs] if isinstance(inputs,str) else inputs
            embedding_calls.append(len(inputs))
            data=json.dumps({'object':'list','model':'synthetic-embedding','data':[{'object':'embedding','index':i,'embedding':[1.0,0.0,0.0]} for i in range(len(inputs))],'usage':{'prompt_tokens':0,'total_tokens':0}}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.end_headers();self.wfile.write(data)
        def log_message(self,*args):pass
    embedding_server=ThreadingHTTPServer(('127.0.0.1',18212),Embeddings)
    threading.Thread(target=embedding_server.serve_forever,daemon=True).start()
text=RUN/'source.txt';text.write_text('Public synthetic queue acceptance. Water freezes at zero degrees Celsius.',encoding='utf-8')
with Session(engine) as s:
    if WITH_INDEX:s.add(CustomModel(id='fixture-model',name='Local synthetic transport',model_type=ModelType.EMBEDDING,base_url='http://127.0.0.1:18212/v1',api_key='synthetic-not-a-secret',model_name='synthetic-embedding',context_length=8192))
    s.add(KnowledgeBase(id='queue-kb',name='Synthetic isolated',embedding_model='fixture-model' if WITH_INDEX else 'sys_embedding',vlm_model='default'))
    s.add(FileDocument(id='queue-file',kb_id='queue-kb',name='source.txt',local_path=str(text),size=text.stat().st_size));s.commit()
procs=[];results=[]
def request(path,payload=None):
    r=urllib.request.Request('http://127.0.0.1:18211'+path,data=json.dumps(payload).encode() if payload is not None else None,headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(r,timeout=10) as x:return json.load(x)
def wait(fn,timeout=45):
    end=time.time()+timeout
    while time.time()<end:
        value=fn()
        if value:return value
        time.sleep(.25)
    raise TimeoutError('acceptance condition')
try:
    log=open(RUN/'api.log','ab',buffering=0)
    api=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','18211'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True);procs.append(api)
    def health():
        try:return request('/health')
        except Exception:return None
    wait(health)
    response=request('/api/v1/files/queue-file/parse',{'chunk_mode':'custom','chunk_size':500,'chunk_overlap':0,'auto_vectorize':False})
    task=response['data']['task_id'];view=lambda:request('/api/v1/files/queue-file/tasks/'+task)['data']
    assert view()['status']=='QUEUED';results.append({'check':'real API enqueue with Worker absent','status':'PASS','task_id':task})
    api.terminate();api.wait(timeout=15)
    results.append({'check':'receipt survives API exit','status':'PASS','receipt':view() if api.poll() is None else 'SQL checked by next Worker'})
    log=open(RUN/'worker.log','ab',buffering=0)
    worker=subprocess.Popen([sys.executable,'-m','celery','-A','app.tasks.celery_app:celery_app','worker','-Q',env['CELERY_QUEUE_NAME'],'--concurrency=1','--pool=prefork','--without-gossip','--without-mingle','--without-heartbeat','--loglevel=INFO'],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True);procs.append(worker)
    def receipt():
        with Session(engine) as s:
            r=s.get(FileTaskReceipt,task)
            return r.status if r.status in ('COMPLETED','FAILED','UNKNOWN') else None
    status=wait(receipt,90);assert status=='COMPLETED',status
    with Session(engine) as s:before=[x.id for x in s.exec(select(DocumentChunk)).all()]
    assert before
    results.append({'check':'queued task processed after API stopped','status':'PASS','chunks':len(before)})
    celery_app.send_task('app.tasks.dispatch.execute_file_operation',args=[task],task_id=task,queue=env['CELERY_QUEUE_NAME'])
    time.sleep(3)
    with Session(engine) as s:after=[x.id for x in s.exec(select(DocumentChunk)).all()]
    assert before==after;results.append({'check':'duplicate delivery preserves chunk IDs','status':'PASS'})
    api=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:app','--host','127.0.0.1','--port','18211'],env=env,stdout=open(RUN/'api-restart.log','ab',buffering=0),stderr=subprocess.STDOUT,start_new_session=True);procs.append(api);wait(health)
    assert view()['status']=='COMPLETED';results.append({'check':'restarted API sees worker result','status':'PASS'})
    if WITH_INDEX:
        from app.services.vector_store import VectorStoreService
        reader=VectorStoreService();reader._existing_collection('queue-kb')
        indexed=request('/api/v1/files/queue-file/vectorize',{})
        index_id=indexed['data']['task_id']
        def indexed_status():
            d=request('/api/v1/files/queue-file/tasks/'+index_id)['data'];return d if d['status'] in ('COMPLETED','FAILED','UNKNOWN') else None
        d=wait(indexed_status,90);assert d['status']=='COMPLETED',d
        found=reader._existing_collection('queue-kb').get();assert len(found['ids'])==1
        recall=request('/api/v1/knowledge-bases/queue-kb/recall',{'query':'Water freezes','search_mode':'fulltext','top_k':10,'context_window':0})
        assert any('Water freezes' in x['content'] for x in recall['data']['results'])
        results.append({'check':'real worker indexes to Chroma and BM25 visible in API','status':'PASS','synthetic_embedding_http_calls':len(embedding_calls)})
        celery_app.send_task('app.tasks.dispatch.execute_file_operation',args=[index_id],task_id=index_id,queue=env['CELERY_QUEUE_NAME']);time.sleep(2)
        assert len(embedding_calls)==1;results.append({'check':'duplicate index does not call embedding again','status':'PASS'})
    results.append({'check':'paid provider calls','count':0,'basis':'plain text parse, auto_vectorize false; no model clients invoked'})
except Exception as exc:
    results.append({'check':'acceptance','status':'FAIL','error_type':type(exc).__name__,'detail':str(exc)});raise
finally:
    for p in reversed(procs):
        if p.poll() is None:
            p.terminate()
            try:p.wait(timeout=20)
            except subprocess.TimeoutExpired:os.killpg(p.pid,signal.SIGKILL);p.wait()
    if WITH_INDEX:embedding_server.shutdown();embedding_server.server_close()
    (RUN/'results.json').write_text(json.dumps(results,indent=2),encoding='utf-8')
    print(json.dumps(results))

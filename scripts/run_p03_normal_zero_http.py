"""P0.3 normal-route HTTP/SSE validation; no external network or model calls."""
import argparse, hashlib, json, multiprocessing as mp, os, shutil, socket, sqlite3, sys, time, uuid
from pathlib import Path

CODE = Path('/opt/amrag/experiments/p03-stage-20261028-b/backend')
EXPECTED = '088e042643f8169a4a024e30d9ac95195581725dac81361b588fb1a88895c931'
BASE = Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
DATA, PRIOR = BASE/'dataset200', BASE/'runtime200-r4'
QIDS = {'numeric':'ecs-table-probe/v2:121', 'two_value':'ecs-table-probe/v2:118',
        'unanswerable':'mmlongbench-doc:2ff6aa9237fc777b6627dc57a486e9225ac5fb86:row-0685'}

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def rows(path): return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
def verify():
    files=sorted((CODE/'app').rglob('*.py'))
    if not files: raise RuntimeError('Missing staged backend')
    digest=hashlib.sha256()
    for path in files:
        digest.update(path.relative_to(CODE).as_posix().encode()); digest.update(b'\0'); digest.update(bytes.fromhex(sha(path)))
    if digest.hexdigest()!=EXPECTED: raise RuntimeError('Staged source hash mismatch')
    freeze=json.loads((DATA/'freeze.json').read_text(encoding='utf-8'))
    for name, value in freeze['files'].items():
        path=(DATA/name).resolve()
        if not path.is_relative_to(DATA.resolve()) or sha(path)!=value: raise RuntimeError('Dataset freeze mismatch')
    gold={r['qid']:r for r in rows(DATA/'gold100.jsonl')}
    registry={r['qid']:r for r in rows(DATA/'registry200.jsonl')}
    qrels={r['qid']:r for r in json.loads((PRIOR/'resolved-qrels-after.json').read_text())}
    for key,qid in QIDS.items():
        if qid not in gold or qid not in registry or not registry[qid]['profile'].startswith('pdf-') or (key != 'unanswerable' and qid not in qrels):
            raise RuntimeError('Missing frozen PDF case: '+key)
    for path in (PRIOR/'isolated.db',PRIOR/'chroma',PRIOR/'control-sql.jsonl'):
        if not path.exists(): raise RuntimeError('Missing isolated snapshot')
    return gold,registry,qrels

parser=argparse.ArgumentParser(); parser.add_argument('--execute',action='store_true'); args=parser.parse_args()
gold,registry,qrels=verify()
print(json.dumps({'status':'PREFLIGHT_PASS','code_sha256':EXPECTED,'freeze_sha256':sha(DATA/'freeze.json'), 'qids':QIDS,'external_requests':0}),flush=True)
if not args.execute: sys.exit(0)

out=BASE/('p03-normal-zero-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]);out.mkdir(mode=0o700)
src=sqlite3.connect('file:'+str(PRIOR/'isolated.db')+'?mode=ro',uri=True); dst=sqlite3.connect(out/'isolated.db'); src.backup(dst); src.close()
# Do not retain any provider credentials in the isolated database.
dst.execute("update custom_models set api_key=''")
controls=rows(PRIOR/'control-sql.jsonl');kbs={r['value']['name'].removeprefix('prod-'):r['value']['id'] for r in controls if r['kind']=='kb'}
profile=registry[QIDS['numeric']]['profile']; kb=kbs.get(profile,'v200-'+profile+'-after')
if any(registry[q]['profile']!=profile for q in QIDS.values()): raise RuntimeError('Cases require separate KBs')
base_row=dst.execute('select * from knowledge_bases where id=?',(kb,)).fetchone()
if base_row is None: raise RuntimeError('Frozen knowledge base unavailable')
empty=list(base_row); empty[0]=str(uuid.uuid4());empty[1]='isolated-empty-p03';empty[5]=0
empty_kb=empty[0];dst.execute('insert into knowledge_bases values ('+','.join('?'*len(empty))+')',empty);dst.commit();dst.execute('vacuum');dst.close()
shutil.copytree(PRIOR/'chroma',out/'chroma')
os.environ.update(ENV='test',DATABASE_URL='sqlite:///'+str(out/'isolated.db'),CHROMA_DB_DIR=str(out/'chroma'),IMAGE_DIR=str(out/'images'),UPLOAD_DIR=str(out/'uploads'),ANONYMIZED_TELEMETRY='False',RAG_OBSERVABILITY_ENABLED='true',LANGFUSE_ENABLED='false',RAG_SNAPSHOTS_ENABLED='false',RAG_OBSERVATION_LOG=str(out/'events.jsonl'))
os.chdir(out);sys.path.insert(0,str(CODE))
# A socket-level deny protects both synchronous and async clients. Only the
# explicitly selected loopback server port may be contacted.
sock=socket.socket();sock.bind(('127.0.0.1',0));port=sock.getsockname()[1];sock.close()
real_connect=socket.socket.connect
blocked=mp.Value('i',0)
def guarded_connect(self,address):
    if isinstance(address,tuple) and len(address)>=2 and address[0] in ('127.0.0.1','::1') and address[1]==port:
        return real_connect(self,address)
    with blocked.get_lock(): blocked.value+=1
    raise OSError('External network forbidden in zero-model test')
socket.socket.connect=guarded_connect
import httpx, uvicorn
from app.services.chat import ChatService
from app.main import app
original_retrieve=ChatService._retrieve_with_new_service
async def controlled_retrieve(self,*a,**kw):
    if kw.get('query')=='P03_CONTROLLED_RETRIEVAL_FAULT': raise RuntimeError('controlled retrieval fault')
    return await original_retrieve(self,*a,**kw)
ChatService._retrieve_with_new_service=controlled_retrieve

def serve():
    with (out/'server.log').open('w',encoding='utf-8') as log:
        os.dup2(log.fileno(),1);os.dup2(log.fileno(),2)
        uvicorn.run(app,host='127.0.0.1',port=port,log_level='warning')
p=mp.Process(target=serve);p.start(); reports=[]
try:
    for _ in range(100):
        try:
            if httpx.get(f'http://127.0.0.1:{port}/health',timeout=1,trust_env=False).status_code==200: break
        except Exception: time.sleep(.1)
    else: raise RuntimeError('HTTP server startup failed')
    cases=[(key,gold[qid]['question'],kb,qid) for key,qid in QIDS.items()]
    cases += [('no_results','P03_NONEXISTENT_TERM_778411',empty_kb,None),('retrieval_fault','P03_CONTROLLED_RETRIEVAL_FAULT',kb,None)]
    for name,question,scope,qid in cases:
        request={'messages':[{'role':'user','content':question}],'kb_ids':[scope],'mode':'chat','search_mode':'fulltext','top_k':20,'rerank_enabled':False,'context_window':0,'use_rewrite':False}
        with httpx.Client(timeout=90,trust_env=False) as client:
            with client.stream('POST',f'http://127.0.0.1:{port}/api/v1/chat/completions',json=request) as response:
                status=response.status_code;content_type=response.headers.get('content-type','');raw=''.join(response.iter_text())
        (out/(name+'.sse')).write_text(raw,encoding='utf-8')
        parsed=[]
        for block in raw.split('\n\n'):
            lines=block.splitlines();event=next((x[7:] for x in lines if x.startswith('event: ')),None);data=next((x[6:] for x in lines if x.startswith('data: ')),None)
            if event and data: parsed.append((event,json.loads(data)))
        answer=''.join(data.get('content','') for event,data in parsed if event=='answer_chunk')
        citations=[c for event,data in parsed if event=='rag_result' for c in data.get('citations',[])]
        db=sqlite3.connect('file:'+str(out/'isolated.db')+'?mode=ro',uri=True);checks=[]
        for c in citations:
            match=db.execute('select dc.content,dc.page_number,f.kb_id from document_chunks dc join file_documents f on f.id=dc.file_id where dc.id=? and f.id=?',(c.get('chunk_id'),c.get('fileId'))).fetchone()
            text=c.get('content','').strip();checks.append({'chunk_id':c.get('chunk_id'),'file_id':c.get('fileId'),'page':match[1] if match else None,'scope_match':bool(match and match[2]==scope),'quote_in_source':bool(match and text and text in match[0]),'quote_in_answer':bool(text and text in answer)})
        db.close()
        expected_sources=qrels.get(qid, {}).get('expected_sources',[]) if qid else []
        record={'case':name,'qid':qid,'http_status':status,'content_type':content_type,'events':[e for e,_ in parsed],'answer':answer,'citations':checks,'expected_sources':expected_sources,'blocked_external_attempts':blocked.value}
        record['transport_ok']=status==200 and 'text/event-stream' in content_type and raw.endswith('\n\n')
        record['quotes_ok']=all(c['scope_match'] and c['quote_in_source'] and c['quote_in_answer'] for c in checks)
        reports.append(record)
finally:
    p.terminate();p.join(15)
    (out/'result.json').write_text(json.dumps({'cases':reports,'blocked_external_attempts':blocked.value},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'output':str(out),'cases':reports,'blocked_external_attempts':blocked.value},ensure_ascii=False),flush=True)

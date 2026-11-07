"""One-shot post-fix M2 Hybrid continuation; preflight is strictly offline.

Uses private copies of the existing 80-chunk DB and its existing vector index;
no re-indexing, no old-run modification, no model calls during preflight/tests.
"""
import argparse, asyncio, hashlib, importlib.util, json, os, shutil, sqlite3, sys, time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
VAR=ROOT/'var/preferences-acceptance'
PRIOR=VAR/'m2-hybrid-free-run-v1'
AUDIT_SHA='f6d44249a0f9b14bf1c0bf38ad267118ee6e70db812ab0ecd6af762eba172f73'
PRIOR_PLAN_SHA='b3ed34c3895eed4bf29fd8b717057e16396170faa32748bfd3861e9a8ebe9847'
GRANT=VAR/'m2-hybrid-postfix-continuation-v1.started'
PREFIX='m2-hybrid-postfix-v1'

def load(path):
 s=importlib.util.spec_from_file_location('hybrid_previous_run',path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
old=load(ROOT/'scripts/m2_hybrid_free_run.py')
def sha(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()
def rows(path):return [json.loads(x) for x in Path(path).read_text(encoding='utf8').splitlines() if x.strip()]
def identity(item):return (item['qid'],item['mode'],item['arm'],item['repetition'])
def tree_inventory(path):
 result={}
 for p in sorted(Path(path).rglob('*')):
  if p.is_symlink():raise ValueError('Symlink in retained index is forbidden')
  if p.is_file():
   if p.name.endswith(('-wal','-shm','-journal')):raise ValueError('Transient index file exists')
   result[str(p.relative_to(path)).replace('\\','/')]=sha(p)
 if not result:raise ValueError('Retained index missing')
 return result

def prior_check():
 if sha(PRIOR/'independent-receipt-audit-v1.json')!=AUDIT_SHA or sha(PRIOR/'plan.json')!=PRIOR_PLAN_SHA:raise ValueError('Prior independent audit or plan changed')
 audit=json.loads((PRIOR/'independent-receipt-audit-v1.json').read_bytes());plan=json.loads((PRIOR/'plan.json').read_bytes())
 for name,digest in audit['file_sha256'].items():
  if sha(PRIOR/name)!=digest:raise ValueError('Prior evidence changed: '+name)
 for c in audit['calls']:
  key=hashlib.sha256(c['identity'].encode()).hexdigest(); d=PRIOR/'calls'
  started=json.loads((d/(key+'.started.json')).read_bytes());finish=json.loads((d/(key+'.finished.json')).read_bytes())
  if started['identity']!=c['identity'] or sha(d/(key+'.request.json'))!=c['request_sha256'] or sha(d/(key+'.response.body'))!=c['response_sha256'] or finish['state']!=c['transport_state'] or finish['status_code']!=c['http_status']:raise ValueError('Prior provider receipt changed')
 if len(audit['calls'])!=11 or audit['calls'][-1]['http_status']!=400 or audit['calls'][-1]['transport_state']!='FAILED_HTTP':raise ValueError('400 trial is not independently reconciled')
 started=rows(PRIOR/'started.jsonl');trials=rows(PRIOR/'trials.jsonl');all_ids=plan['identities']
 if len(all_ids)!=12 or len({identity(x) for x in all_ids})!=12 or len(started)!=2 or len(trials)!=2:raise ValueError('Unexpected trial history')
 if [x['state'] for x in trials]!=['RETURNED','STOPPED'] or [identity(x) for x in started]!=[identity(x) for x in all_ids[:2]]:raise ValueError('Prior terminal trial identities differ')
 if any(x['transport_state'] not in ('COMPLETED','FAILED_HTTP') for x in audit['calls']):raise ValueError('UNKNOWN provider state prohibits continuation')
 if not old.GRANT.exists():raise ValueError('Original one-shot grant missing')
 grant=json.loads(old.GRANT.read_bytes())
 if grant!={'output':str(PRIOR),'plan_sha256':hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()}:raise ValueError('Prior one-shot grant mismatch')
 original=old.make_plan(); original['grant_available']=True
 # The frozen historical plan and this version may differ only in source hashes.
 compare={**original,'sources':plan['sources']}
 if compare!=plan:raise ValueError('Original corpus, models, configuration or price evidence changed')
 db=PRIOR/'isolated.db';wal=Path(str(db)+'-wal')
 if wal.exists() and wal.stat().st_size:raise ValueError('Prior database WAL nonempty')
 uri=db.resolve().as_uri()+'?mode=ro&immutable=1'
 with sqlite3.connect(uri,uri=True) as con:
  actual={a:(b,c) for a,b,c in con.execute('SELECT id,file_id,content FROM document_chunks')}
  chunks=json.loads(old.MANIFEST.read_bytes())['chunks'];expected={c['id']:(c['source_id'],c['content']) for c in chunks}
  if len(actual)!=80 or actual!=expected:raise ValueError('Prior DB corpus mismatch')
 index=tree_inventory(PRIOR/'chroma')
 with sqlite3.connect((PRIOR/'chroma/chroma.sqlite3').resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as con:
  if con.execute('select name,dimension from collections').fetchall()!=[('kb_m2_ab_kb',1024)]:raise ValueError('Retained Chroma collection or dimensions differ')
  docs=con.execute("select e.embedding_id,m.string_value from embeddings e join embedding_metadata m on e.id=m.id where m.key='chroma:document'").fetchall()
  if len(docs)!=80 or dict(docs)!={c['id']:c['content'] for c in chunks}:raise ValueError('Retained Chroma IDs or documents differ from frozen corpus')
 return plan,{'prior_db_sha256':sha(db),'prior_index_files':index,'prior_grant_sha256':sha(old.GRANT),'prior_receipts_sha256':AUDIT_SHA}

def make_plan():
 plan,proof=prior_check()
 return {'schema':'m2-hybrid-postfix-continuation/v1','purpose':'POSTFIX_NEW_VERSION_ALL_12','identities':[{**x,'run_identity':PREFIX+':'+':'.join(map(str,identity(x)))} for x in plan['identities']], 'original_plan_sha256':PRIOR_PLAN_SHA, **proof,'current_sources':old.snapshot(),'runner_sha256':sha(Path(__file__)),'old_fixed_search':plan['search'],'models':plan['models'],'max_calls':old.MAX_CALLS,'max_per_context':old.MAX_PER_CONTEXT,'max_output_tokens':old.MAX_OUTPUT,'model_calls':0,'actual_bill':'UNKNOWN','grant_available':not GRANT.exists(),'source_version':'post-fix snapshot; prior A is separate old-code result','reuse':'isolated copy of existing DB and index; no corpus re-embedding'}

def validate(plan):
 if plan!=make_plan() or not plan['grant_available']:raise ValueError('Plan changed or continuation already claimed')

def claim(plan,output):
 data=json.dumps({'plan_sha256':hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest(),'output':str(output)},sort_keys=True).encode()
 with GRANT.open('xb') as f:f.write(data);f.flush();os.fsync(f.fileno())
 return data

def copy_reuse(output,plan):
 source=PRIOR/'isolated.db';before=sha(source)
 if before!=plan['prior_db_sha256'] or tree_inventory(PRIOR/'chroma')!=plan['prior_index_files']:raise ValueError('Retained resources changed before copy')
 with sqlite3.connect(source.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as src,sqlite3.connect(output/'isolated.db') as dst:src.backup(dst)
 shutil.copytree(PRIOR/'chroma',output/'chroma',symlinks=False)
 if sha(source)!=before or tree_inventory(PRIOR/'chroma')!=plan['prior_index_files'] or tree_inventory(output/'chroma')!=plan['prior_index_files']:raise ValueError('Retained resources changed during copy')
 if sha(output/'isolated.db')!=before:raise ValueError('SQLite backup bytes differ from retained baseline')

class Guard(old.LimitedCalls):
 def __init__(self,path,plan,grant):super().__init__(path);self.proof,self.grant=plan,grant
 def begin(self,identity,request):
  try:
   if not identity.startswith(PREFIX+':') or old.snapshot()!=self.proof['current_sources'] or sha(Path(__file__))!=self.proof['runner_sha256'] or GRANT.read_bytes()!=self.grant:raise ValueError('Continuation source, identity or grant changed')
   _,prior=prior_check()
   if any(prior[k]!=self.proof[k] for k in prior):raise ValueError('Retained evidence or index changed')
   old.assert_empty_source_wal()
  except Exception as exc:
   self.stopped=True;raise old.guardmod.GuardStop('Continuation pre-send validation failed: '+type(exc).__name__) from None
  return super().begin(identity,request)

class Adapter(old.CappedAdapter):
 def _begin(self,request):return self.guard.begin(PREFIX+':'+self.context+':call-'+format(next(self.serial),'06d'),request)

async def run(plan,output,adapter,api_key):
 from app.services.memory import PreferenceScope,get_memory_service
 from app.services.trace_schema import capture_scope
 cases={c['qid']:c for c in json.loads(old.MANIFEST.read_bytes())['cases']+json.loads(old.SUPPLEMENT.read_bytes())['cases']};memory=get_memory_service()
 try:
  async with old.app.bound_provider(adapter,api_key):
   from app.services.vector_store import VectorStoreService
   if VectorStoreService().get_count(old.app.KB_ID)!=80:raise ValueError('Copied index count differs')
   for item in plan['identities']:
    rid=item['run_identity'];old.persist(output/'started.jsonl',item)
    scope=PreferenceScope(tenant_id='m2-hybrid-postfix',user_id=hashlib.sha256(rid.encode()).hexdigest());begin=time.monotonic();result={**item,'state':'STARTED'}
    try:
     with capture_scope(run_id=output.name,qid=item['qid'],attempt_id=rid,dataset_version='m2-20261104-dev-v3',capture_mode='BASIC'):
      result={**item,**await asyncio.wait_for(old.app.trial(cases[item['qid']],item['mode'],item['arm'],scope,adapter,on_event=lambda e:old.persist(output/'events.jsonl',{'run_identity':rid,**e})),480)}
     if result['state']!='RETURNED' or adapter.guard.stopped:raise old.guardmod.GuardStop('Trial incomplete')
    except BaseException as exc:
     result.update(state='STOPPED',error_type=type(exc).__name__);old.persist(output/'trials.jsonl',{**result,'elapsed_seconds':time.monotonic()-begin});raise
    old.persist(output/'trials.jsonl',{**result,'elapsed_seconds':time.monotonic()-begin})
 finally:await memory.client.aclose()

def execute(plan,output):
 import httpx
 validate(plan)
 key=os.getenv('M2_AB_API_KEY')
 if not key:raise ValueError('Cloud credential absent')
 output=Path(output).resolve()
 if output.parent!=VAR.resolve() or not output.name.startswith('m2-hybrid-postfix-run-') or output.exists():raise ValueError('Fresh private output required')
 if any(n=='app' or n.startswith('app.') for n in sys.modules):raise ValueError('Fresh process required')
 grant=claim(plan,output);output.mkdir();(output/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
 calls=Guard(output/'calls',plan,grant);network=old.http.BoundedNetwork();ai=httpx.AsyncHTTPTransport(retries=0,trust_env=False);si=httpx.HTTPTransport(retries=0,trust_env=False);adapter=Adapter(calls,old.NetworkTransport(ai,network),old.NetworkTransport(si,network));status={'state':'STOPPED','provider_bill':'UNKNOWN'}
 async def go(url):
  try:
   with network.installed(),old.restrict_loopback(url):await run(plan,output,adapter,key)
  finally:await ai.aclose();si.close()
 try:
  with old.base.isolated_redis(output) as url:
   old.app.configure_runtime(output,url,key);copy_reuse(output,plan);asyncio.run(go(url))
  status['state']='RECORDED_REQUIRES_REVIEW'
 except BaseException as exc:status['error_type']=type(exc).__name__
 finally:
  status['calls']=calls.count
  for row in old.usage_records(calls.directory):old.persist(output/'usage.jsonl',row)
  old.persist(output/'summary.jsonl',status)
 return status

def main():
 p=argparse.ArgumentParser();p.add_argument('mode',choices=('preflight','execute'));p.add_argument('--plan',required=True);p.add_argument('--output');a=p.parse_args()
 try:
  path=Path(a.plan).resolve()
  if path.parent!=VAR.resolve() or not path.name.startswith('m2-hybrid-postfix-'):raise ValueError('Private plan required')
  if a.mode=='preflight':
   plan=make_plan()
   with path.open('x',encoding='utf8') as f:json.dump(plan,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
   result={'state':'PASS','model_calls':0,'identities':len(plan['identities']),'grant_available':plan['grant_available']}
  else:
   if not a.output:raise ValueError('Output required')
   result=execute(json.loads(path.read_bytes()),a.output)
  print(json.dumps(result));return 0 if result['state'] in ('PASS','RECORDED_REQUIRES_REVIEW') else 1
 except Exception as exc:print(json.dumps({'state':'BLOCKED','error':str(exc)}));return 1
if __name__=='__main__':raise SystemExit(main())

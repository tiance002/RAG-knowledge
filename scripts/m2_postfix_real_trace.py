"""One fresh post-fix Agent HTTP trace; preflight and tests never call a model."""
import argparse, asyncio, hashlib, importlib.util, json, os, socket, sys
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from decimal import Decimal
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
P=ROOT/'var/preferences-acceptance'
def load(path):
 s=importlib.util.spec_from_file_location('postfix_legacy_http',path); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
old=load(ROOT/'scripts/m2_current_http_smoke.py')
base,cont=old.base,old.cont
recon=old.source.source.prior.recon
GRANT=P/'m2-postfix-real-agent.started'
REPORT=P/'m2-current-http-independent-reconciliation-v1.json'
REPORT_SHA='919b9a409a7ce57686c4cf23df369b4edffa096e46f293023c27691125eb25db'
REMAINING=Decimal('0.1892580')
QID='m2-postfix-real:quarterly-report-20261104-v1'
QUESTION='根据材料：How to prepare the quarterly progress report for each task, including schedule, completion percentage and current status? Please give a step-by-step explanation.'
IDENTITY={'qid':QID,'mode':'agent','arm':'D','repetition':1,'run_identity':'m2-postfix-real:quarterly-report-agent-D-v1'}
AUTH='m2-owner-20261104-postfix-agent-trace-v1'

@contextmanager
def block_ollama():
 original, original_ex=socket.socket.connect,socket.socket.connect_ex
 def check(address):
  if isinstance(address,tuple) and len(address)>1 and address[1]==11434:
   raise RuntimeError('Ollama port blocked')
 def connect(sock,address): check(address);return original(sock,address)
 def connect_ex(sock,address): check(address);return original_ex(sock,address)
 socket.socket.connect,socket.socket.connect_ex=connect,connect_ex
 try: yield
 finally: socket.socket.connect,socket.socket.connect_ex=original,original_ex

def sha(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def current_snapshot(): return recon.r.current.code_snapshot()
def historical_check():
 current=recon.r.current; original=current.code_snapshot
 proofs=json.loads((P/'telemetry-correction-20261104/source-hashes.json').read_bytes())
 expected={'backend/app/services/llm.py','backend/app/services/observability.py'}
 if set(proofs)!=expected: raise ValueError('Historical source proof paths changed')
 def historical_snapshot():
  result=original()
  for k,v in proofs.items():
   saved=P/'telemetry-correction-20261104'/('llm-before.py' if k.endswith('/llm.py') else 'observability-before.py')
   if sha(saved)!=v['frozen_manifest_sha256']: raise ValueError('Historical source proof mismatch')
   result[k]=v['frozen_manifest_sha256']
  return result
 current.code_snapshot=historical_snapshot
 try:
  spec=json.loads((P/'m2-current-http-real-v1/manifest.json').read_bytes())
  return old.validate(spec)
 finally: current.code_snapshot=original

def audit():
 if sha(REPORT)!=REPORT_SHA: raise ValueError('Independent report changed')
 report=json.loads(REPORT.read_bytes())
 for path,digest in report['artifact_sha256'].items():
  if sha(ROOT/path)!=digest: raise ValueError('Historical artifact changed: '+path)
 history,parent,fresh,prior,policy=historical_check()
 run=P/'m2-current-http-real-v1'
 trials=cont.indexed_trials((run/'trials.jsonl').read_bytes())
 calls,_=recon.audit_run(run,trials,cont.rows((run/'provider-ledger.jsonl').read_bytes()),fresh,5,'0.0162644')
 total=Decimal(prior['committed_cny'])+sum(Decimal(x['committed_cny']) for x in calls)
 if total!=Decimal('1.8107420') or report['global_committed_including_unknown_cny']!=str(total) or report['all_unknown_reserved_cny']!='0.099456': raise ValueError('Historical budget mismatch')
 fresh={**fresh,'dataset_version':'m2-postfix-real-20261104-v1','budget_cny':str(REMAINING),'cases':[{**fresh['cases'][0],'qid':QID,'question':QUESTION}]}
 policy=old.source.source.prior.safety.policy_resolution(QUESTION,{'output_preferences':['step_by_step']})
 if not policy['policy_active']: raise ValueError('Step preference inactive')
 return history,parent,fresh,policy

def make_spec():
 audit()
 return {'schema':'m2-postfix-real/v1','authorization_id':AUTH,'purpose':'ONE_POSTFIX_AGENT_TRACE','identities':[IDENTITY],'question':QUESTION,'remaining_cny':str(REMAINING),'prior_report_sha256':REPORT_SHA,'runner_sha256':sha(Path(__file__)),'current_snapshot':current_snapshot(),'cloud':'METADATA_ONLY_REQUIRED'}

def validate(spec):
 if spec!={'schema':'m2-postfix-real/v1','authorization_id':AUTH,'purpose':'ONE_POSTFIX_AGENT_TRACE','identities':[IDENTITY],'question':QUESTION,'remaining_cny':str(REMAINING),'prior_report_sha256':REPORT_SHA,'runner_sha256':sha(Path(__file__)),'current_snapshot':current_snapshot(),'cloud':'METADATA_ONLY_REQUIRED'}: raise ValueError('New contract or current source changed')
 return audit()

def preflight(spec):
 history,parent,fresh,policy=validate(spec)
 if not all(os.getenv(k) for k in ('LANGFUSE_PUBLIC_KEY','LANGFUSE_SECRET_KEY')): raise ValueError('Langfuse credentials absent; no sends')
 original_question=old.source.source.QUESTION
 try:
  old.source.source.QUESTION=QUESTION
  retrieval=old.source.source.retrieval_preflight(history,parent)
 finally: old.source.source.QUESTION=original_question
 return {'status':'PASS','model_calls':0,'http_requests':1,'controlled_prior_cny':'1.8107420','controlled_remaining_cny':str(REMAINING),'historical_unknown_reserved_cny':'0.099456','prior_401_bill':'UNKNOWN','grant_available':not GRANT.exists(),'policy':policy,'retrieval':retrieval,'cloud':'METADATA_ONLY_REQUIRED'}

def claim(spec,output):
 value=base.canonical({'authorization_id':AUTH,'output':str(output),'manifest_sha256':base.digest(base.canonical(spec))})
 with GRANT.open('xb') as f: f.write(value);f.flush();os.fsync(f.fileno())
 return value

class Transport(old.Transport):
 async def send(self,sender,client,request,*args,**kwargs):
  try:
   validate(self.spec)
   if GRANT.read_bytes()!=self.grant_bytes or self.limit!=REMAINING: raise ValueError('Grant or budget changed')
   if any(self.context.get(k)!=v for k,v in IDENTITY.items()): raise ValueError('Identity mismatch')
   p=self.output/'trials.jsonl'
   if p.exists() and any(x.get('run_identity')==IDENTITY['run_identity'] for x in cont.rows(p.read_bytes())): raise ValueError('Identity replay')
  except Exception as exc:
   self.stopped=True;self.diagnostic('PRE_SEND_VALIDATION_'+type(exc).__name__+': '+str(exc),request)
   raise base.GuardStop('Pre-send validation failed') from None
  try: return await base.BudgetTransport.send(self,sender,client,request,*args,**kwargs)
  except base.GuardStop as exc: self.diagnostic(str(exc),request);raise

def execute(spec,output):
 check=preflight(spec)
 if not check['grant_available']: raise ValueError('Run already claimed')
 if not os.getenv('M2_AB_API_KEY'): raise ValueError('Model key absent; no sends')
 if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules: raise ValueError('Fresh process required')
 output=base.private_path(output)
 if output.parent!=P or not output.name.startswith('m2-postfix-real-') or output.exists(): raise ValueError('Fresh private output required')
 history,parent,fresh,_=validate(spec); grant=claim(spec,output); guard=None; previous=Path.cwd()
 result={'status':'NOT_RUN','cloud_readback':'NOT_RUN','prior_401_bill':'UNKNOWN','prior_controlled_cny':'1.8107420'}
 try:
  output.mkdir(); (output/'manifest.json').write_bytes(base.canonical(spec));(output/'preflight.json').write_bytes(base.canonical(check));(output/'dataset.json').write_bytes(base.canonical(fresh))
  cont.backup_reuse(history,parent,output/'isolated.db')
  guard=Transport(fresh,output,spec,grant)
  with base.isolated_redis(output) as url:
   base.configure_runtime(fresh,output,url);old.configure_observability(True)
   with (output/'runtime.log').open('w',encoding='utf8') as log,redirect_stdout(log),redirect_stderr(log):
    with block_ollama(), guard.installed():
     old.source.source.prior.safety.assert_runtime_isolation(output,url)
     asyncio.run(old.serve(fresh,fresh['cases'][0],spec,output,guard))
  result['status']='RECORDED_REQUIRES_INDEPENDENT_REVIEW'
 except base.GuardStop as exc: result.update(status='STOPPED',reason=str(exc))
 except Exception as exc: result.update(status='FAILED',error_type=type(exc).__name__)
 finally:
  os.chdir(previous)
  if 'app.services.observability' in sys.modules: sys.modules['app.services.observability'].flush_observations();result['cloud_flush']='REQUESTED; READBACK REQUIRED'
  if guard: result.update(guard.summary()); result['global_controlled_cny']=str(Decimal('1.8107420')+guard.committed)
  if output.exists(): (output/'summary.json').write_bytes(base.canonical(result))
 return result

def main():
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['manifest','preflight','execute']);p.add_argument('--manifest',required=True);p.add_argument('--output');a=p.parse_args()
 try:
  path=base.private_path(a.manifest)
  if path.parent!=P or not path.name.startswith('m2-postfix-real-'): raise ValueError('Private manifest required')
  if a.mode=='manifest':
   data=make_spec()
   with path.open('xb') as f: f.write(base.canonical(data));f.flush();os.fsync(f.fileno())
   result={'status':'PASS','model_calls':0}
  else:
   spec=json.loads(path.read_bytes())
   if not a.output: raise ValueError('Output required')
   output=base.private_path(a.output)
   if output.parent!=P or not output.name.startswith('m2-postfix-real-'): raise ValueError('Private output required')
   if a.mode=='preflight':
    result=preflight(spec)
    with output.open('xb') as f: f.write(base.canonical(result));f.flush();os.fsync(f.fileno())
   else: result=execute(spec,output)
  print(json.dumps(result));return 0 if result['status'] in ('PASS','RECORDED_REQUIRES_INDEPENDENT_REVIEW') else 1
 except Exception as e: print(json.dumps({'status':'BLOCKED','error':str(e)}));return 1
if __name__=='__main__': raise SystemExit(main())

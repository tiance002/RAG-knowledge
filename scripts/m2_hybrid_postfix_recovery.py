"""Fresh one-shot recovery only after prior continuation proved zero sends."""
import hashlib,importlib.util,json,os,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];VAR=ROOT/'var/preferences-acceptance'
s=importlib.util.spec_from_file_location('hybrid_postfix_zero_send_base',ROOT/'scripts/m2_hybrid_postfix_continuation.py');base=importlib.util.module_from_spec(s);s.loader.exec_module(base)
base.GRANT=VAR/'m2-hybrid-postfix-continuation-v5.started'
base.PREFIX='m2-hybrid-postfix-v5'
FAILED=VAR/'m2-hybrid-postfix-run-v1'
FAILED_GRANT=VAR/'m2-hybrid-postfix-continuation-v1.started'
FAILED_PLAN=VAR/'m2-hybrid-postfix-continuation-plan-v2.json'
FAILED_V2=VAR/'m2-hybrid-postfix-run-v2'
FAILED_V2_GRANT=VAR/'m2-hybrid-postfix-continuation-v2.started'
FAILED_V2_PLAN=VAR/'m2-hybrid-postfix-recovery-plan-v1.json'
RUN_V3=VAR/'m2-hybrid-postfix-run-v3'
RUN_V3_GRANT=VAR/'m2-hybrid-postfix-continuation-v3.started'
RUN_V3_PLAN=VAR/'m2-hybrid-postfix-recovery-plan-v2.json'
RUN_V3_AUDIT=VAR/'m2-hybrid-postfix-v3-independent-audit-v1.json'
RUN_V3_AUDIT_SHA='1d74f3e5626d9e51818b91e29920d0441d2119beefb94cf478a6bd0a88becde2'
RUN_V4=VAR/'m2-hybrid-postfix-run-v4'
RUN_V4_GRANT=VAR/'m2-hybrid-postfix-continuation-v4.started'
RUN_V4_PLAN=VAR/'m2-hybrid-postfix-recovery-plan-v3.json'
RUN_V4_AUDIT=VAR/'m2-hybrid-postfix-v4-independent-audit-v1.json'
RUN_V4_AUDIT_SHA='926103a1697cdd34577f663065f055a04107161249b4e9e37d6796ca390fe9d6'
MAX_NEW_CALLS=34  # 80 total minus original 11, v3's 6 and v4's 29 attempts.
original_prior_check=base.prior_check

def verify_zero_send():
 summary=base.rows(FAILED/'summary.jsonl')
 if summary!=[{'calls':0,'error_type':'JSONDecodeError','provider_bill':'UNKNOWN','state':'STOPPED'}]:raise ValueError('Prior failure not independently proven zero sends')
 expected={'plan.json','summary.jsonl','calls'}
 if {x.name for x in FAILED.iterdir()}!=expected or not (FAILED/'calls').is_dir() or any((FAILED/'calls').iterdir()):raise ValueError('Prior run has unexpected evidence or possible sends')
 plan=json.loads((FAILED/'plan.json').read_bytes())
 if (FAILED/'plan.json').read_bytes()!=FAILED_PLAN.read_bytes():raise ValueError('Prior recorded plan differs from frozen plan')
 grant=json.loads(FAILED_GRANT.read_bytes());digest=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
 if grant!={'output':str(FAILED),'plan_sha256':digest}:raise ValueError('Prior burned grant differs')
 return {'zero_send_prior_plan_sha256':base.sha(FAILED_PLAN),'zero_send_prior_summary_sha256':base.sha(FAILED/'summary.jsonl'),
         'zero_send_prior_grant_sha256':base.sha(FAILED_GRANT),'zero_send_wrapper_sha256':base.sha(Path(__file__))}

def prior_check():
 plan,proof=original_prior_check();return plan,{**proof,**verify_zero_send(),**verify_v2_zero_send(),**verify_v3(),**verify_v4()}
base.prior_check=prior_check

def verify_v2_zero_send():
 summary=base.rows(FAILED_V2/'summary.jsonl');started=base.rows(FAILED_V2/'started.jsonl');trials=base.rows(FAILED_V2/'trials.jsonl')
 if summary!=[{'calls':0,'error_type':'GuardStop','provider_bill':'UNKNOWN','state':'STOPPED'}]:raise ValueError('Prior v2 summary does not prove zero sends')
 if not (FAILED_V2/'calls').is_dir() or any((FAILED_V2/'calls').iterdir()) or len(started)!=1 or len(trials)!=1:raise ValueError('Prior v2 has unresolved provider call or unexpected identities')
 plan=json.loads((FAILED_V2/'plan.json').read_bytes())
 if (FAILED_V2/'plan.json').read_bytes()!=FAILED_V2_PLAN.read_bytes() or started[0]!=plan['identities'][0]:raise ValueError('Prior v2 plan or identity changed')
 trial=trials[0]
 if trial.get('run_identity')!=started[0]['run_identity'] or trial.get('state')!='STOPPED' or trial.get('error_type')!='GuardStop':raise ValueError('Prior v2 trial state changed')
 grant=json.loads(FAILED_V2_GRANT.read_bytes());digest=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
 if grant!={'output':str(FAILED_V2),'plan_sha256':digest}:raise ValueError('Prior v2 grant changed')
 return {'v2_zero_send_plan_sha256':base.sha(FAILED_V2_PLAN),'v2_zero_send_summary_sha256':base.sha(FAILED_V2/'summary.jsonl'),
         'v2_zero_send_started_sha256':base.sha(FAILED_V2/'started.jsonl'),'v2_zero_send_trials_sha256':base.sha(FAILED_V2/'trials.jsonl'),
         'v2_zero_send_grant_sha256':base.sha(FAILED_V2_GRANT)}

def verify_v3():
 if base.sha(RUN_V3_AUDIT)!=RUN_V3_AUDIT_SHA:raise ValueError('Independent v3 audit changed')
 audit=json.loads(RUN_V3_AUDIT.read_bytes());plan=json.loads((RUN_V3/'plan.json').read_bytes())
 if (RUN_V3/'plan.json').read_bytes()!=RUN_V3_PLAN.read_bytes():raise ValueError('v3 recorded plan changed')
 if plan['current_sources']!=base.old.snapshot():raise ValueError('Application source differs from v3')
 for name,digest in audit['file_sha256'].items():
  if base.sha(RUN_V3/name)!=digest:raise ValueError('v3 evidence changed')
 started=base.rows(RUN_V3/'started.jsonl');trials=base.rows(RUN_V3/'trials.jsonl')
 if len(plan['identities'])!=12 or started!=plan['identities'][:2] or [x['state'] for x in trials]!=['RETURNED','STOPPED']:
  raise ValueError('v3 started identities do not match audited prefix')
 if len(audit['calls'])!=6 or audit['provider_calls']!=6 or audit['unstarted']!=10:raise ValueError('v3 provider audit differs')
 if sum(x['state']=='COMPLETED' for x in audit['calls'])!=5 or sum(x['state']=='FAILED_HTTP' and x['http_status']==503 for x in audit['calls'])!=1:raise ValueError('v3 503 must stay UNKNOWN and excluded')
 calls=RUN_V3/'calls'
 if len(list(calls.glob('*.started.json')))!=6 or len(list(calls.glob('*.finished.json')))!=6:raise ValueError('Unexpected v3 provider attempt')
 for c in audit['calls']:
  stem=hashlib.sha256(c['identity'].encode()).hexdigest();s=json.loads((calls/(stem+'.started.json')).read_bytes());f=json.loads((calls/(stem+'.finished.json')).read_bytes())
  if s['identity']!=c['identity'] or base.sha(calls/(stem+'.request.json'))!=c['request_sha256'] or base.sha(calls/(stem+'.response.body'))!=c['response_sha256'] or f['state']!=c['state'] or f['status_code']!=c['http_status']:raise ValueError('v3 provider evidence changed')
 grant=json.loads(RUN_V3_GRANT.read_bytes());digest=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
 if grant!={'output':str(RUN_V3),'plan_sha256':digest}:raise ValueError('v3 grant changed')
 return {'v3_audit_sha256':RUN_V3_AUDIT_SHA,'v3_grant_sha256':base.sha(RUN_V3_GRANT),
         'v3_plan_sha256':base.sha(RUN_V3_PLAN),'v3_started_sha256':base.sha(RUN_V3/'started.jsonl'),
         'v3_excluded_identities':[{k:x[k] for k in ('qid','mode','arm','repetition')} for x in started],
         'v3_unknown_503':'RETAINED_DO_NOT_REPLAY'}

def verify_v4():
 if base.sha(RUN_V4_AUDIT)!=RUN_V4_AUDIT_SHA:raise ValueError('Independent v4 audit changed')
 audit=json.loads(RUN_V4_AUDIT.read_bytes());plan=json.loads((RUN_V4/'plan.json').read_bytes())
 if (RUN_V4/'plan.json').read_bytes()!=RUN_V4_PLAN.read_bytes():raise ValueError('v4 recorded plan changed')
 if plan['current_sources']!=base.old.snapshot():raise ValueError('Application source differs from v4')
 for name,digest in audit['file_sha256'].items():
  if base.sha(RUN_V4/name)!=digest:raise ValueError('v4 evidence changed')
 started=base.rows(RUN_V4/'started.jsonl');trials=base.rows(RUN_V4/'trials.jsonl')
 if len(plan['identities'])!=10 or started!=plan['identities'][:7] or [x['state'] for x in trials]!=['RETURNED']*6+['STOPPED']:
  raise ValueError('v4 started identities do not match audited prefix')
 if audit['attempts']!=29 or audit['completed']!=28 or len(audit['unknown'])!=1 or len(audit['unstarted'])!=3:
  raise ValueError('v4 provider audit differs')
 calls=RUN_V4/'calls'
 if len(list(calls.glob('*.started.json')))!=29 or len(list(calls.glob('*.finished.json')))!=29:raise ValueError('Unexpected v4 provider attempt')
 for c in audit['calls']:
  stem=hashlib.sha256(c['identity'].encode()).hexdigest();s=json.loads((calls/(stem+'.started.json')).read_bytes());f=json.loads((calls/(stem+'.finished.json')).read_bytes());response=calls/(stem+'.response.body')
  if s['identity']!=c['identity'] or base.sha(calls/(stem+'.request.json'))!=c['request_sha256'] or f['state']!=c['state'] or f['status_code']!=c['status']:raise ValueError('v4 provider evidence changed')
  if (base.sha(response) if response.exists() else None)!=c['response_sha256']:raise ValueError('v4 response evidence changed')
 grant=json.loads(RUN_V4_GRANT.read_bytes());digest=hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()
 if grant!={'output':str(RUN_V4),'plan_sha256':digest}:raise ValueError('v4 grant changed')
 return {'v4_audit_sha256':RUN_V4_AUDIT_SHA,'v4_grant_sha256':base.sha(RUN_V4_GRANT),
         'v4_plan_sha256':base.sha(RUN_V4_PLAN),'v4_started_sha256':base.sha(RUN_V4/'started.jsonl'),
         'v4_excluded_identities':[{k:x[k] for k in ('qid','mode','arm','repetition')} for x in started],
         'v4_unknown_connect_error':'RETAINED_DO_NOT_REPLAY'}

original_make_plan=base.make_plan
def remaining_plan():
 plan=original_make_plan();excluded={base.identity(x) for x in plan['v3_excluded_identities']+plan['v4_excluded_identities']}
 plan['identities']=[x for x in plan['identities'] if base.identity(x) not in excluded]
 if len(plan['identities'])!=3:raise ValueError('Expected exactly 3 never-started identities')
 plan.update(purpose='POSTFIX_SAME_VERSION_ONLY_3_NEVER_STARTED',max_calls=MAX_NEW_CALLS,
             call_budget='80 global minus 11 original, 6 v3 and 29 v4 attempts; UNKNOWN retained')
 return plan
base.make_plan=remaining_plan

OriginalGuard=base.Guard
class RemainingGuard(OriginalGuard):
 def __init__(self,*args,**kwargs):
  import threading
  super().__init__(*args,**kwargs);self.continuation_lock=threading.Lock();self.continuation_attempts=0
 def begin(self,identity,request):
  with self.continuation_lock:
   if self.continuation_attempts>=MAX_NEW_CALLS:
    self.stopped=True;raise base.old.guardmod.GuardStop('Combined 80-call limit reached')
   self.continuation_attempts+=1
  return super().begin(identity,request)
base.Guard=RemainingGuard

def verify_output_encoding():
 import codecs
 for stream in (sys.stdout,sys.stderr):
  try: encoding=codecs.lookup(stream.encoding or '').name
  except LookupError:raise ValueError('UTF-8 stdout and stderr required before grant') from None
  if encoding!='utf-8':raise ValueError('UTF-8 stdout and stderr required before grant')

def verify_redis_env():
 raw=os.getenv('RAG_TEST_REDIS_CMD')
 if not raw:raise ValueError('RAG_TEST_REDIS_CMD must be a JSON argv before grant')
 try:args=json.loads(raw)
 except json.JSONDecodeError:raise ValueError('RAG_TEST_REDIS_CMD must be a JSON argv before grant') from None
 binary=(VAR/'redis-test-runtime.txt').read_text(encoding='utf8').strip()
 if args!=['wsl.exe','-d','Ubuntu','--exec',binary]:raise ValueError('RAG_TEST_REDIS_CMD must match the verified isolated Redis argv')
 return args

def execute(plan,output):
 verify_zero_send();verify_v2_zero_send();verify_v3();verify_v4();verify_redis_env();verify_output_encoding();return base.execute(plan,output)

def main():
 import argparse
 p=argparse.ArgumentParser();p.add_argument('mode',choices=['preflight','execute']);p.add_argument('--plan',required=True);p.add_argument('--output');args=p.parse_args()
 try:
  path=Path(args.plan).resolve()
  if path.parent!=VAR.resolve() or not path.name.startswith('m2-hybrid-postfix-recovery-'):raise ValueError('Private recovery plan required')
  if args.mode=='preflight':
   result=base.make_plan()
   with path.open('x',encoding='utf8') as f:json.dump(result,f,ensure_ascii=False,indent=2);f.flush();os.fsync(f.fileno())
   value={'state':'PASS','model_calls':0,'identities':len(result['identities']),'zero_send_previous_run':'VERIFIED','grant_available':result['grant_available']}
  else:
   if not args.output:raise ValueError('Fresh output required')
   value=execute(json.loads(path.read_bytes()),args.output)
  print(json.dumps(value));return 0 if value['state'] in ('PASS','RECORDED_REQUIRES_REVIEW') else 1
 except Exception as exc:print(json.dumps({'state':'BLOCKED','error':str(exc)}));return 1
if __name__=='__main__':raise SystemExit(main())

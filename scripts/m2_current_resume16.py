"""Continue only 16 identities never started in either frozen M2 run."""
import argparse
import asyncio
from contextlib import contextmanager, redirect_stdout, redirect_stderr
from decimal import Decimal
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('m2_resume_frozen',ROOT/'scripts/m2_current_resume.py')
r=importlib.util.module_from_spec(s);s.loader.exec_module(r)
base,cont=r.base,r.cont
OLD=ROOT/'var/preferences-acceptance/m2-current-resume-real-v4'
OLDGRANT=ROOT/'var/preferences-acceptance/m2-current-resume-once.started'
GRANT=ROOT/'var/preferences-acceptance/m2-current-resume16-once.started'

def inspect(spec):
    expected={str(p.relative_to(ROOT)).replace('\\','/'):r.sha(p) for p in [Path(__file__),Path(r.__file__),OLD/'manifest.json',OLD/'dataset.json',OLD/'summary.json',OLD/'trials.jsonl',OLD/'provider-ledger.jsonl',OLDGRANT]}
    if spec.get('files')!=expected: raise ValueError('Resume evidence or source changed')
    oldspec=json.loads((OLD/'manifest.json').read_bytes())
    audit=r.inspect(oldspec)
    grant=json.loads(OLDGRANT.read_bytes())
    if grant != {'manifest_sha256':base.digest(base.canonical(oldspec)),'output':str(OLD)}: raise ValueError('Resume grant mismatch')
    original=json.loads((r.RUN/'manifest.json').read_bytes())
    history,parent,fresh,budget,plan=r.current.validate(original)
    if json.loads((OLD/'dataset.json').read_bytes()) != r.current.dataset_snapshot(fresh): raise ValueError('Resume dataset mismatch')
    summary=json.loads((OLD/'summary.json').read_bytes())
    if summary.get('status')!='STOPPED' or summary.get('lease')!='RETAINED_PENDING_RECONCILIATION' or summary.get('recorded_trials')!=6: raise ValueError('Resume final status mismatch')
    trials=cont.indexed_trials((OLD/'trials.jsonl').read_bytes())
    records=cont.rows((OLD/'provider-ledger.jsonl').read_bytes())
    planned={cont.identity({'qid':c['qid'],'mode':m,'arm':a,'repetition':q}):(c,m,q,a,p) for c,m,q,a,p in plan}
    oldallowed={tuple(x) for x in audit['remaining_identities']}
    if len(trials)!=6 or not set(trials)<=oldallowed: raise ValueError('Resume identities invalid')
    latest={}
    for row in records:
        n=row['call_id']
        if n not in latest:
            if row['state']!='UNKNOWN_PENDING': raise ValueError('Missing reservation')
            latest[n]=row
        else:
            prev=latest[n]
            if prev['state']!='UNKNOWN_PENDING' or row['state']!='COMPLETED' or any(row.get(k)!=prev.get(k) for k in prev if k!='state'): raise ValueError('Invalid transition')
            latest[n]=row
    if set(latest)!=set(range(1,15)): raise ValueError('Unexpected call count')
    ip=Decimal(str(fresh['pricing']['input_cny_per_million'])); op=Decimal(str(fresh['pricing']['output_cny_per_million']))
    cost=Decimal(0); ids=set()
    for n,row in latest.items():
        key=cont.identity(row); usage=row['usage']; a,b=usage['prompt_tokens'],usage['completion_tokens']
        if key not in trials or n not in trials[key]['provider_call_ids'] or row['requested_model']!=fresh['model'] or row['actual_model']!=fresh['model'] or row['output_token_bound']!=fresh['max_output_tokens'] or type(a)!=int or type(b)!=int or not 0<=a<=row['input_token_bound'] or not 0<=b<=row['output_token_bound'] or r.sha(OLD/f'request-{n:04d}.json')!=row['request_sha256']: raise ValueError('Resume call attribution invalid')
        reserve=(Decimal(row['input_token_bound'])*ip+Decimal(row['output_token_bound'])*op)/1000000
        actual=(Decimal(a)*ip+Decimal(b)*op)/1000000
        if reserve!=Decimal(row['reservation_cny']) or actual!=Decimal(row['price_based_cost_cny']): raise ValueError('Resume price mismatch')
        pid=row.get('provider_request_id')
        if pid and pid in ids: raise ValueError('Repeated provider id')
        if pid: ids.add(pid)
        cost+=actual
    prior_ids={x.get('provider_request_id') for x in cont.rows((r.RUN/'provider-ledger.jsonl').read_bytes()) if x.get('provider_request_id')}
    if ids & prior_ids: raise ValueError('Provider id collision')
    if cost!=Decimal('0.0574744') or Decimal(summary['committed_including_unknown_cny'])!=cost or summary.get('unknown_calls')!=0 or summary.get('completed_calls')!=14: raise ValueError('Resume accounting mismatch')
    remaining=[k for k in planned if k in oldallowed and k not in trials]
    if len(remaining)!=16: raise ValueError('Wrong remaining identity count')
    total=Decimal(audit['committed_cny'])+cost
    if total!=Decimal('1.6062196'): raise ValueError('Combined budget mismatch')
    return {'status':'PASS','model_calls':0,'started_identities':80,'planned_trials':16,'prior_unknown_171_reservation_cny':audit['unknown_reservation_cny'],'committed_cny':str(total),'remaining_cny':str(Decimal('2')-total),'remaining_identities':remaining}

def no_writers():
    r.no_live_original()
    command="$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"; if (@($p | Where-Object { !$_.CommandLine }).Count) { exit 3 }; if (@($p | Where-Object { $_.CommandLine -match 'm2_current_resume.py' }).Count) { exit 4 }"
    if subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,timeout=15).returncode: raise ValueError('Cannot exclude previous writer')

class Transport(base.BudgetTransport):
    def __init__(self,fresh,output,spec,allowed):
        super().__init__(fresh,output); self.spec=spec; self.allowed=set(allowed); self.grant_bytes=None
    def diagnostic(self,code,request=None):
        result={'code':code,'identity':self.context}
        if request is not None:
            try:
                payload=json.loads(request.content); cap=self.manifest['max_output_tokens']; payload['max_tokens']=min(payload.get('max_tokens',cap),cap)
                if 'max_completion_tokens' in payload: payload['max_completion_tokens']=min(payload['max_completion_tokens'],cap)
                if payload.get('stream'): payload['stream_options']={**payload.get('stream_options',{}),'include_usage':True}
                body=base.canonical(payload); result.update(request_sha256=base.digest(body),reservation_cny=str(self.cost(len(body)*4+4096,cap)))
            except Exception: result['reservation_cny']='UNAVAILABLE'
        path=self.output/'guard-diagnostics.jsonl'
        with path.open('ab') as f: f.write(base.canonical(result)+b'\n'); f.flush(); os.fsync(f.fileno())
    @contextmanager
    def installed(self):
        import httpx
        with super().installed():
            a,b=httpx.AsyncClient.send,httpx.Client.send
            async def async_guard(*args,**kwargs):
                try: return await a(*args,**kwargs)
                except base.GuardStop as exc:
                    if str(exc)=='Unbound asynchronous HTTP client blocked': self.diagnostic(str(exc))
                    raise
            def sync_guard(*args,**kwargs):
                try: return b(*args,**kwargs)
                except base.GuardStop as exc:
                    self.diagnostic(str(exc)); raise
            httpx.AsyncClient.send,httpx.Client.send=async_guard,sync_guard
            try: yield
            finally: httpx.AsyncClient.send,httpx.Client.send=a,b
    async def send(self,sender,client,request,*args,**kwargs):
        stage='HISTORY'
        try:
            audit=inspect(self.spec)
            stage='GRANT_OR_BUDGET'
            if self.grant_bytes is None or GRANT.read_bytes()!=self.grant_bytes or self.limit!=Decimal(audit['remaining_cny']): raise ValueError('grant_or_budget')
            stage='IDENTITY'
            if cont.identity(self.context) not in self.allowed: raise ValueError('identity')
            stage='TRIAL_RECORD'
            p=self.output/'trials.jsonl'
            if p.exists() and cont.identity(self.context) in cont.indexed_trials(p.read_bytes()): raise ValueError('already_recorded')
            stage='PROCESS'
            no_writers()
        except Exception as exc:
            self.stopped=True; self.diagnostic('PRE_SEND_'+stage+'_'+type(exc).__name__); raise base.GuardStop('Resume16 pre-send validation failed') from None
        try: return await super().send(sender,client,request,*args,**kwargs)
        except base.GuardStop as exc:
            self.diagnostic(str(exc),request); raise

def execute(spec,output):
    audit=inspect(spec); no_writers()
    if not os.getenv('M2_AB_API_KEY'): raise ValueError('Credential missing')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules: raise ValueError('Fresh process required')
    history,parent,fresh,_,plan=r.current.validate(json.loads((r.RUN/'manifest.json').read_bytes()))
    fresh={**fresh,'budget_cny':audit['remaining_cny']}; allowed={tuple(x) for x in audit['remaining_identities']}
    selected=[x for x in plan if cont.identity({'qid':x[0]['qid'],'mode':x[1],'repetition':x[2],'arm':x[3]}) in allowed]
    if len(selected)!=16: raise ValueError('Plan mismatch')
    output=base.private_path(output)
    if output.parent!=ROOT/'var/preferences-acceptance' or not output.name.startswith('m2-current-resume16-'): raise ValueError('Invalid output')
    output.mkdir(exist_ok=False)
    (output/'manifest.json').write_bytes(base.canonical(spec)); (output/'preflight.json').write_bytes(base.canonical(audit)); (output/'dataset.json').write_bytes(base.canonical(r.current.dataset_snapshot(fresh)))
    guard=None; oldcwd=Path.cwd()
    try:
        cont.backup_reuse(history,parent,output/'isolated.db')
        guard=Transport(fresh,output,spec,allowed)
        with base.isolated_redis(output) as redis_url:
            base.configure_runtime(fresh,output,redis_url)
            with (output/'runtime.log').open('w',encoding='utf8') as log, redirect_stdout(log),redirect_stderr(log),guard.installed():
                cont.safety.assert_runtime_isolation(output,redis_url); inspect(spec); no_writers()
                with GRANT.open('xb') as f: f.write(base.canonical({'manifest_sha256':base.digest(base.canonical(spec)),'output':str(output)})); f.flush(); os.fsync(f.fileno())
                guard.grant_bytes=GRANT.read_bytes(); original=base.trial_plan
                try:
                    base.trial_plan=lambda _:iter(selected); asyncio.run(base.run_trials(fresh,output,guard))
                finally: base.trial_plan=original
        status='RECORDED_REQUIRES_EVIDENCE_REVIEW'; reason=None
    except base.GuardStop as exc: status='STOPPED'; reason=str(exc)
    except Exception as exc: status='FAILED'; reason=type(exc).__name__
    finally: os.chdir(oldcwd)
    summary={'status':status,'reason':reason,'prior_committed_cny':audit['committed_cny'],'lease':'RETAINED_PENDING_RECONCILIATION','planned_trials':16}
    if guard: summary.update(guard.summary()); summary['global_committed_including_unknown_cny']=str(Decimal(audit['committed_cny'])+guard.committed)
    p=output/'trials.jsonl'; summary['recorded_trials']=len(cont.indexed_trials(p.read_bytes())) if p.exists() else 0
    if summary['recorded_trials']!=16 and status=='RECORDED_REQUIRES_EVIDENCE_REVIEW': summary['status']='STOPPED'
    (output/'summary.json').write_bytes(base.canonical(summary)); return summary

def main():
    p=argparse.ArgumentParser(); p.add_argument('mode',choices=['preflight','execute']); p.add_argument('--manifest',required=True);p.add_argument('--output'); a=p.parse_args()
    try:
        spec=json.loads(Path(a.manifest).read_bytes()); result=inspect(spec) if a.mode=='preflight' else execute(spec,a.output)
        print(json.dumps(result,ensure_ascii=False)); return 0 if result['status'] in ('PASS','RECORDED_REQUIRES_EVIDENCE_REVIEW') else 1
    except Exception as exc: print(json.dumps({'status':'BLOCKED','error':str(exc),'model_calls':0})); return 1
if __name__=='__main__': raise SystemExit(main())

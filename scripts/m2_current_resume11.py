"""Only 11 never-started M2 identities with conservative budget skips."""
import argparse, asyncio, importlib.util, json, os, subprocess, sys
from decimal import Decimal
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
ROOT=Path(__file__).resolve().parents[1]
s=importlib.util.spec_from_file_location('frozen16',ROOT/'scripts/m2_current_resume16.py');old=importlib.util.module_from_spec(s);s.loader.exec_module(old)
base,cont=old.base,old.cont
PRIOR=ROOT/'var/preferences-acceptance/m2-current-resume16-real-v2'
PRIOR_GRANT=ROOT/'var/preferences-acceptance/m2-current-resume16-once.started'
GRANT=ROOT/'var/preferences-acceptance/m2-current-resume11-once.started'

def inspect(spec):
    paths=[Path(__file__),Path(old.__file__),PRIOR/'manifest.json',PRIOR/'dataset.json',PRIOR/'summary.json',PRIOR/'trials.jsonl',PRIOR/'provider-ledger.jsonl',PRIOR/'guard-diagnostics.jsonl',PRIOR_GRANT]
    if spec.get('files')!={str(p.relative_to(ROOT)).replace('\\','/'):old.r.sha(p) for p in paths}: raise ValueError('Frozen resume16 evidence changed')
    prior_spec=json.loads((PRIOR/'manifest.json').read_bytes()); audit=old.inspect(prior_spec)
    if json.loads(PRIOR_GRANT.read_bytes())!={'manifest_sha256':base.digest(base.canonical(prior_spec)),'output':str(PRIOR)}: raise ValueError('Resume16 grant mismatch')
    history,parent,fresh,_,plan=old.r.current.validate(json.loads((old.r.RUN/'manifest.json').read_bytes()))
    if json.loads((PRIOR/'dataset.json').read_bytes())!=old.r.current.dataset_snapshot(fresh): raise ValueError('Dataset mismatch')
    summary=json.loads((PRIOR/'summary.json').read_bytes())
    if summary.get('status')!='STOPPED' or summary.get('lease')!='RETAINED_PENDING_RECONCILIATION' or summary.get('recorded_trials')!=5 or summary.get('completed_calls')!=9 or summary.get('unknown_calls')!=0: raise ValueError('Resume16 status mismatch')
    trials=cont.indexed_trials((PRIOR/'trials.jsonl').read_bytes()); allowed={tuple(i) for i in audit['remaining_identities']}
    if len(trials)!=5 or not set(trials)<=allowed: raise ValueError('Resume16 trial mismatch')
    records=cont.rows((PRIOR/'provider-ledger.jsonl').read_bytes()); latest={}
    for row in records:
        n=row['call_id']
        if n not in latest:
            if row['state']!='UNKNOWN_PENDING': raise ValueError('Reservation missing')
            latest[n]=row
        else:
            p=latest[n]
            if p['state']!='UNKNOWN_PENDING' or row['state']!='COMPLETED' or any(p.get(k)!=row.get(k) for k in p if k!='state'): raise ValueError('Provider transition invalid')
            latest[n]=row
    if set(latest)!=set(range(1,10)): raise ValueError('Provider count mismatch')
    ip=Decimal(str(fresh['pricing']['input_cny_per_million'])); op=Decimal(str(fresh['pricing']['output_cny_per_million'])); cost=Decimal(0); ids=set()
    for n,row in latest.items():
        key=cont.identity(row); u=row['usage']; a,b=u['prompt_tokens'],u['completion_tokens']
        if key not in trials or n not in trials[key]['provider_call_ids'] or row['requested_model']!=fresh['model'] or row['actual_model']!=fresh['model'] or row['output_token_bound']!=fresh['max_output_tokens'] or type(a)!=int or type(b)!=int or not 0<=a<=row['input_token_bound'] or not 0<=b<=row['output_token_bound'] or old.r.sha(PRIOR/f'request-{n:04d}.json')!=row['request_sha256']: raise ValueError('Call attribution invalid')
        reserve=(Decimal(row['input_token_bound'])*ip+Decimal(row['output_token_bound'])*op)/1000000; c=(Decimal(a)*ip+Decimal(b)*op)/1000000
        if reserve!=Decimal(row['reservation_cny']) or c!=Decimal(row['price_based_cost_cny']): raise ValueError('Cost mismatch')
        pid=row.get('provider_request_id')
        if pid and pid in ids: raise ValueError('Duplicate provider id')
        if pid: ids.add(pid)
        cost+=c
    previous_ids={x.get('provider_request_id') for path in (old.r.RUN,old.OLD) for x in cont.rows((path/'provider-ledger.jsonl').read_bytes()) if x.get('provider_request_id')}
    if ids & previous_ids: raise ValueError('Provider id collision')
    if cost!=Decimal('0.0394604') or Decimal(summary['committed_including_unknown_cny'])!=cost: raise ValueError('Resume16 cost mismatch')
    diagnostics=cont.rows((PRIOR/'guard-diagnostics.jsonl').read_bytes())
    if len(diagnostics)!=1 or not diagnostics[0].get('code','').startswith('BUDGET_BLOCKED:') or cont.identity(diagnostics[0]['identity']) not in trials or Decimal(diagnostics[0]['reservation_cny'])<=Decimal(audit['remaining_cny'])-cost: raise ValueError('Prior budget stop mismatch')
    rem=[k for k in allowed if k not in trials]
    if len(rem)!=11: raise ValueError('Expected 11 remaining identities')
    planned=[(c,m,q,a,p) for c,m,q,a,p in plan if (c['qid'],m,a,q) in rem]
    started_qids={k[0] for k in trials}; planned.sort(key=lambda x:x[0]['qid'] in started_qids)
    if len(planned)!=11 or sum(x[0]['qid'] not in started_qids for x in planned)!=8: raise ValueError('Question ordering mismatch')
    total=Decimal(audit['committed_cny'])+cost
    if total!=Decimal('1.6456800'): raise ValueError('Combined cost mismatch')
    return {'status':'PASS','model_calls':0,'started_identities':85,'planned_trials':11,'prior_unknown_171_reservation_cny':audit['prior_unknown_171_reservation_cny'],'committed_cny':str(total),'remaining_cny':str(Decimal('2')-total),'remaining_identities':[{'qid':x[0]['qid'],'mode':x[1],'repetition':x[2],'arm':x[3]} for x in planned]}

def no_writers():
    old.no_writers()
    cmd="$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"; if (@($p | Where-Object { !$_.CommandLine }).Count) { exit 3 }; if (@($p | Where-Object { $_.CommandLine -match 'm2_current_resume16.py' }).Count) { exit 4 }"
    if subprocess.run(['powershell.exe','-NoProfile','-Command',cmd],capture_output=True,timeout=15).returncode: raise ValueError('Cannot exclude previous writer')

class Transport(old.Transport):
    def __init__(self,fresh,output,spec,allowed):
        base.BudgetTransport.__init__(self,fresh,output);self.spec=spec;self.allowed=set(allowed);self.grant_bytes=None;self.budget_blocked=False
    async def send(self,sender,client,request,*args,**kwargs):
        stage='HISTORY'
        try:
            audit=inspect(self.spec);stage='GRANT_OR_BUDGET'
            if self.grant_bytes is None or GRANT.read_bytes()!=self.grant_bytes or self.limit!=Decimal(audit['remaining_cny']):raise ValueError('grant')
            stage='IDENTITY'
            if cont.identity(self.context) not in self.allowed:raise ValueError('identity')
            stage='TRIAL_RECORD'; p=self.output/'trials.jsonl'
            if p.exists() and cont.identity(self.context) in cont.indexed_trials(p.read_bytes()):raise ValueError('already recorded')
            stage='PROCESS';no_writers()
        except Exception as exc:
            self.stopped=True;self.diagnostic('PRE_SEND_'+stage+'_'+type(exc).__name__);raise base.GuardStop('Pre-send validation failed') from None
        try:return await base.BudgetTransport.send(self,sender,client,request,*args,**kwargs)
        except base.GuardStop as exc:
            self.diagnostic(str(exc),request)
            if str(exc).startswith('BUDGET_BLOCKED:'):self.budget_blocked=True
            raise

async def run_plan(manifest,output,guard,plan):
    original=base.trial_plan
    try:
        for value in plan:
            case,mode,rep,arm,_=value; key=(case['qid'],mode,arm,rep); before=len(guard.calls); guard.budget_blocked=False
            diagpath=Path(output)/'guard-diagnostics.jsonl'; diagbefore=len(cont.rows(diagpath.read_bytes())) if diagpath.exists() else 0
            base.trial_plan=lambda _,value=value:iter([value])
            try:
                await base.run_trials(manifest,output,guard)
                if guard.stopped:raise base.GuardStop('Transport stopped')
            except base.GuardStop:
                path=Path(output)/'trials.jsonl'; rows=cont.indexed_trials(path.read_bytes()) if path.exists() else {}
                diagnostics=cont.rows(diagpath.read_bytes())[diagbefore:] if diagpath.exists() else []
                if len(diagnostics)!=1 or not diagnostics[0].get('code','').startswith('BUDGET_BLOCKED:') or cont.identity(diagnostics[0]['identity'])!=key:raise
                if not guard.budget_blocked or not guard.stopped or key not in rows or rows[key].get('state') not in ('GUARD_STOP','FAILED','NO_DONE') or any(c.get('state')!='COMPLETED' for c in guard.calls):raise
                entry={'qid':key[0],'mode':key[1],'arm':key[2],'repetition':key[3],'provider_call_ids':[c['call_id'] for c in guard.calls[before:]],'reason':'BUDGET_BLOCKED before send; all issued calls completed; identity not retried'}
                with (Path(output)/'skipped-untestable.jsonl').open('ab') as f:f.write(base.canonical(entry)+b'\n');f.flush();os.fsync(f.fileno())
                guard.stopped=False;guard.budget_blocked=False
    finally:base.trial_plan=original

def execute(spec,output):
    audit=inspect(spec);no_writers()
    if not os.getenv('M2_AB_API_KEY'):raise ValueError('Credential missing')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:raise ValueError('Fresh process required')
    history,parent,fresh,_,plan=old.r.current.validate(json.loads((old.r.RUN/'manifest.json').read_bytes()))
    fresh={**fresh,'budget_cny':audit['remaining_cny']}; allowed={cont.identity(x) for x in audit['remaining_identities']}
    selected=[x for x in plan if (x[0]['qid'],x[1],x[3],x[2]) in allowed]
    started={k[0] for k in cont.indexed_trials((PRIOR/'trials.jsonl').read_bytes())};selected.sort(key=lambda x:x[0]['qid'] in started)
    if len(selected)!=11:raise ValueError('Plan changed')
    output=base.private_path(output)
    if output.parent!=ROOT/'var/preferences-acceptance' or not output.name.startswith('m2-current-resume11-'):raise ValueError('Invalid output')
    output.mkdir(exist_ok=False);(output/'manifest.json').write_bytes(base.canonical(spec));(output/'preflight.json').write_bytes(base.canonical(audit));(output/'dataset.json').write_bytes(base.canonical(old.r.current.dataset_snapshot(fresh)))
    guard=None; cwd=Path.cwd()
    try:
        cont.backup_reuse(history,parent,output/'isolated.db');guard=Transport(fresh,output,spec,allowed)
        with base.isolated_redis(output) as url:
            base.configure_runtime(fresh,output,url)
            with (output/'runtime.log').open('w',encoding='utf8') as log,redirect_stdout(log),redirect_stderr(log),guard.installed():
                cont.safety.assert_runtime_isolation(output,url);inspect(spec);no_writers()
                with GRANT.open('xb') as f:f.write(base.canonical({'manifest_sha256':base.digest(base.canonical(spec)),'output':str(output)}));f.flush();os.fsync(f.fileno())
                guard.grant_bytes=GRANT.read_bytes();asyncio.run(run_plan(fresh,output,guard,selected))
        status='RECORDED_REQUIRES_EVIDENCE_REVIEW';reason=None
    except base.GuardStop as exc:status='STOPPED';reason=str(exc)
    except Exception as exc:status='FAILED';reason=type(exc).__name__
    finally:os.chdir(cwd)
    summary={'status':status,'reason':reason,'prior_committed_cny':audit['committed_cny'],'lease':'RETAINED_PENDING_RECONCILIATION','planned_trials':11}
    if guard:summary.update(guard.summary());summary['global_committed_including_unknown_cny']=str(Decimal(audit['committed_cny'])+guard.committed)
    p=output/'trials.jsonl';summary['recorded_trials']=len(cont.indexed_trials(p.read_bytes())) if p.exists() else 0
    p=output/'skipped-untestable.jsonl';summary['skipped_untestable_trials']=len(cont.rows(p.read_bytes())) if p.exists() else 0
    if summary['recorded_trials']!=11 and status=='RECORDED_REQUIRES_EVIDENCE_REVIEW':summary['status']='STOPPED'
    (output/'summary.json').write_bytes(base.canonical(summary));return summary

def main():
    p=argparse.ArgumentParser();p.add_argument('mode',choices=['preflight','execute']);p.add_argument('--manifest',required=True);p.add_argument('--output');a=p.parse_args()
    try:
        spec=json.loads(Path(a.manifest).read_bytes());result=inspect(spec) if a.mode=='preflight' else execute(spec,a.output);print(json.dumps(result,ensure_ascii=False));return 0 if result['status'] in ('PASS','RECORDED_REQUIRES_EVIDENCE_REVIEW') else 1
    except Exception as exc:print(json.dumps({'status':'BLOCKED','error':str(exc),'model_calls':0}));return 1
if __name__=='__main__':raise SystemExit(main())

"""M2 continuation of only the frozen never-started identities."""
import argparse
import asyncio
from contextlib import redirect_stdout, redirect_stderr
import os
import subprocess
import sys
import hashlib
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
s = importlib.util.spec_from_file_location('frozen_current', ROOT / 'scripts/m2_current_ab.py')
current = importlib.util.module_from_spec(s)
s.loader.exec_module(current)
base, cont = current.base, current.cont
RUN = ROOT / 'var/preferences-acceptance/m2-current-real-v3'
LEASE = ROOT / 'var/preferences-acceptance/m2-continuation-locks/7b5524c8bfd1e8bbbef6298f6950fa4a6cc2462fe336789ad66639417e5e75d3.active'
GRANT = LEASE.with_name('current-' + LEASE.stem + '.started')

def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def inspect(manifest):
    spec = json.loads((RUN / 'manifest.json').read_bytes())
    if manifest['files'] != {str(p.relative_to(ROOT)).replace('\\', '/'): sha(p) for p in [RUN/'manifest.json', RUN/'dataset.json', RUN/'summary.json', RUN/'trials.jsonl', RUN/'provider-ledger.jsonl', LEASE, GRANT, Path(__file__)]}:
        raise ValueError('Frozen evidence or code changed')
    history, parent, fresh, budget, plan = current.validate(spec)
    if json.loads((RUN/'dataset.json').read_bytes()) != current.dataset_snapshot(fresh):
        raise ValueError('Frozen dataset mismatch')
    grant = json.loads(GRANT.read_bytes())
    if grant != {'authorization_id': spec['authorization_id'], 'output': str(RUN), 'spec_sha256': base.digest(base.canonical(spec))}:
        raise ValueError('Prior grant mismatch')
    summary = json.loads((RUN/'summary.json').read_bytes())
    if summary.get('status') != 'STOPPED' or summary.get('lease') != 'RETAINED_PENDING_RECONCILIATION':
        raise ValueError('Prior final state mismatch')
    if not LEASE.stat().st_mtime <= GRANT.stat().st_mtime <= (RUN/'provider-ledger.jsonl').stat().st_mtime <= (RUN/'summary.json').stat().st_mtime:
        raise ValueError('Lease/grant/ledger/summary time order mismatch')
    trials = cont.indexed_trials((RUN/'trials.jsonl').read_bytes())
    records = cont.rows((RUN/'provider-ledger.jsonl').read_bytes())
    planned = {cont.identity({'qid': c['qid'], 'mode': m, 'arm': a, 'repetition': r}): (c,m,r,a,p) for c,m,r,a,p in plan}
    latest = {}
    for row in records:
        n = row['call_id']
        if n not in latest:
            if row['state'] != 'UNKNOWN_PENDING': raise ValueError('Attempt without reservation')
            latest[n] = row
        else:
            old = latest[n]
            if old['state'] != 'UNKNOWN_PENDING' or row['state'] not in ('COMPLETED', 'UNKNOWN') or any(old.get(k) != row.get(k) for k in old if k != 'state'):
                raise ValueError('Provider state transition invalid')
            latest[n] = row
    if set(latest) != set(range(1, 172)): raise ValueError('Provider calls not contiguous')
    started = set(trials) | {cont.identity(x) for x in records}
    if not started <= set(planned) or len(started) != 74: raise ValueError('Prior identity mismatch')
    total = Decimal(0)
    ip = Decimal(str(fresh['pricing']['input_cny_per_million']))
    op = Decimal(str(fresh['pricing']['output_cny_per_million']))
    for n,row in latest.items():
        identity = cont.identity(row)
        if identity not in trials or n not in trials[identity]['provider_call_ids'] or row['requested_model'] != fresh['model'] or row['output_token_bound'] != fresh['max_output_tokens']:
            raise ValueError('Provider attribution mismatch')
        req = RUN / f'request-{n:04d}.json'
        if sha(req) != row['request_sha256']: raise ValueError('Request hash mismatch')
        reserve = (Decimal(row['input_token_bound'])*ip + Decimal(row['output_token_bound'])*op)/Decimal(1000000)
        if reserve != Decimal(row['reservation_cny']): raise ValueError('Reservation mismatch')
        if row['state'] == 'UNKNOWN':
            if n != 171: raise ValueError('Unexpected UNKNOWN')
            total += reserve
        else:
            usage = row['usage']; a,b = usage['prompt_tokens'],usage['completion_tokens']
            if type(a) is not int or type(b) is not int or not (0 <= a <= row['input_token_bound'] and 0 <= b <= row['output_token_bound']) or row['actual_model'] != fresh['model']:
                raise ValueError('Invalid usage')
            cost = (Decimal(a)*ip + Decimal(b)*op)/Decimal(1000000)
            if cost != Decimal(row['price_based_cost_cny']): raise ValueError('Cost mismatch')
            total += cost
    if total != Decimal('0.7476532') or Decimal(budget['prior_combined_committed_cny']) != Decimal('0.8010920') or Decimal(summary['committed_including_unknown_cny']) != total:
        raise ValueError('Budget mismatch')
    remaining = [k for k in planned if k not in started]
    if len(remaining) != 22: raise ValueError('Remaining count mismatch')
    return {'status':'PASS', 'model_calls':0, 'planned_trials':22, 'started_trials':74, 'unknown_call':171, 'unknown_reservation_cny':str(latest[171]['reservation_cny']), 'committed_cny':'1.5487452', 'remaining_cny':'0.4512548', 'lease_sha256':sha(LEASE), 'execution':'READY_REQUIRES_REAL_RUN', 'remaining_identities':remaining}

def no_live_original():
    # Query command lines without printing them. Failure or inaccessible process data closes the gate.
    command = "$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"; if (@($p | Where-Object { !$_.CommandLine }).Count) { exit 3 }; if (@($p | Where-Object { $_.CommandLine -match 'm2_current_ab.py|m2_preference_continue.py' }).Count) { exit 4 }"
    r = subprocess.run(['powershell.exe','-NoProfile','-Command',command], capture_output=True, timeout=15)
    if r.returncode: raise ValueError('Cannot exclude a running prior writer')

class ResumeTransport(base.BudgetTransport):
    def __init__(self, fresh, output, manifest, allowed):
        super().__init__(fresh, output)
        self.frozen, self.allowed = manifest, set(allowed)
        self.grant = ROOT/'var/preferences-acceptance/m2-current-resume-once.started'
        self.grant_bytes = None

    async def send(self, *args, **kwargs):
        try:
            audit = inspect(self.frozen)
            if self.grant_bytes is None or self.grant.read_bytes() != self.grant_bytes: raise ValueError('Resume grant changed')
            if cont.identity(self.context) not in self.allowed or self.limit != Decimal(audit['remaining_cny']):
                raise ValueError('Identity or budget changed')
            path = self.output/'trials.jsonl'
            if path.exists() and cont.identity(self.context) in cont.indexed_trials(path.read_bytes()):
                raise ValueError('Identity already recorded')
            no_live_original()
        except Exception:
            self.stopped = True
            raise base.GuardStop('Resume pre-send verification failed') from None
        return await super().send(*args, **kwargs)

def execute(manifest, output):
    audit = inspect(manifest)
    no_live_original()
    if not os.getenv('M2_AB_API_KEY'): raise ValueError('Credential missing; zero calls')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules: raise ValueError('Fresh process required')
    spec = json.loads((RUN/'manifest.json').read_bytes())
    history,parent,fresh,budget,plan = current.validate(spec)
    fresh = {**fresh, 'budget_cny': audit['remaining_cny']}
    allowed = {tuple(x) for x in audit['remaining_identities']}
    selected = [x for x in plan if cont.identity({'qid':x[0]['qid'],'mode':x[1],'arm':x[3],'repetition':x[2]}) in allowed]
    # Prefer the entirely unstarted question while retaining stable order within each group.
    started_qids = {k[0] for k in cont.indexed_trials((RUN/'trials.jsonl').read_bytes())}
    selected.sort(key=lambda x: x[0]['qid'] in started_qids)
    if len(selected) != 22: raise ValueError('Plan changed')
    output = base.private_path(output)
    if output.parent != ROOT/'var/preferences-acceptance' or not output.name.startswith('m2-current-resume-'): raise ValueError('Invalid output name')
    output.mkdir(parents=False, exist_ok=False)
    grant = ROOT/'var/preferences-acceptance/m2-current-resume-once.started'
    (output/'manifest.json').write_bytes(base.canonical(manifest))
    (output/'preflight.json').write_bytes(base.canonical(audit))
    (output/'dataset.json').write_bytes(base.canonical(current.dataset_snapshot(fresh)))
    guard = None
    oldcwd=Path.cwd()
    try:
        cont.backup_reuse(history,parent,output/'isolated.db')
        guard = ResumeTransport(fresh,output,manifest,allowed)
        with base.isolated_redis(output) as redis_url:
            base.configure_runtime(fresh,output,redis_url)
            with (output/'runtime.log').open('w',encoding='utf8') as log, redirect_stdout(log), redirect_stderr(log), guard.installed():
                cont.safety.assert_runtime_isolation(output,redis_url)
                inspect(manifest)
                no_live_original()
                with grant.open('xb') as stream:
                    stream.write(base.canonical({'manifest_sha256':base.digest(base.canonical(manifest)), 'output':str(output)})); stream.flush(); os.fsync(stream.fileno())
                guard.grant_bytes = grant.read_bytes()
                original=base.trial_plan
                try:
                    base.trial_plan=lambda _: iter(selected)
                    asyncio.run(base.run_trials(fresh,output,guard))
                finally:
                    base.trial_plan=original
        status='RECORDED_REQUIRES_EVIDENCE_REVIEW'; reason=None
    except base.GuardStop as e:
        status='STOPPED'; reason=str(e)
    except Exception as e:
        status='FAILED'; reason=type(e).__name__
    finally:
        os.chdir(oldcwd)
    summary={'status':status,'reason':reason,'prior_committed_cny':audit['committed_cny'],'lease':'RETAINED_PENDING_RECONCILIATION','planned_trials':22}
    if guard:
        summary.update(guard.summary())
        summary['global_committed_including_unknown_cny']=str(Decimal(audit['committed_cny'])+guard.committed)
    trials=output/'trials.jsonl'
    summary['recorded_trials'] = len(cont.indexed_trials(trials.read_bytes())) if trials.exists() else 0
    if summary['recorded_trials'] != 22 and summary['status']=='RECORDED_REQUIRES_EVIDENCE_REVIEW': summary['status']='STOPPED'
    (output/'summary.json').write_bytes(base.canonical(summary))
    return summary

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('mode', choices=['preflight','execute']); ap.add_argument('--manifest',required=True); ap.add_argument('--output')
    args=ap.parse_args()
    try:
        manifest=json.loads(Path(args.manifest).read_bytes())
        result=inspect(manifest) if args.mode=='preflight' else execute(manifest,args.output)
        print(json.dumps(result,ensure_ascii=False))
        return 0 if result['status'] in ('PASS','RECORDED_REQUIRES_EVIDENCE_REVIEW') else 1
    except Exception as exc:
        print(json.dumps({'status':'BLOCKED','error':str(exc),'model_calls':0}))
        return 1
if __name__ == '__main__': raise SystemExit(main())

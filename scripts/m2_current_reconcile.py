"""Read-only reconciliation of the four frozen M2 runs; never sends requests."""
import argparse, hashlib, importlib.util, json, os, subprocess
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / 'var/preferences-acceptance'
spec = importlib.util.spec_from_file_location('resume11_frozen', ROOT / 'scripts/m2_current_resume11.py')
prior = importlib.util.module_from_spec(spec); spec.loader.exec_module(prior)
base, cont, r = prior.base, prior.cont, prior.old.r
RUNS = ['m2-current-real-v3', 'm2-current-resume-real-v4', 'm2-current-resume16-real-v2', 'm2-current-resume11-real-v2']
COUNTS = [171, 14, 9, 27]
COSTS = ['0.7476532', '0.0574744', '0.0394604', '0.0704316']

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()
def dec(x): return Decimal(str(x))
def require(condition, message):
    if not condition: raise ValueError(message)

def validate_identities(planned, all_trials):
    require(len(planned) == len(set(planned)) == 96, 'Plan duplicate or size mismatch')
    require(set(all_trials) == set(planned), 'Unstarted identities remain')

def validate_budget_block(reservation, limit, committed):
    require(dec(reservation) > dec(limit) - dec(committed), 'Budget block not justified')

def audit_run(path, trials, records, fresh, count, expected_cost, expected_unknown=()):
    latest = {}
    for row in records:
        n = row['call_id']; require(type(n) is int and n > 0, 'Invalid call ID')
        if n not in latest:
            require(row['state'] == 'UNKNOWN_PENDING', 'Missing pending reservation')
        else:
            prev = latest[n]
            require(prev['state'] == 'UNKNOWN_PENDING' and row['state'] in ('COMPLETED','UNKNOWN') and all(row.get(k) == v for k,v in prev.items() if k != 'state'), 'Invalid transition')
        latest[n] = row
    require(set(latest) == set(range(1, count+1)), 'Missing or duplicate call')
    require(all(x['state'] in ('UNKNOWN','COMPLETED') for x in latest.values()) and {n for n,x in latest.items() if x['state'] == 'UNKNOWN'} == set(expected_unknown), 'Unexpected UNKNOWN or pending')
    refs = {}
    for key, trial in trials.items():
        for n in trial['provider_call_ids']:
            require(type(n) is int and n not in refs, 'Duplicate trial call reference'); refs[n] = key
    require(set(refs) == set(latest), 'Unattributed trial call')
    ip, op = map(dec, (fresh['pricing']['input_cny_per_million'], fresh['pricing']['output_cny_per_million']))
    total = Decimal(0); provider_ids = set(); result = []
    for n, row in latest.items():
        key = cont.identity(row)
        require(key == refs[n], 'Identity attribution mismatch')
        require(row['requested_model'] == fresh['model'] and row['output_token_bound'] == fresh['max_output_tokens'], 'Model or bound mismatch')
        require(type(row['input_token_bound']) is int and row['input_token_bound'] >= 0, 'Invalid bound')
        require(sha(path / f'request-{n:04d}.json') == row['request_sha256'], 'Request hash mismatch')
        reserve = (dec(row['input_token_bound'])*ip + dec(row['output_token_bound'])*op)/1000000
        require(reserve == dec(row['reservation_cny']), 'Reservation mismatch')
        if row['state'] == 'UNKNOWN': cost = reserve
        else:
            u = row['usage']; a,b = u['prompt_tokens'],u['completion_tokens']
            require(type(a) is int and type(b) is int and 0 <= a <= row['input_token_bound'] and 0 <= b <= row['output_token_bound'] and row['actual_model'] == fresh['model'], 'Usage or model mismatch')
            cost = (dec(a)*ip + dec(b)*op)/1000000
            require(cost == dec(row['price_based_cost_cny']), 'Cost mismatch')
            body = (path / f'response-{n:04d}.body').read_text(encoding='utf8')
            try: data = json.loads(body)
            except json.JSONDecodeError:
                objects = [json.loads(line[5:].strip()) for line in body.splitlines() if line.startswith('data:') and line[5:].strip() not in ('','[DONE]')]
                data = [x for x in objects if x.get('usage')][-1]
            require(data.get('usage') == u and data.get('model') == row['actual_model'] and data.get('id') == row.get('provider_request_id'), 'Response usage/model/id mismatch')
            pid = row.get('provider_request_id')
            if pid: require(pid not in provider_ids, 'Duplicate provider ID'); provider_ids.add(pid)
        total += cost
        result.append({'call_id':n,'identity':list(key),'state':row['state'],'request_sha256':row['request_sha256'],'response_sha256':sha(path / f'response-{n:04d}.body') if (path / f'response-{n:04d}.body').exists() else None,'reserved_cny':str(reserve),'committed_cny':str(cost),'provider_request_id':row.get('provider_request_id')})
    require(total == dec(expected_cost), 'Run cost mismatch')
    return result, provider_ids

def reconcile():
    last = P / RUNS[-1]
    last_spec = json.loads((last/'manifest.json').read_bytes())
    ancestor = prior.inspect(last_spec)
    require(json.loads(prior.GRANT.read_bytes()) == {'manifest_sha256':base.digest(base.canonical(last_spec)), 'output':str(last)}, 'Final grant mismatch')
    _,_,fresh,_,plan = r.current.validate(json.loads((r.RUN/'manifest.json').read_bytes()))
    require(json.loads((last/'dataset.json').read_bytes()) == r.current.dataset_snapshot(fresh), 'Final dataset mismatch')
    planned = [cont.identity({'qid':c['qid'],'mode':m,'arm':a,'repetition':rep}) for c,m,rep,a,_ in plan]
    require(len(planned) == len(set(planned)) == 96, 'Plan duplicate or size mismatch')
    all_trials = {}; all_calls = {}; all_ids = set(); per_run_trials = []
    for i,name in enumerate(RUNS):
        path=P/name; trials=cont.indexed_trials((path/'trials.jsonl').read_bytes())
        require(not (set(trials) & set(all_trials)) and set(trials) <= set(planned), 'Duplicate or unplanned identity')
        calls, ids = audit_run(path,trials,cont.rows((path/'provider-ledger.jsonl').read_bytes()),fresh,COUNTS[i],COSTS[i],(171,) if i == 0 else ())
        require(not ids & all_ids, 'Cross-run provider ID collision'); all_ids |= ids
        all_trials.update({k:(name,v) for k,v in trials.items()}); all_calls[name]=calls; per_run_trials.append(trials)
        summary=json.loads((path/'summary.json').read_bytes())
        require(summary['recorded_trials'] == len(trials) and summary['actual_http_calls'] == COUNTS[i] and dec(summary['committed_including_unknown_cny']) == dec(COSTS[i]), 'Summary mismatch')
    validate_identities(planned, all_trials)
    require(set(per_run_trials[-1]) == {cont.identity(x) for x in ancestor['remaining_identities']}, 'Final identities differ from authorized remaining')
    summary=json.loads((last/'summary.json').read_bytes())
    require(summary['status']=='RECORDED_REQUIRES_EVIDENCE_REVIEW' and summary['lease']=='RETAINED_PENDING_RECONCILIATION' and summary['completed_calls']==27 and summary['unknown_calls']==0 and summary['skipped_untestable_trials']==3, 'Final summary mismatch')
    skips=cont.rows((last/'skipped-untestable.jsonl').read_bytes()); diag=cont.rows((last/'guard-diagnostics.jsonl').read_bytes())
    require(len(skips)==len(diag)==3 and len({cont.identity(x) for x in skips})==3, 'Skip diagnostics mismatch')
    limit=dec('0.3543200'); calls=all_calls[RUNS[-1]]
    for skip, d in zip(skips,diag):
        key=cont.identity(skip); trial=per_run_trials[-1][key]; numbers=skip['provider_call_ids']
        require(key==cont.identity(d['identity']) and trial['state'] in ('GUARD_STOP','FAILED','NO_DONE') and numbers==trial['provider_call_ids'] and numbers and all(calls[n-1]['identity']==list(key) and calls[n-1]['state']=='COMPLETED' for n in numbers), 'Skip identity/calls mismatch')
        require(d['code'].startswith('BUDGET_BLOCKED:') and len(d['request_sha256'])==64 and all(c in '0123456789abcdef' for c in d['request_sha256']), 'Invalid blocked request diagnostic')
        committed=sum((dec(x['committed_cny']) for x in calls[:max(numbers)]),Decimal(0))
        validate_budget_block(d['reservation_cny'], limit, committed)
    total=dec('0.8010920')+sum(map(dec,COSTS),Decimal(0))
    require(total==dec('1.7161116') and total==dec(summary['global_committed_including_unknown_cny']) and dec(ancestor['committed_cny'])==dec('1.6456800') and total<=2, 'Global budget mismatch')
    identities=[{'identity':list(k),'run':all_trials[k][0],'trial_state':all_trials[k][1]['state'],'provider_call_ids':all_trials[k][1]['provider_call_ids'],'skipped_untestable':any(cont.identity(s)==k for s in skips)} for k in planned]
    return {'status':'PASS','model_calls':0,'identity_count':len(identities),'identities':identities,'calls':all_calls,'historical_committed_cny':'0.8010920','unknown_171_full_reservation_cny':calls_unknown(all_calls),'committed_cny':str(total),'remaining_cny':str(dec('2')-total),'skips':skips,'budget_block_diagnostics':diag,'provider_bill':'UNKNOWN','lease_sha256':sha(r.LEASE)}

def calls_unknown(calls): return calls[RUNS[0]][170]['reserved_cny']

def no_writers():
    command = "$p=Get-CimInstance Win32_Process -Filter \"Name='python.exe'\"; if (@($p | Where-Object { !$_.CommandLine }).Count) { exit 3 }; if (@($p | Where-Object { $_.CommandLine -match 'm2_current_ab.py|m2_preference_continue.py|m2_current_resume.py|m2_current_resume16.py|m2_current_resume11.py' }).Count) { exit 4 }"
    result=subprocess.run(['powershell.exe','-NoProfile','-Command',command],capture_output=True,timeout=15)
    status = {0:'NO_MATCHING_PYTHON_WRITER_AT_CHECK_TIME', 3:'BLOCKED_INACCESSIBLE_COMMAND_LINES', 4:'BLOCKED_MATCHING_WRITER'}.get(result.returncode,'BLOCKED_PROCESS_QUERY_FAILED')
    return {'checked_at_utc':datetime.now(timezone.utc).isoformat(),'process_query_exit_code':result.returncode,'result':status,'lease_path':str(r.LEASE),'lease_sha256':sha(r.LEASE),'lease_state':'RETAINED; ownership requires revalidation before continuation'}

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--output',required=True); args=ap.parse_args()
    try:
        output=Path(args.output).resolve(); require(output.parent==P and output.name.startswith('m2-current-reconciled-'), 'Invalid output path')
        evidence=no_writers(); result=reconcile(); require(evidence['lease_sha256']==result['lease_sha256'], 'Lease changed during audit'); evidence2=no_writers(); require(evidence2['lease_sha256']==evidence['lease_sha256'], 'Lease changed during audit')
        result['process_and_lease_checks']=[evidence,evidence2]; result['continuation_readiness'] = 'BLOCKED' if any(x['process_query_exit_code'] != 0 for x in (evidence,evidence2)) else 'REQUIRES_FRESH_RUNTIME_PREFLIGHT'
        result['source_sha256']={str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for name in RUNS for p in [P/name/'manifest.json',P/name/'summary.json',P/name/'trials.jsonl',P/name/'provider-ledger.jsonl']}
        result['source_sha256'].update({str(p.relative_to(ROOT)).replace('\\','/'):sha(p) for p in [last_path('guard-diagnostics.jsonl'),last_path('skipped-untestable.jsonl'),Path(__file__),prior.GRANT,r.LEASE]})
        with output.open('xb') as f: f.write(base.canonical(result)); f.flush(); os.fsync(f.fileno())
        print(json.dumps({'status':'PASS','model_calls':0,'output':str(output),'sha256':sha(output),'identity_count':96,'committed_cny':str(result['committed_cny']),'remaining_cny':str(result['remaining_cny']),'continuation_readiness':result['continuation_readiness']}))
        return 0
    except Exception as e:
        print(json.dumps({'status':'BLOCKED','error':str(e),'model_calls':0})); return 1

def last_path(name): return P/RUNS[-1]/name
if __name__ == '__main__': raise SystemExit(main())

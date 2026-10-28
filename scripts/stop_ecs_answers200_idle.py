"""Stop the prior runner only between journaled requests, preserving its ledger."""
import json,os,pathlib,signal,sys,time
base=pathlib.Path(sys.argv[1]).resolve();r=base/'answers200-continuation-r1'
parent=json.loads((r/'dispatch.json').read_bytes())['pid']
for _ in range(40):
 if (r/'completion.json').exists():print(json.dumps({'status':'ALREADY_COMPLETED'}));sys.exit(0)
 children=pathlib.Path(f'/proc/{parent}/task/{parent}/children')
 if not children.exists():raise RuntimeError('Cannot confirm launcher child')
 values=children.read_text().split()
 if len(values)!=1:time.sleep(2);continue
 pid=int(values[0]);cmd=pathlib.Path(f'/proc/{pid}/cmdline').read_bytes()
 if b'continue_ecs_answers200.py' not in cmd:raise RuntimeError('Unexpected process identity')
 try:
  os.kill(pid,signal.SIGSTOP)
  events=[json.loads(l) for l in (r/'requests.jsonl').read_text().splitlines()]
  results=[json.loads(l) for l in (r/'results.jsonl').read_text().splitlines()]
  key=lambda x:(x['qid'],x['arm'],x['attempt'])
  reserved={key(x) for x in events if x['status']=='RESERVED'}
  finished={key(x) for x in events if x['status'] in ('HTTP_SUCCESS','REJECTED_429','UNKNOWN_OR_FAILED')}
  visited={(x['qid'],x['arm']) for x in results}
  safe=reserved<=finished and all((q,a) in visited for q,a,_ in reserved)
  if safe:
   os.kill(pid,signal.SIGTERM);os.kill(pid,signal.SIGCONT)
   result={'status':'STOPPED_BETWEEN_REQUESTS','pid':pid,'saved_results':len(results),'at':time.time()}
   (r/'speed-change.json').write_text(json.dumps(result,indent=2));print(json.dumps(result));sys.exit(0)
  os.kill(pid,signal.SIGCONT)
 except Exception:
  try:os.kill(pid,signal.SIGCONT)
  except ProcessLookupError:pass
  raise
 time.sleep(2)
raise TimeoutError('No safe pause point observed; previous runner remains active')

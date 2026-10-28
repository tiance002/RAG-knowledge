"""ECS-only sequencer: finish 200-set answers, then retry Office 429s."""
import json,pathlib,subprocess,sys,time
base=pathlib.Path(sys.argv[1]).resolve();fast=base/'answers200-fast-r2';office=base/'office50-v1/retry429-r1'
fast.mkdir(mode=0o700,exist_ok=True);office.mkdir(mode=0o700,exist_ok=True)
if len(sys.argv)>2 and sys.argv[2]=='--child':
 with (fast/'run.log').open('ab',buffering=0) as log:
  a=subprocess.run([sys.executable,'-u',str(base/'continue_ecs_answers200_fast.py'),str(base)],cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
 (fast/'completion.json').write_text(json.dumps({'exit_code':a.returncode,'finished_at':time.time()}))
 with (office/'run.log').open('ab',buffering=0) as log:
  b=subprocess.run([sys.executable,'-u',str(base/'retry_ecs_office429.py'),str(base)],cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
 (office/'completion.json').write_text(json.dumps({'exit_code':b.returncode,'finished_at':time.time(),'previous_fast_exit_code':a.returncode}))
 sys.exit(b.returncode if b.returncode else a.returncode)
assert (base/'answers200-continuation-r1/completion.json').exists()
with (fast/'dispatch.json').open('x') as marker:
 with (fast/'launcher.log').open('ab',buffering=0) as log:
  p=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),str(base),'--child'],cwd=base,
   stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
 marker.write(json.dumps({'pid':p.pid,'independent_session':True,'started_at':time.time(),'office_follows':True}))
print(json.dumps({'pid':p.pid,'independent_session':True,'office_follows':True}))

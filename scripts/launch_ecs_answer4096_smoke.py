"""Run the bounded answer-limit diagnostic independently on ECS."""
import json,pathlib,subprocess,sys,time
base=pathlib.Path(sys.argv[1]).resolve();root=base/'answer-cap4096-v1'
if len(sys.argv)>2 and sys.argv[2]=='--child':
 with (root/'smoke.log').open('ab',buffering=0) as log:
  p=subprocess.run([sys.executable,'-u',str(base/'smoke_ecs_answer4096.py'),str(base)],cwd=root,
   stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
 (root/'smoke-completion.json').write_text(json.dumps({'exit_code':p.returncode,'finished_at':time.time()}));sys.exit(p.returncode)
assert (root/'smoke-plan.json').exists() and not (root/'smoke-requests.jsonl').exists()
with (root/'smoke-dispatch.json').open('x') as marker:
 with (root/'smoke-launcher.log').open('ab',buffering=0) as log:
  p=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),str(base),'--child'],cwd=root,
    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
 marker.write(json.dumps({'pid':p.pid,'independent_session':True,'started_at':time.time()}))
print(json.dumps({'pid':p.pid,'independent_session':True}))

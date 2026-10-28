"""Detach answer continuation on ECS without touching the old execution."""
import json,pathlib,subprocess,sys,time
base=pathlib.Path(sys.argv[1]).resolve();out=base/'answers200-continuation-r1';out.mkdir(mode=0o700,exist_ok=True)
if len(sys.argv)>2 and sys.argv[2]=='--child':
 with (out/'run.log').open('ab',buffering=0) as log:
  r=subprocess.run([sys.executable,'-u',str(base/'continue_ecs_answers200.py'),str(base)],cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
 (out/'completion.json').write_text(json.dumps({'exit_code':r.returncode,'finished_at':time.time()}));sys.exit(r.returncode)
with (out/'dispatch.json').open('x') as marker:
 with (out/'launcher.log').open('ab',buffering=0) as log:
  p=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),str(base),'--child'],cwd=base,
    stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
 marker.write(json.dumps({'pid':p.pid,'independent_session':True,'started_at':time.time()}))
print(json.dumps({'pid':p.pid,'independent_session':True}))

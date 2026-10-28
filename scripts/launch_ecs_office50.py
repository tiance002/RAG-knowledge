"""Launch and monitor the 50-case Office extension entirely on ECS."""
import json,pathlib,subprocess,sys,time

base=pathlib.Path(sys.argv[1]).resolve();root=base/'office50-v1';runtime=root/'evaluation';runtime.mkdir(mode=0o700,exist_ok=True)
if len(sys.argv)>2 and sys.argv[2]=='--child':
 with (runtime/'run.log').open('ab',buffering=0) as f:
  result=subprocess.run([sys.executable,'-u',str(base/'run_ecs_office50.py'),str(base)],cwd=base,
    stdin=subprocess.DEVNULL,stdout=f,stderr=subprocess.STDOUT)
 (runtime/'completion.json').write_text(json.dumps({'exit_code':result.returncode,'finished_at':time.time()}));sys.exit(result.returncode)
assert (root/'dataset250/freeze250.json').is_file()
marker=runtime/'dispatch.json'
with marker.open('x') as f:
 with (runtime/'launcher.log').open('ab',buffering=0) as log:
  child=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),str(base),'--child'],
   cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
 f.write(json.dumps({'pid':child.pid,'independent_session':True,'started_at':time.time()}))
print(marker.read_text())

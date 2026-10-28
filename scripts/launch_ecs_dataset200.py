"""Detach an ECS-only benchmark, retaining exit evidence and refusing replay."""
import argparse,json,os,pathlib,subprocess,sys,time

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);parser.add_argument('--child',action='store_true')
 args=parser.parse_args();base=pathlib.Path(args.root).resolve()
 runtime=base/json.loads((base/'model-policy.json').read_bytes())['runtime_name']
 if args.child:
  with (runtime/'run.log').open('ab',buffering=0) as log:
   result=subprocess.run([sys.executable,'-u',str(base/'run_ecs_dataset200.py'),'--root',str(base)],
       cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT)
  (runtime/'completion.json').write_text(json.dumps({'exit_code':result.returncode,'finished_at':time.time()}))
  return result.returncode
 state=json.loads((runtime/'state.json').read_bytes())
 assert state['status']=='PREPARED' and not state['requests'],'Preflight must pass with zero requests'
 marker=runtime/'dispatch.json'
 with marker.open('x') as stream:
  stream.write(json.dumps({'status':'DISPATCHING','started_at':time.time()}));stream.flush();os.fsync(stream.fileno())
 with (runtime/'launcher.log').open('ab',buffering=0) as log:
  child=subprocess.Popen([sys.executable,str(pathlib.Path(__file__).resolve()),'--root',str(base),'--child'],
      cwd=base,stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,start_new_session=True,close_fds=True)
 record={'launcher_pid':child.pid,'started_at':time.time(),'independent_session':True,'automatic_restart':False,
         'runtime':str(runtime),'runner':str(base/'run_ecs_dataset200.py')}
 marker.write_text(json.dumps(record,indent=2));print(json.dumps(record))
 return 0

if __name__=='__main__':sys.exit(main())

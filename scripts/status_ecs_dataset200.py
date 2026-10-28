"""Print compact server-side progress without model credentials or document content."""
import argparse,collections,json,pathlib,time

parser=argparse.ArgumentParser();parser.add_argument('--root',required=True);args=parser.parse_args()
base=pathlib.Path(args.root)
runtime=base/json.loads((base/'model-policy.json').read_bytes())['runtime_name']
state=json.loads((runtime/'state.json').read_bytes())
out={'checked_at':time.time(),'runtime':str(runtime),'status':state['status'],'phase':state['phase'],
     'requests':dict(collections.Counter(r['status'] for r in state['requests']))}
for label,filename in [('retrieval_completed','retrieval-results.jsonl'),('answers_completed','results.jsonl')]:
 path=runtime/filename;out[label]=sum(1 for _ in path.open()) if path.exists() else 0
assets=set()
for filename in ('prior-caption-results.jsonl','caption-results.jsonl'):
 path=runtime/filename
 if path.exists():
  for line in path.open(encoding='utf-8'):
   row=json.loads(line)
   if row.get('finish_reason')=='stop' and (row.get('caption') or '').strip():assets.add(row['asset_sha256'])
out['captions_completed']=len(assets)
out['request_counters']={k:state[k] for k in ('caption_calls','embedding_inputs','rerank_calls','generation_calls')}
for key,name in [('dispatch','dispatch.json'),('completion','completion.json')]:
 if (runtime/name).exists():out[key]=json.loads((runtime/name).read_bytes())
if 'completion' in out:out['process_status']='COMPLETED' if out['completion']['exit_code']==0 else 'STOPPED'
print(json.dumps(out))

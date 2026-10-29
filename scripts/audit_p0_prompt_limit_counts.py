import json,pathlib,collections,statistics
b=pathlib.Path('/opt/amrag/experiments/pdf-table-hardcases-20261027'); subs=['prompt-limit-factorial-2x2-20261028-v2','prompt-limit-factorial-2x2-continuation-20261028-v1','prompt-limit-factorial-2x2-traced-20261028-v2','prompt-limit-factorial-2x2-traced-20261028-v3'];seen={};outs={}
for sub in subs:
 p=b/sub
 for line in (p/'requests.jsonl').read_text().splitlines():
  x=json.loads(line);key=(x['qid'],x['variant'],x['max_tokens']);
  if x['status']=='RESERVED':assert key not in seen;seen[key]=sub
 for line in (p/'results.jsonl').read_text().splitlines() if (p/'results.jsonl').exists() else []:
  x=json.loads(line);key=(x['qid'],x['variant'],x['max_tokens']);assert key not in outs;outs[key]=x
print('reserved',len(seen),'saved',len(outs),'unknown',len(seen)-len(outs))
for v in ('baseline','concise'):
 for cap in (2000,4096):
  xs=[x for k,x in outs.items() if k[1:]==(v,cap)];print(v,cap,'n',len(xs),'empty',sum(x['empty'] for x in xs),'length',sum(x['finish_reason']=='length' for x in xs),'output',sum(x['usage']['completion_tokens'] for x in xs),'input',sum(x['usage']['prompt_tokens'] for x in xs),'out_mean',round(statistics.mean(x['usage']['completion_tokens'] for x in xs),1))
obs=[json.loads(x) for x in (b/subs[-1]/'observations.jsonl').read_text().splitlines()];ends=[x['latency_ms'] for x in obs if x.get('stage')=='p0.prompt_limit' and x.get('event')=='end'];print('traced_elapsed_n',len(ends),'min_sec',min(ends)/1000,'median_sec',statistics.median(ends)/1000,'max_sec',max(ends)/1000)

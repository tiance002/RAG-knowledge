import json,sqlite3,hashlib,urllib.request,urllib.parse,urllib.error,time,os,sys
from pathlib import Path
P=Path('/opt/amrag/experiments/e20-20261030/approved-four-request-bundle.json')
O=Path('/opt/amrag/experiments/dynamic-routing-20261101')
def sha(x): return hashlib.sha256(x).hexdigest()
def canon(x): return json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
b=json.loads(P.read_bytes()); assert sha(P.read_bytes())=='62306478ff6904f4c5bb5f07b651819782a4384f0c8492b899e523bae28b15a4'
cases=[c for c in b['cases'] if c['arm']=='B']; assert [c['qid'] for c in cases]==['office50:031','office50:014']
db=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True)
rows=db.execute('select model_name,base_url,api_key from custom_models where model_name in (?,?)',('XingChenAGI/Xing4.0-29B','zai-org/GLM-5.3-Flash')).fetchall(); db.close(); assert len(rows)==2
models={n:(u.rstrip('/'),k) for n,u,k in rows}; assert all(urllib.parse.urlparse(u).hostname=='api.siliconflow.cn' and k for u,k in models.values())
s=sqlite3.connect('file:'+b['index_db_path']+'?mode=ro',uri=True); checks=[]
for c in cases:
 assert sha(canon(c['expected_messages']))==c['expected_messages_sha256']; assert sha(canon(c['expanded']))==c['expanded_snapshot_sha256']
 members=0
 for group in c['expanded']:
  for m in group.get('context_group',{}).get('source_members',[]):
   row=s.execute('select d.content,f.local_path,f.kb_id from document_chunks d join file_documents f on f.id=d.file_id where d.id=? and f.id=?',(m['id'],m['file_id'])).fetchone(); assert row and sha(row[0].encode())==m['content_hash'] and row[2]==c['kb_id']; assert m['file_id']=='office-'+sha(Path(row[1]).read_bytes()); members+=1
 assert members; size=len(canon(c['expected_messages'])); assert size+4096+512<256000
 checks.append({'qid':c['qid'],'arm':'B','members':members,'messages_sha256':c['expected_messages_sha256'],'messages_bytes':size})
s.close()
url,key=models['XingChenAGI/Xing4.0-29B']; req=urllib.request.Request(url+'/models',headers={'Authorization':'Bearer '+key})
with urllib.request.urlopen(req,timeout=30) as r: listing=json.load(r); code=r.status
names=[x.get('id') for x in listing.get('data',[])]; assert 'XingChenAGI/Xing4.0-29B' in names
pre={'status':'PASS','grant':'dynamic-routing-owner-20261101','provider_models_http':code,'cheap_listed':True,'expensive_listed':'zai-org/GLM-5.3-Flash' in names,'endpoint_sha256':sha(url.encode()),'bundle_sha256':sha(P.read_bytes()),'source_checks':checks,'capacity_source':'https://cloud-rd.siliconflow.cn/models','context_tokens':256000,'capacity_bound':'UTF-8 message bytes plus 4096 output and 512 framing < 256000','model_requests':0}
O.mkdir(mode=0o700,exist_ok=True)
if sys.argv[1]=='preflight':
 p=O/'preflight.json'; assert not p.exists(); p.write_text(json.dumps(pre,indent=2),encoding='utf8'); print(json.dumps(pre)); sys.exit()
assert sys.argv[1]=='execute'; prior=json.loads((O/'preflight.json').read_text()); assert prior==pre
ledger=sqlite3.connect(O/'ledger.sqlite3'); ledger.execute('pragma synchronous=FULL'); ledger.execute('create table if not exists attempts (qid text, model text, state text, info text, primary key(qid,model))'); ledger.execute('create table if not exists attempt_events (id integer primary key, qid text, model text, state text, info text, recorded_at text default CURRENT_TIMESTAMP)'); ledger.commit()
for c in reversed(cases):
 model='XingChenAGI/Xing4.0-29B'; assert not ledger.execute('select 1 from attempts where qid=? and model=?',(c['qid'],model)).fetchone()
 payload={'model':model,'messages':c['expected_messages'],'temperature':0.7,'max_tokens':4096,'stream':False}; ledger.execute('insert into attempts values(?,?,?,?)',(c['qid'],model,'UNKNOWN_PENDING',json.dumps({'grant':pre['grant'],'payload_sha256':sha(canon(payload))}))); ledger.execute('insert into attempt_events(qid,model,state,info) values(?,?,?,?)',(c['qid'],model,'UNKNOWN_PENDING',json.dumps({'grant':pre['grant'],'payload_sha256':sha(canon(payload))}))); ledger.commit()
 start=time.monotonic()
 try:
  req=urllib.request.Request(url+'/chat/completions',data=canon(payload),headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
  with urllib.request.urlopen(req,timeout=120) as r: raw=r.read(); status=r.status
  data=json.loads(raw); choices=[{'finish_reason':x.get('finish_reason'),'content':(x.get('message') or {}).get('content')} for x in data.get('choices',[])]; result={'http_status':status,'id':data.get('id'),'model':data.get('model'),'usage':data.get('usage'),'choices':choices,'elapsed_seconds':round(time.monotonic()-start,3),'response_sha256':sha(raw)}; state='RETURNED'; (O/(c['qid'].replace(':','-')+'-cheap.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
 except BaseException as e:
  result={'error_type':type(e).__name__,'elapsed_seconds':round(time.monotonic()-start,3)}; state='UNKNOWN'
 ledger.execute('update attempts set state=?,info=? where qid=? and model=?',(state,json.dumps(result,ensure_ascii=False),c['qid'],model)); ledger.execute('insert into attempt_events(qid,model,state,info) values(?,?,?,?)',(c['qid'],model,state,json.dumps(result,ensure_ascii=False))); ledger.commit(); print(json.dumps({'qid':c['qid'],'state':state,'detail':result},ensure_ascii=False),flush=True)
 if state!='RETURNED': break

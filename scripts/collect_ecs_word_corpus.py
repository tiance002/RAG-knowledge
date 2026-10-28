"""Fetch Word documents on ECS, preserving corpus ID/hash discrepancies."""
import hashlib,json,pathlib,sys,time,urllib.request,zipfile
root=pathlib.Path(sys.argv[1])/'office50-v1';sources=root/'sources';records=root/'download-records.jsonl'
existing=[json.loads(l)['source_url'] for l in records.open(encoding='utf-8') if json.loads(l).get('source_url','').endswith('.docx') and json.loads(l).get('status')=='skipped']
def get(url):
 for attempt in range(3):
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'rag-office-eval/1.0'}),timeout=30) as r:return r.read(6_000_001)
  except Exception:
   if attempt==2:raise
   time.sleep(2**attempt)
for url in existing:
 try:
  a=get(url);b=get(url);actual=hashlib.sha256(a).hexdigest();expected=pathlib.Path(url).stem
  assert a==b and a.startswith(b'PK') and len(a)<=6_000_000
  path=sources/(actual+'.docx')
  if not path.exists():path.write_bytes(a)
  if not zipfile.is_zipfile(path):raise ValueError('invalid OOXML')
  rec={'status':'downloaded','dataset':'superdoc-dev/docx-corpus','source_url':url,'source_id':expected,'path':str(path),
       'sha256':actual,'source_id_matches_sha256':actual==expected,'repeat_download_matches':True,'size':len(a)}
 except Exception as e:rec={'status':'skipped','source_url':url,'error':type(e).__name__}
 with records.open('a',encoding='utf-8') as f:f.write(json.dumps(rec)+'\n')
counts={s:len(list(sources.glob('*'+s))) for s in ('.docx','.xlsx')}
(root/'word-download-complete.json').write_text(json.dumps(counts));print(json.dumps(counts),flush=True)

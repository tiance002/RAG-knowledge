"""Download Office sources directly on ECS; record hashes and isolated failures."""
import base64,hashlib,json,pathlib,sys,time,urllib.request

base=pathlib.Path(sys.argv[1]);root=base/'office50-v1';root.mkdir(mode=0o700,exist_ok=True)
sources=root/'sources';sources.mkdir(exist_ok=True)
log=root/'download-records.jsonl'
def record(row):
 with log.open('a',encoding='utf-8') as f:f.write(json.dumps(row,ensure_ascii=False)+'\n')
def get(url,limit=6_000_000):
 last=None
 for attempt in range(1,4):
  try:
   with urllib.request.urlopen(urllib.request.Request(url,headers={'User-Agent':'rag-office-eval/1.0'}),timeout=30) as r:
    body=r.read(limit+1)
    if len(body)>limit:raise ValueError('download exceeds limit')
    return body
  except Exception as e:
   last=e;record({'url':url,'attempt':attempt,'status':'retry','error':type(e).__name__});time.sleep(attempt*3)
 raise RuntimeError(type(last).__name__)
def save(name,body,metadata):
 path=sources/name
 if path.exists():raise RuntimeError('source already exists')
 path.write_bytes(body);digest=hashlib.sha256(body).hexdigest()
 if not body.startswith(b'PK'):raise ValueError('not an OOXML ZIP file')
 record({'path':str(path),'size':len(body),'sha256':digest,'status':'downloaded',**metadata})
 return path
manifest='https://api.docxcorp.us/manifest?type=reports&topic=government&lang=en&min_confidence=0.9'
with urllib.request.urlopen(urllib.request.Request(manifest,headers={'User-Agent':'rag-office-eval/1.0'}),timeout=30) as r:
 urls=[r.readline().decode().strip() for _ in range(14)]
for u in urls:
 if not u:continue
 try:
  body=get(u);expected=pathlib.Path(u).stem
  if hashlib.sha256(body).hexdigest()!=expected:raise ValueError('content SHA mismatch')
  save(expected+'.docx',body,{'source_url':u,'dataset':'superdoc-dev/docx-corpus','filter':'reports/government/en/0.9'})
 except Exception as e:record({'source_url':u,'status':'skipped','error':type(e).__name__})
repo='msaltnet/excel-search-mcp'
tree=json.loads(get('https://api.github.com/repos/'+repo+'/git/trees/main?recursive=1'))['tree']
wanted=['sample/1_FY2018 PLS_Tables_1_thru_6A.xlsx','sample/2_FY2018 PLS_Tables_7_thru_13A.xlsx',
 'sample/BiotechCropsAllTables2024.xlsx','sample/Apples-2022.xlsx','sample/Avocados-2022.xlsx']
for i,name in enumerate(wanted):
 try:
  entry=next(x for x in tree if x['path']==name);url='https://api.github.com/repos/'+repo+'/git/blobs/'+entry['sha']
  body=base64.b64decode(json.loads(get(url))['content']);blob=hashlib.sha1(b'blob '+str(len(body)).encode()+b'\0'+body).hexdigest()
  if blob!=entry['sha']:raise ValueError('git blob mismatch')
  save('excel-'+str(i+1)+'.xlsx',body,{'source_url':url,'original_path':name,'git_blob_sha1':blob,'dataset':repo})
 except Exception as e:record({'original_path':name,'status':'skipped','error':type(e).__name__})
counts={suffix:len(list(sources.glob('*'+suffix))) for suffix in ('.docx','.xlsx')}
(root/'download-complete.json').write_text(json.dumps({'counts':counts,'status':'COMPLETE','real_provider_calls':0},indent=2))
print(json.dumps({'counts':counts,'status':'COMPLETE'}),flush=True)

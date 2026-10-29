"""Bounded cached-vector hybrid preflight; no external model or network calls."""
import asyncio,hashlib,json,os,shutil,sqlite3,sys,time,uuid
from array import array
from pathlib import Path
base=Path('/opt/amrag/experiments/pdf-table-hardcases-20261027'); data=base/'dataset200'; prior=base/'runtime200-r4'
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p):return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
freeze=json.loads((data/'freeze.json').read_text())
for n,h in freeze['files'].items():
 if sha(data/n)!=h:raise ValueError('freeze mismatch')
cases={x['qid']:x for name in ('gold100.jsonl','qrel100.jsonl') for x in rows(data/name)}
registry={x['qid']:x for x in rows(data/'registry200.jsonl')}; qrels={x['qid']:x for x in json.loads((prior/'resolved-qrels-after.json').read_text())}
controls=rows(prior/'control-sql.jsonl');kbs={r['value']['name'].removeprefix('prod-'):r['value']['id'] for r in controls if r['kind']=='kb'}
cache=sqlite3.connect('file:'+str(prior/'vector-cache.db')+'?mode=ro',uri=True)
def cached(q):return cache.execute('select dimensions,data from vectors where key=?',(hashlib.sha256(q.encode()).hexdigest(),)).fetchone()
valid=[q for q in cases if q in qrels and qrels[q].get('expected_chunk_ids') and cached(cases[q]['question'])]
pairs=[]
for q in valid:
 for a in valid:
  if q==a or registry[q]['profile']!=registry[a]['profile']:continue
  if not set(qrels[q]['expected_sources'])&set(qrels[a]['expected_sources']):continue
  pairs.append((q,a))
out=base/('p02-hybrid-preflight-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]);out.mkdir(mode=0o700)
shutil.copytree(prior/'chroma',out/'chroma')
os.environ.update(ENV='test',DATABASE_URL='sqlite:///'+str(prior/'isolated.db'),CHROMA_DB_DIR=str(out/'chroma'),RAG_OBSERVABILITY_ENABLED='false',ANONYMIZED_TELEMETRY='False')
sys.path.insert(0,sys.argv[1])
from sqlmodel import Session,create_engine
from app.services.retrieval import RetrievalService
engine=create_engine('sqlite:///file:'+str(prior/'isolated.db')+'?mode=ro&uri=true');service=RetrievalService()
async def embedding(session,kb_id,question):
 row=cached(question)
 if row is None:raise RuntimeError('CACHE_MISS_STOP')
 v=array('d');v.frombytes(row[1]);assert len(v)==row[0];return v.tolist()
service._embed_query_for_kb=embedding
memo={}; start=time.monotonic(); checked=0; found=[];failures=[]
def search(q,kb):
 key=(q,kb)
 if key not in memo:
  with Session(engine) as session:
   try:
    hits,metrics=asyncio.run(service.search(session,cases[q]['question'],[kb],search_mode='hybrid',top_k=5,score_threshold=0,vector_weight=.7,bm25_weight=.3,use_cache=False,log_retrieval=False))
    memo[key]=[{'id':x['id'],'score':x['score']} for x in hits]
   except Exception as e:
    failures.append({'qid':q,'error_type':type(e).__name__});memo[key]=None
 return memo[key]
for q,a in pairs:
 if time.monotonic()-start>45:break
 kb=kbs.get(registry[q]['profile'],'v200-'+registry[q]['profile']+'-after'); want=set(qrels[q]['expected_chunk_ids'])
 first=search(q,kb)
 if first is None or want.intersection(x['id'] for x in first):continue
 alternative=search(a,kb);checked+=1
 if alternative and want.intersection(x['id'] for x in alternative):
  found.append({'qid':q,'alternative_qid':a,'kb_id':kb,'profile':registry[q]['profile'],'question':cases[q]['question'],'alternative_query':cases[a]['question'],'expected_sources':qrels[q]['expected_sources'],'expected_chunk_ids':sorted(want),'original_hits':first,'alternative_hits':alternative,'shared_qrel_sources':sorted(set(qrels[q]['expected_sources'])&set(qrels[a]['expected_sources']))})
result={'status':'PASS' if found else 'NO_MATCH_WITHIN_BOUNDS','provider_calls':0,'time_limit_seconds':45,'elapsed_seconds':time.monotonic()-start,'cached_cases':len(valid),'shared_source_ordered_pairs':len(pairs),'evaluated_miss_pairs':checked,'distinct_searches':len(memo),'failures':failures,'config':{'search_mode':'hybrid','top_k':5,'score_threshold':0,'vector_weight':.7,'bm25_weight':.3,'rerank':False,'context_window':0},'freeze_sha256':sha(data/'freeze.json'),'qrels_sha256':sha(prior/'resolved-qrels-after.json'),'result':found}
(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'output':str(out),**result},ensure_ascii=False));sys.exit(0 if found else 1)

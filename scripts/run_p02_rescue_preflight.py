"""Zero-model BM25 selection against frozen qrels and immutable isolated index."""
import hashlib,json,os,re,sqlite3,sys,time,uuid
from pathlib import Path
base=Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
data=base/'dataset200'; prior=base/'runtime200-r4'; backend=Path(sys.argv[1])
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p): return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
freeze=json.loads((data/'freeze.json').read_text())
for n,h in freeze['files'].items():
    if digest(data/n)!=h: raise ValueError('dataset freeze mismatch')
registry={r['qid']:r for r in rows(data/'registry200.jsonl')}
gold={r['qid']:r for r in rows(data/'gold100.jsonl')}
qrels=json.loads((prior/'resolved-qrels-after.json').read_text())
controls=rows(prior/'control-sql.jsonl')
kbs={r['value']['name'].removeprefix('prod-'):r['value']['id'] for r in controls if r['kind']=='kb'}
os.environ.update(ENV='test',DATABASE_URL='sqlite:///'+str(prior/'isolated.db'),RAG_OBSERVABILITY_ENABLED='false')
sys.path.insert(0,str(backend))
from sqlmodel import Session, create_engine
from app.services.bm25 import BM25Index,tokenize
engine=create_engine('sqlite:///file:'+str(prior/'isolated.db')+'?mode=ro&uri=true')
index=BM25Index(); found=None; tested=0
with Session(engine) as session:
 for rel in qrels:
    case=gold.get(rel['qid']); wanted=set(rel.get('expected_chunk_ids',[]))
    if not case or not wanted: continue
    profile=registry[case['qid']]['profile']; kb=kbs.get(profile,'v200-'+profile+'-after')
    def search(q): return index.search(session,q,kb,top_k=5)
    hits=search(case['question']); tested+=1
    if wanted.intersection(x['chunk_id'] for x in hits): continue
    toks=tokenize(case['question']); alternatives=[]
    for size in (2,3,4):
      for i in range(len(toks)-size+1):
        candidate=' '.join(toks[i:i+size])
        if candidate not in alternatives: alternatives.append(candidate)
    for q in alternatives[:20]:
      alt=search(q)
      if wanted.intersection(x['chunk_id'] for x in alt):
       found={'qid':case['qid'],'profile':profile,'kb_id':kb,'question':case['question'],'alternative_query':q,'expected_sources':rel['expected_sources'],'unit':rel['unit'],'expected_chunk_ids':sorted(wanted),'original_hits':hits,'alternative_hits':alt,'alternative_method':'contiguous tokens extracted exclusively from original question','candidate_deletion':False};break
    if found: break
out=base/('p02-rescue-preflight-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8]);out.mkdir(mode=0o700)
result={'status':'PASS' if found else 'NO_MATCH','tested_cases':tested,'top_k':5,'index_db':str(prior/'isolated.db'),'freeze_sha256':digest(data/'freeze.json'),'qrels_sha256':digest(prior/'resolved-qrels-after.json'),'provider_calls':0,'result':found}
(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'output':str(out),**result},ensure_ascii=False),flush=True)
sys.exit(0 if found else 1)

"""P0.3 frozen-query preflight; read-only, zero network/model calls."""
import hashlib, json, pathlib, sqlite3
BASE=pathlib.Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
DATA=BASE/'dataset200'; PRIOR=BASE/'runtime200-r4'
ORIGINAL='What are two examples of reflecting surfaces?'; ALTERNATIVE='examples reflecting'
def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def rows(p):return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()]
freeze=json.loads((DATA/'freeze.json').read_text())
for name,h in freeze['files'].items():
 p=(DATA/name).resolve()
 if not p.is_relative_to(DATA.resolve()) or digest(p)!=h:raise RuntimeError('Frozen data mismatch')
gold=next(x for x in rows(DATA/'gold100.jsonl') if x['question']==ORIGINAL)
qrel=next(x for x in json.loads((PRIOR/'resolved-qrels-after.json').read_text()) if x['qid']==gold['qid'])
if gold['expected_pages']!=[9] or qrel['unit']!='pdf_page' or not qrel['expected_chunk_ids']:raise RuntimeError('Gold page mismatch')
c=sqlite3.connect('file:'+str(PRIOR/'isolated.db')+'?mode=ro',uri=True)
chunks=[]
for cid in qrel['expected_chunk_ids']:
 row=c.execute('select id,page_number,content_type,content from document_chunks where id=?',(cid,)).fetchone()
 if not row or row[1]!=9:raise RuntimeError('Expected source chunk mismatch')
 chunks.append({'id':row[0],'page':row[1],'type':row[2], 'content_sha256':hashlib.sha256(row[3].encode()).hexdigest(), 'has_mirror_and_shiny':all(x in row[3].lower() for x in ('shiny','mirror'))})
c.close()
v=sqlite3.connect('file:'+str(PRIOR/'vector-cache.db')+'?mode=ro',uri=True)
def cached(s):return bool(v.execute('select 1 from vectors where key=?',(hashlib.sha256(s.encode()).hexdigest(),)).fetchone())
original_cached,alternative_cached=cached(ORIGINAL),cached(ALTERNATIVE);v.close()
result={'status':'BLOCKED','reason':'Alternative omits the noun surfaces and cardinality two, leaving the target ambiguous; semantic equivalence is not established.','original':ORIGINAL,'alternative':ALTERNATIVE,'qid':gold['qid'],'expected_pages':gold['expected_pages'],'expected_source_sha256':gold['expected_source_sha256'],'expected_answer_points':gold['answer_points'],'frozen_source_type':gold['official_row']['evidence_sources'],'qrel_expected_sources':qrel['expected_sources'],'chunks':chunks,'original_embedding_cached':original_cached,'alternative_embedding_cached':alternative_cached,'new_embedding_requests':0,'generation_requests':0,'dataset_freeze_sha256':digest(DATA/'freeze.json')}
print(json.dumps(result,ensure_ascii=False,indent=2))

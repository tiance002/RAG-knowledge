"""Standalone ECS experiment: frozen200cases,400retrievals,200answers; production read-only."""
import argparse,asyncio,base64,concurrent.futures,hashlib,json,os,pathlib,shutil,sqlite3,sys,threading,time
from ecs_benchmark200_protocol import Ledger,sha,template_sha
from ecs_benchmark200_helpers import VectorCache,bulk_bm25,verify_bulk_bm25


def answer_prompt(context):
 prompt=f'''你是一个智能问答助手，请根据以下参考资料回答用户的问题。
参考资料：
{context}
要求：
1. 基于参考资料回答，如果资料中没有相关信息，请如实说明
2. 回答要准确、完整、有条理
3. 适当引用来源（如"根据文档..."）
4. 使用中文回答'''
 return prompt


def main():
 ap=argparse.ArgumentParser();ap.add_argument('--root',required=True);ap.add_argument('--preflight',action='store_true')
 args=ap.parse_args();base=pathlib.Path(args.root)
 policy=json.loads((base/'model-policy.json').read_bytes());root=base/policy['runtime_name']
 freeze=json.loads((base/'dataset200/freeze.json').read_bytes())
 for name,digest in freeze['files'].items():assert sha((base/'dataset200'/name).read_bytes())==digest
 cases=[]
 for name in ('gold100.jsonl','qrel100.jsonl'):
  cases.extend(json.loads(s) for s in (base/'dataset200'/name).read_text(encoding='utf-8').splitlines())
 registry={r['qid']:r for r in (json.loads(s) for s in (base/'dataset200/registry200.jsonl').read_text(encoding='utf-8').splitlines())}
 assert len(cases)==len(registry)==200
 assembly=json.loads((root/'assembled.json').read_bytes())
 assert assembly['status']=='ASSEMBLED' and sha((root/'profiles.jsonl').read_bytes())==assembly['profiles_sha256']
 assert template_sha(__file__)=='7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94'
 prod=pathlib.Path('/opt/amrag/backend/app')
 production_hashes={str(p.relative_to('/opt/amrag')):sha(p.read_bytes()) for p in prod.rglob('*.py')}
 expected=json.loads((base/'identity-before.json').read_bytes())['source_files']
 assert production_hashes==expected
 backend=root/'source/backend'
 if not backend.exists():shutil.copytree(prod,backend/'app',ignore=shutil.ignore_patterns('__pycache__'))
 (backend/'app/__init__.py').write_text('');(backend/'app/services/__init__.py').write_text('')
 os.environ.update(ENV='test',DATABASE_URL='sqlite:///'+str(root/'isolated.db'),CHROMA_DB_DIR=str(root/'chroma'),
                   IMAGE_DIR=str(root/'images'),UPLOAD_DIR=str(root/'uploads'),ANONYMIZED_TELEMETRY='False')
 os.chdir(root);sys.path.insert(0,str(backend))
 import httpx
 from sqlmodel import SQLModel,Session,select
 from sqlalchemy import insert
 from app.core.database import engine
 from app.core.config import get_settings
 from app.models import CustomModel,KnowledgeBase,FileDocument,DocumentChunk,ContentType
 from app.models.models import FileStatus
 from app.services.embedding import EmbeddingService
 from app.services.vector_store import VectorStoreService
 from app.services.retrieval import RetrievalService
 from app.services.rerank import RerankService,batch_rerank,expand_context
 from openai import OpenAI
 con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);con.row_factory=sqlite3.Row
 ids=json.loads(pathlib.Path('/opt/amrag/ids.json').read_bytes())['models']
 models={role:CustomModel(**dict(con.execute('select * from custom_models where id=?',(ids[role],)).fetchone())) for role in ('llm','embedding','rerank','vlm')};con.close()
 assert models['vlm'].model_name==policy['vlm_model'],'Persistent VLM policy drift'
 prior_models=json.loads(pathlib.Path('/opt/amrag/experiments/pdf-table-ablation-20261027-r1/state.json').read_bytes())['models']
 for role,value in prior_models.items():
  current={'id':models[role].id,'model':models[role].model_name,'endpoint_sha256':sha(models[role].base_url.encode())}
  if role=='vlm':
   assert value['id']==current['id'] and value['model']=='Qwen/Qwen2.5-VL-72B-Instruct' and value['endpoint_sha256']==current['endpoint_sha256']
  else:assert value==current
 profiles=[json.loads(l) for l in (root/'profiles.jsonl').open()]
 control=[json.loads(l) for l in (root/'control-sql.jsonl').open()]
 old_kbs={r['value']['name'].removeprefix('prod-'):r['value']['id'] for r in control if r['kind']=='kb'}
 assert set(old_kbs)=={'longbench-zh','hotpotqa'}
 for r in control:
  if r['kind']=='kb':assert r['value']['embedding_model']==models['embedding'].id
 assets=json.loads((root/'image-assets.json').read_bytes())
 assert len(assets)==166
 bounds={'embedding_inputs':assembly['profile_chunks']+200,'rerank_calls':4000,'generation_calls':200,'caption_calls':len(assets)}
 prior_caption_path=root/'prior-caption-results.jsonl'
 prior_caption_sha=sha(prior_caption_path.read_bytes()) if prior_caption_path.exists() else None
 manifest={'schema':'dataset200-ab/v1','dataset_freeze_sha256':sha((base/'dataset200/freeze.json').read_bytes()),
  'assembly':assembly,'bounds':bounds,'production_source_hashes':production_hashes,'runner_sha256':sha(pathlib.Path(__file__).read_bytes()),
  'persistent_model_policy':policy,
  'fixed':{'top_k':50,'score_threshold':0.,'search_mode':'hybrid','vector_weight':.7,'bm25_weight':.3,
   'rerank_top_k':20,'context_window':1,'temperature':.7,'max_output_tokens':2000,'agent':False},
  'orchestration':{'caption_workers':2,'embedding_workers':2,'retrieval_workers':2,'generation_workers':2},
  'caption_prompt':get_settings().VLM_PROMPT,'caption_model':models['vlm'].model_name,
  'caption_asset_sha256s':sorted(assets),'prior_caption_results_sha256':prior_caption_sha,
  'caption_retry_until_success':True,
  'prompt_template_sha256':template_sha(__file__),'protocol':'caption failures retry same image/model until nonempty success; all other provider failures stop dispatch'}
 old_manifest=json.loads((root/'run-manifest.json').read_bytes()) if (root/'run-manifest.json').exists() else None
 if old_manifest:assert old_manifest==manifest
 else:(root/'run-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
 ledger=Ledger(root,models,bounds,manifest)
 ledger.state['allowed_caption_assets']=sorted(assets);ledger.save()
 unit=verify_bulk_bm25();(root/'bulk-bm25-verification.json').write_text(json.dumps(unit))
 vs=VectorStoreService()
 file_kbs={r['value']['id']:r['value']['kb_id'] for r in control if r['kind']=='file'}
 for key,kbid in old_kbs.items():
  wanted=[r['value']['id'] for r in control if r['kind']=='chunk' and file_kbs.get(r['value']['file_id'])==kbid]
  collection=vs.client.get_collection(vs._get_collection_name(kbid))
  for start in range(0,len(wanted),256):assert len(collection.get(ids=wanted[start:start+256],include=[])['ids'])==len(wanted[start:start+256])
 if args.preflight:
  print(json.dumps({'status':'PREPARED_ZERO_CALLS','cases':200,'bounds':bounds,'bulk_bm25':unit,'snapshot_vector_coverage':'PASS'}),flush=True);return
 ledger.state['status']='RUNNING';ledger.save();ledger.install()
 lock=threading.RLock()
 def append(name,value):
  with lock:
   with (root/name).open('a',encoding='utf-8') as f:f.write(json.dumps(value,ensure_ascii=False)+'\n')
 def client(role):
  m=models[role]
  return OpenAI(base_url=m.base_url,api_key=m.api_key,max_retries=0,timeout=120,http_client=httpx.Client(trust_env=False))
 def parallel(function,items,workers):
  with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
   for value in pool.map(function,items):yield value
 ledger.phase('CAPTIONS')
 captions={}
 if prior_caption_path.exists():
  for line in prior_caption_path.read_text(encoding='utf-8').splitlines():
   row=json.loads(line);key=row.get('asset_sha256');caption_text=row.get('caption','')
   if row.get('finish_reason')=='stop' and isinstance(caption_text,str) and caption_text.strip() and key in assets:
    captions[key]=caption_text
 (root/'caption-cache-reuse.json').write_text(json.dumps({'source':str(prior_caption_path),'source_sha256':prior_caption_sha,
   'reused_assets':sorted(captions),'reused_count':len(captions)},indent=2))
 print(json.dumps({'captions_reused':len(captions),'total':len(assets)}),flush=True)
 def caption(pair):
  key,asset=pair;path=pathlib.Path(asset['path']);data=path.read_bytes();assert sha(data)==key
  mime='image/jpeg' if path.suffix.lower() in ('.jpg','.jpeg') else 'image/png'
  attempt=0
  while True:
   if ledger.stop.is_set():raise RuntimeError('Stopped by an unrelated provider failure')
   attempt+=1;start=time.monotonic()
   try:
    with client('vlm') as api:
     response=api.chat.completions.create(model=models['vlm'].model_name,messages=[{'role':'user','content':[
        {'type':'text','text':manifest['caption_prompt']},{'type':'image_url','image_url':{'url':f'data:{mime};base64,'+base64.b64encode(data).decode()}}]}],max_tokens=1000)
    text=response.choices[0].message.content or ''
    row={'asset_sha256':key,'caption':text,'finish_reason':response.choices[0].finish_reason,
         'usage':response.usage.model_dump() if response.usage else None,'seconds':time.monotonic()-start,'attempt':attempt}
    append('caption-results.jsonl',row)
    if not text.strip() or response.choices[0].finish_reason!='stop':
     raise RuntimeError('Incomplete caption response; retry this image')
    return key,text
   except Exception as exc:
    delay=min(60,2**min(attempt,6))
    print(json.dumps({'caption_retry':key,'attempt':attempt,'error':type(exc).__name__,'delay_seconds':delay}),flush=True)
    time.sleep(delay)
 for key,text in parallel(caption,[(k,v) for k,v in assets.items() if k not in captions],2):
  captions[key]=text;print(json.dumps({'captions_completed':len(captions),'total':len(assets)}),flush=True)
 for profile in profiles:
  for c in profile['chunks']:
   if c['content_type']=='image' and not c.get('caption_frozen'):c['content']=captions[c['image_asset_sha256']]+'\n'+c['content']
 (root/'caption-snapshot.json').write_text(json.dumps(captions,ensure_ascii=False))
 ledger.phase('EMBEDDING')
 cache=VectorCache(root/'vector-cache.db')
 texts=list(dict.fromkeys([c['content'] for p in profiles for c in p['chunks']]+[c['question'] for c in cases]))
 missing=[t for t in texts if not cache.has(t)]
 def embed(batch):
  if ledger.stop.is_set():raise RuntimeError('Stopped')
  m=models['embedding'];service=EmbeddingService(base_url=m.base_url,api_key=m.api_key,model=m.model_name)
  vectors=asyncio.run(service.embed_documents(batch,model_id=m.model_name));assert len(batch)==len(vectors)
  assert all(len(v)==1024 for v in vectors);return batch,vectors
 for batch,vectors in parallel(embed,[missing[i:i+16] for i in range(0,len(missing),16)],2):cache.put_many(batch,vectors)
 ledger.phase('INDEXING');SQLModel.metadata.create_all(engine)
 mapping={};kb_chunks={};file_info={};all_ids=set()
 kb_for=lambda profile,arm:old_kbs[profile] if profile in old_kbs else 'v200-'+profile+('-'+arm if profile.startswith('pdf-') else '')
 def put_chunk(session,row,kbid,source_id,pages):
  cid=row['id'];assert cid not in all_ids;all_ids.add(cid)
  session.add(DocumentChunk(**row));mapping[cid]={'source_id':source_id,'pages':[p for p in pages if p is not None]}
  kb_chunks.setdefault(kbid,[]).append((cid,row['content']))
 with Session(engine) as s:
  for row in control:
   v=row['value'];kind=row['kind']
   if kind=='kb':s.add(KnowledgeBase(**{k:v[k] for k in ('id','name','description','embedding_model','vlm_model','chunk_count','is_deleted')}));s.commit()
   elif kind=='file':
    f={k:v[k] for k in ('id','kb_id','name','local_path','size')};s.add(FileDocument(**f));file_info[v['id']]=(v['kb_id'],v['id']);s.commit()
   else:
    c={k:v[k] for k in ('id','file_id','content','page_number','original_index','parent_id','heading_level','heading_text','chunk_type','token_count')}
    c['content_type']=ContentType(v['content_type'].lower());kbid,fid=file_info[v['file_id']]
    put_chunk(s,c,kbid,fid,[v['page_number']])
  s.commit()
  for p in profiles:
   kbid=kb_for(p['profile'],p['arm'])
   if s.get(KnowledgeBase,kbid) is None:
    s.add(KnowledgeBase(id=kbid,name=kbid,embedding_model=models['embedding'].id,vlm_model=models['vlm'].id));s.commit()
   fid=kbid+'-'+sha(p['source_id'].encode())
   if p['profile'].startswith('pdf-'):path=json.loads((root/'sources.json').read_bytes())[p['source_id']]['path']
   else:path=str(root/'text-sources'/p['profile']/(sha((p['profile']+':'+p['source_id']).encode())+'.txt'))
   s.add(FileDocument(id=fid,kb_id=kbid,name=p['name'],local_path=path,size=pathlib.Path(path).stat().st_size));s.commit()
   docs=[];vectors=[]
   for i,c in enumerate(p['chunks']):
    cid=fid+'-'+str(i)
    row={'id':cid,'file_id':fid,'content':c['content'],'page_number':c['page_number'],'content_type':ContentType(c['content_type']),'original_index':i}
    put_chunk(s,row,kbid,p['source_id'],c.get('source_pages') or [c['page_number']])
    docs.append({'id':cid,'content':c['content'],'metadata':{'file_id':fid,'file_name':p['name'],'page_number':c['page_number'] or 0,'content_type':c['content_type']}})
    vectors.append(cache.get(c['content']))
    if len(docs)==64:vs.add_documents(kbid,docs,vectors);docs=[];vectors=[]
   if docs:vs.add_documents(kbid,docs,vectors)
   s.commit()
  for kbid,values in kb_chunks.items():bulk_bm25(s,kbid,values)
  assert not s.exec(select(CustomModel)).first()
 query_vectors={c['question']:cache.get(c['question']) for c in cases}
 def labels(cid,unit):
  src=mapping[cid]
  return {src['source_id']} if unit=='source_document' else {src['source_id']+':'+str(p) for p in src['pages']}
 def expected(case):
  if case.get('positive_document_ids'):return 'source_document',set(case['positive_document_ids'])
  if case.get('expected_pages'):return 'pdf_page',{case['expected_source_sha256']+':'+str(p) for p in case['expected_pages']}
  return 'none',set()
 def recall(ranked,unit,wanted,unique=False):
  if not wanted:return None
  found=set()
  if unique:
   sequence=[]
   for r in ranked:
    for key in sorted(labels(r['id'],unit)):
     if key not in sequence:sequence.append(key)
   found=set(sequence[:10])
  else:
   for r in ranked[:10]:found.update(labels(r['id'],unit))
  return len(found&wanted)/len(wanted)
 for arm in ('before','after'):
  qrels=[]
  for case in cases:
   unit,wanted=expected(case)
   if not wanted:continue
   profile=registry[case['qid']]['profile'];kbid=kb_for(profile,arm)
   qrels.append({'qid':case['qid'],'unit':unit,'expected_sources':sorted(wanted),
      'expected_chunk_ids':[cid for cid,_ in kb_chunks[kbid] if labels(cid,unit)&wanted]})
  (root/('resolved-qrels-'+arm+'.json')).write_text(json.dumps(qrels,ensure_ascii=False))
 ledger.phase('RETRIEVAL')
 class ModelSession:
  def __init__(self,session):self.session=session
  def get(self,kind,key):
   if kind is CustomModel:return next((m for m in models.values() if m.id==key),None)
   return self.session.get(kind,key)
  def __getattr__(self,key):return getattr(self.session,key)
 contexts=root/'contexts';contexts.mkdir()
 jobs=[]
 for case in cases:
  for arm in (('before','after') if case['ordinal']%2==0 else ('after','before')):jobs.append((case,arm))
 def retrieve(job):
  if ledger.stop.is_set():raise RuntimeError('Stopped')
  case,arm=job;meta=registry[case['qid']];kbid=kb_for(meta['profile'],arm);start=time.monotonic()
  service=RetrievalService()
  async def query(session,kb_id,text):return query_vectors[text]
  service._embed_query_for_kb=query
  with Session(engine) as raw:
   session=ModelSession(raw)
   candidates,metrics=asyncio.run(service.search(session,case['question'],[kbid],search_mode='hybrid',top_k=50,
      score_threshold=0,vector_weight=.7,bm25_weight=.3,use_cache=False,log_retrieval=False))
   unit,wanted=expected(case)
   raw_recall=recall(candidates,unit,wanted);raw_unique=recall(candidates,unit,wanted,True)
   ranked=batch_rerank(case['question'],candidates,RerankService(session),models['rerank'],top_k=20,score_threshold=0)
   if ledger.stop.is_set() or not ranked:raise RuntimeError('Rerank failed or empty')
   expanded=expand_context(ranked,session,window_size=1)
   context='\n\n'.join(f'[{i}] 来源: {r.get("file_name","未知")}\n{r.get("content","")}' for i,r in enumerate(expanded,1))
   prompt=answer_prompt(context)
   (contexts/f'{case["ordinal"]}-{arm}.txt').write_text(context,encoding='utf-8')
   row={'ordinal':case['ordinal'],'qid':case['qid'],'arm':arm,**meta,'metric_unit':unit,
    'raw_recall_at_10':raw_recall,'raw_unique_recall_at_10':raw_unique,
    'recall_at_10':recall(ranked,unit,wanted),'unique_recall_at_10':recall(ranked,unit,wanted,True),
    'sources_at_10':[{'chunk_id':r['id'],**mapping[r['id']]} for r in ranked[:10]],
    'context_sha256':sha(context.encode()),'prompt_sha256':sha(prompt.encode()),'context_chars':len(context),
    'retrieval_metrics':metrics,'seconds':time.monotonic()-start}
  append('retrieval-results.jsonl',row);return row
 retrieved={}
 for row in parallel(retrieve,jobs,2):
  retrieved[(row['ordinal'],row['arm'])]=row
  print(json.dumps({'retrieval_completed':len(retrieved),'total':400}),flush=True)
 ledger.phase('GENERATION')
 def generate(job):
  if ledger.stop.is_set():raise RuntimeError('Stopped')
  case,arm=job;ctx=(contexts/f'{case["ordinal"]}-{arm}.txt').read_text(encoding='utf-8');prompt=answer_prompt(ctx);start=time.monotonic()
  with client('llm') as api:
   response=api.chat.completions.create(model=models['llm'].model_name,messages=[{'role':'system','content':prompt},
      {'role':'user','content':case['question']}],temperature=.7,max_tokens=2000,stream=False)
  row={**retrieved[(case['ordinal'],arm)],'question':case['question'],'answer':response.choices[0].message.content,
       'finish_reason':response.choices[0].finish_reason,'usage':response.usage.model_dump() if response.usage else None,
       'response_model':response.model,'generation_seconds':time.monotonic()-start}
  append('results.jsonl',row);return row
 count=0
 for row in parallel(generate,[j for j in jobs if registry[j[0]['qid']]['split']=='answer'],2):
  count+=1;print(json.dumps({'answers_completed':count,'total':200}),flush=True)
 assert count==200 and len(retrieved)==400
 ledger.state['status']='COMPLETED';ledger.phase('COMPLETED')


if __name__=='__main__':main()

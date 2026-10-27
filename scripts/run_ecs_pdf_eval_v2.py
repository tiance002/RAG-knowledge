"""Isolated paired evaluation using frozen ECS retrieval code and live models.

Run in a dedicated experiment directory with /opt/amrag/.venv/bin/python.
Production SQLite is read-only; production Chroma, KBs and services are untouched.
"""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time
import ast


def sha(value):
    return hashlib.sha256(value).hexdigest()


def prompt_template_sha256(path):
    tree=ast.parse(Path(path).read_text(encoding='utf-8'))
    values=[node.value for node in ast.walk(tree) if isinstance(node,ast.Assign)
            and any(isinstance(t,ast.Name) and t.id=='prompt' for t in node.targets)]
    if len(values)!=1:raise ValueError('Expected exactly one answer prompt template')
    return sha(ast.dump(values[0],include_attributes=False).encode())


def validate_protocol(payload):
    if len(payload['cases'])!=12 or len({c['qid'] for c in payload['cases']})!=12:
        raise ValueError('Expected 12 unique frozen cases')
    if payload['bounds']['generation_calls']!=24:
        raise ValueError('Expected exactly two bounded arms per case')
    expected={'top_k':50,'score_threshold':0.0,'search_mode':'hybrid','vector_weight':.7,
        'bm25_weight':.3,'rerank_enabled':True,'rerank_top_k':20,'context_window':1,
        'temperature':.7,'max_output_tokens':2000,
        'image_policy':'identical_existing_caption_text_no_new_VLM_calls',
        'sources':'same_three_ECS_source_hashes_both_arms'}
    if payload['fixed']!=expected:raise ValueError('Frozen inference configuration changed')


def main():
    root=Path(__file__).resolve().parent
    payload_bytes=(root/'input.json').read_bytes()
    payload=json.loads(payload_bytes)
    prior=json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else None
    if prior and (prior.get('requests') or prior['status']!='PREPARED' or prior['input_sha256']!=sha(payload_bytes)):
        raise SystemExit('Existing dispatch journal: no automatic replay')
    validate_protocol(payload)
    production_hashes={str(p.relative_to('/opt/amrag')):sha(p.read_bytes()) for p in Path('/opt/amrag/backend/app').rglob('*.py')}
    if production_hashes!=payload['expected_production_source_hashes']:
        raise ValueError('Production source differs from frozen preflight')
    # Freeze actual deployed source; lazy package exports only avoid starting the API.
    backend=root/'source/backend'
    if not backend.exists():
        shutil.copytree('/opt/amrag/backend/app',backend/'app',ignore=shutil.ignore_patterns('__pycache__'))
    (backend/'app/__init__.py').write_text('')
    (backend/'app/services/__init__.py').write_text('')
    os.environ.update(DATABASE_URL='sqlite:///'+str(root/'isolated.db'),
                      CHROMA_DB_DIR=str(root/'chroma'),IMAGE_DIR=str(root/'images'),
                      UPLOAD_DIR=str(root/'uploads'),ANONYMIZED_TELEMETRY='False')
    os.chdir(root)
    sys.path.insert(0,str(backend))
    import httpx
    from sqlmodel import SQLModel,Session,select
    from app.core.database import engine
    from app.core.config import get_settings
    from app.models import CustomModel,KnowledgeBase,FileDocument,DocumentChunk,ContentType
    from app.services.embedding import EmbeddingService
    from app.services.vector_store import VectorStoreService
    from app.services.bm25 import BM25Index
    from app.services.retrieval import RetrievalService
    from app.services.rerank import RerankService,batch_rerank,expand_context
    from openai import OpenAI

    # Model credentials stay in process memory and never enter the isolated DB/output.
    con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True)
    con.row_factory=sqlite3.Row
    ids=json.loads(Path('/opt/amrag/ids.json').read_text())['models']
    models={}
    for role in ('llm','embedding','rerank'):
        row=dict(con.execute('select * from custom_models where id=?',(ids[role],)).fetchone())
        models[role]=CustomModel(**row)
    con.close()
    settings=get_settings()
    state={'status':'PREPARED','input_sha256':sha(payload_bytes),'requests':[],
           'embedding_inputs':0,'rerank_calls':0,'generation_calls':0,
           'models':{r:{'id':m.id,'model':m.model_name,'endpoint_sha256':sha(m.base_url.encode())} for r,m in models.items()},
           'fixed':payload['fixed'],'source_hashes':{str(p.relative_to(backend)):sha(p.read_bytes()) for p in (backend/'app').rglob('*.py')}}
    state['prompt_template_sha256']=prompt_template_sha256(Path(__file__))
    if state['prompt_template_sha256']!=payload['expected_prompt_template_sha256']:
        raise ValueError('Answer prompt template changed')
    if state['models']!=payload['expected_models']:
        raise ValueError('Model identity differs from frozen baseline')
    if prior and prior['source_hashes']!=state['source_hashes']:
        raise ValueError('Frozen source changed')
    def save():
        temp=root/'state.tmp'
        temp.write_text(json.dumps(state,indent=2),encoding='utf-8')
        temp.replace(root/'state.json')
    allowed={m.base_url.rstrip('/') for m in models.values()}
    def reserve(request):
        if state['status']=='UNKNOWN_STOP':raise RuntimeError('Previous provider failure: stopped')
        url=str(request.url)
        if not any(url.startswith(base+'/') for base in allowed):
            raise RuntimeError('Unexpected network destination')
        body=json.loads(request.content)
        model=body.get('model')
        if request.url.path.endswith('/embeddings') and model==models['embedding'].model_name:
            count=len(body['input']) if isinstance(body['input'],list) else 1
            if state['embedding_inputs']+count>payload['bounds']['embedding_inputs']:raise RuntimeError('Embedding bound')
            state['embedding_inputs']+=count;role='embedding'
        elif request.url.path.endswith('/rerank') and model==models['rerank'].model_name:
            if state['rerank_calls']>=payload['bounds']['rerank_calls']:raise RuntimeError('Rerank bound')
            state['rerank_calls']+=1;role='rerank'
        elif request.url.path.endswith('/chat/completions') and model==models['llm'].model_name:
            if body.get('max_tokens')!=2000 or state['generation_calls']>=24:raise RuntimeError('Generation bound')
            state['generation_calls']+=1;role='generation'
        else:
            state['status']='UNKNOWN_STOP';save()
            raise RuntimeError('Unexpected model/endpoint; fallback prohibited')
        record={'role':role,'request_sha256':sha(request.content),'status':'RESERVED','ordinal':len(state['requests'])}
        state['requests'].append(record);save()
        return record
    def finish(record,response=None):
        if response is None or response.status_code>=400:
            record['status']='UNKNOWN_OR_FAILED';state['status']='UNKNOWN_STOP'
        else:record['status']='HTTP_SUCCESS'
        if response is not None and response.is_stream_consumed:
            try:
                record['usage']=response.json().get('usage')
            except (ValueError,AttributeError):
                record['usage']=None
        save()
    sync_send=httpx.Client.send;async_send=httpx.AsyncClient.send
    def guarded_sync(client,request,*args,**kwargs):
        record=reserve(request)
        try:response=sync_send(client,request,*args,**kwargs)
        except BaseException:finish(record);raise
        finish(record,response);return response
    async def guarded_async(client,request,*args,**kwargs):
        record=reserve(request)
        try:response=await async_send(client,request,*args,**kwargs)
        except BaseException:finish(record);raise
        finish(record,response);return response
    httpx.Client.send=guarded_sync;httpx.AsyncClient.send=guarded_async
    save()
    if '--preflight' in sys.argv:
        print(json.dumps({'status':'PREPARED_ZERO_CALLS','input_sha256':state['input_sha256'],
                          'models':state['models'],'bounds':payload['bounds']}),flush=True)
        return
    if '--execute' not in sys.argv:raise SystemExit('Use --preflight or --execute')
    if '--reuse-before' in sys.argv:raise ValueError('V2 uses fresh paired inference')
    state['runner_sha256']=sha(Path(__file__).read_bytes())
    state['status']='RUNNING';save()
    SQLModel.metadata.create_all(engine)
    emb=models['embedding']
    embedding=EmbeddingService(base_url=emb.base_url,api_key=emb.api_key,model=emb.model_name)
    texts=list(dict.fromkeys(c['content'] for chunks in payload['arms'].values() for c in chunks))
    cache={}
    async def embed_all():
        for index in range(0,len(texts),16):
            batch=texts[index:index+16]
            vectors=await embedding.embed_documents(batch,model_id=emb.model_name)
            if len(vectors)!=len(batch):raise ValueError('Embedding response cardinality')
            cache.update(zip(batch,vectors))
    asyncio.run(embed_all())
    vector_store=VectorStoreService();bm25=BM25Index()
    source_ids={};chunk_sources={}
    with Session(engine) as session:
        for arm,chunks in payload['arms'].items():
            kb_id='pdf-ab-'+arm
            session.add(KnowledgeBase(id=kb_id,name=kb_id,embedding_model=emb.id,vlm_model='frozen-no-call'))
            for c in chunks:
                source_ids[c['source_sha256']]=c['file_name']
            for source,name in source_ids.items():
                session.add(FileDocument(id=arm+'-'+source,kb_id=kb_id,name=name,
                    local_path=str(root/'no-source-mutations'/source),size=0))
            session.commit()
            documents=[];vectors=[]
            for index,c in enumerate(chunks):
                cid=arm+'-'+sha(json.dumps([c['source_sha256'],index,c['content']],sort_keys=True).encode())
                file_id=arm+'-'+c['source_sha256']
                chunk=DocumentChunk(id=cid,file_id=file_id,content=c['content'],page_number=c['page_number'],
                    content_type=ContentType(c['content_type']),image_path=None,original_index=c['original_index'])
                session.add(chunk)
                chunk_sources[cid]=(c['source_sha256'],c['page_number'])
                documents.append({'id':cid,'content':c['content'],'metadata':{'file_id':file_id,
                    'file_name':c['file_name'],'page_number':c['page_number'] or 0,'content_type':c['content_type']}})
                vectors.append(cache[c['content']])
            session.commit()
            for chunk in session.exec(select(DocumentChunk).where(DocumentChunk.file_id.in_([arm+'-'+s for s in source_ids]))).all():
                bm25.index_chunk(session,chunk.id,chunk.content,kb_id)
            session.commit()
            vector_store.add_documents(kb_id,documents,vectors)
    # Proxy model lookup leaves credentials outside the isolated database.
    class ModelSession:
        def __init__(self,session):self.session=session
        def get(self,kind,key):
            if kind is CustomModel:return next((m for m in models.values() if m.id==key),None)
            return self.session.get(kind,key)
        def __getattr__(self,name):return getattr(self.session,name)
    retriever=RetrievalService()
    query_cache={}
    async def cached_query(session,kb_id,query):
        if query not in query_cache:query_cache[query]=await embedding.embed_query(query,model_id=emb.model_name)
        return query_cache[query]
    retriever._embed_query_for_kb=cached_query
    llm=models['llm']
    client=OpenAI(base_url=llm.base_url,api_key=llm.api_key,max_retries=0,
                  timeout=120,http_client=httpx.Client(trust_env=False))
    results=[]
    with Session(engine) as raw_session:
        session=ModelSession(raw_session)
        for case in payload['cases']:
            # Alternate order to reduce systematic time/order bias.
            order=('before','after') if case['ordinal']%2==0 else ('after','before')
            for arm in order:
                start=time.monotonic()
                ranked,metrics=asyncio.run(retriever.search(session,case['question'],['pdf-ab-'+arm],
                    search_mode='hybrid',top_k=50,score_threshold=0,vector_weight=.7,bm25_weight=.3,
                    use_cache=False,log_retrieval=False))
                raw_sources=[{'chunk_id':r['id'],'source_sha256':chunk_sources[r['id']][0],
                              'page':chunk_sources[r['id']][1]} for r in ranked[:10]]
                # Persist retrieval before generation; qrels never influence ranking.
                ranked=batch_rerank(case['question'],ranked,RerankService(session),models['rerank'],top_k=20,score_threshold=0)
                if state['status']=='UNKNOWN_STOP' or not ranked:raise RuntimeError('Rerank failure: stop')
                sources=[{'chunk_id':r['id'],'source_sha256':chunk_sources[r['id']][0],
                          'page':chunk_sources[r['id']][1]} for r in ranked[:10]]
                pages={s['page'] for s in sources if s['source_sha256']==case['expected_source_sha256']}
                expected=set(case['expected_pages'])
                page_recall=len(pages&expected)/len(expected) if expected else None
                raw_pages={s['page'] for s in raw_sources if s['source_sha256']==case['expected_source_sha256']}
                expanded=expand_context(ranked,session,window_size=1)
                context='\n\n'.join(f'[{i}] 来源: {r.get("file_name","未知")}\n{r.get("content","")}' for i,r in enumerate(expanded,1))
                prompt=f'''你是一个智能问答助手，请根据以下参考资料回答用户的问题。
参考资料：
{context}
要求：
1. 基于参考资料回答，如果资料中没有相关信息，请如实说明
2. 回答要准确、完整、有条理
3. 适当引用来源（如"根据文档..."）
4. 使用中文回答'''
                with (root/'retrieval-results.jsonl').open('a',encoding='utf-8') as handle:
                    handle.write(json.dumps({'ordinal':case['ordinal'],'qid':case['qid'],'arm':arm,
                        'raw_sources_at_10':raw_sources,'sources_at_10':sources,
                        'page_recall_at_10':page_recall,'context_sha256':sha(context.encode())},ensure_ascii=False)+'\n')
                response=client.chat.completions.create(model=llm.model_name,messages=[
                    {'role':'system','content':prompt},{'role':'user','content':case['question']}],
                    temperature=.7,max_tokens=2000,stream=False)
                row={'ordinal':case['ordinal'],'qid':case['qid'],'arm':arm,'question':case['question'],
                    'sources_at_10':sources,'source_recall_at_10':float(bool(pages)) if expected else None,
                    'raw_sources_at_10':raw_sources,'raw_source_recall_at_10':float(bool(raw_pages)) if expected else None,
                    'raw_page_recall_at_10':len(raw_pages&expected)/len(expected) if expected else None,
                    'page_recall_at_10':page_recall,'answer':response.choices[0].message.content,
                    'finish_reason':response.choices[0].finish_reason,'usage':response.usage.model_dump() if response.usage else None,
                    'response_model':response.model,'prompt_sha256':sha(prompt.encode()),
                    'reasoning_chars':len((getattr(response.choices[0].message,'model_extra',None) or {}).get('reasoning_content') or ''),
                    'context_sha256':sha(context.encode()),'context_chars':len(context),'retrieval_metrics':metrics,
                    'seconds':round(time.monotonic()-start,3)}
                results.append(row)
                with (root/'results.jsonl').open('a',encoding='utf-8') as handle:handle.write(json.dumps(row,ensure_ascii=False)+'\n')
                (root/f'context-{case["ordinal"]}-{arm}.txt').write_text(context,encoding='utf-8')
                print(json.dumps({'ordinal':case['ordinal'],'arm':arm,'page_recall_at_10':page_recall,'completed':len(results)}),flush=True)
    state['status']='COMPLETED';save()


if __name__=='__main__':main()

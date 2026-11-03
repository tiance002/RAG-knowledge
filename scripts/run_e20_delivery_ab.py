"""Replay frozen E1 citations through real expansion and normal chat; no network."""
import argparse
import asyncio
import copy
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import time
import uuid
from types import SimpleNamespace
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())

def deny(*args, **kwargs):
    raise RuntimeError('E20_NETWORK_DENIED')

_original_connect = socket.socket.connect
_original_socketpair = socket.socketpair
_inside_socketpair = False
def guarded_connect(sock, address):
    if _inside_socketpair and isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
        return _original_connect(sock, address)
    return deny()
def guarded_socketpair(*args, **kwargs):
    global _inside_socketpair
    _inside_socketpair = True
    try: return _original_socketpair(*args, **kwargs)
    finally: _inside_socketpair = False
socket.socket.connect = guarded_connect
socket.socketpair = guarded_socketpair
socket.socket.connect_ex = deny
socket.create_connection = deny
socket.getaddrinfo = deny
os.environ.update(RAG_GROUNDED_GENERATION='true', RAG_PDF_TABLE_GROUNDING='false',
                  RAG_OBSERVABILITY_ENABLED='false', RAG_SNAPSHOTS_ENABLED='false')

from sqlmodel import SQLModel, Session, create_engine
from app.models import DocumentChunk
from app.schemas import Message, MessageRole
from app.services import chat, rerank, spreadsheet_grounding

def sha(s): return hashlib.sha256(s.encode('utf8')).hexdigest()
def canon(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
def norm(s): return re.sub(r'\s+', '', s).lower()

# Require correct source and distinct row/field association; never bare numeric matching.
def matches(qid, content):
    checks = {
        'office50:001': ['Project Title:', 'Stormwater Management to Address Highway Runoff Toxicity Due to 6PPD-Quinone from Tire Rubber'],
        'office50:008': ['$1.08M', 'Maine DOT joined'],
        'office50:010': ['$115,000', '$7,560', '7%'],
        'office50:014': ['$1,802,000', '$653,728', '36%'],
        'office50:020': ['$511,500.00', '$191,367.77', '75.28%'],
        'office50:031': ['Alabama', '108149.892'],
    }
    if qid not in checks: return False
    if qid == 'office50:031' and '[工作表: T07]' not in content: return False
    return any(all(norm(x) in norm(line) for x in checks[qid]) for line in content.splitlines())

def secure(path):
    path.mkdir(parents=True, exist_ok=False)
    if os.name == 'nt':
        out = subprocess.run(['whoami','/user','/fo','csv','/nh'], capture_output=True, text=True, check=True)
        sid = next(csv.reader(out.stdout.splitlines()))[1]
        if not re.fullmatch(r'S-1-[0-9-]+', sid): raise ValueError('invalid SID')
        subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F',
                        '/grant:r','*S-1-5-18:(OI)(CI)F'], capture_output=True, check=True)
    else: os.chmod(path, 0o700)

def candidates(q, chunks):
    out=[]
    for c in q['candidates']:
        h=c['historical_citation']; d=chunks[c['id']]; m=c['historical_after_dedup_candidate']
        assert sha(h['content']) == d['content_sha256'] == c['db_content_sha256']
        out.append({'id':c['id'], 'content':h['content'], 'file_id':d['file_id'],
                    'file_name':h['fileName'], 'kb_id':h['kb_id'], 'kb_name':h['kb_name'],
                    'page_number':d['page_number'], 'content_type':d['content_type'].lower(),
                    'image_path':d['image_path'], 'heading_text':d['heading_text'],
                    'heading_level':d['heading_level'], 'source_metadata':h.get('source_metadata') or {},
                    'score':m['score'], 'bm25_score':m['bm25_score'],
                    'vector_score':m['vector_score'], 'rerank_score':m['rerank_score']})
    return out

class FakeSession:
    def __init__(self,*args): pass
    def __enter__(self): return self
    def __exit__(self,*args): return False

async def arm(q, expanded, strategy):
    os.environ['RAG_CHAT_CONTEXT_STRATEGY']=strategy
    calls=[]
    async def fake_create(**kwargs):
        calls.append(copy.deepcopy(kwargs))
        return SimpleNamespace(id='synthetic', model='synthetic', usage=None, choices=[SimpleNamespace(
            message=SimpleNamespace(content='Synthetic response; not scored.'), finish_reason='stop')])
    service=chat.ChatService.__new__(chat.ChatService)
    service.llm_service=SimpleNamespace(model='synthetic',_get_client=lambda:SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create))))
    async def fake_retrieval(**kwargs): return copy.deepcopy(expanded), {'retrieval_status':'SUCCESS'}
    service._retrieve_with_new_service=fake_retrieval
    started=time.perf_counter()
    events=[x async for x in service._chat_stream_normal([Message(role=MessageRole.USER,content=q['question'])],
        ['office50-v1'],20,0,None,False,0,None,search_mode='fulltext',context_window=1,use_rewrite=False)]
    elapsed_ms=round((time.perf_counter()-started)*1000,3)
    assert len(calls)==1 and events[-1][0]=='done', (q['qid'], strategy, [x[0] for x in events])
    citations=next(data['citations'] for kind,data in events if kind=='rag_result')
    messages=calls[0]['messages']; provider_text='\n'.join(m['content'] for m in messages)
    delivered=[{'id':c['chunk_id'],'content_sha256':sha(c['content']),
                'exact_content_in_provider_request':c['content'] in provider_text,
                'file_id':c.get('fileId'),'location':c.get('location'),'source_metadata':c.get('source_metadata'),
                'relation':c.get('evidence_relation')} for c in citations]
    return {'messages':messages,'request_sha256':sha(canon(messages)), 'citations':citations,
            'delivered':delivered,'provider_text':provider_text,'events':events,'elapsed_ms':elapsed_ms}

def main():
    p=argparse.ArgumentParser();p.add_argument('--preflight',action='store_true');p.add_argument('--snapshot',type=Path,required=True);p.add_argument('--output-dir',type=Path,required=True);a=p.parse_args()
    raw=a.snapshot.read_bytes(); frozen=json.loads(raw); chunks={x['id']:x for x in frozen['chunks']}
    assert len(frozen['questions'])==6 and not frozen['candidate_sse_index_mismatches']
    assert sha(canon(frozen['chunks'])) == frozen['actual_snapshot_chunk_subset_fingerprint']
    try: socket.create_connection(('203.0.113.1', 443), timeout=1)
    except RuntimeError as exc: assert str(exc)=='E20_NETWORK_DENIED'
    else: raise AssertionError('network guard failed')
    if a.preflight:
        print(json.dumps({'status':'PASS','snapshot_sha256':hashlib.sha256(raw).hexdigest(),'questions':len(frozen['questions']),'network':'DENIED','external_model_calls':0}));return
    secure(a.output_dir)
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=__import__('sqlalchemy').pool.StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        for x in frozen['chunks']:
            assert sha(x['content'])==x['content_sha256']
            s.add(DocumentChunk(**{k:v for k,v in x.items() if k!='content_sha256'}))
        s.commit()
    chat.Session=FakeSession
    spreadsheet_grounding.prepare_spreadsheet_grounding=lambda *args: None
    ledger=[]; details=[]
    for q in frozen['questions']:
        seeds=candidates(q,chunks)
        with Session(engine) as session:
            expanded=rerank.expand_context(copy.deepcopy(seeds),session,window_size=1,max_parent_length=4000,include_source_members=True)
        expansion_hash=sha(canon(expanded)); seed_ids=[x['id'] for x in seeds]
        gold_ids=[x['id'] for x in frozen['chunks'] if q['source_sha256'] in x['file_id'] and matches(q['qid'],x['content'])]
        assert gold_ids, ('no source-linked target field',q['qid'])
        possible={m['id'] for group in expanded for m in group.get('context_group',{}).get('source_members',[])}
        expanded_seed_ids=[x['id'] for x in expanded]
        results={}
        for label,strategy in [('A','ORIGINAL_CONTENT'),('B','EXPANDED_CONTENT')]:
            result=asyncio.run(arm(q,expanded,strategy)); actual={x['id'] for x in result['delivered'] if x['exact_content_in_provider_request']}; hit=sorted(set(gold_ids)&actual)
            present=set(gold_ids)&set(seed_ids); recoverable=(not present and bool(set(gold_ids)&possible))
            row={'qid':q['qid'],'arm':label,'run_id':'e20-'+uuid.uuid4().hex,'request_sha256':result['request_sha256'],
                 'experiment_id':'e20-historical-delivery-ab-v1','trace_id':None,
                 'expanded_snapshot_sha256':expansion_hash,'gold_unit_granularity':q['unit_granularity'],'gold_evidence_ids':gold_ids,
                 'gold_coverage':1 if hit else 0,'delivered_gold_ids':hit,'seed_preservation':all(x in actual for x in seed_ids),
                 'expanded_group_seed_preservation':all(x in actual for x in expanded_seed_ids),
                 'undelivered_initial_seed_ids':[x for x in seed_ids if x not in actual],
                 'recoverable_missing_denominator':int(recoverable),'recovered_missing':int(recoverable and bool(hit)),
                 'post_expansion_delivery_failure':bool(set(gold_ids)&possible) and not bool(hit),
                 'delivered_evidence':result['delivered'],'provider_request_characters':sum(len(m['content']) for m in result['messages']),
                 'context_body_characters':sum(len(c['content']) for c in result['citations']),
                 'provider_input_utf8_bytes':sum(len(m['content'].encode('utf-8')) for m in result['messages']),
                 'local_elapsed_ms':result['elapsed_ms'],
                 'truncation_status':'NONE_DETECTED' if all(x['exact_content_in_provider_request'] for x in result['delivered']) else 'UNKNOWN',
                 'model_token_count':'UNKNOWN','irrelevant_evidence':'UNKNOWN','correctness':'NOT_RUN','faithfulness':'NOT_RUN',
                 'answer_completeness':'NOT_RUN','citation_coverage':'NOT_RUN','cost':'UNKNOWN',
                 'citation_support':'NOT_RUN','answer_hash':'NOT_APPLICABLE_SYNTHETIC_RESPONSE','status':'COMPLETED_SYNTHETIC_PROVIDER'}
            ledger.append(row);results[label]=row;details.append({'qid':q['qid'],'arm':label,'provider_messages':result['messages'],'citations':result['citations'],'expanded':expanded})
        for row in results.values(): row['provider_character_delta_B_minus_A']=results['B']['provider_request_characters']-results['A']['provider_request_characters']
    (a.output_dir/'ledger.json').write_text(json.dumps(ledger,ensure_ascii=False,indent=2),encoding='utf8')
    (a.output_dir/'evidence.json').write_text(json.dumps(details,ensure_ascii=False,indent=2),encoding='utf8')
    summary={'status':'PASS','snapshot_sha256':hashlib.sha256(raw).hexdigest(),'snapshot_subset_fingerprint':frozen['actual_snapshot_chunk_subset_fingerprint'],
        'case_count':len(frozen['questions']),'rows':len(ledger),'source':'historical candidate replay; no live retrieval',
        'controlled_factors':{'window_size':1,'max_parent_length':4000,'top_k':20,'rerank':False,'rewrite':False,'pdf_grounding':False,'spreadsheet_grounding':False},
        'actual_external_model_calls':0,'network_guard':'outbound denied; loopback permitted only inside Windows asyncio socketpair construction',
        'fake_provider':'fixed synthetic response, never scored','index_fingerprint':'historical full index NOT_CAPTURED; subset fingerprint recorded',
        'gold_granularities':'five Word table fields, one Excel cell; no cross-granularity aggregate',
        'result':[{k:r[k] for k in ('qid','arm','gold_coverage','seed_preservation','recoverable_missing_denominator','recovered_missing','post_expansion_delivery_failure','provider_character_delta_B_minus_A')} for r in ledger]}
    (a.output_dir/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf8');print(json.dumps(summary,ensure_ascii=False))

if __name__=='__main__': main()

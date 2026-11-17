"""Owner-authorized fixed-model diagnostic on non-PASS frozen questions."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
from run_routing200 import (atomic_json, canonical, credentials, open_ledger, private,
                            record, send, sha, trace_context, validate_manifest)

MODEL = 'zai-org/GLM-5.3-Flash'

def run(selection_path, output, expected_hash, execute=False):
    raw = Path(selection_path).read_bytes()
    if sha(raw) != expected_hash:
        raise ValueError('selection hash mismatch')
    plan = json.loads(raw)
    if (plan['model'], plan['temperature'], plan['max_tokens'], plan['max_new_calls']) != (MODEL, 0.7, 4096, 36):
        raise ValueError('unapproved settings')
    original = json.loads(Path(plan['source_manifest']).read_text(encoding='utf-8-sig'))
    cfg = validate_manifest(original)
    if sha(canonical(original)) != plan['source_manifest_canonical_sha256']:
        raise ValueError('source manifest mismatch')
    source = {c['qid']: c for c in original['cases']}
    ids = [c['qid'] for c in plan['cases']]
    if len(ids) != 36 or len(set(ids)) != 36 or not set(ids) <= source.keys():
        raise ValueError('invalid selection')
    def prior():
        with sqlite3.connect(Path(plan['source_ledger']).resolve().as_uri()+'?mode=ro',uri=True) as d:
            return {r[0] for r in d.execute("select qid from attempts where role='expensive'")}
    if prior() != set(plan['existing_expensive_qids']) or set(ids) & prior():
        raise ValueError('historical expensive attempts changed')
    providers = credentials('/opt/amrag/backend/local.db', cfg)
    if not execute:
        return {'status':'PREFLIGHT_PASS','selection_sha256':sha(raw),'source_manifest_sha256':sha(canonical(original)), 'cases':len(ids),'model':MODEL,'model_calls':0,'cost_status':'UNKNOWN'}
    os.umask(0o077)
    private(output, directory=True)
    with open(Path(output)/'runner.lock','a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        db = open_ledger(output, plan)
        try:
            def status():
                counts = dict(db.execute('select state,count(*) from attempts group by state').fetchall())
                value = {'status':'RECORDED','selection_sha256':sha(raw),'counts':counts,'total':len(ids),'cost_status':'UNKNOWN','purpose':'manual fixed-model diagnostic'}
                atomic_json(Path(output)/'status.json',value)
                return value
            status()
            for qid in ids:
                if db.execute('select 1 from attempts where qid=? and role=?',(qid,'diagnostic')).fetchone():
                    continue
                if prior() != set(plan['existing_expensive_qids']):
                    raise ValueError('historical expensive attempts changed')
                case=source[qid]
                payload={'model':MODEL,'messages':case['messages'],'temperature':0.7,'max_tokens':4096,'stream':False}
                meta={'selection_sha256':sha(raw),'source_manifest_sha256':plan['source_manifest_canonical_sha256'], 'messages_sha256':case['messages_sha256'],'request_hash':sha(canonical(payload)), 'model':MODEL,'cost_status':'UNKNOWN','attempt_id':sha(raw)+':'+qid+':diagnostic','router_mode':'OWNER_FIXED_MODEL_DIAGNOSTIC','excluded_from_core_accuracy':next(c['excluded_from_core_accuracy'] for c in plan['cases'] if c['qid']==qid)}
                record(db,qid,'diagnostic','UNKNOWN_PENDING',meta,initial=True)
                status()
                state='UNKNOWN'; info=dict(meta); start=time.monotonic()
                trace_meta=dict(meta,request_payload=payload,run_id=sha(raw),dataset_version=original.get('dataset_version'))
                with trace_context(case,'diagnostic',MODEL,trace_meta) as trace:
                    try:
                        code, raw_response=send(*providers[MODEL],payload,180)
                        info.update(http_status=code,raw_response=raw_response.decode('utf-8',errors='replace'),response_sha256=sha(raw_response))
                        data=json.loads(raw_response); choices=data.get('choices'); choice=choices[0] if isinstance(choices,list) and choices and isinstance(choices[0],dict) else {}
                        text=(choice.get('message') or {}).get('content'); text=text if isinstance(text,str) else ''
                        info.update(response_payload=data,usage=data.get('usage'),provider_request_id=data.get('id'),finish_reason=choice.get('finish_reason'),text=text,actual_model=data.get('model'),answer_sha256=sha(text.encode('utf-8')))
                        if data.get('error') or code != 200: state='PROVIDER_FAILURE'
                        elif choice.get('finish_reason')=='length': state='TRUNCATED'
                        elif choice.get('finish_reason')=='stop': state='COMPLETED' if text.strip() else 'EMPTY'
                    except urllib.error.HTTPError as exc:
                        state='HTTP_FAILURE'; info.update(http_status=exc.code,error_type=type(exc).__name__)
                        try:
                            response=exc.read(); info.update(raw_response=response.decode('utf-8',errors='replace'),response_sha256=sha(response))
                        except Exception: pass
                    except Exception as exc: info['error_type']=type(exc).__name__
                    info.update(state=state,latency_ms=round((time.monotonic()-start)*1000,3))
                    if trace is not None:
                        try:
                            trace.finish(dict(info,model=info.get('actual_model'))); info['trace_id']=trace.trace_id
                        except Exception: pass
                    record(db,qid,'diagnostic',state,info)
                status()
            return status()
        finally: db.close()

if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--selection',required=True); p.add_argument('--output',required=True); p.add_argument('--sha256',required=True); p.add_argument('--execute',action='store_true'); args=p.parse_args()
    try: print(json.dumps(run(args.selection,args.output,args.sha256,args.execute),ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'status':'FAIL','error_type':type(exc).__name__})); raise SystemExit(1)
    finally:
        try:
            from routing_trace_bridge import flush
            flush()
        except Exception: pass

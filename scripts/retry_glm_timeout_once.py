"""One owner-authorized retry of the diagnostic batch's timed-out GLM call."""
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
from run_routing200 import canonical, credentials, open_ledger, private, record, send, sha, validate_manifest

ROOT = Path('/opt/amrag/experiments/dynamic-routing-20261101')

def main():
    os.umask(0o077)
    original = json.loads((ROOT/'gold150/manifest.json').read_text(encoding='utf8'))
    cfg = validate_manifest(original)
    path = ROOT/'expensive-nonpass-diagnostic/run/ledger.sqlite3'
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True) as db:
        rows = [(q, json.loads(i)) for q, i in db.execute("select qid,info from attempts where role='diagnostic' and state='UNKNOWN'")]
    assert len(rows) == 1 and rows[0][1].get('error_type') == 'TimeoutError'
    qid, prior = rows[0]
    case = next(x for x in original['cases'] if x['qid'] == qid)
    payload = {'model': cfg['expensive_model'], 'messages': case['messages'], 'temperature': 0.7, 'max_tokens': 4096, 'stream': False}
    assert sha(canonical(payload)) == prior['request_hash']
    plan = {'authorization_id':'owner-explicit-glm-timeout-retry-once', 'qid':qid,
            'previous_request_hash':prior['request_hash'], 'original_manifest_sha256':sha(canonical(original)),
            'max_calls':1, 'model':cfg['expensive_model']}
    out = ROOT/'expensive-timeout-retry-once'; out.mkdir(mode=0o700, exist_ok=False)
    (out/'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf8')
    providers = credentials('/opt/amrag/backend/local.db', cfg)
    db = open_ledger(out/'run', plan)
    try:
        meta = {'request_hash': prior['request_hash'], 'previous_state':'UNKNOWN', 'cost_status':'UNKNOWN'}
        record(db, qid, 'expensive_retry', 'UNKNOWN_PENDING', meta, initial=True)
        start = time.monotonic(); state = 'UNKNOWN'; info = dict(meta)
        try:
            code, raw = send(*providers[cfg['expensive_model']], payload, 180)
            data = json.loads(raw); choices = data.get('choices') or []; choice = choices[0] if choices else {}
            answer = (choice.get('message') or {}).get('content') or ''
            info.update(http_status=code, raw_response=raw.decode('utf8', errors='replace'), response_sha256=sha(raw),
                        text=answer, answer_sha256=sha(answer.encode('utf8')), usage=data.get('usage'),
                        provider_request_id=data.get('id'), actual_model=data.get('model'), finish_reason=choice.get('finish_reason'))
            if choice.get('finish_reason') == 'length': state = 'TRUNCATED'
            elif choice.get('finish_reason') == 'stop': state = 'COMPLETED' if answer.strip() else 'EMPTY'
        except urllib.error.HTTPError as exc:
            state='HTTP_FAILURE'; info.update(http_status=exc.code, error_type=type(exc).__name__)
        except Exception as exc:
            info['error_type'] = type(exc).__name__
        info.update(state=state, latency_ms=round((time.monotonic()-start)*1000, 3))
        record(db,qid,'expensive_retry',state,info)
        print(json.dumps({'qid':qid,'state':state,'cost_status':'UNKNOWN'}), flush=True)
    finally:
        db.close()

if __name__ == '__main__': main()

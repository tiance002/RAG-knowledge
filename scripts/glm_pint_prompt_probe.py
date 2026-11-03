"""One owner-authorized prompt probe, preserving the frozen source and question."""
import json
import os
from pathlib import Path
import sqlite3
import time
import urllib.error
from run_routing200 import canonical, credentials, open_ledger, record, send, sha, validate_manifest

ROOT = Path('/opt/amrag/experiments/dynamic-routing-20261101')
EXTRA = ('请直接回答原问题，并说明品脱与液量盎司换算所依据的资料原文或字段、所用数值和简短算式。'
         '如果提供的资料没有给出足够的换算依据，请明确说明，不要凭印象补充。')

def main():
    os.umask(0o077)
    manifest = json.loads((ROOT/'gold150/manifest.json').read_text(encoding='utf8'))
    cfg = validate_manifest(manifest)
    assert sha(canonical(manifest)) == 'e21ac9e14f411eda246e4bd854ac62b5bec8447d547c65f3fb2f05b7da3dcb34'
    case = next(c for c in manifest['cases'] if c['qid'] == 'office50:045')
    assert sha(canonical(case['messages'])) == case['messages_sha256']
    messages = [dict(m) for m in case['messages']]
    assert messages[-1]['role'] == 'user'
    messages[-1]['content'] += '\n\n' + EXTRA
    model = 'zai-org/GLM-5.3-Flash'
    payload = dict(model=model, messages=messages, temperature=.7, max_tokens=4096, stream=False)
    plan = {'grant':'owner-pint-conversion-one-call-20261102','qid':case['qid'],
            'baseline_messages_sha256':case['messages_sha256'],'messages_sha256':sha(canonical(messages)),
            'request_sha256':sha(canonical(payload)),'max_calls':1,'model':model,'temperature':.7,'max_tokens':4096}
    out=ROOT/'pint-explanation-probe';out.mkdir(mode=0o700,exist_ok=False)
    (out/'plan.json').write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding='utf8')
    providers=credentials('/opt/amrag/backend/local.db',cfg)
    db=open_ledger(out/'run',plan)
    try:
        record(db,case['qid'],'probe','UNKNOWN_PENDING',plan,initial=True)
        start=time.monotonic();state='UNKNOWN';info=dict(plan)
        try:
            code,raw=send(*providers[model],payload,180);data=json.loads(raw);choice=(data.get('choices') or [{}])[0]
            content=(choice.get('message') or {}).get('content') or ''
            info.update(http_status=code,raw_response=raw.decode('utf8',errors='replace'),response_sha256=sha(raw),
                        text=content,usage=data.get('usage'),provider_request_id=data.get('id'),
                        actual_model=data.get('model'),finish_reason=choice.get('finish_reason'))
            if choice.get('finish_reason')=='length':state='TRUNCATED'
            elif choice.get('finish_reason')=='stop':state='COMPLETED' if content.strip() else 'EMPTY'
        except urllib.error.HTTPError as exc:state='HTTP_FAILURE';info.update(http_status=exc.code,error_type=type(exc).__name__)
        except Exception as exc:info['error_type']=type(exc).__name__
        info.update(state=state,latency_ms=round((time.monotonic()-start)*1000,3))
        record(db,case['qid'],'probe',state,info)
        print(json.dumps({'state':state,'qid':case['qid'],'finish_reason':info.get('finish_reason'),'usage':info.get('usage'),'latency_ms':info['latency_ms']},ensure_ascii=True),flush=True)
    finally:db.close()

if __name__=='__main__':main()

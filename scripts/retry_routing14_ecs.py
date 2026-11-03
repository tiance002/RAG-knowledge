"""Owner-authorized one-time retries of 12 known 429s and two old timeouts."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path('/opt/amrag/experiments/dynamic-routing-20261101')
ORIGINAL = ROOT / 'gold150'
OUT = ROOT / 'retry14-owner-authorized'

def main():
    os.umask(0o077)
    sys.path.insert(0, str(ROOT))
    from run_routing200 import canonical, run
    original = json.loads((ORIGINAL / 'manifest.json').read_text(encoding='utf8'))
    old_hash = hashlib.sha256(canonical(original)).hexdigest()
    assert old_hash == 'e21ac9e14f411eda246e4bd854ac62b5bec8447d547c65f3fb2f05b7da3dcb34'
    with sqlite3.connect((ORIGINAL / 'run/ledger.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        previous = [(q, s, json.loads(i)) for q, s, i in db.execute("select qid,state,info from attempts where role='cheap'") if s in ('HTTP_FAILURE', 'UNKNOWN')]
    assert len(previous) == 14
    assert sum(s == 'HTTP_FAILURE' and i.get('http_status') == 429 for q, s, i in previous) == 12
    assert sum(s == 'UNKNOWN' and i.get('error_type') == 'TimeoutError' for q, s, i in previous) == 2
    old = {q: {'state': s, 'request_hash': i.get('request_hash')} for q, s, i in previous}
    cases = [case for case in original['cases'] if case['qid'] in old]
    assert len(cases) == 14
    manifest = dict(original, authorization_id='owner-explicit-retry-14-20261101', cases=cases,
                    retry_of_manifest=old_hash, previous_attempts=old)
    manifest['config'] = dict(original['config'], max_cheap_calls=14, max_expensive_calls=0)
    OUT.mkdir(mode=0o700, exist_ok=False)
    (OUT / 'manifest.json').write_bytes(canonical(manifest) + b'\n')
    os.environ.update(json.loads((ROOT / 'cloud-config.json').read_text(encoding='utf8')))
    os.environ.update(RAG_TRACE_SCHEMA='e12', RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true',
                      RAG_PROMETHEUS_ENABLED='false', DATABASE_URL='sqlite://', RAG_E12_CONTENT_POLICY='LOCAL_ONLY',
                      RAG_SNAPSHOTS_ENABLED='false', RAG_OBSERVATION_LOG=str(OUT / 'events.jsonl'))
    sys.path.insert(0, '/opt/amrag/experiments/e20-20261030/code-r2/backend')
    try:
        result = run(manifest, OUT / 'run', mode='cheap', continue_unvisited=True)
        print(json.dumps(result), flush=True)
    finally:
        from routing_trace_bridge import flush
        flush()

if __name__ == '__main__':
    main()

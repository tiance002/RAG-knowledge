"""Owner-authorized new attempts for the two repeated 429 results."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

ROOT = Path('/opt/amrag/experiments/dynamic-routing-20261101')

def main():
    os.umask(0o077)
    sys.path.insert(0, str(ROOT))
    from run_routing200 import canonical, run
    parent = ROOT / 'retry14-owner-authorized'
    original = json.loads((parent / 'manifest.json').read_text(encoding='utf8'))
    with sqlite3.connect((parent / 'run/ledger.sqlite3').as_uri() + '?mode=ro', uri=True) as db:
        prior = [(q, json.loads(i)) for q, s, i in db.execute("select qid,state,info from attempts where role='cheap'") if s == 'HTTP_FAILURE']
    assert len(prior) == 2 and all(i.get('http_status') == 429 for q, i in prior)
    prior_map = {q: {'state': 'HTTP_FAILURE', 'http_status': 429, 'request_hash': i.get('request_hash')} for q, i in prior}
    cases = [x for x in original['cases'] if x['qid'] in prior_map]
    assert len(cases) == 2
    manifest = dict(original, authorization_id='owner-explicit-retry-429-again-20261101', cases=cases,
                    retry_of_manifest=hashlib.sha256(canonical(original)).hexdigest(), previous_attempts=prior_map)
    manifest['config'] = dict(original['config'], max_cheap_calls=2, max_expensive_calls=0)
    out = ROOT / 'retry429-owner-round2'; out.mkdir(mode=0o700, exist_ok=False)
    (out / 'manifest.json').write_bytes(canonical(manifest) + b'\n')
    os.environ.update(json.loads((ROOT / 'cloud-config.json').read_text(encoding='utf8')))
    os.environ.update(RAG_TRACE_SCHEMA='e12', RAG_OBSERVABILITY_ENABLED='true', LANGFUSE_ENABLED='true',
                      RAG_PROMETHEUS_ENABLED='false', DATABASE_URL='sqlite://', RAG_E12_CONTENT_POLICY='LOCAL_ONLY',
                      RAG_SNAPSHOTS_ENABLED='false', RAG_OBSERVATION_LOG=str(out/'events.jsonl'))
    sys.path.insert(0, '/opt/amrag/experiments/e20-20261030/code-r2/backend')
    try:
        print(json.dumps(run(manifest, out/'run', mode='cheap', continue_unvisited=True)), flush=True)
    finally:
        from routing_trace_bridge import flush
        flush()

if __name__ == '__main__':
    main()

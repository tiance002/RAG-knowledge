"""Isolated ECS entry: reuse frozen inputs, existing SDK environment and policy."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path('/opt/amrag/experiments/dynamic-routing-20261101')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=('cheap', 'expensive'))
    parser.add_argument('--reviews')
    args = parser.parse_args()
    os.umask(0o077)
    manifest = json.loads((ROOT / 'gold150/manifest.json').read_text(encoding='utf8'))
    assert manifest['authorization_id'] == 'dynamic-routing-150-owner-20261101'
    assert len(manifest['cases']) == 150
    for name, digest in manifest['source_hashes'].items():
        assert hashlib.sha256(Path(name).read_bytes()).hexdigest() == digest, 'Frozen source changed'
    split = json.loads((ROOT / 'gold150/split.json').read_text(encoding='utf8'))
    from run_routing200 import canonical, run
    assert hashlib.sha256(canonical(manifest)).hexdigest() == split['manifest_sha256']
    cloud = json.loads((ROOT / 'cloud-config.json').read_text(encoding='utf8'))
    assert cloud['LANGFUSE_BASE_URL'].rstrip('/') == 'https://us.cloud.langfuse.com'
    os.environ.update(cloud)
    os.environ.update(RAG_TRACE_SCHEMA='e12', RAG_OBSERVABILITY_ENABLED='true',
                      LANGFUSE_ENABLED='true', RAG_PROMETHEUS_ENABLED='false',
                      DATABASE_URL='sqlite://', RAG_E12_CONTENT_POLICY='LOCAL_ONLY',
                      RAG_SNAPSHOTS_ENABLED='false',
                      RAG_OBSERVATION_LOG=str(ROOT / 'gold150/events.jsonl'))
    # Only unchanged observability helpers are imported from this existing tree.
    sys.path.insert(0, '/opt/amrag/experiments/e20-20261030/code-r2/backend')
    try:
        result = run(manifest, ROOT / 'gold150/run', mode=args.mode,
                     reviews_path=args.reviews, continue_unvisited=True)
        print(json.dumps(result), flush=True)
        return 0 if not any(key.endswith(('UNKNOWN', 'PENDING', 'FAILURE'))
                            for key in result.get('counts', {})) else 2
    finally:
        from routing_trace_bridge import flush
        flush()


if __name__ == '__main__':
    raise SystemExit(main())

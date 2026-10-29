"""Publish deterministic, redacted scores; default is a network-free dry run."""
import argparse
import hashlib
import json
import os
from pathlib import Path


def score_payloads(records):
    seen = set()
    payloads = []
    for record in records:
        trace = record['trace_id']
        version = record['experiment_version']
        def add(name, value, kind):
            identity = hashlib.sha256(f'{trace}|{version}|{name}|p0-v1'.encode()).hexdigest()
            if identity in seen:
                raise ValueError('Duplicate score identity')
            seen.add(identity)
            payloads.append(dict(score_id=identity, trace_id=trace, name='p0.' + name,
                                 value=value, data_type=kind,
                                 metadata={'experiment_version': version, 'method': 'deterministic-p0-v1'}))
        for stage, state in record['evidence'].items():
            add('evidence.' + stage, state, 'CATEGORICAL')
        for name, value in record['scores'].items():
            if value is None or isinstance(value, dict):
                continue
            if isinstance(value, bool):
                add(name, float(value), 'BOOLEAN')
            elif isinstance(value, (int, float)):
                add(name, float(value), 'NUMERIC')
        usage = record['scores'].get('tokens') or {}
        for name in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
            if isinstance(usage.get(name), int):
                add('generation.' + name, float(usage[name]), 'NUMERIC')
    return payloads


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--config', type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--publish', action='store_true')
    parser.add_argument('--verify-only', action='store_true', help='Only read back existing deterministic score IDs')
    args = parser.parse_args()
    records = [json.loads(x) for x in args.input.read_text(encoding='utf-8').splitlines() if x.strip()]
    payloads = score_payloads(records)
    online = args.publish or args.verify_only
    report = {'dry_run': not online, 'verify_only': args.verify_only, 'records': len(records), 'scores': len(payloads), 'verified': False}
    if online:
        if not args.config:
            raise ValueError('--config required for publish')
        for line in args.config.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.strip().startswith('#'):
                key, value = line.split('=', 1)
                if key.strip() in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL'):
                    os.environ[key.strip()] = value.strip().strip('"').strip("'")
        from langfuse import Langfuse
        client = Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'], timeout=30)
        try:
            if not args.verify_only:
                for payload in payloads:
                    client.create_score(**payload)
                client.flush()
            expected = {p['score_id'] for p in payloads}
            found = set()
            for trace in dict.fromkeys(p['trace_id'] for p in payloads):
                cursor = None
                while True:
                    page = client.api.scores.get_many(trace_id=trace, limit=100, cursor=cursor)
                    found.update(row.id for row in page.data)
                    cursor = getattr(page.meta, 'cursor', None)
                    if not cursor:
                        break
            report['verified'] = expected.issubset(found)
            report['missing_scores'] = len(expected - found)
        finally:
            client.shutdown()
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))
    if online and not report['verified']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()

"""Read back sanitized observation structure; no prompts or credentials in output."""
import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--results', type=Path, help='Restrict readback to this immutable run result file')
    args = parser.parse_args()
    for line in args.config.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.strip().startswith('#'):
            key, value = line.split('=', 1)
            os.environ[key.strip()] = value.strip().strip('"').strip("'")
    from langfuse import Langfuse
    client = Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'], timeout=30)
    reports = []
    try:
        if args.results:
            trace_ids = [json.loads(s)['trace_id'] for s in args.results.read_text(encoding='utf-8').splitlines() if s.strip()]
        else:
            roots = client.api.observations.get_many(name='p0.smoke.question', limit=100)
            trace_ids = [root.trace_id for root in roots.data]
        for trace_id in trace_ids:
            result = client.api.observations.get_many(trace_id=trace_id, limit=100)
            root = next((x for x in result.data if x.name == 'p0.smoke.question'), None)
            if root is None:
                raise RuntimeError('Cloud root observation missing')
            reports.append({'trace_id': trace_id,
                            'root_id': root.id,
                            'observations': [{'id': x.id, 'parent_id': x.parent_observation_id,
                                              'name': x.name, 'type': x.type} for x in result.data],
                            'trace_url': client.get_trace_url(trace_id=trace_id)})
    finally:
        client.shutdown()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps({'cloud_readback': True, 'root_traces': len(reports), 'traces': reports}, indent=2), encoding='utf-8')
    print(json.dumps({'cloud_readback': True, 'root_traces': len(reports), 'output': str(args.output)}))


if __name__ == '__main__':
    main()

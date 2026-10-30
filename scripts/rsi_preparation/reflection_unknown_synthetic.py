"""SYNTHETIC probe of GEPA's actual batched-reflection fallback, no LLM."""
import argparse
import json
import socket
import sys
from pathlib import Path
from types import SimpleNamespace


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--source', required=True)
    ap.add_argument('--output', required=True)
    args = ap.parse_args()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=False)
    sys.path.insert(0, str(Path(args.source).resolve()))
    def deny(*args, **kwargs): raise RuntimeError('Network forbidden')
    socket.socket.connect = deny
    from gepa.proposer.reflective_mutation.reflective_mutation import ReflectiveMutationProposer
    results = []
    for guarded in [False, True]:
        path = out / ('guarded-ledger.json' if guarded else 'native-ledger.json')
        attempts, invocations, logs = [], [], []

        def request(*args, **kwargs):
            invocations.append('reflection-function-entered')
            if guarded and path.exists():
                raise TimeoutError('Persisted UNKNOWN: repeat physical attempt denied')
            attempts.append({'status': 'UNKNOWN', 'synthetic_attempt': len(attempts) + 1})
            path.write_text(json.dumps(attempts, indent=2))
            raise TimeoutError('SYNTHETIC reflection outcome unknown')

        owner = SimpleNamespace(logger=SimpleNamespace(log=lambda value: logs.append(str(value))), _propose_texts_batch=request, propose_new_texts=request)
        value = ReflectiveMutationProposer._propose_texts_batch_safe(owner, [({'instruction': 'P0'}, {'instruction': []}, ['instruction'])])
        assert value == [None]
        assert len(attempts) == (1 if guarded else 2)
        results.append({'guarded': guarded, 'synthetic_attempts': len(attempts), 'wrapper_invocations': len(invocations), 'returned': value, 'outer_required_status': 'UNKNOWN'})
        (out / ('guarded.log' if guarded else 'native.log')).write_text('\n'.join(logs))
    summary = {'status': 'PASS', 'kind': 'SYNTHETIC', 'external_model_calls': 0, 'results': results, 'finding': 'Official safety wrapper retries reflection on arbitrary Exception and returns None; durable UNKNOWN guard and outer terminal classification are required.'}
    (out / 'summary.json').write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary))


if __name__ == '__main__':
    main()

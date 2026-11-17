"""Private persistent SSH permit worker; credentials never leave ECS.

stdout is a controller-only JSONL protocol, never a user-facing log. No retries.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
import time

MODEL = 'zai-org/GLM-5.3-Flash'
HOST = 'https://api.siliconflow.cn/v1'
BINDING = 'e6a15570-9f5f-422f-bb13-ebcd95a95f1c'
DB = Path('/opt/amrag/backend/local.db')


def canonical(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def sha(x):
    return hashlib.sha256(x).hexdigest()


def emit(x):
    sys.stdout.write(json.dumps(x, ensure_ascii=True, allow_nan=False) + '\n')
    sys.stdout.flush()


def persist(path, value):
    with path.open('xb') as out:
        out.write(canonical(value))
        out.flush()
        os.fsync(out.fileno())


def stats():
    result = {}
    for p in [DB, Path(str(DB) + '-wal')]:
        result[p.name] = {'size': p.stat().st_size, 'mtime_ns': p.stat().st_mtime_ns} if p.exists() else None
    result['disk_free_bytes'] = shutil.disk_usage(DB.parent).free
    return result


def main():
    import httpx
    init = json.loads(sys.stdin.readline())
    runid = init['run_id']
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', runid):
        raise ValueError('INVALID_RUN_ID')
    root = Path('/opt/amrag/evaluation') / ('glm40-fix-' + runid)
    if root.is_symlink() or Path(__file__).resolve().parent != root.resolve():
        raise ValueError('REMOTE_HELPER_OUTSIDE_RUN_ROOT')
    root.mkdir(mode=0o700, exist_ok=True)
    os.chmod(root, 0o700)
    before = stats()
    con = sqlite3.connect('file:' + str(DB) + '?mode=ro', uri=True)
    row = con.execute('select model_name,base_url,api_key from custom_models where id=?', (BINDING,)).fetchone()
    con.close()
    if not row or row[0] != MODEL or row[1].rstrip('/') != HOST or not row[2]:
        emit({'type': 'FATAL', 'error_code': 'REMOTE_BINDING_IDENTITY_MISMATCH'})
        return
    identity = {'model': MODEL, 'base_url': HOST, 'binding': BINDING,
                'worker_sha256': init['worker_sha256'], 'remote_root': str(root)}
    if sha(Path(__file__).read_bytes()) != init['worker_sha256']:
        raise ValueError('REMOTE_HELPER_HASH_MISMATCH')
    emit({'type': 'HELLO', 'identity': identity, 'stats_before': before, 'concurrency': 1})
    pending = None
    with httpx.Client(trust_env=False, timeout=180, follow_redirects=False) as client:
        for line in sys.stdin:
            command = json.loads(line)
            action = command['action']
            if action == 'STOP':
                emit({'type': 'STOPPED', 'stats_after': stats()})
                return
            if action == 'PREPARE':
                if pending is not None:
                    raise ValueError('UNFINISHED_PERMIT')
                key, body, contract = command['request_key'], command['body'], command['contract']
                if not re.fullmatch(r'[a-zA-Z0-9_.-]+', key):
                    raise ValueError('INVALID_REQUEST_KEY')
                if (sha(canonical(body)) != command['request_hash'] or len(canonical(body)) > 131072
                        or body.get('model') != MODEL or body.get('max_tokens') != 4096
                        or body.get('stream') is not False or body.get('enable_thinking') is not False
                        or body.get('temperature') != .7 or body.get('tools')):
                    raise ValueError('REMOTE_REQUEST_CONTRACT_MISMATCH')
                size = len(canonical(body))
                if (contract.get('version') != 'glm40-candidate-contract/v1'
                        or contract.get('request_hash') != command['request_hash']
                        or contract.get('model') != MODEL or contract.get('host') != HOST
                        or contract.get('binding') != BINDING
                        or contract.get('canonical_utf8_bytes') != size
                        or contract.get('canonical_utf8_cap') != 131072
                        or contract.get('input_tokens_upper') != size + 1024
                        or contract.get('output_tokens_upper') != 4096
                        or contract.get('cost_nano_upper') != (size + 1024) * 800 + 4096 * 2800
                        or contract.get('tools') is not False or contract.get('money_reserved') is not False
                        or set(body) != {'model', 'messages', 'max_tokens', 'temperature', 'stream', 'enable_thinking'}):
                    raise ValueError('REMOTE_IMMUTABLE_CONTRACT_MISMATCH')
                folder = root / key
                if folder.exists():
                    raise ValueError('EXISTING_REMOTE_ATTEMPT')
                folder.mkdir(mode=0o700)
                persist(folder / 'request.json', body)
                persist(folder / 'contract.json', contract)
                pending = (key, body, folder, command['request_hash'])
                emit({'type': 'READY', 'request_key': key, 'request_hash': command['request_hash'],
                      'identity': identity, 'contract_hash': sha(canonical(contract)), 'receipt_path': str(folder / 'receipt.json')})
                continue
            if action != 'POST' or pending is None or command['request_key'] != pending[0]:
                raise ValueError('UNBOUND_POST_PERMIT')
            key, body, folder, request_hash = pending
            if (folder / 'receipt.json').exists():
                raise ValueError('STALE_REMOTE_RECEIPT')
            started = time.time()
            clock = time.monotonic()
            receipt = {'request_key': key, 'request_hash': request_hash, 'identity': identity,
                       'started_at': started, 'transport_status': 'UNKNOWN'}
            try:
                response = client.post(HOST + '/chat/completions', content=canonical(body),
                                       headers={'Authorization': 'Bearer ' + row[2], 'Content-Type': 'application/json'})
                raw = response.content
                # Preserve exact raw provider bytes privately, without headers containing credentials.
                with (folder / 'response.raw').open('xb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                receipt.update(http_status=response.status_code,
                               provider_request_id=response.headers.get('x-request-id'),
                               raw_response_sha256=sha(raw), transport_status='RECEIVED')
                try:
                    receipt['response'] = json.loads(raw)
                except (ValueError, UnicodeError):
                    receipt['response_json_status'] = 'MALFORMED'
            except Exception as error:
                # Error strings can contain request headers; retain only the class.
                receipt['error_type'] = type(error).__name__
            receipt.update(latency_ms=(time.monotonic() - clock) * 1000,
                           remote_receipt_path=str(folder / 'receipt.json'), stats_after=stats())
            persist(folder / 'receipt.json', receipt)
            emit({'type': 'RESULT', 'receipt': receipt})
            pending = None


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        emit({'type': 'FATAL', 'error_type': type(error).__name__, 'error_code': 'REMOTE_WORKER_STOPPED'})
        raise SystemExit(2)

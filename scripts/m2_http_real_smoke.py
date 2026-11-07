"""Offline-only TCP/SSE smoke harness; no real provider transport is available.

Requires an immutable, explicitly authorized synthetic manifest. Historical
evidence is verified through the existing continuation verifier; this harness
does not authorize or implement paid calls.
"""
from __future__ import annotations

import argparse
import asyncio
import contextvars
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import sys
import time
from contextlib import contextmanager

MODULE = importlib.util.spec_from_file_location('m2_http_cont', Path(__file__).with_name('m2_preference_continue.py'))
cont = importlib.util.module_from_spec(MODULE)
MODULE.loader.exec_module(cont)
base = cont.base


def preflight(spec, *, real=False):
    schema = 'm2-http-real/v1' if real else 'm2-http-synthetic/v1'
    purpose = 'NEW_HTTP_SSE_ONLY' if real else 'OFFLINE_SYNTHETIC_ONLY'
    if (spec.get('schema') != schema or spec.get('authorized') is not True
            or not spec.get('authorization_id') or spec.get('purpose') != purpose
            or spec.get('max_new_requests') != 2):
        raise ValueError('Explicit authorization for exactly two requests required')
    history = spec['history']
    if len(history.get('prior_runs', [])) != 3:
        raise ValueError('Primary, supplement and both ordered continuations required')
    manifest, _, budget = cont.verify_history(history)
    if (cont.safety.decimal(budget['global_limit_cny']) != 1 or len(manifest['chunks']) != 80
            or manifest['max_output_tokens'] != 4096):
        raise ValueError('Frozen corpus, output cap or global budget mismatch')
    case = next(c for c in manifest['cases'] if c['qid'] == 'office50:031')
    if case['negative']:
        raise ValueError('Positive case required')
    db = base.private_path(spec['source_db'])
    if base.digest(db.read_bytes()) != spec['source_db_sha256']:
        raise ValueError('Source DB hash mismatch')
    with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True) as connection:
        actual_chunks = {row[0]: (row[1], row[2]) for row in connection.execute('SELECT id, file_id, content FROM document_chunks')}
    expected_chunks = {row['id']: (row['source_id'], row['content']) for row in manifest['chunks']}
    if actual_chunks != expected_chunks:
        raise ValueError('Source database does not contain the exact frozen corpus')
    ids = spec['identities']
    if (len(ids) != 2 or [x.get('mode') for x in ids] != ['chat', 'agent']
            or any(x.get('qid') != 'office50:031' or x.get('arm') != 'A' for x in ids)
            or len({x.get('run_identity') for x in ids}) != 2
            or any(not isinstance(x.get('run_identity'), str) or not x['run_identity'] for x in ids)):
        raise ValueError('Two distinct default chat/agent identities required')
    if real and (spec.get('qid') != 'office50:031' or spec.get('model') != manifest['model']
                 or spec.get('max_output_tokens') != 4096 or spec.get('total_budget_cny') != '1'
                 or spec.get('fixed_search') != base.FIXED_SEARCH or spec.get('synthetic_namespace') is not None
                 or spec.get('synthetic_provider_outcome') is not None
                 or spec.get('http_runner_sha256') != base.digest(Path(__file__).read_bytes())
                 or any(not x['run_identity'].startswith('m2-http-real:') or x.get('repetition') != 3 for x in ids)):
        raise ValueError('Frozen real purpose, identity, search, model or budget mismatch')
    if real:
        prior_outputs = [history['parent_output'], *(item['output'] for item in history['prior_runs'])]
        old_ids = set(); old_tuples = set()
        for old_output in prior_outputs:
            for filename in ('trials.jsonl', 'provider-ledger.jsonl'):
                for row in cont.rows((base.private_path(old_output) / filename).read_bytes()):
                    if row.get('run_identity'):
                        old_ids.add(row['run_identity'])
                    if all(key in row for key in ('qid', 'mode', 'arm', 'repetition')):
                        old_tuples.add(cont.identity(row))
        if old_ids & {item['run_identity'] for item in ids} or old_tuples & {cont.identity(item) for item in ids}:
            raise ValueError('Real identity was already started in historical evidence')
    return manifest, case, budget


class HistoryCheckedTransport(base.BudgetTransport):
    """Reverify frozen ancestry, budget and the selected new identity per send."""
    def __init__(self, manifest, output, spec, budget, *, transport=None, extra_check=None):
        super().__init__(manifest, output, transport=transport)
        self.spec = spec
        self.spec_hash = base.digest(base.canonical(spec))
        self.expected_committed = budget['prior_combined_committed_cny']
        self.allowed = {item['run_identity']: item for item in spec['identities']}
        self.extra_check = extra_check

    async def send(self, sender, client, request, *args, **kwargs):
        try:
            _, _, budget = cont.verify_history(self.spec['history'])
            if self.extra_check:
                self.extra_check()
            ident = self.allowed.get(self.context.get('run_identity'))
            if (base.digest(base.canonical(self.spec)) != self.spec_hash
                    or budget['prior_combined_committed_cny'] != self.expected_committed
                    or cont.safety.decimal(budget['remaining_cny']) != self.limit
                    or ident is None or any(self.context.get(key) != value for key, value in ident.items())):
                raise ValueError('History, budget or new identity changed')
        except Exception:
            self.stopped = True
            raise base.GuardStop('Historical evidence, budget or identity changed; no send') from None
        return await super().send(sender, client, request, *args, **kwargs)


@contextmanager
def no_external_sockets():
    original, original_ex, original_dns, original_sendto = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo, socket.socket.sendto
    original_sendmsg = getattr(socket.socket, 'sendmsg', None)
    original_afalg = getattr(socket.socket, 'sendmsg_afalg', None)
    def check(address):
        try:
            if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
                return
        except ValueError:
            pass
        raise RuntimeError('Offline harness blocks non-loopback network')
    def connect(sock, address):
        check(address)
        return original(sock, address)
    def connect_ex(sock, address):
        check(address)
        return original_ex(sock, address)
    def getaddrinfo(host, *args, **kwargs):
        if host not in ('localhost', '127.0.0.1', '::1'):
            raise RuntimeError('Offline harness blocks non-loopback DNS')
        return original_dns(host, *args, **kwargs)
    def sendto(sock, data, *args):
        check(args[-1])
        return original_sendto(sock, data, *args)
    def sendmsg(sock, buffers, ancdata=(), flags=0, address=None):
        check(address)
        return original_sendmsg(sock, buffers, ancdata, flags, address)
    def sendmsg_afalg(*args, **kwargs):
        raise RuntimeError('Offline harness blocks AF_ALG')
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    socket.getaddrinfo = getaddrinfo
    socket.socket.sendto = sendto
    if original_sendmsg is not None:
        socket.socket.sendmsg = sendmsg
    if original_afalg is not None:
        socket.socket.sendmsg_afalg = sendmsg_afalg
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo, socket.socket.sendto = original, original_ex, original_dns, original_sendto
        if original_sendmsg is not None:
            socket.socket.sendmsg = original_sendmsg
        if original_afalg is not None:
            socket.socket.sendmsg_afalg = original_afalg


class BoundedNetwork:
    """Experimental process-local guard, not yet enabled for real dispatch."""
    def __init__(self):
        self.dispatch_active = contextvars.ContextVar('m2_bounded_dispatch', default=False)
        self.addresses = set()

    @contextmanager
    def dispatch(self):
        token = self.dispatch_active.set(True)
        try:
            yield
        finally:
            self.dispatch_active.reset(token)

    @contextmanager
    def installed(self):
        loop = asyncio.get_running_loop()
        original_create_connection = loop.create_connection
        original_dns = socket.getaddrinfo
        original_connect, original_ex, original_sendto = socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto
        original_sendmsg = getattr(socket.socket, 'sendmsg', None)
        original_afalg = getattr(socket.socket, 'sendmsg_afalg', None)
        def check(address):
            try:
                ip = ipaddress.ip_address(address[0]) if isinstance(address, tuple) else None
            except ValueError:
                ip = None
            if ip is not None and ip.is_loopback:
                return
            if ip is None or not self.dispatch_active.get() or str(ip) not in self.addresses or address[1] != 443:
                raise RuntimeError('Unapproved network connection')
        def resolve(host, *args, **kwargs):
            if isinstance(host, bytes):
                host = host.decode('ascii')
            if host not in ('api.siliconflow.cn', 'localhost', '127.0.0.1', '::1'):
                raise RuntimeError('Unapproved DNS lookup')
            result = original_dns(host, *args, **kwargs)
            if host == 'api.siliconflow.cn':
                self.addresses.update(str(ipaddress.ip_address(row[4][0])) for row in result)
            return result
        def connect(sock, address):
            check(address); return original_connect(sock, address)
        def connect_ex(sock, address):
            check(address); return original_ex(sock, address)
        def sendto(sock, data, *args):
            check(args[-1]); return original_sendto(sock, data, *args)
        def sendmsg(sock, buffers, ancdata=(), flags=0, address=None):
            check(address); return original_sendmsg(sock, buffers, ancdata, flags, address)
        def sendmsg_afalg(*args, **kwargs):
            raise RuntimeError('AF_ALG is blocked')
        async def create_connection(protocol_factory, host=None, port=None, *args, **kwargs):
            if kwargs.get('sock') is not None:
                raise RuntimeError('Preconnected sockets are blocked')
            if host == 'api.siliconflow.cn':
                if not self.dispatch_active.get() or port != 443:
                    raise RuntimeError('Unapproved asynchronous connection')
            else:
                check((host, port))
            return await original_create_connection(protocol_factory, host, port, *args, **kwargs)
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = resolve, connect, connect_ex, sendto
        loop.create_connection = create_connection
        if original_sendmsg is not None: socket.socket.sendmsg = sendmsg
        if original_afalg is not None: socket.socket.sendmsg_afalg = sendmsg_afalg
        try:
            yield
        finally:
            loop.create_connection = original_create_connection
            socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex, socket.socket.sendto = original_dns, original_connect, original_ex, original_sendto
            if original_sendmsg is not None: socket.socket.sendmsg = original_sendmsg
            if original_afalg is not None: socket.socket.sendmsg_afalg = original_afalg


def copy_database(source, destination):
    # sqlite backup reads the existing development index without altering it.
    with sqlite3.connect(f'{source.as_uri()}?mode=ro', uri=True) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)


@contextmanager
def real_locks(spec, output):
    """Share the continuation lease and burn a persistent real-purpose grant."""
    active_dir = base.private_path(base.ROOT / 'var/preferences-acceptance/m2-continuation-locks')
    real_dir = base.private_path(base.ROOT / 'var/preferences-acceptance/m2-http-smoke-real-locks')
    active_dir.mkdir(parents=True, exist_ok=True)
    real_dir.mkdir(parents=True, exist_ok=True)
    active = active_dir / (spec['history']['parent_manifest_sha256'] + '.active')
    lease = secrets.token_hex(24).encode()
    with active.open('xb') as stream:
        stream.write(lease); stream.flush(); os.fsync(stream.fileno())
    retained = {'value': False}
    try:
        lock_value = base.canonical({'authorization_id': spec['authorization_id'], 'output': str(output),
                                     'spec_sha256': base.digest(base.canonical(spec))})
        # A single real-purpose lock per primary corpus prevents a later real
        # run from silently omitting this run's costs from the prior history.
        paths = [real_dir / ('chain-' + spec['history']['parent_manifest_sha256'] + '.started')]
        paths += [real_dir / ('identity-' + base.digest(i['run_identity'].encode()) + '.started') for i in spec['identities']]
        for path in paths:
            with path.open('xb') as stream:
                stream.write(lock_value); stream.flush(); os.fsync(stream.fileno())
        def check():
            if not active.exists() or active.read_bytes() != lease or any(not p.exists() or p.read_bytes() != lock_value for p in paths):
                raise ValueError('Real lease or persistent locks changed')
        yield retained, check
    finally:
        # Old continuation runners cannot account for a new real ledger. Once
        # any model call starts, retaining the shared lease fails closed until
        # a future explicitly reconciled history handles that ledger.
        if not retained['value'] and active.exists() and active.read_bytes() == lease:
            active.unlink()


async def read_http(port, body, evidence=None):
    reader, writer = await asyncio.open_connection('127.0.0.1', port)
    payload = base.canonical(body)
    writer.write(b'POST /api/v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: ' + str(len(payload)).encode() + b'\r\n\r\n' + payload)
    await writer.drain()
    data = b''
    try:
        while True:
            part = await asyncio.wait_for(reader.read(65536), timeout=120)
            if not part:
                break
            if len(data) + len(part) > 8 * 1024 * 1024:
                raise RuntimeError('HTTP response exceeds evidence bound')
            data += part
            if evidence is not None:
                with evidence.open('ab') as stream:
                    stream.write(part); stream.flush(); os.fsync(stream.fileno())
    finally:
        writer.close()
        await writer.wait_closed()
    header, raw = data.split(b'\r\n\r\n', 1)
    if not header.startswith(b'HTTP/1.1 200 ') or b'text/event-stream' not in header.lower():
        raise RuntimeError('HTTP/SSE response invalid')
    if b'transfer-encoding: chunked' in header.lower():
        pieces = []
        while True:
            size_line, raw = raw.split(b'\r\n', 1)
            size = int(size_line.split(b';', 1)[0], 16)
            if not size:
                break
            pieces.append(raw[:size])
            if raw[size:size+2] != b'\r\n':
                raise RuntimeError('Malformed HTTP chunk')
            raw = raw[size+2:]
        raw = b''.join(pieces)
    events = []
    for block in raw.decode('utf-8').replace('\r\n', '\n').split('\n\n'):
        lines = block.splitlines()
        kind = [line[7:] for line in lines if line.startswith('event: ')]
        data = [line[6:] for line in lines if line.startswith('data: ')]
        if kind:
            if len(kind) != 1 or len(data) != 1:
                raise RuntimeError('Malformed SSE event')
            events.append({'event': kind[0], 'data': json.loads(data[0])})
    return events


async def serve(manifest, case, spec, output, guard):
    import httpx
    import uvicorn
    from fastapi import FastAPI
    from app.api.chat import router
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', access_log=False, lifespan='off'))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        for _ in range(200):
            if server.started:
                break
            if task.done():
                raise RuntimeError('TCP server exited')
            await asyncio.sleep(.05)
        else:
            raise RuntimeError('TCP server did not start')
        async with guard.bind_services():
            for identity in spec['identities']:
                if guard.stopped:
                    raise base.GuardStop('Provider guard stopped')
                guard.context = {**identity, 'repetition': identity.get('repetition', 1), 'dataset_version': manifest['dataset_version']}
                # Durable identity marker precedes the first possible provider call.
                with (output / 'started.jsonl').open('ab') as stream:
                    stream.write(base.canonical(identity) + b'\n'); stream.flush(); os.fsync(stream.fileno())
                before = len(guard.calls)
                request = {'messages': [{'role': 'user', 'content': case['question']}],
                           'kb_ids': [base.KB_ID], 'mode': identity['mode'], **base.FIXED_SEARCH}
                events = []
                error = None
                try:
                    events = await read_http(port, request, output / ('http-' + identity['mode'] + '.raw'))
                except BaseException as exc:
                    error = type(exc).__name__
                record = {**identity, 'events': events, 'provider_call_ids': [c['call_id'] for c in guard.calls[before:]],
                          'state': 'FAILED' if error else 'RETURNED', 'error_type': error,
                          'guard_stopped': guard.stopped,
                          'provider_states': [c['state'] for c in guard.calls[before:]]}
                with (output / 'trials.jsonl').open('ab') as stream:
                    stream.write(base.canonical(record) + b'\n'); stream.flush(); os.fsync(stream.fileno())
                if error:
                    raise base.GuardStop('HTTP/SSE failed; recorded without retry')
                if guard.stopped or not events or events[-1]['event'] != 'done' or any(e['event'] == 'error' for e in events):
                    raise base.GuardStop('SSE failed or provider guard stopped; no further requests')
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 15)
        sock.close()


def execute(spec, output):
    import httpx
    manifest, case, budget = preflight(spec)
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise RuntimeError('Fresh process required')
    lock_dir = base.private_path(base.ROOT / 'var/preferences-acceptance/m2-http-smoke-locks')
    lock_dir.mkdir(parents=True, exist_ok=True)
    # One persistent lock per immutable historical chain prevents a new manifest
    # from reusing the same extra HTTP purpose after partial execution.
    chain_data = {'parent': spec['history']['parent_ledger_sha256'], 'prior': spec['history']['prior_runs']}
    if spec.get('synthetic_namespace'):
        # Namespace is available only in this offline-only executable. Preserve
        # identity uniqueness across every synthetic evidence directory.
        for started in lock_dir.parent.glob('m2-http-smoke-*/started.jsonl'):
            old = cont.rows(started.read_bytes())
            if {x['run_identity'] for x in old} & {x['run_identity'] for x in spec['identities']}:
                raise ValueError('Synthetic started identity cannot be repeated')
        chain_data['synthetic_namespace'] = spec['synthetic_namespace']
    chain = base.digest(base.canonical(chain_data))
    with (lock_dir / (chain + '.started')).open('xb') as lock:
        lock.write(base.canonical({'manifest_sha256': base.digest(base.canonical(spec)), 'output': str(output)})); lock.flush(); os.fsync(lock.fileno())
    for identity in spec['identities']:
        identity_key = base.digest(identity['run_identity'].encode('utf-8'))
        with (lock_dir / ('identity-' + identity_key + '.started')).open('xb') as lock:
            lock.write(base.canonical(identity)); lock.flush(); os.fsync(lock.fileno())
    output.mkdir(parents=True, exist_ok=False)
    (output / 'manifest.json').write_bytes(base.canonical(spec))
    source = base.private_path(spec['source_db'])
    copy_database(source, output / 'isolated.db')
    if base.digest(source.read_bytes()) != spec['source_db_sha256']:
        raise ValueError('Source DB changed during backup')
    (output / 'database-backup.json').write_bytes(base.canonical({'source_sha256': spec['source_db_sha256'],
        'backup_sha256': base.digest((output / 'isolated.db').read_bytes()), 'reason': 'RetrievalLog requires writes; preserve existing development index', 'source_corpus_files_copied': False}))
    calls = []
    async def provider(request):
        p = json.loads(request.content); calls.append(True)
        if spec.get('synthetic_provider_outcome') == 'UNKNOWN':
            raise httpx.ReadTimeout('synthetic provider timeout')
        message = {'role': 'assistant', 'content': 'Synthetic answer [1]'}; finish = 'stop'
        if p.get('tools') and not any(m.get('role') == 'tool' for m in p['messages']):
            message = {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'synthetic-tool', 'type': 'function', 'function': {'name': 'search_knowledge_base', 'arguments': json.dumps({'query': case['question']})}}]}; finish = 'tool_calls'
        elif '文档相关性评分员' in str(p['messages']):
            message = {'role': 'assistant', 'content': '0.95'}
        obj = {'id': f'synthetic-{len(calls)}', 'model': manifest['model'], 'object': 'chat.completion', 'created': 1,
               'choices': [{'index': 0, 'message': message, 'finish_reason': finish}], 'usage': {'prompt_tokens': 20, 'completion_tokens': 10, 'total_tokens': 30}}
        if p.get('stream'):
            first = {**obj, 'choices': [{'index': 0, 'delta': message, 'finish_reason': None}], 'usage': None}
            last = {**obj, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}], 'usage': None}
            tail = {**obj, 'choices': []}
            return httpx.Response(200, request=request, content=b''.join(b'data: ' + base.canonical(x) + b'\n\n' for x in (first, last, tail)) + b'data: [DONE]\n\n', headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, request=request, json=obj)
    runtime = {**manifest, 'budget_cny': budget['remaining_cny']}
    guard = HistoryCheckedTransport(runtime, output, spec, budget, transport=httpx.MockTransport(provider))
    summary = {'status': 'FAILED', 'real_provider_calls': 0, 'prior_budget': budget}
    previous = Path.cwd()
    try:
        with base.isolated_redis(output) as redis_url:
            os.environ['M2_AB_API_KEY'] = 'synthetic-only'
            base.configure_runtime(runtime, output, redis_url)
            with no_external_sockets(), guard.installed():
                cont.safety.assert_runtime_isolation(output, redis_url)
                asyncio.run(serve(runtime, case, spec, output, guard))
        summary['status'] = 'PASS_SYNTHETIC_ONLY'
    except BaseException as exc:
        summary.update(status='STOPPED', error_type=type(exc).__name__)
    finally:
        os.chdir(previous)
        simulated = guard.summary()
        summary['synthetic_accounting'] = simulated
        summary['historical_real_committed_cny'] = budget['prior_combined_committed_cny']
        summary['synthetic_hypothetical_combined_cny'] = str(cont.safety.decimal(budget['prior_combined_committed_cny']) + guard.committed)
        summary['provider_bill'] = 'UNKNOWN'
        (output / 'summary.json').write_bytes(base.canonical(summary))
    return summary


def execute_real(spec, spec_path, output):
    """Explicit opt-in only; never called by offline tests."""
    manifest, case, budget = preflight(spec, real=True)
    if not os.getenv('M2_AB_API_KEY'):
        raise ValueError('M2_AB_API_KEY missing; no model calls made')
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise RuntimeError('Fresh process required')
    if output.exists():
        raise ValueError('Fresh output directory required')
    spec_bytes = spec_path.read_bytes()
    if json.loads(spec_bytes) != spec:
        raise ValueError('Real authorization manifest changed during preflight')
    spec_digest = base.digest(spec_bytes)
    runtime = {**manifest, 'budget_cny': budget['remaining_cny']}
    previous = Path.cwd()
    with real_locks(spec, output) as (retained, check_locks):
        output.mkdir(parents=True, exist_ok=False)
        (output / 'manifest.json').write_bytes(spec_path.read_bytes())
        source = base.private_path(spec['source_db'])
        copy_database(source, output / 'isolated.db')
        if base.digest(source.read_bytes()) != spec['source_db_sha256']:
            raise ValueError('Source DB changed during backup')
        (output / 'database-backup.json').write_bytes(base.canonical({'source_sha256': spec['source_db_sha256'],
            'backup_sha256': base.digest((output / 'isolated.db').read_bytes()), 'reason': 'RetrievalLog writes; preserve frozen development index', 'source_corpus_files_copied': False}))
        def verify_before_send():
            check_locks()
            if base.digest(spec_path.read_bytes()) != spec_digest:
                raise ValueError('Real authorization manifest changed')
        guard = HistoryCheckedTransport(runtime, output, spec, budget, extra_check=verify_before_send)
        summary = {'status': 'FAILED', 'purpose': spec['purpose'], 'prior_budget': budget,
                   'correctness': 'REQUIRES_INDEPENDENT_REVIEW'}
        try:
            with base.isolated_redis(output) as redis_url:
                base.configure_runtime(runtime, output, redis_url)
                os.environ.update(RAG_OBSERVABILITY_ENABLED='false', RAG_METRICS_ENABLED='false', LANGFUSE_ENABLED='false')
                with guard.installed():
                    cont.safety.assert_runtime_isolation(output, redis_url)
                    asyncio.run(serve(runtime, case, spec, output, guard))
            summary['status'] = 'RECORDED_REQUIRES_EVIDENCE_REVIEW'
        except BaseException as exc:
            summary.update(status='STOPPED', error_type=type(exc).__name__)
        finally:
            os.chdir(previous)
            retained['value'] = bool(guard.calls)
            summary.update(guard.summary())
            summary['global_committed_including_unknown_cny'] = str(cont.safety.decimal(budget['prior_combined_committed_cny']) + guard.committed)
            summary['continuation_active_lease'] = 'RETAINED_PENDING_LEDGER_RECONCILIATION' if retained['value'] else 'RELEASED_IF_OWNED'
            (output / 'summary.json').write_bytes(base.canonical(summary))
        return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['preflight', 'execute'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--real', action='store_true', help='Use explicitly authorized real manifest and provider; never used by offline tests')
    args = parser.parse_args()
    spec_path = base.private_path(args.manifest)
    spec = json.loads(spec_path.read_bytes())
    output = base.private_path(args.output)
    if args.mode == 'preflight':
        _, _, budget = preflight(spec, real=args.real)
        result = {'status': 'PASS_REAL_PREFLIGHT' if args.real else 'PASS_SYNTHETIC_PREFLIGHT', 'budget': budget, 'real_provider_calls': 0}
    else:
        result = execute_real(spec, spec_path, output) if args.real else execute(spec, output)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'].startswith('PASS_') or result['status'] == 'RECORDED_REQUIRES_EVIDENCE_REVIEW' else 2


if __name__ == '__main__':
    raise SystemExit(main())

"""One-shot isolated M2 Hybrid/Rerank A/D experiment; execute only after review."""
import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import time
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[1]
VAR = ROOT / 'var/preferences-acceptance'
MANIFEST = VAR / 'm2-manifest-v3.json'
SOURCE = VAR / 'm2-cont-real-r2/isolated.db'
SUPPLEMENT = VAR / 'm2-current-supplement-real-english-v1/dataset.json'
MODEL_EVIDENCE = VAR / 'm2-hybrid-free-models-check-v1.json'
SUPPLEMENT_SHA = 'bbc959249b65c1c6a928fcff546347cdfd4eac581e9a21cf4b1a4963685ccb40'
GRANT = VAR / 'm2-hybrid-free-run-v1.started'
QIDS = ('m2-supp-current:quarterly-report-english-procedure-v1', 'office50:031', 'mmlongbench-doc:2ff6aa9237fc777b6627dc57a486e9225ac5fb86:row-0685')
MAX_CALLS = 80
MAX_PER_CONTEXT = 16
MAX_OUTPUT = 4096


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guardmod = load('m2_hybrid_guard', ROOT / 'scripts/m2_hybrid_free_regression.py')
app = load('m2_hybrid_app', ROOT / 'scripts/m2_hybrid_free_app.py')
base = load('m2_hybrid_base', ROOT / 'scripts/m2_preference_ab.py')
http = load('m2_hybrid_http', ROOT / 'scripts/m2_http_real_smoke.py')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot():
    paths = sorted((ROOT / 'backend/app').rglob('*.py'))
    paths += [Path(__file__), ROOT / 'scripts/m2_hybrid_free_app.py', ROOT / 'scripts/m2_hybrid_free_regression.py',
              ROOT / 'scripts/m2_preference_ab.py', ROOT / 'scripts/m2_http_real_smoke.py', ROOT / 'scripts/m2_preference_continue.py', ROOT / 'scripts/m2_preference_supplement.py']
    return {str(p.relative_to(ROOT)).replace('\\', '/'): sha(p) for p in paths}


def assert_empty_source_wal():
    wal = Path(str(SOURCE) + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('Source SQLite WAL is nonempty; snapshot not stable')


def make_plan():
    assert_empty_source_wal()
    result = guardmod.inspect(MANIFEST, SOURCE)
    frozen = json.loads(MANIFEST.read_bytes())
    supplement = json.loads(SUPPLEMENT.read_bytes())
    if sha(SUPPLEMENT) != SUPPLEMENT_SHA or supplement['chunks'] != frozen['chunks'] or not supplement.get('authorized'):
        raise ValueError('Supplement or corpus differs')
    cases = {c['qid']: c for c in frozen['cases'] + supplement['cases']}
    evidence = json.loads(MODEL_EVIDENCE.read_bytes())
    if evidence.get('status_code') != 200 or evidence.get('model_calls') != 0 or not all(evidence.get('models_visible', {}).get(m) is True for m in guardmod.ALLOWED_MODELS.values()):
        raise ValueError('Account model visibility evidence invalid')
    if any(q not in cases for q in QIDS) or any(cases[q]['negative'] != (i == 2) for i, q in enumerate(QIDS)):
        raise ValueError('Frozen cases differ')
    identities = [{'qid': q, 'mode': m, 'arm': a, 'repetition': 1} for q in QIDS for m in ('chat', 'agent') for a in ('A', 'D')]
    policies = {qid: http.cont.safety.policy_resolution(cases[qid]['question'], {'output_preferences': ['step_by_step']}) for qid in QIDS}
    if not policies[QIDS[0]]['policy_active']:
        raise ValueError('Procedural question does not activate D')
    return {'supplement_sha256': SUPPLEMENT_SHA, 'account_evidence_sha256': sha(MODEL_EVIDENCE),
            'account_evidence_path': str(MODEL_EVIDENCE), 'pricing_source': evidence['pricing_source'],
            'qwen_price_source': evidence['qwen_price_source'], 'policy_resolutions': policies,
            'schema': 'm2-hybrid-free-run/v1', 'manifest_sha256': result['manifest_sha256'],
            'source_db_sha256': sha(SOURCE), 'sources': snapshot(), 'identities': identities,
            'search': app.FIXED_SEARCH, 'max_calls': MAX_CALLS, 'max_per_context': MAX_PER_CONTEXT,
            'max_output_tokens': MAX_OUTPUT, 'models': guardmod.ALLOWED_MODELS,
            'entry': 'ChatService.chat_stream', 'fusion': 'weighted_normalized_sum',
            'qwen_thinking': False, 'cloud_trace': 'disabled', 'grant_available': not GRANT.exists(),
            'model_calls': 0, 'provider_bill': 'UNKNOWN', 'readiness': 'REQUIRES_REVIEW'}


def validate(plan):
    current = make_plan()
    if plan != current or not current['grant_available']:
        raise ValueError('Plan, source snapshot, or one-shot grant changed')
    return json.loads(MANIFEST.read_bytes())


def persist(path, value):
    with path.open('ab') as f:
        f.write(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8') + b'\n')
        f.flush(); os.fsync(f.fileno())


class LimitedCalls(guardmod.DurableCalls):
    def __init__(self, directory, plan=None):
        super().__init__(directory)
        self.plan = plan

    def begin(self, identity, request):
        if self.plan is not None:
            try:
                assert_empty_source_wal()
            except (OSError, ValueError):
                self.stopped = True
                raise guardmod.GuardStop('Source SQLite WAL is nonempty or unreadable') from None
        if self.plan is not None and (snapshot() != self.plan['sources'] or sha(SOURCE) != self.plan['source_db_sha256']
                or sha(MANIFEST) != self.plan['manifest_sha256'] or sha(SUPPLEMENT) != self.plan['supplement_sha256']
                or sha(MODEL_EVIDENCE) != self.plan['account_evidence_sha256']):
            self.stopped = True
            raise guardmod.GuardStop('Source or code changed before provider dispatch')
        # Sequential application trials; the lock also protects Agent concurrency.
        with self.lock:
            if getattr(self, 'reserved', 0) >= MAX_CALLS:
                self.stopped = True
                raise guardmod.GuardStop('Global call limit reached')
            context = identity.rsplit(':call-', 1)[0]
            contexts = getattr(self, 'contexts', {})
            if contexts.get(context, 0) >= MAX_PER_CONTEXT:
                self.stopped = True
                raise guardmod.GuardStop('Per-context call limit reached')
            contexts[context] = contexts.get(context, 0) + 1
            self.contexts = contexts
            self.reserved = getattr(self, 'reserved', 0) + 1
        return super().begin(identity, request)


def chat_observation(response):
    """Separate provider HTTP status from terminal generation and returned model."""
    try:
        raw = response.content
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            lines = [line[5:].strip() for line in raw.splitlines() if line.startswith(b'data:')]
            terminal = bool(lines and lines[-1] == b'[DONE]')
            items = [json.loads(line) for line in lines if line and line != b'[DONE]']
        else:
            terminal = True
            items = [json.loads(raw)]
        models = sorted({x.get('model') for x in items if x.get('model')})
        reasons = [c.get('finish_reason') for x in items for c in x.get('choices', []) if c.get('finish_reason')]
        state = 'UNKNOWN'
        if terminal and models == ['Qwen/Qwen3.5-4B'] and reasons:
            state = 'TRUNCATED' if 'length' in reasons else 'TERMINAL'
        return {'generation_status': state, 'actual_models': models, 'finish_reasons': reasons, 'stream_terminal': terminal}
    except (ValueError, TypeError, AttributeError):
        return {'generation_status': 'UNKNOWN', 'actual_models': [], 'finish_reasons': [], 'stream_terminal': False}


class CappedAdapter(guardmod.ProviderAdapter):
    def bound(self, request):
        import httpx
        if request.url.path != '/v1/chat/completions':
            return request
        data = json.loads(request.content)
        for name in ('max_tokens', 'max_completion_tokens'):
            if name in data and (type(data[name]) is not int or data[name] <= 0):
                raise guardmod.GuardStop('Invalid token limit')
        data['max_tokens'] = min(data.get('max_tokens') or MAX_OUTPUT, MAX_OUTPUT)
        if 'max_completion_tokens' in data:
            data['max_completion_tokens'] = min(data['max_completion_tokens'], MAX_OUTPUT)
        data['enable_thinking'] = False
        if data.get('stream'):
            data['stream_options'] = {**data.get('stream_options', {}), 'include_usage': True}
        headers = [(k, v) for k, v in request.headers.multi_items() if k.lower() != 'content-length']
        return httpx.Request(request.method, request.url, headers=headers, json=data, extensions=request.extensions)
    def complete(self, key, request, response):
        try:
            guardmod.validate_provider_response(request, response)
            if request.url.path == '/v1/chat/completions' and response.status_code == 200:
                
                if chat_observation(response)['generation_status'] == 'UNKNOWN':
                    raise guardmod.GuardStop('Chat completion status unknown')
        except guardmod.GuardStop:
            self.guard.finish(key, response=response, invalid='invalid or incomplete provider response')
        self.guard.finish(key, response=response)
        return response
    async def async_send(self, request):
        request = self.bound(request)
        key = self._begin(request)
        try:
            response = await self.async_inner.handle_async_request(request)
            await response.aread()
            response.request = request
        except BaseException as exc:
            self.guard.finish(key, error=exc)
            raise
        return self.complete(key, request, response)
    def sync_send(self, request):
        request = self.bound(request)
        key = self._begin(request)
        try:
            response = self.sync_inner.handle_request(request)
            response.read()
            response.request = request
        except BaseException as exc:
            self.guard.finish(key, error=exc)
            raise
        return self.complete(key, request, response)


class NetworkTransport:
    def __init__(self, inner, network):
        self.inner, self.network = inner, network
    async def handle_async_request(self, request):
        with self.network.dispatch():
            response = await self.inner.handle_async_request(request)
            await response.aread()
            return response
    def handle_request(self, request):
        with self.network.dispatch():
            response = self.inner.handle_request(request)
            response.read()
            return response


@contextmanager
def restrict_loopback(redis_url):
    import socket
    import ipaddress
    from urllib.parse import urlsplit
    parsed = urlsplit(redis_url)
    if parsed.hostname != '127.0.0.1' or not parsed.port or parsed.port == 11434:
        raise ValueError('Expected owned loopback Redis URL')
    old, old_ex = socket.socket.connect, socket.socket.connect_ex
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    old_create = loop.create_connection if loop else None
    def check(address):
        if not isinstance(address, tuple) or len(address) < 2:
            raise guardmod.GuardStop('Unapproved local connection')
        host = address[0]
        if host == 'localhost':
            if address[1] != parsed.port:
                raise guardmod.GuardStop('Unapproved loopback connection')
            return
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return  # BoundedNetwork separately validates provider host and dispatch.
        if ip.is_loopback and (str(ip) != '127.0.0.1' or address[1] != parsed.port):
            raise guardmod.GuardStop('Unapproved loopback connection')
    def connect(sock, address):
        check(address); return old(sock, address)
    def connect_ex(sock, address):
        check(address); return old_ex(sock, address)
    async def create_connection(protocol_factory, host=None, port=None, *args, **kwargs):
        check((host, port))
        return await old_create(protocol_factory, host, port, *args, **kwargs)
    socket.socket.connect, socket.socket.connect_ex = connect, connect_ex
    if loop is not None:
        loop.create_connection = create_connection
    try:
        yield
    finally:
        if loop is not None:
            loop.create_connection = old_create
        socket.socket.connect, socket.socket.connect_ex = old, old_ex


async def run(plan, output, api_key, adapter, redis_url):
    from app.services.memory import PreferenceScope, get_memory_service
    from app.services.trace_schema import capture_scope
    cases = {c['qid']: c for c in (json.loads(MANIFEST.read_bytes())['cases'] + json.loads(SUPPLEMENT.read_bytes())['cases'])}
    memory = get_memory_service()
    try:
        async with app.bound_provider(adapter, api_key):
            index = await asyncio.wait_for(app.build_index(json.loads(MANIFEST.read_bytes()), SOURCE, adapter, expected_source_sha256=plan['source_db_sha256']), 600)
            persist(output / 'index.jsonl', index)
            for item in plan['identities']:
                identity = f"{item['qid']}:{item['mode']}:{item['arm']}"
                persist(output / 'started.jsonl', item)
                scope = PreferenceScope(tenant_id='m2-hybrid', user_id=hashlib.sha256(identity.encode()).hexdigest())
                start = time.monotonic()
                result = {**item, 'state': 'STARTED'}
                try:
                    with capture_scope(run_id=output.name, qid=item['qid'], attempt_id=identity, dataset_version='m2-20261104-dev-v3', capture_mode='BASIC'):
                        result = await asyncio.wait_for(app.trial(cases[item['qid']], item['mode'], item['arm'], scope, adapter,
                            on_event=lambda event: persist(output / 'events.jsonl', {'identity': identity, **event})), 480)
                    if result['state'] != 'RETURNED' or adapter.guard.stopped:
                        raise guardmod.GuardStop('Trial incomplete or provider stopped')
                except BaseException as exc:
                    result.update(state='STOPPED', error_type=type(exc).__name__)
                    persist(output / 'trials.jsonl', {**result, 'elapsed_seconds': time.monotonic()-start})
                    raise
                persist(output / 'trials.jsonl', {**result, 'elapsed_seconds': time.monotonic()-start})
    finally:
        await memory.client.aclose()


def usage_records(directory):
    records = []
    for started in sorted(directory.glob('*.started.json')):
        data = json.loads(started.read_bytes())
        stem = started.name.removesuffix('.started.json')
        finished, body = directory / (stem + '.finished.json'), directory / (stem + '.response.body')
        outcome = json.loads(finished.read_bytes()) if finished.exists() else {'state': 'UNKNOWN_PENDING'}
        usage = None
        if body.exists():
            raw = body.read_bytes()
            try:
                obj = json.loads(raw)
                usage = obj.get('usage') if isinstance(obj, dict) else None
            except (ValueError, UnicodeDecodeError):
                for line in raw.splitlines():
                    if line.startswith(b'data:') and line[5:].strip() not in (b'', b'[DONE]'):
                        try:
                            obj = json.loads(line[5:])
                            if isinstance(obj, dict) and obj.get('usage'):
                                usage = obj['usage']
                        except ValueError:
                            pass
        record = {'identity': data['identity'], 'endpoint': data['endpoint'], 'model': data['model'],
                  'http_status': outcome.get('status_code'), 'transport_state': outcome['state'],
                  'usage': usage, 'usage_status': 'RECEIPT' if usage is not None else 'UNKNOWN'}
        if body.exists() and data['endpoint'] == '/v1/chat/completions':
            import httpx
            raw = body.read_bytes()
            headers = {'content-type': 'text/event-stream'} if raw.lstrip().startswith(b'data:') else {}
            record.update(chat_observation(httpx.Response(200, content=raw, headers=headers)))
        records.append(record)
    return records


def execute(plan, output):
    import httpx
    manifest = validate(plan)
    del manifest
    if not os.getenv('M2_AB_API_KEY'):
        raise ValueError('M2_AB_API_KEY missing; no sends')
    output = Path(output).resolve()
    if output.parent != VAR.resolve() or not output.name.startswith('m2-hybrid-free-run-') or output.exists():
        raise ValueError('Fresh private output required')
    if any(name == 'app' or name.startswith('app.') for name in sys.modules):
        raise ValueError('Fresh process required')
    with GRANT.open('xb') as f:
        f.write(json.dumps({'plan_sha256': hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest(), 'output': str(output)}).encode())
        f.flush(); os.fsync(f.fileno())
    output.mkdir()
    (output / 'plan.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding='utf-8')
    calls = LimitedCalls(output / 'calls', plan)
    network = http.BoundedNetwork()
    async_inner = httpx.AsyncHTTPTransport(retries=0, trust_env=False)
    sync_inner = httpx.HTTPTransport(retries=0, trust_env=False)
    adapter = CappedAdapter(calls, NetworkTransport(async_inner, network), NetworkTransport(sync_inner, network))
    async def go(url):
        try:
            with network.installed(), restrict_loopback(url):
                await run(plan, output, os.environ['M2_AB_API_KEY'], adapter, url)
        finally:
            await async_inner.aclose(); sync_inner.close()
    status = {'state': 'STOPPED', 'provider_bill': 'UNKNOWN', 'calls': 0}
    try:
        with base.isolated_redis(output) as url:
            app.configure_runtime(output, url, os.environ['M2_AB_API_KEY'])
            asyncio.run(go(url))
        status['state'] = 'RECORDED_REQUIRES_REVIEW'
    except BaseException as exc:
        status['error_type'] = type(exc).__name__
    finally:
        status['calls'] = calls.count
        for record in usage_records(calls.directory):
            persist(output / 'usage.jsonl', record)
        persist(output / 'summary.jsonl', status)
    return status


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=('preflight', 'execute'))
    p.add_argument('--plan', required=True)
    p.add_argument('--output')
    args = p.parse_args()
    plan_path = Path(args.plan).resolve()
    if plan_path.parent != VAR.resolve() or not plan_path.name.startswith('m2-hybrid-free-'):
        raise ValueError('Plan must be under private experiment directory')
    if args.mode == 'preflight':
        plan = make_plan()
        with plan_path.open('x', encoding='utf-8') as f:
            json.dump(plan, f, ensure_ascii=False, indent=2)
        print(json.dumps({'status':'PASS', 'model_calls':0, 'plan':str(plan_path), 'identities':len(plan['identities'])}))
    else:
        if not args.output: raise ValueError('Output required')
        result = execute(json.loads(plan_path.read_bytes()), args.output)
        print(json.dumps(result))
        if result['state'] != 'RECORDED_REQUIRES_REVIEW': sys.exit(1)


if __name__ == '__main__':
    main()

"""Synthetic contracts and temporary ledgers only; no network or benchmark Gold."""
import importlib.util
import json
from pathlib import Path
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('glm40_runner', ROOT / 'scripts/run_glm40_candidate.py')
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def case(item='synthetic'):
    body = {'model': r.MODEL, 'messages': [{'role': 'user', 'content': 'synthetic source question'}],
            'max_tokens': 4096, 'temperature': .7, 'stream': False, 'enable_thinking': False}
    size = len(r.canonical(body))
    contract = {'version': r.VERSION, 'item_id': item, 'request_hash': r.digest(body), 'model': r.MODEL,
                'host': r.HOST, 'binding': r.BINDING, 'canonical_utf8_bytes': size, 'canonical_utf8_cap': r.CAP,
                'input_tokens_upper': size + 1024, 'output_tokens_upper': 4096,
                'cost_nano_upper': (size + 1024)*800 + 4096*2800,
                'tools': False, 'money_reserved': False, 'pricing': r.PRICING}
    return {'item_id': item, 'body': body, 'contract': contract, 'status': 'READY'}


@pytest.fixture
def budget(tmp_path):
    b = r.Budget(tmp_path / 'test.sqlite')
    r.install_prices(b)
    return b


class RPC:
    def __init__(self, status='OK', delay=0):
        self.identity = {'model': r.MODEL, 'base_url': r.HOST, 'binding': r.BINDING,
                         'worker_sha256': 'synthetic', 'remote_root': r.remote_root('test')}
        self.status, self.delay, self.posts = status, delay, 0

    def prepare(self, key, body, contract):
        self.key, self.body, self.contract = key, body, contract
        return {'type': 'READY', 'request_key': key, 'contract_hash': r.digest(contract)}

    def post(self, key):
        self.posts += 1
        time.sleep(self.delay)
        response = {'id': 'synthetic-id', 'model': r.MODEL,
                    'usage': {'prompt_tokens': 30, 'completion_tokens': 8},
                    'choices': [{'message': {'content': 'synthetic answer'}, 'finish_reason': 'stop'}]}
        result = {'request_key': key, 'request_hash': r.digest(self.body), 'identity': self.identity,
                  'transport_status': 'RECEIVED', 'http_status': 200, 'response': response,
                  'remote_receipt_path': self.identity['remote_root'] + '/' + key + '/receipt.json'}
        if self.status == 'malformed':
            result['response'] = []
        if self.status == 'stale':
            result['remote_receipt_path'] = '/stale/receipt.json'
        if self.status == 'partial':
            response['choices'][0]['finish_reason'] = 'length'
        return result


def test_scope_rejects_final_even_if_excluded_metadata_is_present():
    ids = ['x' + str(i) for i in range(39)] + ['qst_0223']
    with pytest.raises(ValueError, match='INVALID_SCOPE40'):
        r.scope_ids({'included_ids': ids, 'excluded_ids': list(r.EXCLUDED)})


@pytest.mark.parametrize('value', ['../x', '', 'a/b', 'x;echo'])
def test_remote_root_rejects_unsafe_run_id(value):
    with pytest.raises(ValueError):
        r.remote_root(value)


def test_success_usage_and_resume_never_resends(tmp_path, budget):
    rpc = RPC()
    c = case()
    one = r.execute_one(c, tmp_path / 'out', budget, rpc)
    two = r.execute_one(c, tmp_path / 'out', budget, rpc)
    assert one['status'] == 'OK' and two['resumed'] and rpc.posts == 1
    assert budget.snapshot()['attempts'] == budget.snapshot()['completed'] == 1
    assert one['usage'] == {'input_tokens': 30, 'output_tokens': 8, 'input_tokens_details': {'cached_tokens': 0}}


def test_malformed_response_bounded_and_stops(tmp_path, budget):
    rpc = RPC('malformed')
    result = r.execute_one(case(), tmp_path / 'out', budget, rpc)
    assert result['status'] == 'FATAL_IDENTITY_MISMATCH'
    state = budget.snapshot()
    assert not state['pending_request_keys'] and not state['unbounded_usage_requests']
    assert len(state['bounded_unknown_usage_costs_nano']) == 1


def test_stale_remote_receipt_path_is_fatal(tmp_path, budget):
    result = r.execute_one(case(), tmp_path / 'out', budget, RPC('stale'))
    assert result['status'] == 'FATAL_IDENTITY_MISMATCH'


def test_pending_external_attempt_blocks_dispatch(tmp_path, budget):
    budget.start('external', 'test', r.MODEL, 'hash')
    budget.inflight.clear()
    rpc = RPC()
    with pytest.raises(RuntimeError, match='BILLING_OR_REQUEST_STATUS_UNKNOWN'):
        r.execute_one(case(), tmp_path / 'out', budget, rpc)
    assert rpc.posts == 0


def test_resume_corrupt_receipt_never_resends(tmp_path, budget):
    rpc = RPC()
    c = case()
    result = r.execute_one(c, tmp_path / 'out', budget, rpc)
    receipt = tmp_path / 'out' / result['request_key'] / 'receipt.json'
    receipt.write_text('{malformed')
    with pytest.raises(RuntimeError, match='EXPLICIT_RECOVERY'):
        r.execute_one(c, tmp_path / 'out', budget, rpc)
    assert rpc.posts == 1


def test_receipt_recovery_without_result_no_resend(tmp_path, budget):
    rpc = RPC()
    c = case()
    result = r.execute_one(c, tmp_path / 'out', budget, rpc)
    (tmp_path / 'out' / result['request_key'] / 'result.json').unlink()
    restored = r.execute_one(c, tmp_path / 'out', budget, rpc)
    assert restored['resumed'] and rpc.posts == 1 and budget.snapshot()['completed'] == 1


def test_first_fatal_prevents_other_dispatch(tmp_path, budget):
    rpcs = [RPC('partial'), RPC()]
    results, stopped = r.execute([case(str(i)) for i in range(4)], tmp_path / 'out', budget, rpcs)
    assert stopped == 'INCOMPLETE' and len(results) == 1
    assert [x.posts for x in rpcs] == [1, 0]


def test_concurrency_two_after_first_probe(tmp_path, budget):
    active = 0
    peak = 0
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    class ConcurrentRPC(RPC):
        def post(self, key):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if sum(x.posts for x in rpcs) == 0:
                    assert active == 1
            try:
                if not key.startswith('glm40-candidate-v1-0-'):
                    barrier.wait(timeout=5)
                return super().post(key)
            finally:
                with lock:
                    active -= 1
    rpcs = [ConcurrentRPC(delay=.03), ConcurrentRPC(delay=.03)]
    results, stopped = r.execute([case(str(i)) for i in range(5)], tmp_path / 'out', budget, rpcs)
    assert stopped is None and len(results) == 5 and peak == 2
    assert budget.snapshot()['completed'] == 5


def test_serialize_required_waits_and_does_not_retry_post(tmp_path, budget, monkeypatch):
    original = budget.start
    entered = threading.Event()
    calls = []
    def start(key, *args):
        calls.append(key)
        if len(calls) == 1:
            entered.set()
            raise RuntimeError('BUDGET_SERIALIZE_REQUIRED')
        return original(key, *args)
    monkeypatch.setattr(budget, 'start', start)
    control = r.DispatchControl(budget)
    thread = threading.Thread(target=lambda: control.start('key', case()['body']))
    thread.start()
    assert entered.wait(2)
    with control.condition:
        control.condition.notify_all()
    thread.join(2)
    assert not thread.is_alive() and calls == ['key', 'key']


def test_immutable_contract_tamper_rejected(tmp_path, budget):
    c = case()
    c['contract']['output_tokens_upper'] = 1
    rpc = RPC()
    with pytest.raises(ValueError, match='CASE_BINDING_MISMATCH'):
        r.execute_one(c, tmp_path / 'out', budget, rpc)
    assert rpc.posts == 0


def test_offline_prepare_no_gold_runtime(tmp_path, monkeypatch):
    requests = tmp_path / 'requests'
    requests.mkdir()
    cases = []
    for i in range(40):
        c = case('synthetic-' + str(i))
        path = requests / (str(i) + '.json')
        path.write_bytes(r.canonical(c['body']))
        cases.append({'item_id': c['item_id'], 'request_path': str(path), 'request_sha256': r.sha(path.read_bytes())})
    (tmp_path / 'manifest.json').write_text(json.dumps({'cases': cases}))
    scope = tmp_path / 'scope.json'
    scope.write_text(json.dumps({'included_ids': [c['item_id'] for c in cases], 'excluded_ids': list(r.EXCLUDED)}))
    prepared, _ = r.prepare(scope, tmp_path)
    assert len(prepared) == 40 and all(x['status'] == 'READY' for x in prepared)
    assert all(set(x['body']) == {'model', 'messages', 'max_tokens', 'temperature', 'stream', 'enable_thinking'} for x in prepared)


def test_helper_upload_is_only_in_run_directory():
    source = Path(r.__file__).read_text()
    assert "remote_helper = remote_root(run_id) + '/worker-'" in source
    assert 'glm40-controller-' not in source
    worker = (ROOT / 'scripts/glm40_remote_worker.py').read_text()
    assert '?mode=ro' in worker and "'-wal'" in worker
    assert 'REMOTE_IMMUTABLE_CONTRACT_MISMATCH' in worker

def test_pending_durable_receipt_is_accounted_without_resend(tmp_path, budget):
    c, rpc = case(), RPC()
    key = 'glm40-candidate-v1-' + c['item_id'] + '-' + r.digest(c['body'])
    folder = tmp_path / 'out' / key
    folder.mkdir(parents=True)
    r.save(folder / 'request.json', c['body'])
    r.save(folder / 'contract.json', c['contract'])
    budget.log.append('contract', 'MODEL_REQUEST_GLM40_CONTRACT', dict(c['contract'], request_key=key))
    budget.start(key, 'test', r.MODEL, r.digest(c['body']))
    budget.inflight.clear()  # new controller after interruption
    rpc.prepare(key, c['body'], c['contract'])
    receipt = rpc.post(key)
    rpc.posts = 0
    r.save(folder / 'receipt.json', receipt)
    results, stopped = r.execute([c], tmp_path / 'out', budget, [rpc, RPC()])
    assert stopped is None and results[0]['resumed'] and rpc.posts == 0
    assert not budget.snapshot()['pending_request_keys']


def test_last_single_call_may_overshoot_then_no_second_post(tmp_path, budget):
    budget.limit_nano = 1
    rpcs = [RPC(), RPC()]
    results, stopped = r.execute([case('a'), case('b'), case('c')], tmp_path / 'out', budget, rpcs)
    assert stopped == 'BUDGET_EXHAUSTED'
    assert sum(x.posts for x in rpcs) == 1
    assert budget.snapshot()['recorded_cost_nano_upper'] > budget.limit_nano


def test_resume_tampered_answer_or_status_rejected(tmp_path, budget):
    rpc, c = RPC(), case()
    result = r.execute_one(c, tmp_path / 'out', budget, rpc)
    path = tmp_path / 'out' / result['request_key'] / 'result.json'
    result['answer'] = 'tampered'
    path.write_text(json.dumps(result))
    with pytest.raises(RuntimeError, match='EXPLICIT_RECOVERY'):
        r.execute_one(c, tmp_path / 'out', budget, rpc)
    assert rpc.posts == 1


def test_rpc_uses_pinned_existing_runtime_and_preserves_fatal_hello(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace
    launches = []
    fatal = {'type': 'FATAL', 'error_type': 'ModuleNotFoundError', 'error_code': 'REMOTE_WORKER_STOPPED'}
    class Process:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO(r.canonical(fatal) + b'\n')
            self.closed = False
        def poll(self):
            return 0 if self.closed else None
        def terminate(self):
            self.closed = True
        def wait(self, timeout):
            return 0
    def launch(command, **kwargs):
        launches.append(command)
        return Process()
    monkeypatch.setattr(r.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    monkeypatch.setattr(r.subprocess, 'Popen', launch)
    monkeypatch.setattr(r, 'restrict_directory', lambda directory: None)
    with pytest.raises(RuntimeError, match='REMOTE_READY_FAILED:.*ModuleNotFoundError'):
        r.SSHRPC('synthetic', diagnostics_dir=tmp_path)
    assert launches[0][-1].startswith(r.shell_quote(r.REMOTE_PYTHON) + ' -u ')
    assert r.REMOTE_PYTHON == '/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python'
    files = list(tmp_path.glob('startup-failure-*.json'))
    assert len(files) == 1
    diagnostic = r.load(files[0])
    assert diagnostic['remote_message'] == fatal and diagnostic['api_calls'] == 0
    assert diagnostic['remote_python'] == r.REMOTE_PYTHON

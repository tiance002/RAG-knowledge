"""Isolated GLM40 execution controller. Default is offline metadata-only prepare.

Two persistent channels share one Budget; the first probe is serial.
Near the ceiling dispatch waits for accounting before a new permit. No retries, budget reset/reservation, service or database writes.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'backend'))
from app.services.glm_answer_policy import augment_glm_messages
from rsik.budgets import Budget, PRICES
from rsik.events import canonical, digest
from rsik.locking import WorkspaceLock, atomic_write, restrict_directory

TASK = Path('E:/codex_workspace/2026-11-03/task/rsi-work/var/glm40-fix-20261117')
LEDGER = Path('E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-v22-20261115/task-model-budget.sqlite')
MODEL = 'zai-org/GLM-5.3-Flash'
HOST = 'https://api.siliconflow.cn/v1'
BINDING = 'e6a15570-9f5f-422f-bb13-ebcd95a95f1c'
SSH_KEY = Path('C:/Users/22088/.ssh/study-plan-dev.pem')
# Reuse the existing ECS replay runtime; never install or rebuild dependencies.
REMOTE_PYTHON = '/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python'
EXCLUDED = {'qst_0223', 'qst_0665', 'qst_0697', 'terra-ecs-b10-004'}
CAP = 131072
VERSION = 'glm40-candidate-contract/v1'
PRICING = {'version': 'glm53-flash-conservative-cny-numerals-as-usd-20261117/v1',
           'model': MODEL, 'control_currency': 'USD', 'not_fx_or_invoice': True, 'cost_kind': 'conservative_upper_not_actual_fx_or_invoice',
           'control_usd_per_million': {'input': .80, 'cached': .23, 'output': 2.80},
           'official_cn': {'currency': 'CNY', 'per_million': {'input': .80, 'cached': .23, 'output': 2.80},
                           'url': 'https://www.siliconflow.cn/models/zai-org%2FGLM-5.3-Flash'},
           'official_global': {'currency': 'USD', 'per_million': {'input': .15, 'cached': .03, 'output': .50},
                               'url': 'https://www.siliconflow.com/zh/pricing'},
           'rates_nano_per_token': {'input': 800, 'cached': 230, 'write': 800, 'output': 2800},
           'basis': 'CN price numerals deliberately treated 1:1 as USD upper for budget control; not conversion or invoiced cost'}
POLICY_FILE = ROOT / 'backend/app/services/glm_answer_policy.py'
HELPER = Path(__file__).with_name('glm40_remote_worker.py')


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def save(path, obj):
    atomic_write(Path(path), canonical(obj), exclusive=True)


def install_prices(budget):
    """Process-local additive rate; existing entries/rates are never changed."""
    rates = PRICING['rates_nano_per_token']
    if MODEL in PRICES and PRICES[MODEL] != rates:
        raise RuntimeError('GLM_PRICE_CONFLICT')
    PRICES[MODEL] = dict(rates)
    budget.log.append('glm40-price-policy-v1', 'MODEL_PRICING_POLICY_DECLARED', PRICING)


def scope_ids(scope):
    ids, excluded = scope.get('included_ids'), scope.get('excluded_ids')
    if (not isinstance(ids, list) or len(ids) != 40 or len(set(ids)) != 40
            or set(ids) & EXCLUDED
            or not isinstance(excluded, list) or not EXCLUDED <= set(excluded)
            or set(ids) & set(excluded)):
        raise ValueError('INVALID_SCOPE40')
    return set(ids), set(excluded)


def prepare(scope_path, candidate_path):
    scope = load(scope_path)
    allowed, excluded = scope_ids(scope)
    manifest_path = Path(candidate_path) / 'manifest.json'
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    selected = [row for row in manifest['cases'] if row['item_id'] in allowed and row['item_id'] not in excluded]
    if len(selected) != 40 or {r['item_id'] for r in selected} != allowed:
        raise ValueError('CANDIDATE_SCOPE_MISMATCH')
    bindings = {'scope_sha256': sha(Path(scope_path).read_bytes()), 'candidate_manifest_sha256': sha(manifest_raw),
                'policy_sha256': sha(POLICY_FILE.read_bytes()), 'worker_sha256': sha(HELPER.read_bytes()),
                'runner_sha256': sha(Path(__file__).read_bytes())}
    cases = []
    for row in selected:
        item = row['item_id']
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', item):
            raise ValueError('UNSAFE_ITEM_ID')
        path = Path(row['request_path']).resolve()
        if path.parent != (Path(candidate_path) / 'requests').resolve():
            raise ValueError('UNSAFE_CANDIDATE_REQUEST_PATH')
        raw = path.read_bytes()
        if sha(raw) != row['request_sha256']:
            raise ValueError('CANDIDATE_SOURCE_HASH_MISMATCH')
        source = json.loads(raw)
        if set(source) != {'model', 'messages', 'max_tokens', 'temperature', 'stream', 'enable_thinking'}:
            raise ValueError('UNAUTHORIZED_RUNTIME_FIELD')
        if (source['model'] != MODEL or source['max_tokens'] != 4096 or source['temperature'] != .7
                or source['stream'] is not False or source['enable_thinking'] is not False):
            raise ValueError('BASELINE_SAMPLING_MISMATCH')
        messages = source['messages']
        if not isinstance(messages, list) or any(not isinstance(m, dict) or m.get('role') not in {'system', 'user', 'assistant'}
                or not isinstance(m.get('content'), str) for m in messages):
            raise ValueError('UNSUPPORTED_MESSAGES')
        body = dict(source, messages=augment_glm_messages(messages))
        request_hash = digest(body)
        size = len(canonical(body))
        contract = {'version': VERSION, 'item_id': item, 'request_hash': request_hash,
                    'model': MODEL, 'host': HOST, 'binding': BINDING, 'canonical_utf8_bytes': size,
                    'canonical_utf8_cap': CAP, 'input_tokens_upper': size + 1024, 'output_tokens_upper': 4096,
                    'cost_nano_upper': (size + 1024) * 800 + 4096 * 2800,
                    'tools': False, 'money_reserved': False, 'pricing': PRICING,
                    'source_request_sha256': sha(raw), 'bindings': bindings}
        cases.append({'item_id': item, 'body': body, 'contract': contract,
                      'status': 'READY' if size <= CAP else 'too_large'})
    return cases, bindings


def gate(budget):
    state = budget.snapshot()
    if state['pending_request_keys'] or state['unbounded_usage_requests']:
        raise RuntimeError('BILLING_OR_REQUEST_STATUS_UNKNOWN')
    if state['recorded_cost_nano_upper'] >= budget.limit_nano:
        raise RuntimeError('BUDGET_EXHAUSTED')
    return state


def normalize_usage(raw):
    if not isinstance(raw, dict):
        return None
    prompt, output = raw.get('prompt_tokens'), raw.get('completion_tokens')
    if type(prompt) is not int or type(output) is not int or prompt < 0 or output < 0:
        return None
    details = raw.get('prompt_tokens_details')
    if details is not None and not isinstance(details, dict):
        return None
    cached = details.get('cached_tokens', 0) if details else 0
    if type(cached) is not int or not 0 <= cached <= prompt:
        return None
    # No discount unless provider explicitly reports cached prompt tokens.
    return {'input_tokens': prompt, 'output_tokens': output, 'input_tokens_details': {'cached_tokens': cached}}


def bound_unknown(budget, key, body, contract):
    with budget.mutex, WorkspaceLock(budget.lock, 'glm40-bounded-unknown'):
        events = budget.log.read()
        starts = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_STARTED' and e['payload']['request_key'] == key]
        completed = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_COMPLETED' and e['payload']['request_key'] == key]
        declared = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_GLM40_CONTRACT' and e['payload']['request_key'] == key]
        size = len(canonical(body))
        if (len(starts) != 1 or len(completed) != 1 or completed[0]['usage_status'] != 'UNKNOWN'
                or declared != [dict(contract, request_key=key)] or starts[0]['request_hash'] != digest(body)
                or contract['request_hash'] != digest(body) or contract['model'] != MODEL or body.get('model') != MODEL
                or size > CAP or contract['canonical_utf8_bytes'] != size or contract['input_tokens_upper'] != size + 1024
                or body.get('max_tokens') != 4096 or body.get('tools')
                or contract['pricing'] != PRICING or contract['cost_nano_upper'] != (size + 1024) * 800 + 4096 * 2800):
            raise ValueError('UNBOUND_GLM40_COST_BOUND')
        budget.log.append('posthoc-bound:' + key, 'MODEL_REQUEST_POSTHOC_COST_BOUND', {
            'request_key': key, 'request_hash': digest(body), 'contract_version': VERSION,
            'contract_hash': digest(contract), 'cost_nano_upper': contract['cost_nano_upper'],
            'input_tokens_upper': size + 1024, 'output_tokens_upper': 4096, 'usage_status': 'UNKNOWN_BOUNDED',
            'price_version': PRICING['version'], 'money_reserved': False,
            'basis': 'exact pre-POST canonical request byte count + 1024 token overhead, max output4096, conservative USD control policy; not actual invoice'},
            evidence_hashes=[digest(body), digest(contract), digest(PRICING)])


def remote_root(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', run_id):
        raise ValueError('INVALID_RUN_ID')
    return '/opt/amrag/evaluation/glm40-fix-' + run_id


def valid_identity(receipt, key, contract, remote_identity):
    response = receipt.get('response')
    return (receipt.get('request_key') == key and receipt.get('request_hash') == contract['request_hash']
            and receipt.get('identity') == remote_identity and remote_identity.get('base_url') == HOST
            and remote_identity.get('model') == MODEL and remote_identity.get('binding') == BINDING
            and receipt.get('remote_receipt_path') == remote_identity.get('remote_root', '') + '/' + key + '/receipt.json'
            and isinstance(response, dict) and response.get('model') == MODEL
            and isinstance(response.get('id'), str) and bool(response['id']))


class SSHRPC:
    def __init__(self, run_id, helper=HELPER, diagnostics_dir=None):
        # Upload helper source only through SSH stdin. No local credentials/API key.
        helper_raw = Path(helper).read_bytes()
        remote_helper = remote_root(run_id) + '/worker-' + sha(helper_raw) + '.py'
        upload = "import sys,pathlib,os;p=pathlib.Path(" + repr(remote_helper) + ");b=sys.stdin.buffer.read();p.parent.mkdir(mode=0o700,parents=True,exist_ok=True);os.chmod(p.parent,0o700);assert not p.parent.is_symlink();assert not p.is_symlink();f=p.open('xb') if not p.exists() else None;f.write(b) if f else None;f.close() if f else None;assert p.read_bytes()==b;os.chmod(p,0o700)"
        base = ['ssh', '-i', str(SSH_KEY), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=15', 'root@120.55.115.162']
        done = subprocess.run(base + ['python3 -c ' + shell_quote(upload)], input=helper_raw, capture_output=True, timeout=45)
        if done.returncode != 0:
            raise RuntimeError('REMOTE_HELPER_UPLOAD_FAILED')
        self.process = subprocess.Popen(base + [shell_quote(REMOTE_PYTHON) + ' -u ' + shell_quote(remote_helper)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lines = queue.Queue()
        def reader():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)
        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()
        try:
            self.send({'run_id': run_id, 'worker_sha256': sha(helper_raw)})
            hello = self.receive(45)
        except Exception as error:
            self.close()
            raise RuntimeError('REMOTE_HELLO_PROTOCOL_FAILED:' + type(error).__name__) from error
        if hello.get('type') != 'HELLO':
            self.close()
            diagnostic = {'phase': 'HELLO', 'remote_python': REMOTE_PYTHON,
                          'worker_sha256': sha(helper_raw), 'remote_message': hello,
                          'api_calls': 0}
            diagnostic_path = None
            if diagnostics_dir is not None:
                directory = Path(diagnostics_dir)
                directory.mkdir(parents=True, exist_ok=True)
                restrict_directory(directory)
                diagnostic_path = directory / ('startup-failure-' + digest(diagnostic) + '.json')
                if not diagnostic_path.exists():
                    save(diagnostic_path, diagnostic)
            # Only controlled error metadata is user-visible. Exact JSON is private.
            metadata = {key: hello[key] for key in ('type', 'error_code', 'error_type')
                        if isinstance(hello.get(key), str) and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}', hello[key])}
            metadata['diagnostic_path'] = str(diagnostic_path) if diagnostic_path else None
            raise RuntimeError('REMOTE_READY_FAILED:' + json.dumps(metadata, sort_keys=True))
        self.hello = hello
        self.identity = hello['identity']
        expected = {'model': MODEL, 'base_url': HOST, 'binding': BINDING, 'worker_sha256': sha(helper_raw),
                    'remote_root': remote_root(run_id)}
        if self.identity != expected:
            self.close()
            raise RuntimeError('REMOTE_IDENTITY_MISMATCH')

    def send(self, value):
        self.process.stdin.write(canonical(value) + b'\n')
        self.process.stdin.flush()

    def receive(self, timeout=205):
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError('SSH_PROTOCOL_DEADLINE')
        if line is None or len(line) > 8_000_000:
            raise RuntimeError('SSH_PROTOCOL_CLOSED_OR_OVERSIZE')
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError('SSH_PROTOCOL_SHAPE')
        return value

    def prepare(self, key, body, contract):
        self.send({'action': 'PREPARE', 'request_key': key, 'request_hash': digest(body), 'body': body, 'contract': contract})
        value = self.receive(45)
        if (value.get('type') != 'READY' or value.get('request_key') != key
                or value.get('request_hash') != digest(body) or value.get('identity') != self.identity
                or value.get('contract_hash') != digest(contract)
                or value.get('receipt_path') != self.identity['remote_root'] + '/' + key + '/receipt.json'):
            raise RuntimeError('REMOTE_PREPARE_FAILED')
        return value

    def post(self, key):
        self.send({'action': 'POST', 'request_key': key})
        value = self.receive()
        if value.get('type') != 'RESULT' or not isinstance(value.get('receipt'), dict):
            raise RuntimeError('REMOTE_RESULT_PROTOCOL_FAILURE')
        return value['receipt']

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()

    def stop(self):
        self.send({'action': 'STOP'})
        value = self.receive(15)
        self.close()
        return value


def shell_quote(value):
    return "'" + value.replace("'", "'\"'\"'") + "'"


class DispatchControl:
    """Permit synchronization only; never retry a POST or reserve money."""
    def __init__(self, budget, max_call_upper=0):
        self.max_call_upper = max_call_upper
        self.budget = budget
        self.condition = threading.Condition(budget.mutex)
        self.stopped = None

    def start(self, key, body):
        with self.condition:
            while True:
                if self.stopped:
                    raise RuntimeError('DISPATCH_STOPPED:' + self.stopped)
                state = self.budget.snapshot()
                if self.budget.inflight and self.budget.limit_nano - state['recorded_cost_nano_upper'] <= 2 * self.max_call_upper:
                    self.condition.wait()
                    continue
                try:
                    self.budget.start(key, 'glm40_candidate_concurrency2', MODEL, digest(body))
                    return
                except RuntimeError as error:
                    if str(error) != 'BUDGET_SERIALIZE_REQUIRED':
                        raise
                    self.condition.wait()

    def finished(self, status):
        with self.condition:
            if status != 'OK':
                self.stopped = status
            self.condition.notify_all()


def account_receipt(case, folder, budget, rpc, receipt, persist_result=True):
    item, body, contract = case['item_id'], case['body'], case['contract']
    key = folder.name
    response = receipt.get('response') if isinstance(receipt.get('response'), dict) else {}
    raw_usage = response.get('usage')
    usage = normalize_usage(raw_usage) if receipt.get('transport_status') == 'RECEIVED' else None
    with budget.mutex:
        completed = [e['payload'] for e in budget.log.read() if e['event_type'] == 'MODEL_REQUEST_COMPLETED' and e['payload']['request_key'] == key]
        if not completed:
            budget.complete(key, usage, status=receipt.get('transport_status'), provider_request_id=receipt.get('provider_request_id') or response.get('id'))
        elif len(completed) != 1 or completed[0]['usage'] != usage:
            raise RuntimeError('RESUME_BINDING_OR_USAGE_MISMATCH')
        if usage is None:
            bound_unknown(budget, key, body, contract)
    result = {'schema': VERSION, 'item_id': item, 'request_key': key, 'request_hash': digest(body),
              'contract_hash': digest(contract), 'usage': usage, 'raw_usage': raw_usage,
              'status': 'UNKNOWN', 'answer': '', 'finish_reason': None,
              'latency_ms': receipt.get('latency_ms'), 'remote_receipt_path': receipt.get('remote_receipt_path'),
              'provider_response_id': response.get('id'), 'provider_request_id': receipt.get('provider_request_id')}
    if receipt.get('transport_status') == 'RECEIVED':
        if not valid_identity(receipt, key, contract, rpc.identity):
            result['status'] = 'FATAL_IDENTITY_MISMATCH'
        elif usage is None:
            result['status'] = 'UNKNOWN_USAGE_STOP'
        elif receipt.get('http_status') != 200:
            result['status'] = 'PROVIDER_HTTP_FAILURE'
        else:
            choices = response.get('choices')
            choice = choices[0] if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict) else {}
            message = choice.get('message') if isinstance(choice.get('message'), dict) else {}
            answer = message.get('content')
            result['answer'] = answer if isinstance(answer, str) else ''
            result['finish_reason'] = choice.get('finish_reason')
            result['status'] = 'OK' if result['answer'].strip() and result['finish_reason'] == 'stop' else 'INCOMPLETE'
    if persist_result:
        save(folder / 'result.json', result)
    return result


def execute_one(case, output, budget, rpc, control=None):
    item, body, contract = case['item_id'], case['body'], case['contract']
    if (item in EXCLUDED or body.get('model') != MODEL or contract['request_hash'] != digest(body)
            or contract.get('item_id') != item or contract.get('version') != VERSION
            or contract.get('model') != MODEL or contract.get('host') != HOST
            or contract.get('binding') != BINDING or contract.get('pricing') != PRICING
            or contract.get('canonical_utf8_bytes') != len(canonical(body))
            or contract.get('input_tokens_upper') != len(canonical(body)) + 1024
            or contract.get('output_tokens_upper') != 4096
            or contract.get('cost_nano_upper') != (len(canonical(body)) + 1024) * 800 + 4096 * 2800):
        raise ValueError('CASE_BINDING_MISMATCH')
    if len(canonical(body)) > CAP:
        return {'item_id': item, 'status': 'too_large', 'request_hash': digest(body)}
    key = 'glm40-candidate-v1-' + item + '-' + digest(body)
    folder = Path(output) / key
    if folder.exists():
        try:
            if load(folder / 'request.json') != body or load(folder / 'contract.json') != contract:
                raise ValueError('RESUME_INPUT_MISMATCH')
            events = budget.log.read()
            starts = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_STARTED' and e['payload']['request_key'] == key]
            declarations = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_GLM40_CONTRACT' and e['payload']['request_key'] == key]
            if len(starts) != 1 or starts[0]['request_hash'] != digest(body) or declarations != [dict(contract, request_key=key)]:
                raise ValueError('RESUME_LEDGER_BINDING_MISMATCH')
            receipt = load(folder / 'receipt.json')
            if not isinstance(receipt, dict) or receipt.get('request_key') != key or receipt.get('request_hash') != digest(body):
                raise ValueError('RESUME_RECEIPT_BINDING_MISMATCH')
            if receipt.get('transport_status') == 'RECEIVED' and not valid_identity(receipt, key, contract, rpc.identity):
                raise ValueError('RESUME_PROVIDER_IDENTITY_MISMATCH')
            if (folder / 'result.json').exists():
                result = load(folder / 'result.json')
                completed = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_COMPLETED' and e['payload']['request_key'] == key]
                response = receipt.get('response') if isinstance(receipt.get('response'), dict) else {}
                usage = normalize_usage(response.get('usage')) if receipt.get('transport_status') == 'RECEIVED' else None
                if (len(completed) != 1 or completed[0]['usage'] != usage or result.get('usage') != usage
                        or result.get('request_key') != key or result.get('item_id') != item
                        or result.get('contract_hash') != digest(contract) or result.get('request_hash') != digest(body)):
                    raise ValueError('RESUME_BINDING_OR_USAGE_MISMATCH')
                if result != account_receipt(case, folder, budget, rpc, receipt, persist_result=False):
                    raise ValueError('RESUME_RESULT_RECEIPT_MISMATCH')
                return dict(result, resumed=True)
            # Recover only a durable, fully bound receipt. Never resend the old key.
            return dict(account_receipt(case, folder, budget, rpc, receipt), resumed=True)
        except (OSError, ValueError, TypeError, KeyError) as error:
            raise RuntimeError('EXISTING_ATTEMPT_EXPLICIT_RECOVERY_REQUIRED') from error
    with budget.mutex:
        state = budget.snapshot()
        if state['unbounded_usage_requests'] or set(state['pending_request_keys']) - budget.inflight:
            raise RuntimeError('BILLING_OR_REQUEST_STATUS_UNKNOWN')
        if state['recorded_cost_nano_upper'] >= budget.limit_nano:
            raise RuntimeError('BUDGET_EXHAUSTED')
    folder.mkdir(parents=True, exist_ok=False)
    restrict_directory(folder)
    save(folder / 'request.json', body)
    save(folder / 'contract.json', contract)
    ready = rpc.prepare(key, body, contract)
    save(folder / 'remote-ready.json', ready)
    budget.log.append('glm40-contract:' + key, 'MODEL_REQUEST_GLM40_CONTRACT', dict(contract, request_key=key),
                      evidence_hashes=[digest(body), digest(contract)])
    (control or DispatchControl(budget)).start(key, body)
    try:
        receipt = rpc.post(key)
        if not isinstance(receipt, dict):
            raise ValueError('INVALID_RECEIPT')
    except Exception as error:
        receipt = {'transport_status': 'UNKNOWN', 'request_key': key, 'request_hash': digest(body),
                   'error_type': type(error).__name__, 'protocol_failure': True}
    save(folder / 'receipt.json', receipt)
    with budget.mutex:
        result = account_receipt(case, folder, budget, rpc, receipt)
        if control:
            control.finished(result['status'])
        return result


def execute(cases, output, budget, rpcs):
    if len(rpcs) != 2:
        raise ValueError('TWO_CHANNELS_REQUIRED')
    control = DispatchControl(budget, max((c['contract']['cost_nano_upper'] for c in cases), default=0))
    results = {}
    def call(index, rpc):
        try:
            result = execute_one(cases[index], output, budget, rpc, control)
            control.finished(result['status'])
            return result
        except Exception as error:
            control.finished(str(error))
            return {'item_id': cases[index]['item_id'], 'status': 'CONTROLLER_STOP', 'error_type': type(error).__name__, 'error_code': str(error) if isinstance(error, RuntimeError) else 'VALIDATION_FAILED'}
    if not cases:
        return [], None
    # A durable receipt may repair an interrupted accounting step, never a POST.
    state = budget.snapshot()
    unresolved = set(state['pending_request_keys']) | set(state['unbounded_usage_requests'])
    if unresolved:
        for index, c in enumerate(cases):
            key = 'glm40-candidate-v1-' + c['item_id'] + '-' + digest(c['body'])
            if key in unresolved:
                result = call(index, rpcs[0])
                if result['status'] != 'OK':
                    return [result], control.stopped
                unresolved.remove(key)
        if unresolved:
            return [], 'BILLING_OR_REQUEST_STATUS_UNKNOWN'
    # Provider identity and answer envelope are proven with one call before parallel dispatch.
    results[0] = call(0, rpcs[0])
    if control.stopped:
        return [results[0]], control.stopped
    with ThreadPoolExecutor(max_workers=2) as pool:
        next_index, active = 1, {}
        free = list(rpcs)
        while active or (next_index < len(cases) and not control.stopped):
            while free and next_index < len(cases) and not control.stopped:
                rpc = free.pop()
                future = pool.submit(call, next_index, rpc)
                active[future] = (next_index, rpc)
                next_index += 1
            if not active:
                break
            done, _ = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                index, rpc = active.pop(future)
                results[index] = future.result()
                free.append(rpc)
    return [results[i] for i in sorted(results)], control.stopped


def account_baseline(budget, source):
    """Optional historical accounting only. Never opens ANY question request file."""
    source = Path(source)
    manifest_path, results_path = source / 'requests-manifest.json', source / 'results.json'
    manifest = {x['item_id']: x['request_sha256'] for x in load(manifest_path)}
    # Metadata projection only: historical answers are never evaluated or persisted.
    rows = [{k: x.get(k) for k in ('item_id', 'usage', 'started_at', 'provider_model', 'provider_request_id')}
            for x in load(results_path)]
    if len(rows) != 44 or len(manifest) != 44 or {x['item_id'] for x in rows} != set(manifest):
        raise ValueError('BASELINE44_METADATA_MISMATCH')
    plans = []
    for row in rows:
        usage = normalize_usage(row['usage'])
        if usage is None or row['provider_model'] != MODEL:
            raise ValueError('BASELINE_KNOWN_USAGE_OR_IDENTITY_MISSING')
        plans.append((row, usage))
    source_binding = {'manifest_sha256': sha(manifest_path.read_bytes()), 'results_sha256': sha(results_path.read_bytes()),
                      'source': str(source.resolve()), 'historical_count': 44, 'pricing': PRICING}
    for row, usage in plans:
        key = 'glm44-historical-v1-' + row['item_id'] + '-' + manifest[row['item_id']]
        existing = [e['payload'] for e in budget.log.read() if e['event_type'] == 'MODEL_REQUEST_COMPLETED' and e['payload']['request_key'] == key]
        if existing:
            if len(existing) != 1 or existing[0]['usage'] != usage:
                raise RuntimeError('HISTORICAL_USAGE_CONFLICT')
            continue
        # Historical completed calls are accounted even after the ceiling: they
        # already happened, so this is an additive fact, never a dispatch permit.
        budget.log.append('historical-source:' + key, 'MODEL_HISTORICAL_SOURCE_BINDING',
                          dict(source_binding, request_key=key, original_started_at=row['started_at'],
                               request_hash=manifest[row['item_id']], item_id=row['item_id']))
        budget.log.append('request-start:' + key, 'MODEL_REQUEST_STARTED', {
            'request_key': key, 'role': 'glm44_historical_accounting_only', 'model': MODEL,
            'request_hash': manifest[row['item_id']], 'price_version': PRICING['version'],
            'money_reserved': False, 'historical_only': True, 'original_started_at': row['started_at']})
        budget.complete(key, usage, status='HISTORICAL_COMPLETED', provider_request_id=row['provider_request_id'])
    return budget.snapshot()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', type=Path, default=TASK / 'scope40.json')
    parser.add_argument('--candidate', type=Path, default=TASK / 'context-candidate')
    parser.add_argument('--output', type=Path, default=TASK / 'glm40-execution')
    parser.add_argument('--run-id')
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument('--prepare', action='store_true')
    actions.add_argument('--run', action='store_true')
    actions.add_argument('--account-baseline', type=Path)
    args = parser.parse_args()
    if not LEDGER.is_file():
        raise RuntimeError('AUTHORITATIVE_LEDGER_REQUIRED')
    if args.account_baseline:
        raise RuntimeError('HISTORICAL_ACCOUNTING_DISABLED_REQUIRES_EXPLICIT_REVIEW')
        with WorkspaceLock(LEDGER.with_suffix('.glm40-controller.lock'), 'glm44-historical-accounting'):
            budget = Budget(LEDGER)
            gate(budget)
            install_prices(budget)
            state = account_baseline(budget, args.account_baseline)
            print(json.dumps({'historical_accounting': 44, 'budget': state, 'api_calls': 0}))
        return
    cases, bindings = prepare(args.scope, args.candidate)
    if not args.run:
        print(json.dumps({'schema': VERSION, 'api_calls': 0, 'ledger_writes': 0, 'concurrency': 2, 'first_probe_serial': True,
                          'baseline_concurrency': 2, 'bindings': bindings,
                          'cases': [{'item_id': c['item_id'], 'status': c['status'],
                                     'bytes': c['contract']['canonical_utf8_bytes'],
                                     'request_hash': c['contract']['request_hash'],
                                     'cost_nano_upper': c['contract']['cost_nano_upper']} for c in cases]}))
        return
    if not args.run_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', args.run_id):
        parser.error('--run requires a safe explicit --run-id')
    if any(c['status'] != 'READY' for c in cases):
        raise RuntimeError('PREPARE_TOO_LARGE_NO_CALLS')
    output = args.output / args.run_id
    output.mkdir(parents=True, exist_ok=True)
    restrict_directory(output)
    with WorkspaceLock(LEDGER.with_suffix('.glm40-controller.lock'), 'glm40-serial-controller'):
        budget = Budget(LEDGER)
        before = budget.snapshot()
        install_prices(budget)
        rpcs = []
        after_remote = []
        try:
            rpcs = [SSHRPC(args.run_id, diagnostics_dir=output)]
            rpcs.append(SSHRPC(args.run_id, diagnostics_dir=output))
            results, stopped = execute(cases, output, budget, rpcs)
            for rpc in rpcs:
                try:
                    after_remote.append(rpc.stop())
                except Exception as error:
                    after_remote.append({'type': 'STOP_UNKNOWN', 'error_type': type(error).__name__})
        finally:
            for rpc in rpcs:
                rpc.close()
        summary = {'schema': VERSION, 'run_id': args.run_id, 'bindings': bindings,
                   'concurrency': 2, 'baseline_concurrency': 2, 'first_probe_serial': True,
                   'total': 40, 'results': results, 'stop_reason': stopped,
                   'not_dispatched_ids': [c['item_id'] for c in cases if c['item_id'] not in {r['item_id'] for r in results}],
                   'budget_before': before, 'budget_after': budget.snapshot(),
                   'remote_before': [rpc.hello for rpc in rpcs], 'remote_after': after_remote}
        output.mkdir(parents=True, exist_ok=True)
        path = output / ('summary-' + digest(summary) + '.json')
        if not path.exists():
            save(path, summary)
        print(json.dumps({'summary_path': str(path), 'attempted': sum('request_key' in result for result in results), 'stop_reason': stopped,
                          'statuses': {s: sum(r['status'] == s for r in results) for s in {r['status'] for r in results}}}))
        if stopped:
            raise SystemExit(2)


if __name__ == '__main__':
    main()

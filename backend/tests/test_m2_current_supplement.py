"""Offline adapter checks. Responses are synthetic; no provider calls permitted."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import socket

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
module = importlib.util.spec_from_file_location('current_supplement_test', ROOT / 'scripts/m2_current_supplement.py')
m = importlib.util.module_from_spec(module)
module.loader.exec_module(m)


@pytest.fixture(autouse=True)
def no_external_sockets(monkeypatch):
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    def deny(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original_connect(sock, address, *args, **kwargs)
        raise AssertionError('External network forbidden in adapter tests')
    def deny_ex(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original_connect_ex(sock, address, *args, **kwargs)
        raise AssertionError('External network forbidden in adapter tests')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny_ex)


@pytest.fixture(scope='module')
def contract():
    return m.make_spec()


def test_real_frozen_accounting_procedural_source_and_disjoint_plan(contract):
    _, _, fresh, audit, policy = m.validate(contract)
    assert audit['committed_cny'] == '1.7161116'
    assert audit['remaining_cny'] == '0.2838884'
    assert audit['unknown_171_full_reservation_cny'] == '0.075968'
    assert fresh['cases'][0]['expected_chunk_ids'] == ['word-paragraph-6']
    assert not policy['A']['policy_active'] and policy['D']['policy_active']
    plan = list(m.safety.supplement_plan(fresh))
    identities = {(c['qid'], mode, arm, rep) for c, mode, rep, arm, _ in plan}
    assert len(identities) == 8
    assert {i[0] for i in identities} == {m.QID}
    assert identities.isdisjoint(tuple(row['identity']) for row in audit['identities'])


@pytest.mark.parametrize('field,value', [
    ('reconciliation_sha256', '0' * 64), ('lease_sha256', '0' * 64),
    ('prior_grants', {}), ('runner_sha256', '0' * 64),
    ('global_limit_cny', '3'), ('repetitions', 1), ('fixed_search', {}),
    ('authorization_id', 'invented'),
])
def test_changed_history_grants_lease_code_or_scope_fail_closed(contract, field, value):
    bad = deepcopy(contract); bad[field] = value
    with pytest.raises(ValueError):
        m.validate(bad)


def test_unsupported_gold_or_replayed_case_rejected(contract):
    bad = deepcopy(contract); bad['case']['qid'] = 'old-id'
    with pytest.raises(ValueError, match='source/Gold'):
        m.validate(bad)


def test_atomic_one_shot_excludes_competitor_without_changing_old_lease(tmp_path, monkeypatch, contract):
    grant = tmp_path / 'one-shot.started'; lease = tmp_path / 'old.active'; lease.write_bytes(b'preserve')
    monkeypatch.setattr(m, 'GRANT', grant)
    first = m.claim(contract, tmp_path / 'first')
    with pytest.raises(FileExistsError):
        m.claim(contract, tmp_path / 'second')
    assert grant.read_bytes() == first and lease.read_bytes() == b'preserve'


def guard_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'validate', lambda _: None)
    monkeypatch.setattr(m, 'no_writers', lambda: {})
    grant = tmp_path / 'one-shot.started'; grant.write_bytes(b'owned')
    monkeypatch.setattr(m, 'GRANT', grant)
    manifest = {'model': 'zai-org/GLM-5.3-Flash', 'budget_cny': str(m.REMAINING),
                'base_url': 'https://api.siliconflow.cn/v1', 'max_output_tokens': 4096,
                'pricing': {'input_cny_per_million': '.8', 'output_cny_per_million': '2.8'},
                'cases': [{'qid': m.QID}]}
    guard = m.Transport(manifest, tmp_path, {}, b'owned')
    guard.context = {'qid': m.QID, 'mode': 'chat', 'arm': 'D', 'repetition': 1}
    request = httpx.Request('POST', m.base.endpoint(manifest['base_url']),
                           json={'model': manifest['model'], 'messages': []})
    return guard, request, grant


@pytest.mark.parametrize('reason', ['history', 'lease', 'grant', 'writer', 'identity', 'recorded'])
def test_per_send_checks_prevent_dispatch(tmp_path, monkeypatch, reason):
    guard, request, grant = guard_fixture(tmp_path, monkeypatch)
    def changed(*_):
        raise ValueError('changed frozen evidence')
    if reason in ('history', 'lease'): monkeypatch.setattr(m, 'validate', changed)
    elif reason == 'grant': grant.write_bytes(b'other')
    elif reason == 'writer': monkeypatch.setattr(m, 'no_writers', changed)
    elif reason == 'identity': guard.context['qid'] = 'old'
    else: (tmp_path / 'trials.jsonl').write_bytes(m.base.canonical({**guard.context, 'state': 'FAILED'}) + b'\n')
    async def forbidden(*args, **kwargs):
        pytest.fail('No dispatch allowed')
    with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(forbidden, None, request))
    assert not guard.calls


@pytest.mark.parametrize('outcome', ['completed', 'timeout', '429'])
def test_cost_accounting_unknown_full_reservation_and_no_retry(tmp_path, monkeypatch, outcome):
    guard, request, _ = guard_fixture(tmp_path, monkeypatch); attempts = []
    async def sender(_, req, **kwargs):
        attempts.append(1)
        assert guard.calls[-1]['state'] == 'UNKNOWN_PENDING'
        if outcome == 'timeout': raise httpx.ReadTimeout('synthetic')
        if outcome == '429': return httpx.Response(429, request=req)
        return httpx.Response(200, request=req, json={'model': guard.manifest['model'],
            'id': 'synthetic', 'usage': {'prompt_tokens': 12, 'completion_tokens': 7}})
    if outcome == 'completed':
        asyncio.run(guard.send(sender, None, request))
        assert guard.committed == guard.cost(12, 7)
    else:
        with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, request))
        assert guard.calls[0]['state'] == 'UNKNOWN'
        assert guard.committed == m.Decimal(guard.calls[0]['reservation_cny'])
        with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, request))
    assert len(attempts) == 1
    assert m.Decimal('1.7161116') + guard.committed <= 2


def test_budget_stop_archives_actual_rejected_payload_and_sends_zero(tmp_path, monkeypatch):
    guard, _, _ = guard_fixture(tmp_path, monkeypatch)
    request = httpx.Request('POST', m.base.endpoint(guard.manifest['base_url']),
        json={'model': guard.manifest['model'], 'messages': [{'role': 'user', 'content': 'x' * 100000}]})
    async def forbidden(*args, **kwargs): pytest.fail('Over-budget request must not send')
    with pytest.raises(m.base.GuardStop, match='BUDGET_BLOCKED'):
        asyncio.run(guard.send(forbidden, None, request))
    rows = [json.loads(row) for row in (tmp_path / 'guard-diagnostics.jsonl').read_bytes().splitlines()]
    diagnostic = rows[0]; body = (tmp_path / diagnostic['request_path']).read_bytes()
    assert diagnostic['request_sha256'] == m.base.digest(body)
    assert m.Decimal(diagnostic['reservation_cny']) > m.REMAINING
    assert guard.committed == 0 and not guard.calls


def test_wrapper_unknown_retains_one_shot_and_frozen_lease(tmp_path, monkeypatch, contract):
    _, parent, fresh, audit, policy = m.validate(contract)
    monkeypatch.setattr(m, 'P', tmp_path)
    monkeypatch.setattr(m, 'GRANT', tmp_path / 'new.started')
    monkeypatch.setattr(m.base, 'private_path', lambda x: Path(x).resolve())
    monkeypatch.setattr(m, 'preflight', lambda _: {'status': 'PASS', 'grant_available': True, 'entry': 'synthetic'})
    monkeypatch.setattr(m, 'validate', lambda _: ({}, parent, fresh, audit, policy))
    monkeypatch.setattr(m, 'no_writers', lambda: {})
    monkeypatch.setenv('M2_AB_API_KEY', 'synthetic-only')
    monkeypatch.setattr(m.cont, 'backup_reuse', lambda *args: None)
    monkeypatch.setattr(m.base, 'configure_runtime', lambda *args: None)
    monkeypatch.setattr(m.safety, 'assert_runtime_isolation', lambda *args: None)
    @contextmanager
    def redis(_): yield 'redis://127.0.0.1:1'
    monkeypatch.setattr(m.base, 'isolated_redis', redis)
    attempts = []
    async def run_trials(manifest, output, guard):
        guard.context = {'qid': m.QID, 'mode': 'agent', 'arm': 'D', 'repetition': 1}
        async def sender(*args, **kwargs):
            attempts.append(1); raise httpx.ReadTimeout('synthetic')
        req = httpx.Request('POST', m.base.endpoint(fresh['base_url']), json={'model': fresh['model'], 'messages': []})
        await guard.send(sender, None, req)
    monkeypatch.setattr(m.base, 'run_trials', run_trials)
    lease = tmp_path / 'frozen.active'; lease.write_bytes(b'unchanged')
    result = m.execute(contract, tmp_path / 'm2-current-supplement-real-test')
    assert result['status'] == 'STOPPED' and result['unknown_calls'] == 1
    assert len(attempts) == 1 and m.GRANT.exists() and lease.read_bytes() == b'unchanged'
    assert m.Decimal(result['global_committed_including_unknown_cny']) <= 2
    with pytest.raises(FileExistsError): m.execute(contract, tmp_path / 'm2-current-supplement-real-other')


def test_frozen_reconciliation_unchanged_after_adapter_tests():
    result = m.recon.reconcile()
    assert result['status'] == 'PASS' and result['identity_count'] == 96
    assert result['committed_cny'] == '1.7161116'

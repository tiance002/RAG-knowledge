"""Zero-provider-call checks for the distinct English supplemental adapter."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import socket

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
s = importlib.util.spec_from_file_location('english_adapter_test', ROOT / 'scripts/m2_current_supplement_english.py')
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)


@pytest.fixture(autouse=True)
def deny_external_sockets(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original(sock, address, *args, **kwargs)
        raise AssertionError('External sockets forbidden in English adapter tests')
    monkeypatch.setattr(socket.socket, 'connect', connect)


@pytest.fixture(scope='module')
def contract():
    return m.make_spec()


def test_actual_read_only_retrieval_prior_accounting_and_fresh_plan(contract):
    _, _, fresh, audit, policy = m.validate(contract)
    assert audit['committed_cny'] == '1.7227956'
    assert audit['remaining_cny'] == '0.2772044'
    assert audit['unknown_171_full_reservation_cny'] == '0.075968'
    retrieval = contract['retrieval_preflight']
    assert retrieval['process_exit_code'] == retrieval['model_calls'] == 0
    assert retrieval['gold_rank'] == 1 and retrieval['full_gold_chunk_matches_frozen_quote']
    assert retrieval['source_sha256_before'] == retrieval['source_sha256_after']
    assert not policy['A']['policy_active'] and policy['D']['policy_active']
    plan = list(m.prior.safety.supplement_plan(fresh))
    assert len(plan) == 8
    assert {x[0]['qid'] for x in plan} == {m.QID}
    assert m.QID != m.prior.QID
    assert fresh['cases'][0]['question'] != m.prior.case_from(fresh)['question']


def test_private_runtime_hooks_preserve_frozen_prior_validator(contract):
    assert m.prior.REMAINING == m.Decimal('0.2838884')
    assert m.runner.REMAINING == m.Decimal('0.2772044')
    assert m.runner.Transport is m.Transport
    assert m.runner.validate is m.validate
    assert m.prior.validate is not m.validate
    assert m.prior.recon.reconcile()['committed_cny'] == '1.7161116'


@pytest.mark.parametrize('field,value', [
    ('prior_report_sha256', '0' * 64), ('runner_sha256', '0' * 64),
    ('global_limit_cny', '3'), ('authorization_id', 'old'), ('fixed_search', {}),
    ('case', {}), ('retrieval_preflight', {}),
])
def test_changed_authorization_history_source_or_retrieval_rejected(contract, field, value):
    bad = deepcopy(contract); bad[field] = value
    with pytest.raises(ValueError): m.validate(bad)


def fixture_guard(tmp_path, monkeypatch):
    monkeypatch.setattr(m, 'validate', lambda _: None)
    monkeypatch.setattr(m, 'no_writers', lambda: {})
    grant = tmp_path / 'english.started'; grant.write_bytes(b'owned')
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


@pytest.mark.parametrize('reason', ['history', 'grant', 'old_identity', 'writer'])
def test_stage_diagnostic_captures_failed_check_and_request(tmp_path, monkeypatch, reason):
    guard, request, grant = fixture_guard(tmp_path, monkeypatch)
    def changed(*args): raise ValueError('controlled diagnostic failure')
    if reason == 'history': monkeypatch.setattr(m, 'validate', changed)
    elif reason == 'grant': grant.write_bytes(b'changed')
    elif reason == 'old_identity': guard.context['qid'] = m.prior.QID
    else: monkeypatch.setattr(m, 'no_writers', changed)
    async def forbidden(*args, **kwargs): pytest.fail('Failed check must prevent dispatch')
    with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(forbidden, None, request))
    row = m.cont.rows((tmp_path / 'guard-diagnostics.jsonl').read_bytes())[0]
    assert row['code'].startswith('PRE_SEND_') and 'ValueError' in row['code']
    assert (tmp_path / row['request_path']).exists()
    assert m.base.digest((tmp_path / row['request_path']).read_bytes()) == row['request_sha256']
    assert not guard.calls


@pytest.mark.parametrize('outcome', ['completed', 'unknown', 'over_budget'])
def test_remaining_budget_known_usage_unknown_full_reserve_and_no_retry(tmp_path, monkeypatch, outcome):
    guard, request, _ = fixture_guard(tmp_path, monkeypatch); calls = []
    async def sender(_, req, **kwargs):
        calls.append(True)
        if outcome == 'unknown': raise httpx.ReadTimeout('synthetic')
        return httpx.Response(200, request=req, json={'id': 'synthetic-english', 'model': guard.manifest['model'],
                            'usage': {'prompt_tokens': 12, 'completion_tokens': 7}})
    if outcome == 'over_budget':
        request = httpx.Request('POST', request.url, json={'model': guard.manifest['model'],
            'messages': [{'role': 'user', 'content': 'x' * 100000}]})
    if outcome == 'completed':
        asyncio.run(guard.send(sender, None, request))
        assert guard.committed == guard.cost(12, 7)
    else:
        with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, request))
        with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, request))
        if outcome == 'unknown':
            assert len(calls) == 1 and guard.calls[0]['state'] == 'UNKNOWN'
            assert guard.committed == m.Decimal(guard.calls[0]['reservation_cny'])
        else: assert not calls and guard.committed == 0
    assert m.Decimal('1.7227956') + guard.committed <= 2


def test_english_one_shot_atomic_grant_uses_new_scope(tmp_path, monkeypatch, contract):
    grant = tmp_path / 'english.started'; monkeypatch.setattr(m.runner, 'GRANT', grant)
    first = m.runner.claim(contract, tmp_path / 'first')
    with pytest.raises(FileExistsError): m.runner.claim(contract, tmp_path / 'second')
    assert grant.read_bytes() == first

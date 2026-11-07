"""Only never-started Agent identities; all provider responses synthetic."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import socket
from types import SimpleNamespace

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
s = importlib.util.spec_from_file_location('english_agents_test', ROOT / 'scripts/m2_current_supplement_english_agents.py')
m = importlib.util.module_from_spec(s); s.loader.exec_module(m)


@pytest.fixture(autouse=True)
def deny_external_sockets(monkeypatch):
    original = socket.socket.connect
    def connect(sock, address, *args, **kwargs):
        if isinstance(address, tuple) and address[0] in ('127.0.0.1', '::1'):
            return original(sock, address, *args, **kwargs)
        raise AssertionError('External sockets forbidden')
    monkeypatch.setattr(socket.socket, 'connect', connect)


@pytest.fixture(scope='module')
def contract(): return m.make_spec()


def test_real_history_exact_four_agent_identities_and_unchanged_case(contract):
    _, _, fresh, audit, policy, allowed = m.validate(contract)
    assert audit['committed_cny'] == '1.7368600'
    assert audit['remaining_cny'] == '0.2631400'
    assert audit['unknown_171_full_reservation_cny'] == '0.075968'
    assert allowed == {(m.source.QID, 'agent', arm, rep) for arm in ('A', 'D') for rep in (1, 2)}
    plan = list(m.agent_plan(fresh))
    assert len(plan) == 4
    assert {(c['qid'], mode, arm, rep) for c, mode, rep, arm, prefs in plan} == allowed
    assert fresh['cases'][0]['question'] == m.source.QUESTION
    assert not policy['A']['policy_active'] and policy['D']['policy_active']
    assert (m.source.QID, 'chat', 'D', 2) not in allowed


@pytest.mark.parametrize('field,value', [
    ('prior_report_sha256', '0' * 64), ('runner_sha256', '0' * 64),
    ('global_limit_cny', '3'), ('allowed_identities', []), ('fixed_search', {}),
])
def test_changed_history_configuration_or_plan_rejected(contract, field, value):
    spec = deepcopy(contract); spec[field] = value
    with pytest.raises(ValueError): m.validate(spec)


@pytest.mark.parametrize('code,status', [(0, 'NO_MATCH_AT_CHECK_TIME'), (3, 'INACCESSIBLE_COMMAND_LINES'),
                                      (4, 'MATCHED_COMMAND_LINE_NOT_PROOF_OF_WRITER')])
def test_initial_process_scan_records_specific_code_as_advisory(monkeypatch, code, status):
    monkeypatch.setattr(m.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=code))
    result = m.initial_process_check()
    assert result['exit_code'] == code and result['status'] == status
    assert result['role'].startswith('INITIAL_ADVISORY_ONLY')


def guard_fixture(tmp_path, monkeypatch, contract):
    monkeypatch.setattr(m, 'validate', lambda _: None)
    def forbidden_scan(*args, **kwargs): pytest.fail('No per-send process-name heuristic')
    monkeypatch.setattr(m, 'initial_process_check', forbidden_scan)
    monkeypatch.setattr(m.source, 'no_writers', forbidden_scan)
    grant = tmp_path / 'agents.started'; grant.write_bytes(b'owned'); monkeypatch.setattr(m, 'GRANT', grant)
    manifest = {'model': 'zai-org/GLM-5.3-Flash', 'budget_cny': str(m.REMAINING),
        'base_url': 'https://api.siliconflow.cn/v1', 'max_output_tokens': 4096,
        'pricing': {'input_cny_per_million': '.8', 'output_cny_per_million': '2.8'},
        'cases': [{'qid': m.source.QID}]}
    guard = m.Transport(manifest, tmp_path, contract, b'owned')
    guard.context = {'qid': m.source.QID, 'mode': 'agent', 'arm': 'D', 'repetition': 1}
    request = httpx.Request('POST', m.base.endpoint(manifest['base_url']), json={'model': manifest['model'], 'messages': []})
    return guard, request, grant


@pytest.mark.parametrize('outcome', ['completed', 'unknown', 'budget'])
def test_dispatch_safety_without_process_scan(tmp_path, monkeypatch, contract, outcome):
    guard, request, _ = guard_fixture(tmp_path, monkeypatch, contract); calls = []
    async def sender(_, req, **kwargs):
        calls.append(True)
        if outcome == 'unknown': raise httpx.ReadTimeout('synthetic')
        return httpx.Response(200, request=req, json={'id': 'synthetic-agent', 'model': guard.manifest['model'],
                             'usage': {'prompt_tokens': 12, 'completion_tokens': 7}})
    if outcome == 'budget':
        request = httpx.Request('POST', request.url, json={'model': guard.manifest['model'],
            'messages': [{'role': 'user', 'content': 'x' * 100000}]})
    if outcome == 'completed':
        asyncio.run(guard.send(sender, None, request)); assert guard.committed == guard.cost(12, 7)
    else:
        with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, request))
        with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, request))
        if outcome == 'unknown':
            assert len(calls) == 1 and guard.calls[0]['state'] == 'UNKNOWN'
            assert guard.committed == m.Decimal(guard.calls[0]['reservation_cny'])
        else: assert calls == [] and guard.committed == 0
    assert m.Decimal('1.7368600') + guard.committed <= 2


@pytest.mark.parametrize('failure', ['guard_stopped_chat_D2', 'completed_chat', 'recorded_agent', 'grant', 'history'])
def test_started_identity_grant_or_history_cannot_replay(tmp_path, monkeypatch, contract, failure):
    guard, request, grant = guard_fixture(tmp_path, monkeypatch, contract)
    if failure == 'guard_stopped_chat_D2': guard.context.update(mode='chat', arm='D', repetition=2)
    elif failure == 'completed_chat': guard.context.update(mode='chat', arm='A', repetition=1)
    elif failure == 'recorded_agent':
        (tmp_path / 'trials.jsonl').write_bytes(m.base.canonical({**guard.context, 'state': 'GUARD_STOP'}) + b'\n')
    elif failure == 'grant': grant.write_bytes(b'changed')
    else:
        def changed(*args): raise ValueError('history changed')
        monkeypatch.setattr(m, 'validate', changed)
    async def forbidden(*args, **kwargs): pytest.fail('No replay/changed history send')
    with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(forbidden, None, request))
    assert not guard.calls


def test_new_atomic_grant_blocks_competitor_and_keeps_existing_lease(tmp_path, monkeypatch, contract):
    grant = tmp_path / 'agents.started'; monkeypatch.setattr(m, 'GRANT', grant)
    lease = tmp_path / 'frozen.active'; lease.write_bytes(b'preserve')
    value = m.claim(contract, tmp_path / 'first')
    with pytest.raises(FileExistsError): m.claim(contract, tmp_path / 'second')
    assert grant.read_bytes() == value and lease.read_bytes() == b'preserve'

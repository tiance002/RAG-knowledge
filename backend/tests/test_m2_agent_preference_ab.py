"""Offline safety checks for the fresh A/B harness."""
import hashlib
import importlib.util
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import httpx
import pytest


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('network forbidden in offline tests')
    monkeypatch.setattr(socket.socket, 'connect', blocked)
    monkeypatch.setattr(socket.socket, 'connect_ex', blocked)


@pytest.fixture
def runner():
    path = Path(__file__).resolve().parents[2] / 'scripts/m2_agent_preference_ab.py'
    spec = importlib.util.spec_from_file_location('fresh_m2_ab_test', path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_unknown_ledger_and_missing_body_fail_closed(runner, tmp_path):
    tmp_path = tmp_path / 'calls'
    tmp_path.mkdir()
    request = b'{"test":1}'
    (tmp_path / 'one.request.json').write_bytes(request)
    (tmp_path / 'one.started.json').write_text(json.dumps({'request_sha256': hashlib.sha256(request).hexdigest()}))
    assert runner.unknown_hashes(tmp_path) == [runner.request_fingerprint(request)]
    (tmp_path / 'one.finished.json').write_text(json.dumps({'state': 'COMPLETED', 'status_code': 200}))
    assert runner.unknown_hashes(tmp_path) == []
    (tmp_path / 'one.finished.json').write_text(json.dumps({'state': 'FAILED_HTTP', 'status_code': 503}))
    assert runner.unknown_hashes(tmp_path) == [runner.request_fingerprint(request)]
    (tmp_path / 'one.request.json').unlink()
    with pytest.raises(ValueError):
        runner.unknown_hashes(tmp_path)


def test_guard_rejects_unknown_and_other_run_before_send(runner, tmp_path, monkeypatch):
    request = httpx.Request('POST', 'https://api.siliconflow.cn/v1/chat/completions', json={'model': 'Qwen/Qwen3.5-4B'})
    digest = runner.request_fingerprint(request.content)
    plan = {'run_id': 'm2-agent-preference-ab-fresh', 'unknown_request_hashes': [digest], 'sources': {}, 'runner_sha256': 'hash', 'manifest_sha256': 'hash', 'supplement_sha256': 'hash'}
    monkeypatch.setattr(runner.old, 'snapshot', lambda: {})
    monkeypatch.setattr(runner.previous, 'sha', lambda _: 'hash')
    guard = runner.Guard(tmp_path / 'calls', plan)
    with pytest.raises(runner.old.guardmod.GuardStop):
        guard.begin(plan['run_id'] + ':trial:call-1', request)
    assert guard.count == 0
    guard = runner.Guard(tmp_path / 'calls2', {**plan, 'unknown_request_hashes': []})
    with pytest.raises(runner.old.guardmod.GuardStop):
        guard.begin('old-run:call-1', request)
    assert guard.count == 0


def test_plan_has_paired_fresh_identities_and_active_preference(runner, monkeypatch):
    monkeypatch.setattr(runner.previous, 'prior_check', lambda: ({'models': {}}, {}))
    monkeypatch.setattr(runner.old, 'snapshot', lambda: {})
    monkeypatch.setattr(runner, 'unknown_hashes', lambda _: [])
    plan = runner.make_plan('m2-agent-preference-ab-fresh')
    assert len(plan['identities']) == 12
    assert len({x['run_identity'] for x in plan['identities']}) == 12
    assert plan['preference'] == {'answer_style': 'detailed'}
    assert plan['model_calls'] == 0
    assert 'independent retrieval' in plan['context_contract']


def test_detailed_policy_applies_to_all_frozen_questions(runner, monkeypatch):
    monkeypatch.syspath_prepend(str(runner.ROOT / 'backend'))
    from app.services.memory_commands import resolve_display_preferences
    cases = runner.frozen_cases()
    for qid in runner.QIDS:
        values = {'answer_style': 'detailed', 'knowledge_level': 'intermediate', 'output_preferences': []}
        snapshot = SimpleNamespace(values=SimpleNamespace(model_dump=lambda: values.copy()),
                                   sources={'answer_style': 'user', 'knowledge_level': 'default'})
        policy, effective, _ = resolve_display_preferences(snapshot, cases[qid]['question'])
        assert policy and effective['answer_style'] == 'detailed'
        assert '详细解释' in policy


def test_continuation_excludes_both_previously_started_arms(runner, monkeypatch):
    monkeypatch.setattr(runner.previous, 'prior_check', lambda: ({'models': {}}, {}))
    monkeypatch.setattr(runner.old, 'snapshot', lambda: {})
    monkeypatch.setattr(runner, 'unknown_hashes', lambda _: [])
    monkeypatch.setattr(runner, 'continuation_proof', lambda _: {
        'directory': 'previous', 'excluded': [
            {'qid': runner.QIDS[0], 'mode': 'chat', 'arm': 'A'},
            {'qid': runner.QIDS[0], 'mode': 'chat', 'arm': 'B'}]})
    plan = runner.make_plan('m2-agent-preference-ab-new', 'previous')
    assert len(plan['identities']) == 10
    assert not any(x['qid'] == runner.QIDS[0] and x['mode'] == 'chat' for x in plan['identities'])
    assert all(x['run_identity'].startswith(plan['run_id'] + ':') for x in plan['identities'])


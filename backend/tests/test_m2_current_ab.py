"""Offline-only checks; all provider responses are synthetic."""
import asyncio
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
s = importlib.util.spec_from_file_location('m2_current_test', ROOT / 'scripts/m2_current_ab.py')
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)
SPEC = ROOT / 'var/preferences-acceptance/m2-current-manifest-v3.json'


def contract():
    return json.loads(SPEC.read_bytes())


def test_reconciled_frozen_preflight_and_identities():
    spec = contract()
    _, parent, fresh, budget, plan = m.validate(spec)
    assert m.preflight(spec)['planned_trials'] == 96
    assert budget['prior_combined_committed_cny'] == '0.8010920'
    assert fresh['budget_cny'] == '1.1989080'
    assert len({(c['qid'], mode, arm, rep) for c,mode,rep,arm,_ in plan}) == 96
    assert all(row[2] == 1 for row in plan[:48]) and all(row[2] == 2 for row in plan[48:])
    assert {c['question'] for c in fresh['cases']} == {c['question'] for c in parent['cases']}
    assert set(c['qid'] for c in fresh['cases']).isdisjoint(c['qid'] for c in parent['cases'])
    assert spec['fixed_search'] == m.base.FIXED_SEARCH
    dataset = m.dataset_snapshot(fresh)
    assert m.base.digest(m.base.canonical(dataset)) == spec['dataset_sha256']
    assert [{k: v for k, v in c.items() if k != 'qid'} for c in dataset['cases']] == [{k: v for k, v in c.items() if k != 'qid'} for c in parent['cases']]


def test_changed_snapshot_history_or_search_fail_closed():
    for key, value in [('history_sha256', '0'*64), ('code_snapshot', {}), ('fixed_search', {})]:
        bad = contract(); bad[key] = value
        with pytest.raises(ValueError): m.validate(bad)


def guard_fixture(tmp_path, monkeypatch):
    spec = contract(); history, parent, fresh, budget, plan = m.validate(spec)
    monkeypatch.setattr(m, 'validate', lambda _: (history, parent, fresh, budget, plan))
    lease = tmp_path / 'lease'; lease.write_bytes(b'lease')
    allowed = {(c['qid'], mode, arm, rep) for c,mode,rep,arm,_ in plan}
    guard = m.CurrentTransport(fresh, tmp_path, spec, lease, b'lease', allowed)
    c, mode, rep, arm, _ = plan[0]
    guard.context = {'qid': c['qid'], 'mode': mode, 'arm': arm, 'repetition': rep}
    req = httpx.Request('POST', m.base.endpoint(fresh['base_url']), json={'model':fresh['model'], 'messages':[]})
    return guard, req, lease


def test_transport_cost_and_completed_identity_no_replay(tmp_path, monkeypatch):
    guard, req, _ = guard_fixture(tmp_path, monkeypatch)
    async def sender(_, request, *args, **kwargs):
        assert guard.calls[-1]['state'] == 'UNKNOWN_PENDING'
        return httpx.Response(200, request=request, json={'id':'synthetic','model':guard.manifest['model'],
             'usage':{'prompt_tokens':12,'completion_tokens':7}})
    asyncio.run(guard.send(sender, None, req))
    assert guard.committed == guard.cost(12, 7)
    (tmp_path / 'trials.jsonl').write_bytes(m.base.canonical({**guard.context, 'state':'RETURNED'}) + b'\n')
    with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, req))
    assert len(guard.calls) == 1


@pytest.mark.parametrize('failure', ['429', 'timeout', 'lease'])
def test_unknown_or_collision_stops_without_retry(tmp_path, monkeypatch, failure):
    guard, req, lease = guard_fixture(tmp_path, monkeypatch)
    sends = []
    async def sender(_, request, *args, **kwargs):
        sends.append(1)
        if failure == 'timeout': raise httpx.ReadTimeout('synthetic')
        return httpx.Response(429, request=request)
    if failure == 'lease': lease.write_bytes(b'other')
    with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, req))
    if failure != 'lease':
        assert guard.calls[0]['state'] == 'UNKNOWN'
        assert guard.committed == m.Decimal(guard.calls[0]['reservation_cny'])
        with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(sender, None, req))
        assert len(sends) == 1
    else:
        assert not sends and not guard.calls


def test_completed_paid_attempt_retains_shared_lease_and_blocks_old_runner(tmp_path, monkeypatch):
    from contextlib import contextmanager
    import sys
    spec = contract()
    history, parent, fresh, budget, plan = m.validate(spec)
    monkeypatch.setattr(m, 'validate', lambda _: (history, parent, fresh, budget, plan))
    monkeypatch.setattr(m.base, 'ROOT', tmp_path)
    monkeypatch.setenv('M2_AB_API_KEY', 'synthetic-only')
    monkeypatch.delitem(sys.modules, 'app.core.database', raising=False)
    monkeypatch.delitem(sys.modules, 'app.core.config', raising=False)
    @contextmanager
    def redis(_):
        yield 'redis://127.0.0.1:1'
    monkeypatch.setattr(m.base, 'isolated_redis', redis)
    monkeypatch.setattr(m.base, 'configure_runtime', lambda *args: None)
    monkeypatch.setattr(m.cont.safety, 'assert_runtime_isolation', lambda *args: None)
    monkeypatch.setattr(m.cont, 'backup_reuse', lambda *args: None)
    async def trial(_manifest, _output, guard):
        c, mode, rep, arm, _ = plan[0]
        guard.context = {'qid':c['qid'], 'mode':mode, 'arm':arm, 'repetition':rep}
        req = httpx.Request('POST', guard.url, json={'model':fresh['model'], 'messages':[]})
        async def sender(_, request, *args, **kwargs):
            return httpx.Response(200, request=request, json={'id':'synthetic','model':fresh['model'],
                                  'usage':{'prompt_tokens':12,'completion_tokens':7}})
        await guard.send(sender, None, req)
    monkeypatch.setattr(m.base, 'run_trials', trial)
    output = tmp_path / 'var/preferences-acceptance/current'
    result = m.execute(spec, output)
    assert result['completed_calls'] == 1
    assert result['lease'] == 'RETAINED_PENDING_RECONCILIATION'
    dataset = json.loads((output / 'dataset.json').read_bytes())
    assert m.base.digest(m.base.canonical(dataset)) == spec['dataset_sha256']
    active = tmp_path / 'var/preferences-acceptance/m2-continuation-locks' / (history['parent_manifest_sha256'] + '.active')
    assert active.exists()
    monkeypatch.setattr(m.cont, 'validate_spec', lambda _: {'status':'PASS'})
    monkeypatch.setattr(m.cont, 'verify_history', lambda _: (parent, [], budget))
    with pytest.raises(FileExistsError):
        m.cont.execute(history, tmp_path / 'var/preferences-acceptance/old')
    assert active.exists()


@pytest.mark.parametrize('status,recorded,expected', [('STOPPED', 1, 1), ('FAILED', 1, 1),
    ('RECORDED_REQUIRES_EVIDENCE_REVIEW', 95, 1), ('RECORDED_REQUIRES_EVIDENCE_REVIEW', 96, 0)])
def test_cli_exit_status(tmp_path, monkeypatch, status, recorded, expected):
    import sys
    path = tmp_path / 'manifest.json'; path.write_text('{}')
    monkeypatch.setattr(m.base, 'private_path', lambda p: Path(p))
    monkeypatch.setattr(m, 'execute', lambda *_: {'status':status, 'recorded_trials':recorded, 'planned_trials':96})
    monkeypatch.setattr(sys, 'argv', ['runner','execute','--manifest',str(path),'--output',str(tmp_path / 'out')])
    assert m.main() == expected

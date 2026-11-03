"""Offline transport doubles; no provider request or model quality claims."""
import copy
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import run_dynamic_router_v1_experiment as runner


@pytest.fixture
def manifest():
    messages = [{'role': 'system', 'content': '[1] 来源: real.txt\nactual evidence'},
                {'role': 'user', 'content': 'question'}]
    endpoint = 'https://api.siliconflow.cn/v1'
    return dict(cases=[dict(qid=str(i), question='question', messages=copy.deepcopy(messages),
        messages_sha256=runner.sha(runner.canonical(messages)),
        citations=[{'content': 'actual evidence', 'source_file': 'real.txt'}],
        retrieval_status='SUCCESS', gold='SECRET GOLD MUST NEVER ENTER PAYLOAD') for i in range(12)],
        baseline_model=runner.CHEAP, authorization_id='offline-test-only', approved_max_calls=48,
        code_sha256=runner.code_identity(), history_inventory_audit='offline-empty-inventory',
        history_ledgers=[], endpoint_sha256={m: runner.sha(endpoint.encode())
                                             for m in (runner.CHEAP, runner.EXPENSIVE)})


def execute(manifest, tmp_path, sender):
    return runner.run(manifest, tmp_path, execute=True, sender=sender,
                      providers={m: ('https://api.siliconflow.cn/v1', 'fake')
                                 for m in (runner.CHEAP, runner.EXPENSIVE)})


def response(model, text='answer [1]', finish='stop'):
    return 200, json.dumps(dict(model=model, id='mock', usage={'total_tokens': 2},
        choices=[dict(message={'content': text}, finish_reason=finish)])).encode()


def test_preflight_is_offline_and_no_output(manifest, tmp_path):
    assert runner.run(manifest, tmp_path)['model_calls'] == 0
    assert not list(tmp_path.iterdir())


def test_isolated_default_none_preflight_needs_only_standard_library(manifest, tmp_path):
    scripts = tmp_path / 'scripts'
    services = tmp_path / 'backend' / 'app' / 'services'
    scripts.mkdir()
    services.mkdir(parents=True)
    (services.parent / '__init__.py').write_text('')
    (services / '__init__.py').write_text('')
    for name in ('run_dynamic_router_v1_experiment.py', 'run_routing200.py'):
        shutil.copyfile(Path(runner.__file__).with_name(name), scripts / name)
    for name in ('model_router.py', 'grounded_generation.py'):
        shutil.copyfile(Path(runner.__file__).parents[1] / 'backend/app/services' / name,
                        services / name)
    frozen = tmp_path / 'frozen.json'
    frozen.write_text(json.dumps(manifest), encoding='utf-8')
    result = subprocess.run([sys.executable, '-S', str(scripts / 'run_dynamic_router_v1_experiment.py'),
        'preflight', '--manifest', str(frozen), '--output', str(tmp_path / 'out')],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['model_calls'] == 0


def test_exact_inputs_all_arms_no_gold_thinking_and_resume(manifest, tmp_path):
    calls = []
    def sender(url, key, payload, timeout):
        calls.append(payload)
        assert payload['messages'] == manifest['cases'][0]['messages']
        assert 'SECRET GOLD' not in json.dumps(payload)
        assert payload['temperature'] == .7 and payload['max_tokens'] == 4096
        if payload['model'] == runner.CHEAP:
            assert payload['enable_thinking'] is False
        return response(payload['model'])
    status = execute(manifest, tmp_path, sender)
    assert status['attempts'] == 36 and status['recorded_slots'] == 36
    assert status['attempt_states'] == {'COMPLETED': 36}
    assert execute(manifest, tmp_path, sender)['attempts'] == 36
    assert len(calls) == 36
    records = json.loads((tmp_path / 'records.json').read_text())
    dynamic = next(r for r in records if r['arm'] == 'DYNAMIC')
    assert dynamic['route']['role'] in ('BASELINE', 'CHEAP')
    assert dynamic['escalation']['should_retry'] is False


def test_ambiguous_interval_retains_explicit_baseline_role(manifest, tmp_path):
    manifest['router_config'] = {'threshold': .05}
    execute(manifest, tmp_path, lambda u, k, p, t: response(p['model']))
    rows = json.loads((tmp_path / 'records.json').read_text())
    dynamic = next(r for r in rows if r['arm'] == 'DYNAMIC')
    assert dynamic['route']['role'] == 'BASELINE'
    assert dynamic['route']['model'] == runner.CHEAP
    assert dynamic['escalation']['should_retry'] is False


def test_evidence_missing_blocks_all_arms_without_sending(manifest, tmp_path):
    for c in manifest['cases']:
        c.pop('citations')
    status = execute(manifest, tmp_path, lambda *a: pytest.fail('must not send'))
    assert status['attempts'] == 0 and status['recorded_slots'] == 36


def test_insufficient_evidence_blocks_even_fixed_arms(manifest, tmp_path):
    for c in manifest['cases']:
        c['retrieval_status'] = 'INSUFFICIENT_EVIDENCE'
    assert execute(manifest, tmp_path, lambda *a: pytest.fail('must not send'))['attempts'] == 0


def test_unknown_timeout_is_never_resent(manifest, tmp_path):
    calls = []
    def sender(*args):
        calls.append(args)
        raise TimeoutError('uncertain')
    assert execute(manifest, tmp_path, sender)['attempt_states'] == {'UNKNOWN': 36}
    execute(manifest, tmp_path, sender)
    assert len(calls) == 36


def test_http_failure_is_known_failure_without_retry(manifest, tmp_path):
    def sender(*args):
        raise urllib.error.HTTPError('https://api.siliconflow.cn/v1', 429, 'limited', {}, None)
    status = execute(manifest, tmp_path, sender)
    assert status['attempt_states'] == {'PROVIDER_FAILURE': 36}
    rows = json.loads((tmp_path / 'records.json').read_text())
    assert all(row['attempts'][0]['http_status'] == 429 for row in rows)
    assert execute(manifest, tmp_path, sender)['attempts'] == 36


def test_crash_pending_commit_precedes_transport_and_resume(manifest, tmp_path):
    def crash(*args):
        with sqlite3.connect(tmp_path / 'ledger.sqlite3') as db:
            assert db.execute("select count(*) from attempts where state='UNKNOWN_PENDING'").fetchone()[0] == 1
            assert db.execute('select count(*) from events').fetchone()[0] == 1
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        execute(manifest, tmp_path, crash)
    calls = []
    def resumed(url, key, payload, timeout):
        calls.append(payload)
        return response(payload['model'])
    status = execute(manifest, tmp_path, resumed)
    assert status['attempts'] == 36 and len(calls) == 35
    assert status['attempt_states']['UNKNOWN_PENDING'] == 1


def test_history_unknown_incomplete_globally_blocks(manifest, tmp_path):
    path = tmp_path / 'old.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table attempts(qid,role,state,info)')
        db.execute('insert into attempts values (?,?,?,?)', ('', 'cheap', 'TIMEOUT_UNKNOWN', '{}'))
    manifest['history_ledgers'] = [{'path': str(path), 'sha256': runner.sha(path.read_bytes())}]
    with pytest.raises(ValueError, match='unresolved historical'):
        execute(manifest, tmp_path / 'out', lambda *a: pytest.fail('must not send'))


def test_same_historical_unknown_blocks_matching_input(manifest, tmp_path):
    path = tmp_path / 'old.sqlite3'
    info = dict(model=runner.CHEAP, messages_sha256=manifest['cases'][0]['messages_sha256'],
        request_hash='known-old-request-hash', endpoint_sha256=manifest['endpoint_sha256'][runner.CHEAP])
    with sqlite3.connect(path) as db:
        db.execute('create table attempts(qid,role,state,info)')
        db.execute('insert into attempts values (?,?,?,?)', ('0', 'cheap', 'UNKNOWN', json.dumps(info)))
    manifest['history_ledgers'] = [{'path': str(path), 'sha256': runner.sha(path.read_bytes())}]
    def sender(url, key, payload, timeout):
        return response(payload['model'])
    status = execute(manifest, tmp_path / 'out', sender)
    # All 12 synthetic cases share the exact messages; model+messages matching
    # intentionally blocks them regardless of differing qid.
    assert status['attempts'] == 12
    rows = json.loads((tmp_path / 'out' / 'records.json').read_text())
    assert sum(r['execution_status'] == 'HISTORICAL_UNKNOWN_BLOCKED' for r in rows) == 24


def test_frozen_message_and_code_identity_enforced(manifest, tmp_path):
    manifest['cases'][0]['messages'][0]['content'] = 'changed'
    with pytest.raises(ValueError, match='hash mismatch'):
        runner.run(manifest, tmp_path)


def test_escalation_is_actual_router_deterministic_failure_max_once(manifest, tmp_path):
    # A fully observed, short single-source lookup yields the CHEAP route.
    manifest['router_config'] = {'threshold': 1.0}
    manifest['quality_check'] = 'REFERENCE_RANGE'
    calls = []
    def sender(url, key, payload, timeout):
        calls.append(payload)
        return response(payload['model'], 'bad reference [99]')
    status = execute(manifest, tmp_path, sender)
    assert 36 <= status['attempts'] <= 48
    rows = json.loads((tmp_path / 'records.json').read_text())
    dynamics = [r for r in rows if r['arm'] == 'DYNAMIC']
    assert all(r['route']['role'] == 'CHEAP' for r in dynamics)
    assert all(r['quality_status'] == 'FAIL' and r['escalation']['should_retry'] for r in dynamics)
    assert status['attempts'] == 48


@pytest.mark.parametrize('actual,finish,text', [
    ('different-model', 'stop', 'bad [99]'),
    (None, 'stop', 'bad [99]'),
    (runner.CHEAP, 'length', 'bad [99]'),
    (runner.CHEAP, None, 'bad [99]'),
    (runner.CHEAP, 'stop', 'No reference supplies no verdict'),
])
def test_unknown_identity_truncated_missing_finish_no_reference_never_escalates(
        manifest, tmp_path, actual, finish, text):
    manifest['router_config'] = {'threshold': 1.0}
    manifest['quality_check'] = 'REFERENCE_RANGE'
    def sender(url, key, payload, timeout):
        return response(actual, text, finish)
    assert execute(manifest, tmp_path, sender)['attempts'] == 36


def test_default_none_does_not_add_strict_quality_gate(manifest, tmp_path):
    manifest['router_config'] = {'threshold': 1.0}
    assert execute(manifest, tmp_path, lambda u,k,p,t: response(p['model'], 'bad [99]'))['attempts'] == 36


@pytest.mark.parametrize('old,expected', [
    ({'request_hash': 'exact'}, True),
    ({'model': runner.CHEAP, 'messages_sha256': 'message'}, True),
    ({'qid': 'prior:followup:source:1'}, True),
    ({'qid': 'source:2'}, False),
    ({'qid': 'source:1', 'model': runner.EXPENSIVE}, False),
    ({'qid': 'source:1', 'messages_sha256': 'different'}, False),
])
def test_conservative_legacy_unknown_identity_match(old, expected):
    case = {'qid': 'initial:source:1', 'messages_sha256': 'message'}
    assert runner.historical_match(old, case, runner.CHEAP, 'exact', 'endpoint') is expected


def test_append_only_events_and_resume_manifest_change_rejected(manifest, tmp_path):
    execute(manifest, tmp_path, lambda u, k, p, t: response(p['model']))
    with sqlite3.connect(tmp_path / 'ledger.sqlite3') as db:
        with pytest.raises(sqlite3.IntegrityError, match='append only'):
            db.execute('delete from events')
    manifest['authorization_id'] = 'changed'
    with pytest.raises(ValueError, match='resume identity mismatch'):
        execute(manifest, tmp_path, lambda *a: pytest.fail('must not send'))


def continuation(manifest, original, source_path, tmp_path):
    manifest_path = tmp_path / 'original-manifest.json'
    manifest_path.write_text(json.dumps(original), encoding='utf-8')
    with sqlite3.connect(source_path) as db:
        identity = json.loads(db.execute("select value from metadata where key='identity'").fetchone()[0])
    manifest['execution_arms'] = ['DYNAMIC']
    manifest['authorization_id'] = 'continue-dynamic-only'
    manifest['previous_run'] = dict(ledger_path=str(source_path),
        ledger_sha256=runner.sha(source_path.read_bytes()), manifest_path=str(manifest_path),
        manifest_file_sha256=runner.sha(manifest_path.read_bytes()), code_sha256=identity['code_sha256'])
    return manifest


def test_dynamic_only_continuation_preserves_completed_and_pending_cross_arm(manifest, tmp_path):
    for case in manifest['cases']:
        case['messages'][-1]['content'] = 'question ' + case['qid']
        case['messages_sha256'] = runner.sha(runner.canonical(case['messages']))
    original = copy.deepcopy(manifest)
    source = tmp_path / 'source'
    def partial(url, key, payload, timeout):
        with sqlite3.connect(source / 'ledger.sqlite3') as db:
            qid, arm = db.execute('select qid,arm from attempts order by rowid desc limit 1').fetchone()
        if qid == '3' and arm == 'CHEAP_ONLY':
            raise KeyboardInterrupt()
        return response(payload['model'])
    with pytest.raises(KeyboardInterrupt):
        execute(original, source, partial)
    source_path = source / 'ledger.sqlite3'
    source_bytes = source_path.read_bytes()
    manifest = continuation(manifest, original, source_path, tmp_path)
    calls = []
    def dynamic_only(url, key, payload, timeout):
        calls.append(payload)
        with sqlite3.connect(tmp_path / 'continued' / 'ledger.sqlite3') as db:
            assert db.execute('select arm from attempts order by rowid desc limit 1').fetchone()[0] == 'DYNAMIC'
        return response(payload['model'])
    status = execute(manifest, tmp_path / 'continued', dynamic_only)
    assert len(calls) == 8
    assert status['attempts'] == 18  # 10 previous reservations + 8 sends.
    assert source_path.read_bytes() == source_bytes
    with sqlite3.connect(source_path) as old, sqlite3.connect(tmp_path / 'continued' / 'ledger.sqlite3') as new:
        for row in old.execute('select qid,arm,step,state,info from attempts'):
            assert new.execute('select qid,arm,step,state,info from attempts where qid=? and arm=? and step=?',
                               row[:3]).fetchone() == row
        for row in old.execute('select qid,arm,info from slots'):
            assert new.execute('select qid,arm,info from slots where qid=? and arm=?', row[:2]).fetchone() == row
    rows = json.loads((tmp_path / 'continued' / 'records.json').read_text())
    dynamic = [row for row in rows if row['arm'] == 'DYNAMIC']
    assert len(dynamic) == 12
    assert next(row for row in dynamic if row['qid'] == '3')['execution_status'] == 'HISTORICAL_UNKNOWN_BLOCKED'
    execute(manifest, tmp_path / 'continued', lambda *a: pytest.fail('must not repeat'))


def test_continuation_rejects_changed_inputs_source_and_original_output(manifest, tmp_path):
    original = copy.deepcopy(manifest)
    execute(original, tmp_path / 'source', lambda u,k,p,t: response(p['model']))
    source_path = tmp_path / 'source' / 'ledger.sqlite3'
    continuation(manifest, original, source_path, tmp_path)
    with pytest.raises(ValueError, match='output must differ'):
        execute(manifest, tmp_path / 'source', lambda *a: pytest.fail('must not send'))
    manifest['quality_check'] = 'REFERENCE_RANGE'
    with pytest.raises(ValueError, match='settings or inputs'):
        runner.run(manifest, tmp_path / 'continued')
    manifest.pop('quality_check')
    manifest['previous_run']['ledger_sha256'] = 'changed'
    with pytest.raises(ValueError, match='previous ledger changed'):
        runner.run(manifest, tmp_path / 'continued')

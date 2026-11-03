"""Offline provider doubles only; no model calls or quality inference."""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import urllib.error

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import run_dynamic_router138 as runner


@pytest.fixture
def manifest():
    cases = []
    for i in range(166):
        question = 'question ' + str(i)
        messages = [{'role': 'system', 'content': '[1] actual evidence'},
                    {'role': 'user', 'content': question}]
        case = dict(qid='initial:' + str(i), question=question, messages=messages,
                    messages_sha256=runner.sha(runner.canonical(messages)),
                    citations=[dict(content='actual evidence', source_file='real.txt')],
                    retrieval_status='SUCCESS', gold='NEVER SENT SECRET GOLD')
        case['comparison_input_hash'] = runner.base.comparison_hash(case)
        cases.append(case)
    url = 'https://api.siliconflow.cn/v1'
    return dict(cases=cases, baseline_model=runner.CHEAP, execution_arms=['DYNAMIC'],
                quality_check='NONE', authorization_id='offline-only', approved_max_calls=166,
                code_sha256=runner.code_identity(), history_inventory_audit='offline-empty',
                history_ledgers=[], endpoint_sha256={m: runner.sha(url.encode())
                                                    for m in (runner.CHEAP, runner.EXPENSIVE)})


def execute(manifest, path, sender):
    return runner.run(manifest, path, execute=True, sender=sender,
                      providers={m: ('https://api.siliconflow.cn/v1', 'fake')
                                 for m in (runner.CHEAP, runner.EXPENSIVE)})


def response(model, text='answer [1]', finish='stop'):
    return 200, json.dumps(dict(model=model, id='mock', usage=dict(total_tokens=2),
        choices=[dict(message=dict(content=text), finish_reason=finish)])).encode()


def records(path):
    return json.loads((path / 'records.json').read_text())


def historical(manifest, path, info, qid='totally-different-qid', state='UNKNOWN'):
    with sqlite3.connect(path) as db:
        db.execute('create table attempts(qid,role,state,info)')
        db.execute('insert into attempts values (?,?,?,?)', (qid, 'EXPENSIVE_ONLY', state, json.dumps(info)))
    manifest['history_ledgers'] = [dict(path=str(path), sha256=runner.sha(path.read_bytes()))]


def test_preflight_offline_no_output(manifest, tmp_path):
    result = runner.run(manifest, tmp_path)
    assert result['maximum_calls'] == result['cases'] == result['planned_arm_slots'] == 166
    assert result['model_calls'] == 0
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('field,value', [('execution_arms', ['CHEAP_ONLY']),
    ('quality_check', 'REFERENCE_RANGE'), ('baseline_model', runner.EXPENSIVE),
    ('approved_max_calls', 167)])
def test_unapproved_config_rejected(manifest, tmp_path, field, value):
    manifest[field] = value
    with pytest.raises(ValueError):
        execute(manifest, tmp_path, lambda *a: pytest.fail('no send'))


def test_only_dynamic_exact_payload_and_no_rerun(manifest, tmp_path):
    sent = []
    def sender(u, k, p, t):
        sent.append(copy.deepcopy(p))
        assert 'NEVER SENT' not in json.dumps(p)
        assert p['messages'] == manifest['cases'][len(sent) - 1]['messages']
        assert p['temperature'] == .7 and p['max_tokens'] == 4096 and p['stream'] is False
        if p['model'] == runner.CHEAP:
            assert p['enable_thinking'] is False
        return response(p['model'])
    result = execute(manifest, tmp_path, sender)
    assert result['attempts'] == result['recorded_slots'] == len(sent) == 166
    assert result['attempt_states'] == {'COMPLETED': 166}
    assert all(r['arm'] == 'DYNAMIC' and r['actual_calls'] == 1 for r in records(tmp_path))
    assert all(not r['escalation']['should_retry'] for r in records(tmp_path))
    assert execute(manifest, tmp_path, sender)['attempts'] == len(sent) == 166


@pytest.mark.parametrize('threshold,role,model', [(.05, 'BASELINE', runner.CHEAP),
    (1.0, 'CHEAP', runner.CHEAP), (0.0, 'EXPENSIVE', runner.EXPENSIVE)])
def test_actual_router_roles_including_qwen_baseline(manifest, tmp_path, threshold, role, model):
    manifest['router_config'] = dict(threshold=threshold)
    result = execute(manifest, tmp_path, lambda u,k,p,t: response(p['model']))
    assert result['attempts'] == 166
    assert all(r['route']['role'] == role and r['route']['model'] == model for r in records(tmp_path))


def test_skip_missing_and_evidence_does_not_block_remaining(manifest, tmp_path):
    manifest['cases'][0]['not_run_reason'] = 'MISSING_ACTUAL_HISTORY'
    manifest['cases'][1]['citations'] = []
    manifest['cases'][1]['comparison_input_hash'] = runner.base.comparison_hash(manifest['cases'][1])
    result = execute(manifest, tmp_path, lambda u,k,p,t: response(p['model']))
    assert result['attempts'] == 164 and result['recorded_slots'] == 166
    assert result['slot_states']['NOT_RUN'] == 2


@pytest.mark.parametrize('key', ['source_status', 'source_state'])
def test_explicit_missing_source_never_sends_even_if_payload_present(manifest, tmp_path, key):
    for case in manifest['cases']:
        case[key] = 'MISSING_INPUT'
    result = execute(manifest, tmp_path, lambda *a: pytest.fail('missing source forbids sends'))
    assert result['attempts'] == 0 and result['slot_states'] == {'NOT_RUN': 166}


def test_timeout_rate_limit_malformed_continue_without_retry(manifest, tmp_path):
    calls = []
    def sender(u,k,p,t):
        calls.append(p)
        index = len(calls)
        if index == 1:
            raise TimeoutError('do not disclose this credential-bearing message')
        if index == 2:
            raise urllib.error.HTTPError(u, 429, 'limited', {}, None)
        if index == 3:
            return 429, b'plain text rate limit'
        if index == 4:
            return 200, b'invalid JSON'
        return response(p['model'], finish='length' if index == 5 else 'stop')
    result = execute(manifest, tmp_path, sender)
    assert result['attempts'] == len(calls) == 166
    assert result['attempt_states'] == dict(UNKNOWN=2, PROVIDER_RATE_LIMITED=2, TRUNCATED=1, COMPLETED=161)
    assert execute(manifest, tmp_path, sender)['attempts'] == len(calls) == 166
    assert 'credential-bearing' not in (tmp_path / 'records.json').read_text()


def test_crash_reservation_before_send_resume_skips_pending(manifest, tmp_path):
    def crash(*a):
        with sqlite3.connect(tmp_path / 'ledger.sqlite3') as db:
            assert db.execute("select count(*) from attempts where state='UNKNOWN_PENDING'").fetchone()[0] == 1
            assert db.execute('select count(*) from events').fetchone()[0] == 1
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        execute(manifest, tmp_path, crash)
    calls = []
    def sender(u,k,p,t):
        calls.append(p)
        return response(p['model'])
    result = execute(manifest, tmp_path, sender)
    assert len(calls) == 165 and result['attempts'] == 166
    assert result['attempt_states'] == dict(UNKNOWN_PENDING=1, COMPLETED=165)
    row = next(r for r in records(tmp_path) if r['qid'] == 'initial:0')
    assert row['execution_status'] == 'UNKNOWN_PENDING' and row['latency_ms'] is None


@pytest.mark.parametrize('identity', ['request_hash', 'model_messages', 'payload'])
def test_historical_unknown_match_independent_of_arm_qid_and_endpoint(manifest, tmp_path, identity):
    case = manifest['cases'][0]
    payload = runner.base.request_payload(case, runner.CHEAP)
    info = (dict(request_hash=runner.sha(runner.canonical(payload))) if identity == 'request_hash' else
            dict(model=runner.CHEAP, messages_sha256=case['messages_sha256'], endpoint_sha256='old-endpoint')
            if identity == 'model_messages' else dict(request_payload=payload, request_hash='legacy'))
    historical(manifest, tmp_path / 'old.db', info)
    result = execute(manifest, tmp_path / 'out', lambda u,k,p,t: response(p['model']))
    assert result['attempts'] == 165
    assert result['slot_states']['HISTORICAL_UNKNOWN_BLOCKED'] == 1


def test_current_unknown_repeated_input_different_qid_never_replayed(manifest, tmp_path):
    case = copy.deepcopy(manifest['cases'][0])
    case['qid'] = manifest['cases'][1]['qid']
    manifest['cases'][1] = case
    calls = []
    def sender(*args):
        calls.append(args)
        raise TimeoutError()
    result = execute(manifest, tmp_path, sender)
    assert len(calls) == result['attempts'] == 165
    assert result['slot_states']['HISTORICAL_UNKNOWN_BLOCKED'] == 1


def test_cap_atomic_at_attempt_boundary(manifest, tmp_path):
    db = runner.base.open_ledger(tmp_path, {})
    with db:
        for i in range(166):
            db.execute('insert into attempts values (?,?,?,?,?)', (f'prior-{i}', 'DYNAMIC', 0, 'COMPLETED', '{}'))
    providers = {runner.CHEAP: ('https://api.siliconflow.cn/v1', 'fake')}
    outcome = runner.attempt(db, manifest['cases'][0], runner.CHEAP, manifest, providers,
                             lambda *a: pytest.fail('cap forbids send'))
    assert outcome['state'] == 'CALL_CAP_BLOCKED'
    assert db.execute('select count(*) from attempts').fetchone()[0] == 166
    db.close()


def test_input_code_inventory_mutation_rejected(manifest, tmp_path):
    manifest['cases'][0]['messages'][0]['content'] = 'changed'
    with pytest.raises(ValueError, match='hash mismatch'):
        runner.run(manifest, tmp_path)
    manifest['cases'][0]['messages'][0]['content'] = '[1] actual evidence'
    manifest['code_sha256']['runner'] = 'changed'
    with pytest.raises(ValueError, match='runtime identity'):
        execute(manifest, tmp_path, lambda *a: pytest.fail('no send'))
    manifest['code_sha256'] = runner.code_identity()
    historical(manifest, tmp_path / 'old.db', dict(request_hash='unrelated'))
    with open(tmp_path / 'old.db', 'ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ValueError, match='historical ledger changed'):
        execute(manifest, tmp_path / 'out', lambda *a: pytest.fail('no send'))

import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import run_affinity_paired as runner


@pytest.fixture
def manifest():
    cases = []
    for i in range(166):
        q = 'question ' + str(i)
        messages = [{'role': 'system', 'content': '[1] evidence'}, {'role': 'user', 'content': q}]
        c = dict(qid=str(i), question=q, messages=messages,
                 messages_sha256=runner.sha(runner.canonical(messages)),
                 citations=[{'content': 'evidence'}], retrieval_status='SUCCESS', gold='SECRET GOLD')
        c['comparison_input_hash'] = runner.base.comparison_hash(c)
        cases.append(c)
    url = 'https://api.siliconflow.cn/v1'
    return dict(frozen166={'cases': cases}, execution_arms=list(runner.ARMS), quality_check='NONE',
                router_config={'selection_policy': 'AFFINITY_V2', 'threshold': 0.0,
                               'escalate_on_quality_fail': False}, approved_max_calls=332,
                history_inventory_audit='synthetic', history_ledgers=[], authorization_id='mock',
                endpoint_sha256={m: runner.sha(url.encode()) for m in (runner.CHEAP, runner.EXPENSIVE)},
                code_sha256=runner.code_identity())


def execute(m, path, sender):
    return runner.run(m, path, execute=True, sender=sender,
                      providers={x: ('https://api.siliconflow.cn/v1', 'mock') for x in (runner.CHEAP, runner.EXPENSIVE)})


def response(u, k, p, t):
    assert 'SECRET GOLD' not in json.dumps(p)
    return 200, json.dumps({'model': p['model'], 'usage': {'total_tokens': 3},
                            'choices': [{'message': {'content': 'answer'}, 'finish_reason': 'stop'}]}).encode()


def test_preflight_zero_call_no_writes(manifest, tmp_path):
    out = runner.run(manifest, tmp_path)
    assert out['cases'] == 166 and out['planned_arm_slots'] == 332 and out['model_calls'] == 0
    assert not list(tmp_path.iterdir())


def test_same_model_shared_across_arms_and_resume(manifest, tmp_path):
    calls = []
    def sender(*a):
        calls.append(a[2])
        return response(*a)
    result = execute(manifest, tmp_path, sender)
    assert result['attempts'] == len(calls) == 166
    assert result['recorded_slots'] == 332
    assert all(x['model'] == runner.EXPENSIVE for x in calls)
    records = json.loads((tmp_path / 'records.json').read_text())
    assert sum(x['outcome'].get('reuse') == 'CURRENT_LEDGER' for x in records) == 166
    assert all(x['outcome']['usage']['total_tokens'] == 3 for x in records)
    assert execute(manifest, tmp_path, sender)['attempts'] == len(calls) == 166


def test_crash_pending_is_never_retried(manifest, tmp_path):
    def crash(*a):
        raise KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        execute(manifest, tmp_path, crash)
    result = execute(manifest, tmp_path, response)
    assert result['attempts'] == 166
    records = json.loads((tmp_path / 'records.json').read_text())
    assert sum(x['execution_status'] == 'UNKNOWN_PENDING' for x in records) == 2


def test_unknown_history_blocks_both_arms(manifest, tmp_path):
    c = manifest['frozen166']['cases'][0]
    path = tmp_path / 'history.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table attempts(qid,arm,state,info)')
        db.execute('insert into attempts values(?,?,?,?)', (c['qid'], 'OLD', 'UNKNOWN', json.dumps(
            {'model': runner.EXPENSIVE, 'messages_sha256': c['messages_sha256']})))
    manifest['history_ledgers'] = [{'path': str(path), 'sha256': runner.sha(path.read_bytes())}]
    result = execute(manifest, tmp_path / 'run', response)
    assert result['attempts'] == 165
    assert result['slot_states']['HISTORICAL_UNKNOWN_BLOCKED'] == 2


def test_affinity_policy_must_be_explicit(manifest, tmp_path):
    manifest['router_config']['selection_policy'] = 'WEIGHTED_V1'
    with pytest.raises(ValueError, match='AFFINITY_V2'):
        runner.run(manifest, tmp_path)


def test_nonempty_history_wal_fails_closed(manifest, tmp_path):
    path = tmp_path / 'history.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table attempts(qid,arm,state,info)')
    manifest['history_ledgers'] = [{'path': str(path), 'sha256': runner.sha(path.read_bytes())}]
    Path(str(path) + '-wal').write_bytes(b'uncheckpointed')
    with pytest.raises(ValueError, match='active WAL'):
        runner.run(manifest, tmp_path / 'out')

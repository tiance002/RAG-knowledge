"""Offline contract tests; all provider traffic is mocked."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import urllib.error
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location('routing200_runner', Path(__file__).resolve().parents[2] / 'scripts/run_routing200.py')
r = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(r)


def manifest(n=2):
    cases = []
    for i in range(n):
        messages = [{'role': 'system', 'content': 'Use context.'}, {'role': 'user', 'content': 'Question ' + str(i)}]
        cases.append(dict(qid=str(i), question='Question ' + str(i), messages=messages,
                          messages_sha256=r.sha(r.canonical(messages)), gold={'answer': 'SECRET GOLD'},
                          context_sha256='source-reference'))
    return dict(authorization_id='test-only-no-real-calls', dataset_version='test-v1', cases=cases)


def provider_db(tmp_path):
    path = tmp_path / 'credentials.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table custom_models(model_name text,base_url text,api_key text)')
        db.executemany('insert into custom_models values (?,?,?)', [(r.DEFAULTS[k], 'https://api.siliconflow.cn/v1', 'PRIVATE-KEY') for k in ('cheap_model', 'expensive_model')])
    return str(path)


class Response:
    status = 200
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def read(self):
        return r.canonical(dict(id='provider-id', model='reported', usage={'prompt_tokens': 5},
            choices=[{'message': {'content': 'Actual answer'}, 'finish_reason': 'stop'}]))


def execute(tmp_path, m=None, **kwargs):
    return r.run(m or manifest(), tmp_path / 'output', db_path=provider_db(tmp_path), mode='cheap', **kwargs)


def rows(tmp_path):
    with sqlite3.connect(tmp_path / 'output/ledger.sqlite3') as db:
        return db.execute('select qid,role,state,info from attempts order by qid,role').fetchall()


def test_preflight_offline_and_bad_hash(tmp_path):
    m = manifest()
    with patch.object(r.urllib.request, 'build_opener', side_effect=AssertionError('network')):
        assert r.run(m, tmp_path / 'absent')['model_calls'] == 0
        assert not (tmp_path / 'absent').exists()
        m['cases'][0]['messages_sha256'] = 'bad'
        with pytest.raises(ValueError, match='hash mismatch'):
            r.run(m, tmp_path / 'absent')


def test_payload_exact_messages_and_pending_committed_before_network(tmp_path):
    m = manifest()
    seen = []
    def network(request, timeout):
        payload = json.loads(request.data)
        seen.append(payload)
        assert payload['messages'] == m['cases'][len(seen)-1]['messages']
        assert 'SECRET GOLD' not in request.data.decode()
        assert set(payload) == {'model', 'messages', 'temperature', 'max_tokens', 'stream'}
        with sqlite3.connect(tmp_path / 'output/ledger.sqlite3') as db:
            assert db.execute("select count(*) from attempts where state='UNKNOWN_PENDING'").fetchone()[0] == 1
            assert db.execute("select count(*) from events where state='UNKNOWN_PENDING'").fetchone()[0] == len(seen)
        assert timeout == 120
        return Response()
    with patch.object(r.urllib.request.OpenerDirector, 'open', side_effect=network) as mock:
        result = execute(tmp_path, m)
        assert mock.call_count == 2
    assert result['counts'] == {'cheap:COMPLETED': 2}
    details = json.loads(rows(tmp_path)[0][3])
    assert details['provider_request_id'] == 'provider-id'
    assert details['raw_response'] and details['response_sha256']
    assert details['usage'] == {'prompt_tokens': 5}
    assert details['cost_status'] == 'UNKNOWN'
    with sqlite3.connect(tmp_path / 'output/ledger.sqlite3') as db:
        with pytest.raises(sqlite3.IntegrityError, match='append-only'):
            db.execute('delete from events')


def test_timeout_resume_never_replays_unknown(tmp_path):
    db_path = provider_db(tmp_path)
    m = manifest()
    with patch.object(r.urllib.request.OpenerDirector, 'open', side_effect=TimeoutError) as mock:
        result = r.run(m, tmp_path / 'output', 'cheap', db_path)
        assert mock.call_count == 1
        assert result['counts'] == {'cheap:UNKNOWN': 1}
        r.run(m, tmp_path / 'output', 'cheap', db_path)
        assert mock.call_count == 1
    with patch.object(r.urllib.request.OpenerDirector, 'open', return_value=Response()) as mock:
        r.run(m, tmp_path / 'output', 'cheap', db_path, continue_unvisited=True)
        assert mock.call_count == 1
        r.run(m, tmp_path / 'output', 'cheap', db_path, continue_unvisited=True)
        assert mock.call_count == 1
    assert [x[2] for x in rows(tmp_path)] == ['UNKNOWN', 'COMPLETED']


def test_crash_pending_never_replayed(tmp_path):
    m = manifest()
    db_path = provider_db(tmp_path)
    db = r.open_ledger(tmp_path / 'output', m)
    r.record(db, '0', 'cheap', 'UNKNOWN_PENDING', {'request_hash': 'known'}, initial=True)
    db.close()
    with patch.object(r.urllib.request.OpenerDirector, 'open', return_value=Response()) as mock:
        r.run(m, tmp_path / 'output', 'cheap', db_path, continue_unvisited=True)
        assert mock.call_count == 1
    assert rows(tmp_path)[0][2] == 'UNKNOWN_PENDING'


def test_429_known_failure_no_retry(tmp_path):
    error = urllib.error.HTTPError('https://api.siliconflow.cn/v1/chat/completions', 429, 'busy', {}, io.BytesIO(b'{"error":"rate limited"}'))
    with patch.object(r.urllib.request.OpenerDirector, 'open', side_effect=error) as mock:
        result = execute(tmp_path)
        assert mock.call_count == 1
    assert result['counts'] == {'cheap:HTTP_FAILURE': 1}
    assert json.loads(rows(tmp_path)[0][3])['http_status'] == 429


def test_expensive_verified_failure_only(tmp_path):
    m = manifest(1)
    db_path = provider_db(tmp_path)
    with patch.object(r.urllib.request.OpenerDirector, 'open', return_value=Response()):
        r.run(m, tmp_path / 'output', 'cheap', db_path)
    info = json.loads(rows(tmp_path)[0][3])
    review = dict(qid='0', answer_sha256=info['answer_sha256'], manifest_sha256=r.sha(r.canonical(m)),
                  core_correctness='FAIL', failure_kind='GENERATION_QUALITY', evidence_status='PRESENT',
                  answerability=True, reviewer='human', reason='Incorrect supported answer.', evidence_refs=['source:1'])
    path = tmp_path / 'reviews.jsonl'
    with patch.object(r.urllib.request.OpenerDirector, 'open', return_value=Response()) as mock:
        with pytest.raises(ValueError, match='requires reviewed'):
            r.run(m, tmp_path / 'output', 'expensive', db_path)
        for key, bad in [('answer_sha256', 'wrong'), ('failure_kind', 'RETRIEVAL_FAILURE'), ('answerability', False), ('evidence_status', 'UNKNOWN'), ('manifest_sha256', 'wrong')]:
            path.write_text(json.dumps(dict(review, **{key: bad})), encoding='utf-8')
            with pytest.raises(ValueError, match='invalid expensive review'):
                r.run(m, tmp_path / 'output', 'expensive', db_path, path)
            assert mock.call_count == 0
        path.write_text(json.dumps(review), encoding='utf-8')
        r.run(m, tmp_path / 'output', 'expensive', db_path, path)
        assert mock.call_count == 1
        assert mock.call_args.kwargs['timeout'] == 180


def test_ledger_rejects_manifest_change(tmp_path):
    m = manifest()
    db = r.open_ledger(tmp_path / 'output', m)
    db.close()
    m['dataset_version'] = 'altered'
    with pytest.raises(ValueError, match='different frozen manifest'):
        r.open_ledger(tmp_path / 'output', m)


@pytest.mark.parametrize('body, expected', [
    ({'choices': [{'message': {'content': 'partial'}, 'finish_reason': 'length'}]}, 'TRUNCATED'),
    ({'choices': [{'message': {'content': ''}, 'finish_reason': 'stop'}]}, 'EMPTY'),
    ({'choices': []}, 'UNKNOWN'),
    ({'choices': [{'message': {'content': 'answer'}}]}, 'UNKNOWN'),
    ({'error': {'message': 'provider error'}}, 'PROVIDER_FAILURE'),
    (None, 'UNKNOWN'),
])
def test_response_outcomes_preserve_raw(tmp_path, body, expected):
    raw = b'invalid JSON' if body is None else r.canonical(body)
    response = Response()
    response.read = lambda: raw
    with patch.object(r.urllib.request.OpenerDirector, 'open', return_value=response) as mock:
        execute(tmp_path, manifest(1))
        assert mock.call_count == 1
    row = rows(tmp_path)[0]
    assert row[2] == expected
    assert json.loads(row[3])['response_sha256'] == r.sha(raw)


def test_grant_cap_and_partial_review(tmp_path):
    m = manifest(1)
    m['config'] = {'max_cheap_calls': 151}
    with pytest.raises(ValueError, match='invalid call cap'):
        r.validate_manifest(m)
    case = manifest(1)['cases'][0]
    info = dict(text='answer', answer_sha256='answer-hash', finish_reason='stop')
    review = dict(qid='0', answer_sha256='answer-hash', manifest_sha256='manifest',
                  core_correctness='FAIL', failure_kind='GENERATION_QUALITY', evidence_status='PARTIAL',
                  answerability=True, reviewer='human', reason='bad', evidence_refs=['source'])
    cheap = ('COMPLETED', json.dumps(info))
    assert not r.reviewed_failure(case, cheap, review, 'manifest')
    review['evidence_reliable'] = True
    assert r.reviewed_failure(case, cheap, review, 'manifest')
    info['finish_reason'] = 'length'
    assert not r.reviewed_failure(case, ('COMPLETED', json.dumps(info)), review, 'manifest')

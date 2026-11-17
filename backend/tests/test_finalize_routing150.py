"""Offline finalizer verification; never invokes providers or real systemd."""
import importlib.util
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]

def load(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

f = load('finalizer', 'finalize_routing150.py')
r = load('runner_for_finalizer_test', 'run_routing200.py')


def fixture(tmp_path):
    messages = [{'role': 'user', 'content': 'Source and question'}]
    cases = [dict(qid=str(i), question='Question ' + str(i), messages=messages,
                  messages_sha256=r.sha(r.canonical(messages)), gold={'answer_points': ['known answer']},
                  context_sha256='context-hash', retrieved_top10=[{'id': 'source1'}],
                  excluded_from_core_accuracy=i == 2) for i in range(3)]
    manifest = dict(dataset_version='frozen-test', cases=cases)
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    db = r.open_ledger(tmp_path / 'runner', manifest)
    r.record(db, '0', 'cheap', 'UNKNOWN_PENDING', {}, initial=True)
    r.record(db, '0', 'cheap', 'COMPLETED', dict(text='known answer', finish_reason='stop',
             answer_sha256='answer-hash', latency_ms=100, trace_id='abc123', usage={'prompt_tokens': 7}), initial=False)
    r.record(db, '1', 'cheap', 'UNKNOWN_PENDING', {}, initial=True)
    db.close()
    return path, tmp_path / 'runner/ledger.sqlite3', manifest


def test_missing_usage_pending_exclusion_and_no_fake_correctness(tmp_path):
    path, ledger, manifest = fixture(tmp_path)
    summary = f.finalize(path, ledger, tmp_path / 'report', trace_base_url='https://trace.example/project/p')
    assert summary['status'] == 'EXECUTION_INCOMPLETE_WITH_REVIEW_PENDING'
    assert summary['core_accuracy_denominator'] is None
    assert summary['core_accuracy_maximum_eligible'] == 2
    assert summary['nonempty_completed_eligible'] == 1
    assert summary['excluded_qids'] == ['2']
    assert summary['final_core_accuracy'] is None
    assert summary['semantic_review_counts'] == {'PASS': 0, 'FAIL': 0, 'UNKNOWN': 2}
    cheap = summary['lanes']['cheap']
    assert cheap['counts']['COMPLETED'] == 1
    assert cheap['counts']['UNKNOWN_PENDING'] == 1
    assert cheap['counts']['UNVISITED'] == 1
    assert cheap['provider_usage']['input']['reported_subtotal'] == 7
    assert cheap['provider_usage']['input']['unknown_attempts'] == 1
    assert cheap['provider_usage']['input']['exact_all_attempts_total'] is None
    assert cheap['provider_usage']['output']['reported_attempts'] == 0
    assert cheap['attempt_latency_ms'] == {'n': 1, 'p50': 100, 'p95': 100, 'method': 'linear interpolation across recorded nonnegative attempt latencies'}
    assert summary['verification']['event_count'] == 3
    evidence = json.loads((tmp_path / 'report/source_evidence.json').read_text(encoding='utf-8'))['cases']
    assert evidence[0]['core_correctness'] == 'UNKNOWN'  # Literal Gold match never becomes PASS.
    assert evidence[0]['lexical_gold_coverage']['literal_substring_matches'] == 1
    assert evidence[0]['trace_url'] == 'https://trace.example/project/p/traces/abc123'
    assert evidence[0]['frozen_messages_reference']['messages_sha256'] == manifest['cases'][0]['messages_sha256']
    assert 'frozen_messages' not in evidence[0]
    marker = json.loads((tmp_path / 'report/completion.json').read_text(encoding='utf-8'))
    for name, expected in marker['artifacts_sha256'].items():
        assert f.sha((tmp_path / 'report' / name).read_bytes()) == expected
    assert (tmp_path / 'report/review.md').exists()
    assert summary['cost_status'] == summary['savings_status'] == 'UNKNOWN'


def test_identity_replay_and_invalid_review_rejected(tmp_path):
    path, ledger, manifest = fixture(tmp_path)
    reviews = tmp_path / 'reviews.jsonl'
    reviews.write_text(json.dumps(dict(qid='0', core_correctness='PASS', manifest_sha256='wrong',
                      answer_sha256='answer-hash', reviewer='human', reason='reviewed', evidence_refs=['source1'])), encoding='utf-8')
    with pytest.raises(ValueError, match='invalid semantic review'):
        f.finalize(path, ledger, tmp_path / 'report', reviews)
    with sqlite3.connect(ledger) as db:
        db.execute("insert into events(qid,role,state,info) values ('0','cheap','UNKNOWN_PENDING','{}')")
    with pytest.raises(ValueError, match='one send per attempt'):
        f.finalize(path, ledger, tmp_path / 'report')
    manifest['dataset_version'] = 'changed'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    with pytest.raises(ValueError, match='identity mismatch'):
        f.finalize(path, ledger, tmp_path / 'report')


def test_wait_is_local_30_seconds_and_trace_credentials_rejected():
    responses = [SimpleNamespace(stdout='active\n'), SimpleNamespace(stdout='inactive\n'),
                 SimpleNamespace(stdout='Result=success\nExecMainStatus=0\n')]
    with patch.object(f.subprocess, 'run', side_effect=responses) as system, patch.object(f.time, 'sleep') as sleep:
        result = f.wait_service('routing.service')
        sleep.assert_called_once_with(30)
        assert system.call_count == 3
        assert all(call.args[0][0] == 'systemctl' for call in system.call_args_list)
        assert result['active_state'] == 'inactive'
    with pytest.raises(ValueError, match='without credentials'):
        f.trace_url('https://user:SECRET@trace.example/path', 'abc')

"""Synthetic local ledgers; never sends requests or evaluates answer semantics."""
import json
from pathlib import Path
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import finalize_dynamic166 as finalizer


def fixture_run(tmp_path, name='new', qid='initial:q1', state='COMPLETED', usage=None):
    root = tmp_path / name
    root.mkdir()
    case = dict(qid=qid, question='Which value?', messages=[dict(role='user', content='Which value?')],
                citations=[dict(citation_label='1', source_file='source.txt', content='unique corpus body',
                                content_sha256='content-hash')], gold=dict(answer_points=['42'],
                original_case=dict(huge_corpus='do not duplicate')), gold_status='FROZEN',
                cheap_baseline=dict(source_qid=qid, source_role='initial'))
    case['messages_sha256'] = finalizer.sha(finalizer.canonical(case['messages']))
    manifest = dict(cases=[case])
    manifest_path = root / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
    info = dict(model=finalizer.CHEAP, actual_model=finalizer.CHEAP, text='42 [1]',
                messages_sha256=case['messages_sha256'], request_hash=finalizer.payload_hash(case, finalizer.CHEAP),
                answer_sha256=finalizer.sha(b'42 [1]'), latency_ms=100, usage=usage,
                raw_response='SECRET RAW RESPONSE NOT FOR EXPORT')
    if state == 'UNKNOWN_PENDING':
        info = {k: info[k] for k in ('model', 'messages_sha256', 'request_hash')}
    ledger = root / 'ledger.sqlite3'
    with sqlite3.connect(ledger) as db:
        db.executescript('create table metadata(key,value); create table attempts(qid,arm,step,state,info);'
                         'create table slots(qid,arm,info); create table events(id integer primary key,qid,arm,step,state);')
        db.execute('insert into metadata values (?,?)', ('identity', json.dumps(dict(
            manifest_sha256=finalizer.sha(finalizer.canonical(manifest))))))
        db.execute('insert into attempts values (?,?,?,?,?)', (qid, 'DYNAMIC', 0, state, json.dumps(info)))
        db.execute('insert into events(qid,arm,step,state) values (?,?,?,?)', (qid, 'DYNAMIC', 0, 'UNKNOWN_PENDING'))
        if state != 'UNKNOWN_PENDING':
            db.execute('insert into events(qid,arm,step,state) values (?,?,?,?)', (qid, 'DYNAMIC', 0, state))
            db.execute('insert into slots values (?,?,?)', (qid, 'DYNAMIC', json.dumps(dict(execution_status=state))))
    return manifest_path, ledger, case, info


def history(tmp_path, entries):
    path = tmp_path / 'history.sqlite3'
    with sqlite3.connect(path) as db:
        db.execute('create table attempts(qid,role,state,info)')
        db.executemany('insert into attempts values (?,?,?,?)', [
            (q, 'initial', s, json.dumps(i)) for q, s, i in entries])
    return path


def test_unknown_pending_survives_failed_service_and_usage_is_not_zero(tmp_path):
    manifest, ledger, case, info = fixture_run(tmp_path, state='UNKNOWN_PENDING')
    historical = history(tmp_path, [('initial:q1', 'UNKNOWN', info)])
    before = ledger.read_bytes(), historical.read_bytes()
    summary = finalizer.finalize(manifest, ledger, historical, tmp_path/'out', service=dict(active_state='failed'))
    assert summary['status'] == 'EXECUTION_INCOMPLETE_REVIEW_PENDING'
    assert summary['execution_states'] == {'UNKNOWN_PENDING': 1}
    assert summary['historical_unknown_matches'] == 1
    assert summary['dynamic']['provider_usage']['input']['exact_all_attempts_total'] is None
    assert summary['dynamic']['provider_usage']['input']['unknown_attempts'] == 1
    assert summary['dynamic']['latency_ms']['p95'] is None
    assert summary['final_core_accuracy'] is None
    assert (ledger.read_bytes(), historical.read_bytes()) == before


def test_actual_answers_gold_refs_merge_and_no_secret_or_corpus_copy(tmp_path):
    manifest, ledger, case, info = fixture_run(tmp_path, usage=dict(prompt_tokens=10, completion_tokens=2,total_tokens=12))
    prior, prior_ledger, c2, i2 = fixture_run(tmp_path, name='prior', qid='initial:q2')
    historical = history(tmp_path, [('initial:q1', 'COMPLETED', info), ('initial:q2', 'COMPLETED', i2)])
    # Both identical request hashes can be disambiguated only using provenance.
    summary = finalizer.finalize(manifest, ledger, historical, tmp_path/'out', prior, prior_ledger)
    assert summary['scope'] == 2 and summary['new_scope'] == 1 and summary['prior_scope'] == 1
    assert summary['historical_exact_matches'] == 2
    assert summary['quality_status'] == 'UNKNOWN_HUMAN_REVIEW_PENDING'
    assert summary['dynamic']['provider_usage']['total']['reported_subtotal'] == 12
    assert summary['dynamic']['provider_usage']['total']['exact_all_attempts_total'] is None
    raw = (tmp_path/'out/manual_source_review.json').read_text(encoding='utf-8')
    assert 'SECRET RAW' not in raw and 'unique corpus body' not in raw and 'do not duplicate' not in raw
    item = json.loads(raw)['cases'][0]
    assert item['dynamic']['actual_answer'] == '42 [1]'
    assert item['gold']['answer_points'] == ['42']
    assert item['review']['core_correctness'] == 'UNKNOWN'
    assert item['frozen_context_reference']['messages_sha256'] == case['messages_sha256']


def test_history_wrong_model_or_request_cannot_be_used(tmp_path):
    manifest, ledger, case, info = fixture_run(tmp_path)
    info['request_hash'] = 'different'
    historical = history(tmp_path, [('initial:q1', 'COMPLETED', info)])
    summary = finalizer.finalize(manifest, ledger, historical, tmp_path/'out')
    assert summary['historical_exact_matches'] == 0
    item = json.loads((tmp_path/'out/manual_source_review.json').read_text(encoding='utf-8'))['cases'][0]
    assert item['historical_cheap']['actual_answer'] is None


def test_missing_historical_request_hash_keeps_answer_without_claiming_exact_pair(tmp_path):
    manifest, ledger, case, info = fixture_run(tmp_path)
    del info['request_hash']
    del info['answer_sha256']
    historical = history(tmp_path, [('initial:q1', 'COMPLETED', info)])
    summary = finalizer.finalize(manifest, ledger, historical, tmp_path/'out')
    assert summary['historical_exact_matches'] == 0
    assert summary['historical_cheap']['attempts'] == 0
    item = json.loads((tmp_path/'out/manual_source_review.json').read_text(encoding='utf-8'))['cases'][0]
    assert item['historical_cheap']['actual_answer'] == '42 [1]'
    assert item['historical_cheap']['status'] == 'MESSAGES_AND_MODEL_MATCH_REQUEST_CONFIG_UNKNOWN'


@pytest.mark.parametrize('mutation', ['manifest', 'events', 'request', 'answer'])
def test_tamper_rejected(tmp_path, mutation):
    manifest, ledger, case, info = fixture_run(tmp_path)
    historical = history(tmp_path, [('initial:q1', 'COMPLETED', info)])
    if mutation == 'manifest':
        value = json.loads(manifest.read_text())
        value['cases'][0]['question'] = 'changed'
        manifest.write_text(json.dumps(value), encoding='utf-8')
    else:
        with sqlite3.connect(ledger) as db:
            if mutation == 'events':
                db.execute("insert into events(qid,arm,step,state) values ('initial:q1','DYNAMIC',0,'UNKNOWN_PENDING')")
            else:
                info['request_hash' if mutation == 'request' else 'answer_sha256'] = 'wrong'
                db.execute('update attempts set info=?', (json.dumps(info),))
    with pytest.raises(ValueError):
        finalizer.finalize(manifest, ledger, historical, tmp_path/'out')
    assert not (tmp_path/'out/completion.json').exists()


def test_unvisited_is_separate_from_wrong_answer(tmp_path):
    manifest, ledger, case, info = fixture_run(tmp_path)
    with sqlite3.connect(ledger) as db:
        db.execute('delete from attempts')
        db.execute('delete from events')
        db.execute('delete from slots')
    historical = history(tmp_path, [])
    summary = finalizer.finalize(manifest, ledger, historical, tmp_path/'out')
    assert summary['execution_states'] == {'NOT_RUN': 1}
    assert summary['correctness_denominator'] is None

"""Offline fixtures verify selection, historical reconstruction, and fail-closed hashes."""
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('freeze_dynamic138', SCRIPTS / 'freeze_dynamic138.py')
freeze = importlib.util.module_from_spec(spec)
spec.loader.exec_module(freeze)


@pytest.fixture
def frozen_sources(tmp_path):
    root = tmp_path / 'experiments'
    paths = dict(base=root / 'dynamic-routing-20261101/gold150/manifest.json',
                 split=root / 'dynamic-routing-20261101/gold150/split.json',
                 prior=root / 'dynamic-router-v1-20261102/frozen_cases.json',
                 ledger=root / 'followup-routing-20261102/full180-v2/ledger.sqlite3',
                 pipeline=root / 'followup-routing-20261102/full180_pipeline.py',
                 follow=root / 'followup-routing-20261102/input/follow.json',
                 overlay=root / 'followup-routing-20261102/input/overlay.json')
    for p in paths.values():
        p.parent.mkdir(parents=True, exist_ok=True)
    context = '[1] 来源: actual.pdf\nreal evidence\n'
    system = {'role': 'system', 'content': '参考资料：\n' + context + '\n要求：\nanswer'}
    base = {'cases': [dict(qid='fixture:%03d' % i, question='original %d' % i,
                           messages=[system, {'role': 'user', 'content': 'original %d' % i}],
                           context_sha256=freeze.sha(context.encode()), context_source_path='/read-only/context',
                           retrieved_top10=[], retrieved_top10_sha256=freeze.sha(freeze.canonical([])),
                           gold={'answer_points': ['original gold'], 'kb_scope': ['KB-A']}, source_group='fixture',
                           excluded_from_core_accuracy=i == 148) for i in range(150)]}
    prior = {'cases': [{'qid': 'initial:fixture:%03d' % i} for i in range(10)] +
                      [{'qid': 'followup:f:%03d' % i} for i in range(2)]}
    follow = {'version': 'followup30-draft-v1', 'cases': [
        {'id': 'f:%03d' % i, 'previous_qid': 'fixture:020', 'previous_question': 'actual previous variant',
         'followup_question': 'followup %d' % i, 'gold_answer': 'separate gold',
         'history_messages': [{'role': 'user', 'content': 'older question'},
                              {'role': 'assistant', 'content': 'older ACTUAL answer'},
                              {'role': 'user', 'content': 'will replace'},
                              {'role': 'assistant', 'content': 'must not reuse synthetic draft'}]}
        for i in range(30)]}
    # Include real office IDs required by historical year-scoping algorithm.
    for i, n in enumerate(list(range(26, 36)) + list(range(41, 50))):
        base['cases'][100 + i]['qid'] = 'office50:%03d' % n
    overlay = {'dataset_version': 'dynamic-routing-150-question-gold-candidate-v2',
               'base_frozen_manifest_sha256': freeze.sha(freeze.canonical(base)),
               'changes': [{'qid': 'fixture:020', 'candidate': {'question': 'edited 20', 'answer_points': ['owner gold']}}]}
    for key, obj in [('base', base), ('split', {'records': []}), ('prior', prior), ('follow', follow), ('overlay', overlay)]:
        paths[key].write_bytes(freeze.canonical(obj))
    paths['pipeline'].write_text('historical pipeline identity')
    db = sqlite3.connect(paths['ledger'])
    db.execute('create table attempts(qid text,role text,state text,info text)')
    def attempt(qid, role, messages, text='actual cheap reply'):
        info = dict(text=text, finish_reason='stop', actual_model=freeze.CHEAP,
                    messages_sha256=freeze.sha(freeze.canonical(messages)), request_hash='original request hash')
        db.execute('insert into attempts values(?,?,?,?)', (qid, role, 'COMPLETED', json.dumps(info)))
    for c in base['cases']:
        q = 'edited 20' if c['qid'] == 'fixture:020' else c['question']
        if c['qid'].startswith('office50:'):
            n = int(c['qid'].split(':')[1]);q = ('For fiscal year 2018, ' if n <= 35 else 'According to the 2022 data, ') + q
        attempt('initial:' + c['qid'], 'initial', [system, {'role': 'user', 'content': q}])
    for f in follow['cases']:
        attempt('prior:' + f['id'], 'prior', [system, {'role': 'user', 'content': f['previous_question']}])
        history = f['history_messages'][:-2] + [{'role': 'user', 'content': f['previous_question']},
                                               {'role': 'assistant', 'content': 'actual cheap reply'}]
        attempt('followup:' + f['id'], 'followup', [system] + history + [{'role': 'user', 'content': f['followup_question']}])
    db.commit();db.close()
    return root, paths


def test_166_selection_hydration_and_owner_edits(frozen_sources, tmp_path):
    root, paths = frozen_sources
    out = tmp_path / 'frozen.json'
    manifest = freeze.freeze(root, out)
    cases = freeze.load_frozen_cases(manifest)
    assert len(cases) == 166 and manifest['not_run_count'] == 0
    assert len(manifest['unselected_initial_cases']) == 2
    assert all('messages' not in c and 'gold' not in c for c in manifest['cases'])
    revised = next(c for c in cases if c['qid'] == 'initial:fixture:020')
    assert revised['question'] == 'edited 20'
    assert revised['gold']['answer_points'] == ['owner gold']
    assert 'owner gold' not in str(revised['messages'])
    history = next(c for c in cases if c['stratum'] == 'actual_history')['history_messages']
    assert history[-1] == {'role': 'assistant', 'content': 'actual cheap reply'}
    assert history[1]['content'] == 'older ACTUAL answer'
    assert 'synthetic' not in str(history)
    assert out.stat().st_size < 200000
    with pytest.raises(FileExistsError):
        freeze.freeze(root, out)


def test_tampered_source_and_compact_case_fail_closed(frozen_sources, tmp_path):
    root, paths = frozen_sources
    manifest = freeze.freeze(root, tmp_path / 'frozen.json')
    manifest['cases'][0]['messages_sha256'] = 'forged'
    with pytest.raises(AssertionError, match='case reconstruction mismatch'):
        freeze.load_frozen_cases(manifest)
    paths['pipeline'].write_text('changed')
    with pytest.raises(AssertionError, match='source bytes changed'):
        freeze.load_frozen_cases(manifest)


def test_missing_parent_is_not_run_not_synthetic(frozen_sources, tmp_path):
    root, paths = frozen_sources
    db = sqlite3.connect(paths['ledger'])
    db.execute("delete from attempts where qid='prior:f:002'")
    db.commit();db.close()
    manifest = freeze.freeze(root, tmp_path / 'frozen.json')
    case = next(c for c in freeze.load_frozen_cases(manifest) if c['qid'] == 'followup:f:002')
    assert case['not_run_reason'] == 'NOT_RUN_MISSING_OR_INCOMPLETE_ACTUAL_PARENT'
    assert manifest['not_run_count'] == 1
    assert all(m['content'] != 'actual cheap reply' for m in case['history_messages'])

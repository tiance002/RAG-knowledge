"""Synthetic contract tests: these do not establish archived DEV performance."""
import copy
from pathlib import Path
import pytest
from rsik.dev_debug_replay import replay_patch, replay_candidate
from rsik.query_aware import adapt_legacy
from run_rsi_loop import selector

ROOT = Path(__file__).resolve().parents[2]


def worker():
    return adapt_legacy((ROOT / 'scripts/rsi_delivery_worker.py').read_bytes())


def cases():
    members = [{'id':'a','file_id':'f','kb_id':'k','content':'seed'},
               {'id':'b','file_id':'f','kb_id':'k','content':'actual long context'}]
    return [{'case_id':'synthetic-dev','query':'actual runtime question', 'members':members,
             'chunks':{m['id']:dict(m) for m in members},
             'files':{'f':{'id':'f','name':'f.txt','kb_id':'k'}},'seed_ids':['a'],
             'atoms':[{'atom_id':'x','audit_status':'AUDITED','predicate':'any_member','member_ids':['b']}]}]


def patch(body):
    original = selector(worker().decode())[1]
    return {'old':original,'new':'def select_member_indices(members, chunks, files, seed_ids, selection, query):\n'+body+'\n'}


def test_executes_entire_candidate_with_actual_question_and_pairs_members():
    inputs=cases();before=copy.deepcopy(inputs)
    report=replay_patch(worker(), {'selection':'all'}, patch('    return [i for i,m in enumerate(members) if m["id"] in seed_ids or query == "keep everything"]'), inputs)
    assert inputs == before
    assert report['status']=='DEV_REPLAY_COMPLETE'
    row=report['per_case'][0]
    assert row['baseline']['member_ids']==['a','b']
    assert row['candidate']['member_ids']==['a']
    assert row['candidate']['retention']['status']=='MISSING'
    assert row['chars_delta']==-len('actual long context')
    assert report['baseline_chars']==23 and report['candidate_chars']==4
    assert row['candidate']['exit_code']==0 and row['candidate']['stderr']==''
    assert report['formal_admission'] is False


def test_unaudited_predicates_remain_unknown():
    inputs=cases();inputs[0]['atoms'][0]['audit_status']='UNREVIEWED'
    report=replay_candidate(worker(),{'selection':'all'},worker(),inputs)
    assert report['per_case'][0]['baseline']['retention']['status']=='UNKNOWN'


@pytest.mark.parametrize('body',[
    '    import os\n    return []',
    '    return eval(query)',
    '    members[0]["content"]="bad"\n    return []',
    '    return [query.__class__]',
])
def test_strict_guard_rejects_unsafe_code_before_spawn(body,monkeypatch):
    def forbidden(*a,**k): raise AssertionError('must never spawn rejected selector')
    monkeypatch.setattr('rsik.dev_debug_replay.subprocess.run', forbidden)
    with pytest.raises(ValueError):
        replay_patch(worker(),{'selection':'all'},patch(body),cases())


def test_invalid_candidate_indices_fail_pair_and_do_not_count_chars():
    report=replay_patch(worker(),{'selection':'all'},patch('    return [1]'),cases())
    assert report['status']=='DEV_REPLAY_INCOMPLETE'
    assert report['paired_cases']==0 and report['failed_cases']==1
    assert report['baseline_chars']==report['candidate_chars']==0
    assert 'DEV_SEED_LOSS' in report['per_case'][0]['candidate']['reason']


def test_large_range_stops_with_resource_budget():
    report=replay_patch(worker(),{'selection':'all'},patch('    return [i for i in range(1000000000)]'),cases())
    assert report['failed_cases']==1
    assert 'DEV_RANGE_BUDGET' in report['per_case'][0]['candidate']['reason']


def test_instruction_budget_stops_nested_computation():
    report=replay_patch(worker(),{'selection':'all'},patch('    values = [i for i in range(20000) for j in range(20000)]\n    return []'),cases())
    assert report['failed_cases']==1
    assert 'DEV_INSTRUCTION_BUDGET' in report['per_case'][0]['candidate']['reason']


def test_incomplete_archived_selector_inputs_are_unknown_without_execution(monkeypatch):
    inputs=cases()
    inputs[0].update(selector_input_status='UNKNOWN', selector_input_reason='ARCHIVED_SEED_CHILD_RECORDS_ABSENT',seed_ids=['absent-child'])
    def forbidden(*a,**k): raise AssertionError('unavailable input must not execute')
    monkeypatch.setattr('rsik.dev_debug_replay.subprocess.run', forbidden)
    report=replay_candidate(worker(),{'selection':'all'},worker(),inputs)
    assert report['per_case'][0]['pair_status']=='DEV_INPUT_UNAVAILABLE'
    assert report['per_case'][0]['candidate']['retention']['status']=='UNKNOWN'
    assert report['paired_cases']==0


def test_never_executes_worker_module_top_level_code():
    hostile=b'raise RuntimeError("WHOLE_MODULE_MUST_NOT_EXECUTE")\n'+worker()
    report=replay_candidate(hostile,{'selection':'all'},hostile,cases())
    assert report['paired_cases']==1


def test_fixed_trusted_duplicate_helper_preserves_real_pdf_dedup_semantics():
    inputs=cases();case=inputs[0]
    case['files']['f']['name']='actual.pdf'
    for member in case['members']:
        member['content']='same text'
        case['chunks'][member['id']].update(content='same text', source_locator={'page':1,'bbox':[0,0,1,1]},
                                            parent_id='p',heading_text='actual heading')
    report=replay_candidate(worker(),{'selection':'all'},worker(),inputs,candidate_config={'selection':'exact_duplicate'})
    assert report['per_case'][0]['candidate']['member_ids']==['a']
    assert report['per_case'][0]['baseline']['member_ids']==['a','b']


def test_final_partition_rejected():
    inputs=cases();inputs[0]['partition']='FINAL'
    with pytest.raises(ValueError,match='DEV_PARTITION_REQUIRED'):
        replay_candidate(worker(),{'selection':'all'},worker(),inputs)

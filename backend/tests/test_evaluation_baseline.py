from app.services.evaluation import evidence_state, normalize_location, score_record, compare_records
import pytest
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location('replay', Path(__file__).resolve().parents[2] / 'scripts/evaluate_observation_baseline.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def test_states_and_transitions():
    assert [evidence_state(['a', 'b'], x) for x in (None, [], ['a'], ['a', 'b'])] == ['UNKNOWN', 'ABSENT', 'PARTIAL', 'PRESENT']
    r = score_record('q', 'v', ['a', 'b'], {'raw': ['a'], 'merged': ['a', 'b'], 'reranked': []})
    assert r['transitions']['raw_to_merged']['coverage_delta'] == .5
    assert r['transitions']['merged_to_reranked']['lost'] is True
    assert r['transitions']['reranked_to_context']['coverage_delta'] is None
    assert r['scores']['cost'] is None

def test_location_and_identity():
    loc = normalize_location('xlsx', {'sheet': 'S', 'custom': 3})['location']
    assert loc['sheet'] == 'S' and loc['cell'] is None and loc['custom'] == 3
    assert normalize_location(None)['location']['source_sha256'] is None
    r = score_record('q', 'v', [], {})
    with pytest.raises(ValueError):
        compare_records([r, r])

def test_historical_replay_does_not_invent_stages():
    r = module.replay({'qid': 'q', 'arm': 'before', 'sources_at_10': [{'chunk_id': 'c'}], 'verdict': 'correct', 'included_in_answer_accuracy': True, 'answer': '', 'finish_reason': 'length'}, 'v')
    assert r['experiment_version'] == 'v:before'
    assert r['observed_top10_chunk_ids'] == ['c']
    assert set(r['evidence'].values()) == {'UNKNOWN'}
    assert r['scores']['core_correctness'] == 1 and r['scores']['empty'] and r['scores']['truncated']
    excluded = module.replay({'qid': 'q', 'verdict': 'correct', 'included_in_answer_accuracy': False}, 'v')
    assert excluded['scores']['core_correctness'] is None
    assert module.summarize([r, excluded])['top10_observed'] == 1

def test_incomplete_candidate_ids_are_unknown():
    assert module.replay({'qid': 'q', 'sources_at_10': [{}]}, 'v')['observed_top10_chunk_ids'] is None

"""Synthetic fixtures only: projection must not manufacture measurement evidence."""
import copy
from hashlib import sha256
import json

import pytest

from rsik.analytics.case_observations import project_quality_cases, project_replay_cases
from rsik.analytics.reader import digest


EVALUATION = 'e' * 64


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(data, ensure_ascii=False).encode()
    path.write_bytes(raw)
    return sha256(raw).hexdigest()


def quality(tmp_path, attempts=None):
    artifact = {'candidate_id': 'synthetic-candidate', 'workflow': {}}
    call = {'seq': 1, 'case_id': 'q', 'repeat_id': 1, 'attempt_id': 'attempt-1', 'attempt_number': 1,
            'request_status': 'RETURNED', 'usage': {'prompt_tokens': 5, 'completion_tokens': 2, 'total_tokens': 7},
            'context_status': 'VERIFIED_IN_ACTUAL_REQUEST', 'context_chars': 3, 'context_bytes': 6,
            'context_measurement_scope': 'normal_source_numbered_evidence', 'elapsed_seconds': .2,
            'request_file': 'DO-NOT-OPEN', 'generated_content': 'PRIVATE'}
    attempt = {'case_id': 'q', 'repeat_id': 1, 'attempt_id': 'attempt-1', 'attempt_number': 1,
               'status': 'RETURNED', 'provider_calls': [call], 'elapsed_seconds': .5,
               'final_context_chars': 3, 'final_context_bytes': 6, 'final_context_status': 'VERIFIED_IN_ACTUAL_REQUEST',
               'retrieval_config': {'top_k': 5}, 'answer': 'PRIVATE',
               'embedding_calls': [{'seq': 1, 'record': {k: call[k] for k in ('case_id', 'repeat_id', 'attempt_id', 'attempt_number')},
                   'actual_provider_requests': 0, 'request_status': 'CACHE_HIT'}]}
    if attempts is None:
        attempts = [attempt]
    raw = {'results': [{'case_id': 'q', 'repeat_id': 1, 'attempts': attempts,
                        'usage': {'prompt_tokens': 999}, 'answer': 'PRIVATE'}]}
    source_hash = write(tmp_path / 'product/results.json', raw)
    write(tmp_path / 'job.json', {'artifact': artifact})
    worker = {'run_id': 'synthetic-run', 'source_record': '/ecs/transferred/product/results.json', 'source_sha256': source_hash}
    worker_hash = write(tmp_path / 'worker.json', worker)
    args = (tmp_path / 'worker.json', worker_hash)
    kwargs = dict(candidate_id='synthetic-candidate', artifact_sha256=digest(artifact),
                  evaluation_sha256=EVALUATION, candidate_kind='synthetic')
    return args, kwargs, raw


def test_quality_exact_bytes_namespace_usage_and_no_private_bodies(tmp_path):
    args, kwargs, _ = quality(tmp_path)
    result = project_quality_cases(*args, **kwargs)
    row = result['rows'][0]
    assert row['qid'] == 'q' and row['attempt_id'] == 'attempt-1'
    assert row['provider_requests'][0]['usage']['prompt_tokens'] == 5
    assert row['provider_requests'][0]['namespace'] == 'provider'
    assert row['embedding_requests'][0]['namespace'] == 'embedding'
    assert row['embedding_requests'][0]['actual_provider_requests'] == 0
    assert row['embedding_requests'][0]['usage']['total_tokens'] == 'UNKNOWN'
    assert row['prompt_sha256'] == row['final_request_sha256'] == 'UNKNOWN'
    assert row['elapsed_scope'] == 'product_record_attempt'
    assert row['versions']['evaluation_scope'] == 'host_declared_version_only'
    assert 'PRIVATE' not in json.dumps(result) and 'DO-NOT-OPEN' not in json.dumps(result)
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        project_quality_cases(args[0], '0' * 64, **kwargs)
    kwargs['artifact_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='ARTIFACT_VERSION_MISMATCH'):
        project_quality_cases(*args, **kwargs)


def test_quality_duplicate_attempts_and_conflicts(tmp_path):
    args, kwargs, raw = quality(tmp_path)
    attempt = raw['results'][0]['attempts'][0]
    args, kwargs, _ = quality(tmp_path, [attempt, copy.deepcopy(attempt)])
    assert len(project_quality_cases(*args, **kwargs)['rows']) == 1
    changed = copy.deepcopy(attempt)
    changed['status'] = 'FAILED'
    args, kwargs, _ = quality(tmp_path, [attempt, changed])
    with pytest.raises(ValueError, match='CONFLICTING_IDENTITY'):
        project_quality_cases(*args, **kwargs)


def test_quality_unknown_identity_does_not_invent_legacy_attempt(tmp_path):
    args, kwargs, _ = quality(tmp_path, [{'case_id': 'q', 'repeat_id': 1, 'usage': {'prompt_tokens': 999}}])
    row = project_quality_cases(*args, **kwargs)['rows'][0]
    assert row['attempt_id'] == row['identity_status'] == 'UNKNOWN'
    assert row['stages']['case'] == 'UNKNOWN' and row['provider_requests'] == []
    assert row['context_chars'] == row['elapsed_seconds'] == 'UNKNOWN'


def test_quality_identity_and_source_hash_rejected(tmp_path):
    args, kwargs, _ = quality(tmp_path, [{'case_id': 'another', 'repeat_id': 1}])
    with pytest.raises(ValueError, match='IDENTITY_MISMATCH'):
        project_quality_cases(*args, **kwargs)
    (tmp_path / 'product/results.json').write_text('{}')
    with pytest.raises(ValueError, match='SOURCE_HASH_MISMATCH'):
        project_quality_cases(*args, **kwargs)


def test_quality_returned_retrieval_boundary_and_unknown_dispatch(tmp_path):
    _, _, raw = quality(tmp_path)
    attempt = raw['results'][0]['attempts'][0]
    attempt['provider_calls'][0].update(request_status='UNKNOWN', usage=None, provider_model='measured-model')
    attempt['retrieval_config']['rerank_enabled'] = False
    attempt['retrieval_calls'] = [{'search_mode': 'hybrid', 'candidate_ids': ['second', 'first'],
        'metrics': {'vector_count': 4, 'bm25_count': 3, 'merged_count': 5,
                    'total_latency_ms': 12.5, 'fallback_used': False}}]
    args, kwargs, _ = quality(tmp_path, [attempt])
    write(tmp_path / 'job.json', {'artifact': {'candidate_id': 'synthetic-candidate', 'workflow': {}},
        'cases': [{'id': 'q'}, {'id': 'missing'}], 'repeat': 1})
    result = project_quality_cases(*args, **kwargs)
    row = result['rows'][0]
    assert row['provider_requests'][0]['actual_provider_requests'] == 'UNKNOWN'
    assert row['provider_requests'][0]['provider_model'] == 'measured-model'
    retrieval = row['retrieval_calls'][0]
    assert retrieval['candidate_ids'] == ['second', 'first'] and retrieval['candidate_count'] == 2
    assert retrieval['metrics']['total_latency_ms'] == 12.5
    assert retrieval['configured_rerank_enabled'] is False
    assert set(retrieval['stage_memberships'].values()) == {'UNKNOWN'}
    assert set(retrieval['readiness'].values()) == {'UNKNOWN'}
    assert result['missing_planned_cases'] == [{'qid': 'missing', 'repeat_id': 1, 'attempt_id': 'UNKNOWN', 'status': 'UNKNOWN'}]


def replay(tmp_path):
    arms = {'A': {'candidate_id': 'parent', 'worker_sha256': 'a' * 64, 'sha256': 'c' * 64},
            'B': {'candidate_id': 'candidate', 'worker_sha256': 'b' * 64, 'sha256': 'd' * 64}}
    question = {'qid': 'q', 'pair_status': 'PAIRED', 'pair_identity': True, 'execution': 'COMPLETED',
                'upstream_hash': 'f' * 64, 'chars_A': 7, 'chars_B': 5,
                'parent_facts': {'f1': 'PRESENT'}, 'candidate_facts': {'f1': 'ABSENT'},
                'provenance_A': 'PRESENT', 'provenance_B': 'ABSENT'}
    decision = {'angle': 'P1_DEDUP', 'candidate_id': 'candidate', 'worker_hash': 'b' * 64,
                'config_hash': 'd' * 64, 'per_question': [question]}
    round_data = {'round_id': 'round', 'execution_state': 'COMPLETED', 'candidate_decisions': [decision]}
    manifest = {'run_id': 'replay-run', 'arms': arms, 'evaluation': {'sha256': EVALUATION}}
    raw = {'qid': 'q', 'run_id': 'replay-run', 'upstream_sha256': 'f' * 64, 'status': 'PAIRED',
           'arms': {arm: {'qid': 'q', 'run_id': 'replay-run', 'candidate_id': spec['candidate_id'],
                         'context_chars': question['chars_' + arm], 'context_utf8_bytes': question['chars_' + arm],
                         'request_calls': 1, 'receipt_path': 'DO-NOT-OPEN'} for arm, spec in arms.items()}}
    results = {'rows': [raw], 'summary': {'run_id': 'replay-run', 'answer_quality': 'NOT_RUN', 'token_usage': 'UNKNOWN'}}
    paths = [tmp_path / name for name in ('round.json', 'manifest.json', 'results.json')]
    hashes = [write(path, data) for path, data in zip(paths, (round_data, manifest, results))]
    return (paths[0], hashes[0], paths[1], hashes[1], paths[2], hashes[2]), dict(angle='P1_DEDUP', evaluation_sha256=EVALUATION), (round_data, manifest, results)


def test_replay_joins_metadata_but_interception_is_not_provider_usage(tmp_path):
    args, kwargs, _ = replay(tmp_path)
    rows = project_replay_cases(*args, **kwargs)['rows']
    assert len(rows) == 2
    assert [row['context_chars'] for row in rows] == [7, 5]
    assert rows[1]['facts'] == {'f1': 'ABSENT'}
    for row in rows:
        assert row['attempt_id'] == row['repeat_id'] == 'UNKNOWN'
        assert row['provider_requests'] == [] and row['provider_measurement_status'] == 'UNKNOWN'
        assert row['stages']['answer'] == 'NOT_RUN' and row['stages']['token'] == 'UNKNOWN'
        assert row['prompt_sha256'] == row['elapsed_seconds'] == 'UNKNOWN'
    assert 'DO-NOT-OPEN' not in json.dumps(rows)


@pytest.mark.parametrize('change', ['evaluation', 'worker', 'upstream', 'duplicate'])
def test_replay_rejects_version_join_conflict(tmp_path, change):
    args, kwargs, (round_data, manifest, results) = replay(tmp_path)
    args = list(args)
    if change == 'evaluation':
        manifest['evaluation']['sha256'] = '0' * 64
        args[3] = write(args[2], manifest)
    elif change == 'worker':
        manifest['arms']['B']['worker_sha256'] = '0' * 64
        args[3] = write(args[2], manifest)
    else:
        if change == 'upstream':
            results['rows'][0]['upstream_sha256'] = '0' * 64
        else:
            extra = copy.deepcopy(results['rows'][0])
            extra['status'] = 'FAILED'
            results['rows'].append(extra)
        args[5] = write(args[4], results)
    with pytest.raises(ValueError):
        project_replay_cases(*args, **kwargs)


def test_replay_parent_binding_and_original_duplicate_pointer(tmp_path):
    args, kwargs, (round_data, _, results) = replay(tmp_path)
    args = list(args)
    extra = copy.deepcopy(results['rows'][0])
    extra['qid'] = 'q2'
    for arm in extra['arms'].values():
        arm['qid'] = 'q2'
    results['rows'] = [results['rows'][0], copy.deepcopy(results['rows'][0]), extra]
    question = copy.deepcopy(round_data['candidate_decisions'][0]['per_question'][0])
    question['qid'] = 'q2'
    round_data['candidate_decisions'][0]['per_question'].append(question)
    args[1], args[5] = write(args[0], round_data), write(args[4], results)
    rows = project_replay_cases(*args, **kwargs)['rows']
    assert rows[-1]['sources'][-1]['pointer'] == '/rows/2/arms/B'
    round_data['parent_worker_sha256'] = '0' * 64
    args[1] = write(args[0], round_data)
    with pytest.raises(ValueError, match='PARENT_VERSION_MISMATCH'):
        project_replay_cases(*args, **kwargs)

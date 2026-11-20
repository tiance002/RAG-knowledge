"""Orchestration contracts using real registry artifacts and an injected executor."""
import copy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import rsi_quality_execute as executor
import rsi_quality_loop as loop
import rsi_quality_registry as registry
from rsi_candidates import canonical, read_json, sha


@pytest.fixture
def offline_executor(monkeypatch):
    calls = []

    def execute(artifact, inputs_path, output_dir, *, product_root, source_fingerprint, repeat=1):
        calls.append(copy.deepcopy(artifact))
        # Exercise the existing job schema rather than accepting an opaque fixture.
        job = executor.job_for(artifact, inputs_path, repeat, 0, 'http://127.0.0.1',
                               retrieval=artifact['retrieval'])
        assert len(job['cases']) == 1
        assert len(job['chunks']) == 3
        assert len(job['sources']) == 1
        assert all('synthetic' in chunk['content'].lower() for chunk in job['chunks'])
        consumer = {key: float(artifact['retrieval'][key]) for key in
                    ('score_threshold', 'vector_weight', 'bm25_weight')}
        consumer['top_k'] = int(artifact['retrieval']['top_k'])
        consumer['search_mode'] = str(artifact['retrieval']['search_mode'])
        consumer_hash = sha(canonical(consumer))
        return {'run_id': f'mock-offline-{len(calls)}', 'evidence_mode': 'OFFLINE_PROVIDER_STUB',
                'exit_code': 0, 'provider_stub_calls': 1,
                'retrieval_config_sha256': sha(canonical(artifact['retrieval'])),
                'actual_external_calls': 0, 'records': [
                    {'qid': job['cases'][0]['id'], 'repeat_id': 1,
                     'status': 'RETURNED', 'answer': 'ungraded synthetic answer',
                     'evidence_mode': 'OFFLINE_PROVIDER_STUB', 'actual_external_calls': 0,
                     'final_prompt_messages_sha256': 'a' * 64, 'actual_request_sha256': 'b' * 64,
                     'observed_top_k': consumer['top_k'], 'effective_config_sha256': consumer_hash,
                     'retrieval_observations': [{'effective_config': consumer,
                        'effective_config_sha256': consumer_hash, 'observed_top_k': consumer['top_k'],
                        'returned_ids': ['synthetic-chunk-1']}]}]}

    monkeypatch.setattr(executor, 'execute_controlled_artifact', execute, raising=False)
    for name in ('load_config', 'call_json', 'judge_input', 'metrics', 'recommendation'):
        def forbidden(*args, **kwargs):
            pytest.fail('Controlled loop must not invoke model or quality evaluation paths')
        monkeypatch.setattr(loop, name, forbidden)
    return calls, execute


def test_controlled_siblings_materialize_and_reuse_baseline(tmp_path, offline_executor):
    calls, _ = offline_executor
    product = tmp_path / 'product'
    product.mkdir()
    output = tmp_path / 'run'
    result = loop.execute_controlled_loop(output, product_root=product,
                                          source_fingerprint={'root': str(product), 'version': 'mock'})
    assert result['status'] == 'OFFLINE_CONTRACT_COMPLETE'
    assert result['actual_external_calls'] == 0
    assert len(calls) == 3
    assert calls[0] == registry.CONTROLLED_BASELINE
    assert [key for key in calls[0] if calls[0][key] != calls[1][key]] == ['prompt_addendum']
    assert [key for key in calls[0] if calls[0][key] != calls[2][key]] == ['retrieval']
    assert [key for key in calls[0]['retrieval']
            if calls[0]['retrieval'][key] != calls[2]['retrieval'][key]] == ['top_k']
    contract = read_json(output / 'fixture' / 'evaluation-offline-contract.json')
    assert contract == {'schema': 'rsi-f2-offline-evaluation/v1',
                        'answer_quality': 'NOT_RUN', 'token_usage': 'UNKNOWN'}
    for candidate in result['candidates']:
        manifest_path = Path(candidate['manifest']['path'])
        registry.verify_manifest(manifest_path)
        manifest = read_json(manifest_path)
        assert read_json(manifest_path.parent / manifest['arms']['B']['path']) == candidate['config']
        assert candidate['parent_id'] == result['parent_id']
        assert candidate['A']['status'] == 'REUSED'
        assert candidate['A']['results'] == result['baseline']['results']
        record = registry.candidate(output / 'registry', candidate['candidate_id'])
        observation = record['observation_source']['observation']
        core = {key: value for key, value in observation.items() if key != 'observation_id'}
        assert observation['observation_id'] == 'obs-' + sha(canonical(core))[:24]
        assert observation['evidence'][0] == {
            'path': 'results.json', 'field': 'rows[0]',
            'sha256': sha((output / 'baseline' / 'results.json').read_bytes())}
    assert read_json(output / 'baseline' / 'results.json')['rows'][0]['scores'] == []


@pytest.mark.parametrize('fault', ['missing', 'duplicate', 'extra', 'live', 'external', 'failed',
                                  'wrong_config', 'missing_prompt_hash', 'uncoerced_consumer_hash'])
def test_controlled_rejects_unsafe_or_incomplete_worker(tmp_path, monkeypatch, offline_executor, fault):
    calls, execute = offline_executor

    def bad_worker(*args, **kwargs):
        worker = execute(*args, **kwargs)
        if fault == 'missing':
            worker['records'] = []
        elif fault == 'duplicate':
            worker['records'] *= 2
        elif fault == 'extra':
            worker['records'].append({**worker['records'][0], 'qid': 'unexpected'})
        elif fault == 'live':
            worker['evidence_mode'] = 'LIVE'
        elif fault == 'external':
            worker['actual_external_calls'] = 1
        elif fault == 'wrong_config':
            worker['records'][0]['observed_top_k'] = 999
        elif fault == 'missing_prompt_hash':
            worker['records'][0]['final_prompt_messages_sha256'] = None
        elif fault == 'uncoerced_consumer_hash':
            observation = worker['records'][0]['retrieval_observations'][0]
            uncoerced_config = {**observation['effective_config'], 'score_threshold': 0}
            worker['records'][0]['effective_config_sha256'] = sha(canonical(uncoerced_config))
        else:
            worker['records'][0]['status'] = 'UNKNOWN'
        return worker

    monkeypatch.setattr(executor, 'execute_controlled_artifact', bad_worker)
    product, output = tmp_path / 'product', tmp_path / 'run'
    with pytest.raises(ValueError, match='worker evidence'):
        loop.execute_controlled_loop(output, product_root=product, source_fingerprint={'root': str(product)})
    assert len(calls) == 1
    assert read_json(output / 'loop-result.json')['status'] == 'STOPPED'
    assert not (output / 'prompt').exists()
    assert (output / 'baseline' / 'rejected-worker.json').is_file()


@pytest.mark.parametrize('options', [{'prompt_addendum': ''}, {'top_k': 10}, {'top_k': True}])
def test_noop_or_invalid_candidate_stops_before_worker(tmp_path, offline_executor, options):
    calls, _ = offline_executor
    product, output = tmp_path / 'product', tmp_path / 'run'
    with pytest.raises(ValueError):
        loop.execute_controlled_loop(output, product_root=product,
                                     source_fingerprint={'root': str(product)}, **options)
    assert not calls
    assert not output.exists()


def test_candidate_failure_keeps_baseline_and_stops_other_sibling(tmp_path, monkeypatch, offline_executor):
    calls, execute = offline_executor

    def fail_candidate(*args, **kwargs):
        worker = execute(*args, **kwargs)
        if len(calls) == 2:
            worker['records'] = []
        return worker

    monkeypatch.setattr(executor, 'execute_controlled_artifact', fail_candidate)
    product, output = tmp_path / 'product', tmp_path / 'run'
    with pytest.raises(ValueError, match='worker evidence'):
        loop.execute_controlled_loop(output, product_root=product, source_fingerprint={'root': str(product)})
    state = read_json(output / 'loop-result.json')
    assert state['status'] == 'STOPPED'
    assert len(calls) == 2
    assert not state['candidates']
    assert state['baseline']['status'] == 'OBSERVED_UNGRADED'
    assert (output / 'baseline' / 'results.json').is_file()
    assert not (output / 'parameter').exists()
    baseline_bytes = (output / 'baseline' / 'results.json').read_bytes()
    with pytest.raises(ValueError, match='must be new'):
        loop.execute_controlled_loop(output, product_root=product, source_fingerprint={'root': str(product)})
    assert (output / 'baseline' / 'results.json').read_bytes() == baseline_bytes

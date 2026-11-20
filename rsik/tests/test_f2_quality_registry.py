"""Offline registry contracts: typed mutations, immutable lineage, and v1 compatibility."""
import copy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
import rsi_quality_registry as registry
from rsi_candidates import canonical, get, put, sha


def make_baseline(tmp_path, profile=registry.CONTROLLED_PROFILE):
    cases = tmp_path / 'cases.json'
    cases.write_bytes(canonical([{'qid': 'q1', 'question': 'offline fixture'}]))
    evaluation = tmp_path / (registry.EVALUATION_CONTRACT_NAME if profile else 'evaluation.json')
    evaluation.write_bytes(canonical(registry.EVALUATION_CONTRACT if profile else {'legacy': True}))
    store = tmp_path / 'registry'
    baseline = registry.register_baseline(store, {'fixture': 'f2'}, cases, evaluation, profile=profile)
    return store, baseline


def observation(tmp_path, store, parent_id, name):
    parent = registry.candidate(store, parent_id)
    profile = parent.get('profile')
    run = tmp_path / name
    run.mkdir()
    manifest = {key: parent[key] for key in ('source_fingerprint', 'fixed', 'cases', 'evaluation')}
    manifest.update(schema=registry.CONTROLLED_SCHEMA if profile else registry.SCHEMA,
                    arms={'A': {'candidate_id': parent_id, 'sha256': parent['artifact']}})
    if profile:
        manifest.update(profile=profile, execution={'evidence_mode': registry.OFFLINE_EVIDENCE_MODE})
    results = canonical({'summary': {'run_id': name}, 'rows': [{'qid': 'q1'}]})
    obs = {'run_id': name, 'qid': 'q1', 'evidence': [
        {'path': 'results.json', 'sha256': sha(results), 'field': 'rows[0]'}]}
    observation_id = 'obs-' + sha(canonical(obs))[:24]
    obs['observation_id'] = observation_id
    (run / 'manifest.json').write_bytes(canonical(manifest))
    (run / 'results.json').write_bytes(results)
    (run / 'feedback.json').write_bytes(canonical({'run_id': name, 'observations': [obs]}))
    return run, observation_id


def derive(tmp_path, store, parent, scope, artifact, name='source'):
    run, oid = observation(tmp_path, store, parent, name)
    return registry.derive(store, parent, scope, artifact,
                           {key: 'synthetic rationale' for key in registry.NOTES}, run, oid)


def test_v2_full_lineage_and_tamper(tmp_path):
    store, baseline = make_baseline(tmp_path)
    artifact = copy.deepcopy(registry.CONTROLLED_BASELINE)
    artifact['prompt_addendum'] = 'Answer from evidence.'
    prompt = derive(tmp_path, store, baseline, 'prompt', artifact)
    first = registry.materialize(store, baseline, prompt, tmp_path / 'first')
    assert registry.verify_manifest(first)['manifest']['profile'] == registry.CONTROLLED_PROFILE
    artifact['retrieval']['top_k'] = 8
    parameter = derive(tmp_path, store, prompt, 'parameter', artifact, 'second-source')
    manifest_path = registry.materialize(store, prompt, parameter, tmp_path / 'second')
    verified = registry.verify_manifest(manifest_path)
    assert len(verified['manifest']['lineage']) == 3
    assert verified['artifacts']['B']['retrieval']['top_k'] == 8
    assert verified['manifest']['execution']['evidence_mode'] == 'OFFLINE_PROVIDER_STUB'
    (manifest_path.parent / 'B.json').write_bytes(canonical(registry.CONTROLLED_BASELINE))
    with pytest.raises(ValueError, match='fingerprint'):
        registry.verify_manifest(manifest_path)


@pytest.mark.parametrize('mutate', [
    lambda a: a.update(memory_addendum='memory'),
    lambda a: a['workflow'].update(mode='agent'),
    lambda a: a.update(risk='low'),
    lambda a: a['retrieval'].update(extra=True),
    lambda a: a['retrieval'].update(top_k=True),
    lambda a: a['retrieval'].update(top_k=0),
    lambda a: a['retrieval'].update(top_k=101),
    lambda a: a['retrieval'].update(top_k=2.0),
    lambda a: a['retrieval'].update(rerank_enabled=0),
    lambda a: a['retrieval'].update(score_threshold=False),
    lambda a: a['retrieval'].update(vector_weight=0.8),
])
def test_v2_rejects_uncontrolled_artifacts(mutate):
    artifact = copy.deepcopy(registry.CONTROLLED_BASELINE)
    mutate(artifact)
    with pytest.raises(ValueError):
        registry.validate_artifact(artifact, profile=registry.CONTROLLED_PROFILE)


@pytest.mark.parametrize('scope,mixed', [('memory', False), ('workflow', False),
                                          ('prompt', False), ('parameter', True)])
def test_v2_rejects_wrong_or_mixed_scope(tmp_path, scope, mixed):
    store, baseline = make_baseline(tmp_path)
    artifact = copy.deepcopy(registry.CONTROLLED_BASELINE)
    artifact['retrieval']['top_k'] = 9
    if mixed:
        artifact['prompt_addendum'] = 'mixed change'
    with pytest.raises(ValueError, match='scope'):
        derive(tmp_path, store, baseline, scope, artifact)


def test_v1_read_and_hash_stability(tmp_path):
    store, baseline = make_baseline(tmp_path, profile=None)
    record = registry.candidate(store, baseline)
    assert record['artifact'] == sha(canonical(registry.BASELINE))
    assert 'profile' not in record and record['type'] == registry.CANDIDATE
    artifact = copy.deepcopy(registry.BASELINE)
    artifact['memory_addendum'] = 'legacy memory remains supported'
    child = derive(tmp_path, store, baseline, 'memory', artifact)
    manifest_path = registry.materialize(store, baseline, child, tmp_path / 'legacy')
    verified = registry.verify_manifest(manifest_path)
    assert verified['manifest']['schema'] == registry.SCHEMA
    assert 'profile' not in verified['manifest']


def test_v2_evaluation_name_checked_before_open(tmp_path, monkeypatch):
    store, baseline = make_baseline(tmp_path)
    value = json.loads(get(store, baseline))
    value['evaluation']['path'] = str(tmp_path / 'secret-evaluation.json')
    candidate_id = put(store, canonical(value))
    opened = []
    original = Path.read_bytes
    def read(path):
        opened.append(path.name)
        return original(path)
    monkeypatch.setattr(Path, 'read_bytes', read)
    with pytest.raises(ValueError, match='allowlisted'):
        registry.candidate(store, candidate_id)
    assert 'secret-evaluation.json' not in opened


def test_v2_evaluation_hash_before_parse(tmp_path):
    store, baseline = make_baseline(tmp_path)
    (tmp_path / registry.EVALUATION_CONTRACT_NAME).write_text('not JSON')
    with pytest.raises(ValueError, match='fingerprint'):
        registry.candidate(store, baseline)


@pytest.mark.parametrize('mutation', ['v1-type', 'missing-profile', 'risk-field'])
def test_v2_rejects_mixed_record_version_and_extra_fields(tmp_path, mutation):
    store, baseline = make_baseline(tmp_path)
    value = json.loads(get(store, baseline))
    if mutation == 'v1-type':
        value['type'] = registry.CANDIDATE
    elif mutation == 'missing-profile':
        del value['profile']
    else:
        value['risk'] = 'low'
    with pytest.raises(ValueError):
        registry.candidate(store, put(store, canonical(value)))


def test_v2_rejects_non_normative_evaluation(tmp_path):
    cases = tmp_path / 'cases.json'
    cases.write_text('[]')
    evaluation = tmp_path / registry.EVALUATION_CONTRACT_NAME
    evaluation.write_bytes(canonical({**registry.EVALUATION_CONTRACT, 'answer_quality': 'PASS'}))
    with pytest.raises(ValueError, match='evaluation contract'):
        registry.register_baseline(tmp_path / 'registry', {'fixture': 'f2'}, cases, evaluation,
                                   profile=registry.CONTROLLED_PROFILE)


def test_v2_manifest_rejects_live_execution_and_missing_profile(tmp_path):
    store, baseline = make_baseline(tmp_path)
    artifact = copy.deepcopy(registry.CONTROLLED_BASELINE)
    artifact['retrieval']['top_k'] = 7
    child = derive(tmp_path, store, baseline, 'parameter', artifact)
    with pytest.raises(ValueError, match='offline provider stub'):
        registry.materialize(store, baseline, child, tmp_path / 'live',
                             execution={'evidence_mode': 'LIVE'})
    manifest_path = registry.materialize(store, baseline, child, tmp_path / 'run')
    manifest = json.loads(manifest_path.read_bytes())
    manifest['execution']['evidence_mode'] = 'LIVE'
    manifest_path.write_bytes(canonical(manifest))
    with pytest.raises(ValueError, match='offline provider stub'):
        registry.verify_manifest(manifest_path)
    manifest['execution']['evidence_mode'] = registry.OFFLINE_EVIDENCE_MODE
    del manifest['profile']
    manifest_path.write_bytes(canonical(manifest))
    with pytest.raises(ValueError, match='version/profile'):
        registry.verify_manifest(manifest_path)


def test_v2_archive_tamper_is_detected(tmp_path):
    store, baseline = make_baseline(tmp_path)
    artifact = copy.deepcopy(registry.CONTROLLED_BASELINE)
    artifact['prompt_addendum'] = 'evidence only'
    child = derive(tmp_path, store, baseline, 'prompt', artifact)
    manifest_path = registry.materialize(store, baseline, child, tmp_path / 'run')
    record = registry.candidate(store, child)
    archived_results = record['observation_source']['archive_results']
    (manifest_path.parent / 'lineage' / archived_results).write_bytes(b'{}')
    with pytest.raises(ValueError, match='fingerprint'):
        registry.verify_manifest(manifest_path)

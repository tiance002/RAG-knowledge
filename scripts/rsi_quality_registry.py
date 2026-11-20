"""Typed, non-code quality candidates over the existing immutable RSI object store.

Frozen evaluation inputs stay outside candidate artifacts. Materialization references
them read-only and copies only small candidate and lineage objects.
"""
import copy
import json
from pathlib import Path

from rsi_candidates import canonical, get, put, read_json, sha

SCHEMA = 'rsi-quality-experiment/v1'
CANDIDATE = 'rsi-quality-candidate/v1'
BASELINE = {'prompt_addendum': '', 'memory_addendum': '',
            'workflow': {'mode': 'chat', 'use_rewrite': False}}
SCOPES = {'prompt': 'prompt_addendum', 'memory': 'memory_addendum', 'workflow': 'workflow'}
CONTROLLED_PROFILE = 'rsi-f2-offline/v1'
CONTROLLED_SCHEMA = 'rsi-quality-experiment/v2'
CONTROLLED_CANDIDATE = 'rsi-quality-candidate/v2'
CONTROLLED_BASELINE = {**copy.deepcopy(BASELINE), 'retrieval': {
    'search_mode': 'fulltext', 'top_k': 10, 'context_window': 0,
    'rerank_enabled': False, 'score_threshold': 0,
    'vector_weight': 0.7, 'bm25_weight': 0.3}}
EVALUATION_CONTRACT_NAME = 'evaluation-offline-contract.json'
EVALUATION_CONTRACT = {'schema': 'rsi-f2-offline-evaluation/v1',
                       'answer_quality': 'NOT_RUN', 'token_usage': 'UNKNOWN'}
OFFLINE_EVIDENCE_MODE = 'OFFLINE_PROVIDER_STUB'
NOTES = ('observation', 'hypothesis', 'change', 'impact', 'falsification')


def _profile(value):
    if value not in (None, CONTROLLED_PROFILE):
        raise ValueError('unsupported quality profile')
    return value


def _record_profile(value):
    profile = value.get('profile')
    _profile(profile)
    expected = CONTROLLED_CANDIDATE if profile else CANDIDATE
    if value.get('type') != expected or (profile is None and 'profile' in value):
        raise ValueError('mixed quality candidate version/profile')
    if profile:
        fields = {'type', 'profile', 'parent', 'origin', 'scope', 'artifact',
                  'source_fingerprint', 'source_sha256', 'fixed', 'cases', 'evaluation'}
        if value.get('parent') is not None:
            fields |= {'notes', 'observation_source'}
        if set(value) != fields:
            raise ValueError('unsupported controlled candidate fields')
    return profile


def _manifest_profile(value):
    profile = value.get('profile')
    _profile(profile)
    expected = CONTROLLED_SCHEMA if profile else SCHEMA
    if value.get('schema') != expected or (profile is None and 'profile' in value):
        raise ValueError('mixed quality manifest version/profile')
    if profile and value.get('execution', {}).get('evidence_mode') != OFFLINE_EVIDENCE_MODE:
        raise ValueError('controlled execution must use offline provider stub')
    return profile


def _validate_change(original, artifact, scope, profile):
    if profile:
        changed = [key for key in original if original[key] != artifact[key]]
        valid = (scope == 'prompt' and changed == ['prompt_addendum'])
        if scope == 'parameter' and changed == ['retrieval']:
            valid = [key for key in original['retrieval']
                     if original['retrieval'][key] != artifact['retrieval'][key]] == ['top_k']
    else:
        valid = scope in SCOPES and [key for key in original if original[key] != artifact[key]] == [SCOPES[scope]]
    if not valid:
        raise ValueError('candidate must change exactly its named scope')


def validate_artifact(value, *, profile=None):
    _profile(profile)
    baseline = CONTROLLED_BASELINE if profile else BASELINE
    if not isinstance(value, dict) or set(value) != set(baseline):
        raise ValueError('unsupported quality artifact fields')
    for field in ('prompt_addendum', 'memory_addendum'):
        if not isinstance(value[field], str) or len(value[field]) > 6000:
            raise ValueError('invalid quality text addendum')
    workflow = value['workflow']
    if (not isinstance(workflow, dict) or set(workflow) != {'mode', 'use_rewrite'} or
            workflow['mode'] not in ('chat', 'agent') or type(workflow['use_rewrite']) is not bool):
        raise ValueError('unsupported workflow branch')
    if profile:
        if value['memory_addendum'] != '' or workflow != BASELINE['workflow']:
            raise ValueError('controlled memory and workflow are frozen')
        retrieval = value['retrieval']
        if not isinstance(retrieval, dict) or set(retrieval) != set(baseline['retrieval']):
            raise ValueError('unsupported retrieval fields')
        if type(retrieval['top_k']) is not int or not 1 <= retrieval['top_k'] <= 100:
            raise ValueError('top_k must be an integer in 1..100')
        for key, expected in baseline['retrieval'].items():
            if key != 'top_k' and (type(retrieval[key]) is not type(expected) or retrieval[key] != expected):
                raise ValueError('controlled retrieval field is frozen: ' + key)
    return value


def _read_evaluation(ref, profile):
    # Check the allowlisted contract name before opening any evaluation file.
    if profile and Path(ref['path']).name != EVALUATION_CONTRACT_NAME:
        raise ValueError('controlled evaluation filename is not allowlisted')
    if not profile:
        return read_ref(ref)
    path = Path(ref['path'])
    if not path.is_absolute():
        raise ValueError('frozen input fingerprint mismatch')
    data = path.read_bytes()
    if sha(data) != ref['sha256']:
        raise ValueError('frozen input fingerprint mismatch')
    value = json.loads(data.decode('utf-8-sig'))
    if value != EVALUATION_CONTRACT:
        raise ValueError('invalid offline evaluation contract')
    return value


def frozen_ref(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError('frozen input is not a file')
    return {'path': str(path), 'sha256': sha(path.read_bytes())}


def read_ref(ref):
    path = Path(ref['path'])
    if not path.is_absolute() or sha(path.read_bytes()) != ref['sha256']:
        raise ValueError('frozen input fingerprint mismatch')
    return read_json(path)


def register_baseline(registry, source_fingerprint, cases_path, evaluation_path, *, fixed=None, profile=None):
    if not isinstance(source_fingerprint, dict) or not source_fingerprint:
        raise ValueError('source fingerprint must be a nonempty mapping')
    _profile(profile)
    evaluation = frozen_ref(evaluation_path) if not profile else None
    if profile:
        if Path(evaluation_path).name != EVALUATION_CONTRACT_NAME:
            raise ValueError('controlled evaluation filename is not allowlisted')
        evaluation = frozen_ref(evaluation_path)
        _read_evaluation(evaluation, profile)
    value = {'type': CONTROLLED_CANDIDATE if profile else CANDIDATE, 'parent': None, 'origin': 'baseline', 'scope': 'baseline',
             'artifact': put(Path(registry), canonical(CONTROLLED_BASELINE if profile else BASELINE)),
             'source_fingerprint': source_fingerprint,
             'source_sha256': sha(canonical(source_fingerprint)), 'fixed': fixed or {},
             'cases': frozen_ref(cases_path), 'evaluation': evaluation}
    if profile:
        value['profile'] = profile
    return put(Path(registry), canonical(value))


def candidate(registry, key):
    registry = Path(registry)
    value = json.loads(get(registry, key))
    profile = _record_profile(value)
    validate_artifact(json.loads(get(registry, value['artifact'])), profile=profile)
    if sha(canonical(value['source_fingerprint'])) != value['source_sha256']:
        raise ValueError('source fingerprint mismatch')
    read_ref(value['cases'])
    _read_evaluation(value['evaluation'], profile)
    if value['parent']:
        parent_data = get(registry, value['parent'])
        if profile and _record_profile(json.loads(parent_data)) != profile:
            raise ValueError('mixed quality lineage profiles')
    return value


def _observation(registry, parent_id, parent, from_run, observation_id):
    run = Path(from_run).resolve()
    archived = {name: (run / (name + '.json')).read_bytes()
                for name in ('manifest', 'results', 'feedback')}
    source = _bind_observation(parent_id, parent, archived, observation_id)
    return {**source, **{'archive_' + k: put(registry, v) for k, v in archived.items()}}


def _bind_observation(parent_id, parent, archived, observation_id):
    manifest, results, feedback = [json.loads(archived[k].decode('utf-8-sig'))
                                   for k in ('manifest', 'results', 'feedback')]
    profile = _record_profile(parent)
    if (_manifest_profile(manifest) != profile or manifest.get('source_fingerprint') != parent['source_fingerprint'] or
            manifest.get('fixed') != parent['fixed'] or
            any(manifest.get(k) != parent[k] for k in ('cases', 'evaluation'))):
        raise ValueError('observation frozen provenance differs from parent')
    arms = [a for a in manifest['arms'].values() if a.get('candidate_id') == parent_id]
    if len(arms) != 1 or arms[0].get('sha256') != parent['artifact']:
        raise ValueError('observation does not evaluate parent artifact')
    run_id = results['summary']['run_id']
    matches = [o for o in feedback.get('observations', []) if o.get('observation_id') == observation_id]
    if len(matches) != 1 or feedback.get('run_id') != run_id:
        raise ValueError('observation missing or ambiguous')
    obs = matches[0]
    core = {k: v for k, v in obs.items() if k != 'observation_id'}
    if obs.get('run_id') != run_id or 'obs-' + sha(canonical(core))[:24] != observation_id:
        raise ValueError('observation identity mismatch')
    matches = [i for i, row in enumerate(results['rows']) if row.get('qid') == obs.get('qid')]
    refs = [r for r in obs.get('evidence', []) if r.get('path') == 'results.json']
    if (len(matches) != 1 or len(refs) != 1 or refs[0].get('sha256') != sha(archived['results']) or
            refs[0].get('field') != f'rows[{matches[0]}]'):
        raise ValueError('observation row binding mismatch')
    return {'run_id': run_id, 'qid': obs['qid'], 'observation_id': observation_id, 'observation': obs}


def derive(registry, parent_id, scope, artifact, notes, from_run, observation_id):
    registry = Path(registry)
    parent = candidate(registry, parent_id)
    profile = _record_profile(parent)
    artifact = validate_artifact(copy.deepcopy(artifact), profile=profile)
    original = json.loads(get(registry, parent['artifact']))
    _validate_change(original, artifact, scope, profile)
    if any(not isinstance(notes.get(k), str) or not notes[k].strip() for k in NOTES):
        raise ValueError('structured rationale required')
    source = _observation(registry, parent_id, parent, from_run, observation_id)
    value = {**parent, 'parent': parent_id, 'origin': 'derived', 'scope': scope,
             'artifact': put(registry, canonical(artifact)),
             'notes': {k: notes[k] for k in NOTES}, 'observation_source': source}
    return put(registry, canonical(value))


def materialize(registry, parent_id, child_id, out, *, execution=None):
    registry, out = Path(registry), Path(out).resolve()
    parent, child = candidate(registry, parent_id), candidate(registry, child_id)
    profile = _record_profile(parent)
    if _record_profile(child) != profile:
        raise ValueError('mixed quality lineage profiles')
    execution = copy.deepcopy(execution or {})
    if profile:
        execution.setdefault('evidence_mode', OFFLINE_EVIDENCE_MODE)
        if execution['evidence_mode'] != OFFLINE_EVIDENCE_MODE:
            raise ValueError('controlled execution must use offline provider stub')
    if child['parent'] != parent_id:
        raise ValueError('child is not derived from parent')
    if any(parent[k] != child[k] for k in ('source_fingerprint', 'source_sha256', 'fixed', 'cases', 'evaluation')):
        raise ValueError('candidate frozen provenance differs')
    out.mkdir(parents=True, exist_ok=False)
    manifest = {'schema': CONTROLLED_SCHEMA if profile else SCHEMA, 'experiment_id': 'quality-' + child_id[:16],
                'source_fingerprint': parent['source_fingerprint'], 'source_sha256': parent['source_sha256'],
                'fixed': parent['fixed'], 'cases': parent['cases'], 'evaluation': parent['evaluation'],
                'execution': execution or {}, 'arms': {}, 'lineage': {}}
    if profile:
        manifest['profile'] = profile
    for label, cid, value in (('A', parent_id, parent), ('B', child_id, child)):
        artifact_name, record_name = label + '.json', label + '_candidate.json'
        (out / artifact_name).write_bytes(get(registry, value['artifact']))
        (out / record_name).write_bytes(get(registry, cid))
        manifest['arms'][label] = {'candidate_id': cid, 'parent_id': value['parent'],
            'path': artifact_name, 'sha256': value['artifact'],
            'record_path': record_name, 'record_sha256': cid}
    # Preserve the whole single-parent chain and each referenced source archive.
    todo = [child_id]
    while todo:
        cid = todo.pop()
        if cid in manifest['lineage']:
            continue
        value = candidate(registry, cid)
        keys = [cid, value['artifact'], *[v for k, v in value.get('observation_source', {}).items() if k.startswith('archive_')]]
        for key in keys:
            path = out / 'lineage' / key
            path.parent.mkdir(exist_ok=True)
            if not path.exists():
                path.write_bytes(get(registry, key))
        manifest['lineage'][cid] = 'lineage/' + cid
        if value['parent']:
            todo.append(value['parent'])
    (out / 'manifest.json').write_bytes(canonical(manifest))
    verify_manifest(out / 'manifest.json')
    return out / 'manifest.json'


def verify_manifest(manifest_path):
    """Verify immutable materialization before any product execution."""
    manifest_path = Path(manifest_path).resolve()
    base, manifest = manifest_path.parent, read_json(manifest_path)
    profile = _manifest_profile(manifest)
    if set(manifest['arms']) != {'A', 'B'}:
        raise ValueError('unsupported quality manifest')
    if sha(canonical(manifest['source_fingerprint'])) != manifest['source_sha256']:
        raise ValueError('manifest source fingerprint mismatch')
    read_ref(manifest['cases'])
    _read_evaluation(manifest['evaluation'], profile)
    def local_bytes(name, digest):
        path = (base / name).resolve()
        if not path.is_relative_to(base) or sha(path.read_bytes()) != digest:
            raise ValueError('materialized object fingerprint mismatch')
        return path.read_bytes()
    def local(name, digest):
        return json.loads(local_bytes(name, digest).decode('utf-8-sig'))
    artifacts, records = {}, {}
    for label, arm in manifest['arms'].items():
        if arm['candidate_id'] != arm['record_sha256']:
            raise ValueError('candidate identity mismatch')
        record = local(arm['record_path'], arm['record_sha256'])
        artifact = validate_artifact(local(arm['path'], arm['sha256']), profile=profile)
        if (_record_profile(record) != profile or record['parent'] != arm['parent_id'] or
                record['artifact'] != arm['sha256'] or
                any(record[k] != manifest[k] for k in ('source_fingerprint', 'source_sha256', 'fixed', 'cases', 'evaluation'))):
            raise ValueError('candidate provenance mismatch')
        artifacts[label], records[label] = artifact, record
    a, b = records['A'], records['B']
    if b['parent'] != manifest['arms']['A']['candidate_id'] or b['origin'] != 'derived':
        raise ValueError('candidate parent mismatch')
    scope = b['scope']
    _validate_change(artifacts['A'], artifacts['B'], scope, profile)
    lineage = {}
    for cid, name in manifest['lineage'].items():
        rec = local(name, cid)
        if _record_profile(rec) != profile:
            raise ValueError('mixed lineage profile')
        validate_artifact(local('lineage/' + rec['artifact'], rec['artifact']), profile=profile)
        for k, digest in rec.get('observation_source', {}).items():
            if k.startswith('archive_'):
                local('lineage/' + digest, digest)
        if rec['parent'] and rec['parent'] not in manifest['lineage']:
            raise ValueError('missing lineage ancestor')
        lineage[cid] = rec
    if any(arm['candidate_id'] not in manifest['lineage'] for arm in manifest['arms'].values()):
        raise ValueError('missing arm lineage')
    visited, current = set(), manifest['arms']['B']['candidate_id']
    while current:
        if current in visited:
            raise ValueError('cyclic candidate lineage')
        visited.add(current)
        rec = lineage[current]
        artifact = local('lineage/' + rec['artifact'], rec['artifact'])
        if any(rec[k] != manifest[k] for k in ('source_fingerprint', 'source_sha256', 'fixed', 'cases', 'evaluation')):
            raise ValueError('lineage provenance mismatch')
        if rec['parent'] is None:
            if artifact != (CONTROLLED_BASELINE if profile else BASELINE) or rec['origin'] != 'baseline' or rec['scope'] != 'baseline':
                raise ValueError('invalid root baseline')
        else:
            parent = lineage[rec['parent']]
            original = local('lineage/' + parent['artifact'], parent['artifact'])
            if rec['origin'] != 'derived':
                raise ValueError('invalid ancestor origin')
            _validate_change(original, artifact, rec['scope'], profile)
            if any(not isinstance(rec.get('notes', {}).get(k), str) or not rec['notes'][k].strip() for k in NOTES):
                raise ValueError('missing ancestor rationale')
            source = rec['observation_source']
            archived = {name: local_bytes('lineage/' + source['archive_' + name], source['archive_' + name])
                        for name in ('manifest', 'results', 'feedback')}
            bound = _bind_observation(rec['parent'], parent, archived, source['observation_id'])
            if any(source.get(k) != v for k, v in bound.items()):
                raise ValueError('lineage observation binding mismatch')
        current = rec['parent']
    if visited != set(lineage):
        raise ValueError('unrelated lineage candidate')
    return {'manifest': manifest, 'artifacts': artifacts, 'records': records}

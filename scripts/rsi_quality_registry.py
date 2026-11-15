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
NOTES = ('observation', 'hypothesis', 'change', 'impact', 'falsification')


def validate_artifact(value):
    if not isinstance(value, dict) or set(value) != set(BASELINE):
        raise ValueError('unsupported quality artifact fields')
    for field in ('prompt_addendum', 'memory_addendum'):
        if not isinstance(value[field], str) or len(value[field]) > 6000:
            raise ValueError('invalid quality text addendum')
    workflow = value['workflow']
    if (not isinstance(workflow, dict) or set(workflow) != {'mode', 'use_rewrite'} or
            workflow['mode'] not in ('chat', 'agent') or type(workflow['use_rewrite']) is not bool):
        raise ValueError('unsupported workflow branch')
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


def register_baseline(registry, source_fingerprint, cases_path, evaluation_path, *, fixed=None):
    if not isinstance(source_fingerprint, dict) or not source_fingerprint:
        raise ValueError('source fingerprint must be a nonempty mapping')
    value = {'type': CANDIDATE, 'parent': None, 'origin': 'baseline', 'scope': 'baseline',
             'artifact': put(Path(registry), canonical(BASELINE)),
             'source_fingerprint': source_fingerprint,
             'source_sha256': sha(canonical(source_fingerprint)), 'fixed': fixed or {},
             'cases': frozen_ref(cases_path), 'evaluation': frozen_ref(evaluation_path)}
    return put(Path(registry), canonical(value))


def candidate(registry, key):
    registry = Path(registry)
    value = json.loads(get(registry, key))
    if value.get('type') != CANDIDATE:
        raise ValueError('not a quality candidate')
    validate_artifact(json.loads(get(registry, value['artifact'])))
    if sha(canonical(value['source_fingerprint'])) != value['source_sha256']:
        raise ValueError('source fingerprint mismatch')
    for field in ('cases', 'evaluation'):
        read_ref(value[field])
    if value['parent']:
        get(registry, value['parent'])
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
    if (manifest.get('schema') != SCHEMA or manifest.get('source_fingerprint') != parent['source_fingerprint'] or
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
    artifact = validate_artifact(copy.deepcopy(artifact))
    original = json.loads(get(registry, parent['artifact']))
    if scope not in SCOPES or [k for k in original if original[k] != artifact[k]] != [SCOPES[scope]]:
        raise ValueError('child must change exactly its named scope')
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
    if child['parent'] != parent_id:
        raise ValueError('child is not derived from parent')
    if any(parent[k] != child[k] for k in ('source_fingerprint', 'source_sha256', 'fixed', 'cases', 'evaluation')):
        raise ValueError('candidate frozen provenance differs')
    out.mkdir(parents=True, exist_ok=False)
    manifest = {'schema': SCHEMA, 'experiment_id': 'quality-' + child_id[:16],
                'source_fingerprint': parent['source_fingerprint'], 'source_sha256': parent['source_sha256'],
                'fixed': parent['fixed'], 'cases': parent['cases'], 'evaluation': parent['evaluation'],
                'execution': execution or {}, 'arms': {}, 'lineage': {}}
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
    if manifest.get('schema') != SCHEMA or set(manifest['arms']) != {'A', 'B'}:
        raise ValueError('unsupported quality manifest')
    if sha(canonical(manifest['source_fingerprint'])) != manifest['source_sha256']:
        raise ValueError('manifest source fingerprint mismatch')
    for field in ('cases', 'evaluation'):
        read_ref(manifest[field])
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
        artifact = validate_artifact(local(arm['path'], arm['sha256']))
        if (record.get('type') != CANDIDATE or record['parent'] != arm['parent_id'] or
                record['artifact'] != arm['sha256'] or
                any(record[k] != manifest[k] for k in ('source_fingerprint', 'source_sha256', 'fixed', 'cases', 'evaluation'))):
            raise ValueError('candidate provenance mismatch')
        artifacts[label], records[label] = artifact, record
    a, b = records['A'], records['B']
    if b['parent'] != manifest['arms']['A']['candidate_id'] or b['origin'] != 'derived':
        raise ValueError('candidate parent mismatch')
    scope = b['scope']
    if scope not in SCOPES or [k for k in BASELINE if artifacts['A'][k] != artifacts['B'][k]] != [SCOPES[scope]]:
        raise ValueError('candidate changes outside named scope')
    lineage = {}
    for cid, name in manifest['lineage'].items():
        rec = local(name, cid)
        if rec.get('type') != CANDIDATE:
            raise ValueError('invalid lineage candidate')
        validate_artifact(local('lineage/' + rec['artifact'], rec['artifact']))
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
            if artifact != BASELINE or rec['origin'] != 'baseline' or rec['scope'] != 'baseline':
                raise ValueError('invalid root baseline')
        else:
            parent = lineage[rec['parent']]
            original = local('lineage/' + parent['artifact'], parent['artifact'])
            if (rec['origin'] != 'derived' or rec['scope'] not in SCOPES or
                    [k for k in BASELINE if original[k] != artifact[k]] != [SCOPES[rec['scope']]]):
                raise ValueError('invalid ancestor scope')
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

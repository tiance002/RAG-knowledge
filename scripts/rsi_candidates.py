"""Immutable, content-addressed candidate management; execution belongs to run_rsi_offline.py."""
import argparse
import ast
import hashlib
import json
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode('utf-8')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def put(registry, data):
    key = sha(data)
    path = registry / 'objects' / key
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open('xb') as f:
            f.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError('corrupt existing object')
    return key


def get(registry, key):
    if len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        raise ValueError('invalid object digest')
    data = (registry / 'objects' / key).read_bytes()
    if sha(data) != key:
        raise ValueError('object hash mismatch')
    return data


def record(registry, value):
    return put(registry, canonical(value))


def candidate(registry, key):
    value = json.loads(get(registry, key))
    if value.get('type') != 'rsi-candidate-v1':
        raise ValueError('not a candidate')
    for field in ('worker', 'config', 'template', 'cases', 'evaluation'):
        get(registry, value[field])
    if value.get('parent'):
        get(registry, value['parent'])
    return value


def verified_json(base, ref):
    path = (base / ref['path']).resolve()
    if not path.is_relative_to(base.resolve()):
        raise ValueError('artifact escapes manifest directory')
    value = read_json(path)
    if sha(canonical(value)) != ref['sha256']:
        raise ValueError('artifact fingerprint mismatch')
    return canonical(value)


def register(registry, manifest_path, run):
    manifest_path, run = Path(manifest_path).resolve(), Path(run).resolve()
    manifest = read_json(manifest_path)
    if read_json(run / 'manifest.json') != manifest:
        raise ValueError('run does not match manifest')
    result = read_json(run / 'results.json')
    summary = result['summary']
    if (summary['status'] != 'PASS' or summary['failed'] != 0 or
            summary['actual_external_model_calls'] != 0 or
            summary['experiment_id'] != manifest['experiment_id'] or
            summary['total'] != len(result['rows']) or
            any(not row.get('meets_expectation') for row in result['rows'])):
        raise ValueError('R0 run is not a completed passing offline run')
    versions = read_json(run / 'versions.json')
    worker = (run / 'implementation' / 'rsi_delivery_worker.py').read_bytes()
    if (sha(worker) != versions['implementation']['rsi_delivery_worker.py'] or
            versions['base_commit'] != manifest['base_commit'] or
            versions['evaluator'] != manifest['evaluator_version']):
        raise ValueError('run implementation does not match versions')
    shared = {'worker': put(registry, worker),
              'template': put(registry, canonical(manifest)),
              'cases': put(registry, verified_json(manifest_path.parent, manifest['cases'])),
              'evaluation': put(registry, verified_json(manifest_path.parent, manifest['evaluation'])),
              'run_manifest': put(registry, canonical(read_json(run / 'manifest.json'))),
              'run_results': put(registry, canonical(result)),
              'run_versions': put(registry, canonical(versions))}
    ids = {}
    for arm in ('A', 'B'):
        spec = manifest['arms'][arm]
        cfg = verified_json(manifest_path.parent, spec)
        if read_json(run / 'implementation' / (arm + '.json')) != json.loads(cfg):
            raise ValueError('run configuration mismatch')
        value = {'type': 'rsi-candidate-v1', **shared, 'config': put(registry, cfg),
                 'parent': ids.get('A') if arm == 'B' else None,
                 'original_candidate_id': spec['candidate_id'], 'origin': 'r0',
                 'original_parent_id': spec['parent_id'],
                 'origin_metadata': {'historical_design_fact': spec.get('change', 'baseline as recorded'),
                                     'historical_hypothesis': 'UNKNOWN'}}
        if arm == 'B' and spec['parent_id'] != manifest['arms']['A']['candidate_id']:
            raise ValueError('R0 parent mismatch')
        ids[arm] = record(registry, value)
    return ids


def validate_seam(parent, child):
    def strip(source):
        tree = ast.parse(source.decode('utf-8-sig'))
        functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'select_member_indices']
        if len(functions) != 1 or not isinstance(functions[0], ast.FunctionDef):
            raise ValueError('expected one select_member_indices function')
        fn = functions[0]
        for n in ast.walk(fn):
            if isinstance(n, (ast.Import, ast.ImportFrom, ast.Global, ast.Nonlocal)):
                raise ValueError('unsafe selection policy')
            if isinstance(n, ast.Name) and (n.id in {'eval', 'exec', 'compile', 'open', '__import__', 'globals', 'locals', 'getattr', 'setattr'} or n.id.startswith('__')):
                raise ValueError('unsafe selection policy')
            if isinstance(n, ast.Attribute) and n.attr.startswith('__'):
                raise ValueError('unsafe selection policy')
            if isinstance(n, ast.Call):
                safe_names = {'require', 'duplicate_key', 'any', 'all', 'range', 'len', 'enumerate',
                              'list', 'tuple', 'set', 'sorted', 'min', 'max', 'sum', 'abs'}
                safe = ((isinstance(n.func, ast.Name) and n.func.id in safe_names) or
                        (isinstance(n.func, ast.Attribute) and n.func.attr in {'append', 'get'}))
                if not safe:
                    raise ValueError('unsupported call in selection policy')
        fn.body = [ast.Pass()]
        return ast.dump(tree, include_attributes=False)
    if strip(parent) != strip(child):
        raise ValueError('changes must be confined to select_member_indices body')


def derive(registry, parent_id, config_path, worker_path, notes, from_run, observation_id, worker_patch=None):
    p = candidate(registry, parent_id)
    if any(not isinstance(notes.get(k), str) or not notes[k].strip() for k in ('observation', 'hypothesis', 'change', 'impact', 'falsification')):
        raise ValueError('all five documented fields are required')
    config = canonical(read_json(config_path))
    if worker_patch is not None:
        patch = read_json(worker_patch)
        if set(patch) != {'old', 'new'} or any(not isinstance(patch[k], str) or not patch[k] for k in ('old', 'new')):
            raise ValueError('worker patch requires nonempty old and new strings')
        old, new = patch['old'].encode(), patch['new'].encode()
        original = get(registry, p['worker'])
        if original.count(old) != 1:
            raise ValueError('worker patch must match exactly once')
        worker = original.replace(old, new, 1)
    else:
        worker = Path(worker_path).read_bytes()
    from rsi_feedback import build_feedback
    run = Path(from_run).resolve()
    feedback = read_json(run / 'feedback.json')
    run_id = feedback['run_id']
    if feedback != build_feedback(run, run_id):
        raise ValueError('feedback does not match archived run')
    matches = [x for x in feedback['observations'] if x.get('observation_id') == observation_id]
    if len(matches) != 1:
        raise ValueError('observation id missing or ambiguous')
    obs = matches[0]
    results_bytes = (run / 'results.json').read_bytes()
    results = json.loads(results_bytes.decode('utf-8-sig'))
    rows = [r for r in results['rows'] if r.get('qid') == obs.get('qid')]
    refs = [e for e in obs.get('evidence', []) if e.get('path') == 'results.json']
    if (obs.get('run_id') != run_id or len(rows) != 1 or len(refs) != 1 or
            refs[0].get('sha256') != sha(results_bytes) or
            refs[0].get('field') != 'rows[' + str(results['rows'].index(rows[0])) + ']'):
        raise ValueError('observation evidence binding mismatch')
    run_manifest = read_json(run / 'manifest.json')
    is_r1 = 'r1' in run_manifest
    expected = parent_id if is_r1 else p.get('original_candidate_id')
    matching_arms = [label for label in ('A', 'B')
                     if run_manifest['arms'][label]['candidate_id'] == expected]
    if len(matching_arms) != 1 or (not is_r1 and p.get('origin') != 'r0'):
        raise ValueError('observation run does not evaluate parent as B or A')
    arm = matching_arms[0]
    spec = run_manifest['arms'][arm]
    if is_r1:
        worker_file = run / 'implementation' / (arm + '_worker.py')
        if (spec.get('worker_sha256') != p['worker'] or spec.get('sha256') != p['config'] or
                sha(worker_file.read_bytes()) != p['worker'] or
                sha(canonical(read_json(run / 'implementation' / (arm + '.json')))) != p['config']):
            raise ValueError('observation run candidate artifacts differ from parent')
    else:
        versions = read_json(run / 'versions.json')
        if (sha(canonical(run_manifest)) != p['run_manifest'] or
                sha(canonical(results)) != p['run_results'] or
                sha(canonical(versions)) != p['run_versions'] or
                sha((run / 'implementation' / 'rsi_delivery_worker.py').read_bytes()) != p['worker'] or
                sha(canonical(read_json(run / 'implementation' / (arm + '.json')))) != p['config']):
            raise ValueError('legacy observation run differs from registered origin')
    source = {'run_id': run_id, 'qid': obs['qid'], 'observation_id': observation_id,
              'results_sha256': sha(results_bytes), 'feedback_sha256': sha((run / 'feedback.json').read_bytes()),
              'observation': obs, 'archive_manifest': put(registry, canonical(run_manifest)),
              'archive_results': put(registry, results_bytes),
              'archive_feedback': put(registry, (run / 'feedback.json').read_bytes())}
    if arm == 'A':
        source['arm'] = 'A'
    validate_seam(get(registry, p['worker']), worker)
    if sha(worker) == p['worker'] and sha(config) == p['config']:
        raise ValueError('child must change code or configuration')
    child = {**p, 'parent': parent_id, 'worker': put(registry, worker),
             'config': put(registry, config), 'origin': 'derived', 'notes': notes, 'observation_source': source}
    child.pop('original_candidate_id', None)
    child.pop('original_parent_id', None)
    return record(registry, child)


def materialize(registry, parent_id, child_id, out, cases_path=None, evaluation_path=None):
    p, c = candidate(registry, parent_id), candidate(registry, child_id)
    if c['parent'] != parent_id:
        raise ValueError('child is not derived from parent')
    if any(p[k] != c[k] for k in ('template', 'cases', 'evaluation')):
        raise ValueError('candidate provenance mismatch')
    validate_seam(get(registry, p['worker']), get(registry, c['worker']))
    if (cases_path is None) != (evaluation_path is None):
        raise ValueError('fixtures require both cases and evaluation')
    fixtures = None if cases_path is None else {
        'cases': canonical(read_json(cases_path)), 'evaluation': canonical(read_json(evaluation_path))}
    source_manifest = json.loads(get(registry, c['observation_source']['archive_manifest']))
    source_fixture = source_manifest.get('r1', {}).get('fixture')
    if source_fixture is not None:
        if (set(source_fixture) != {'cases', 'evaluation', 'scope'} or
                source_fixture['scope'] != 'engineering-only; no comparison to R0 results' or
                fixtures is None or any(sha(fixtures[k]) != source_fixture[k] for k in ('cases', 'evaluation'))):
            raise ValueError('engineering observation requires the same explicit engineering fixture')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    def write(name, data):
        with (out / name).open('xb') as f:
            f.write(data)
    template = get(registry, p['template'])
    write('parent_template.json', template)
    for name, key in (('observation_manifest.json', 'archive_manifest'),
                      ('observation_results.json', 'archive_results'),
                      ('observation_feedback.json', 'archive_feedback')):
        write(name, get(registry, c['observation_source'][key]))
    manifest = json.loads(template)
    manifest['experiment_id'] = 'r1-' + child_id[:16]
    manifest['r1'] = {'schema': 'rsi-candidate-manifest-v1', 'parent': parent_id, 'child': child_id, 'template_path': 'parent_template.json', 'template_sha256': p['template']}
    for label, cid, v in (('A', parent_id, p), ('B', child_id, c)):
        cfg, worker = get(registry, v['config']), get(registry, v['worker'])
        write(label + '.json', cfg)
        write(label + '_worker.py', worker)
        write(label + '_candidate.json', get(registry, cid))
        manifest['arms'][label] = {'candidate_id': cid, 'parent_id': v['parent'],
            'path': label + '.json', 'sha256': sha(cfg), 'worker_path': label + '_worker.py',
            'worker_sha256': sha(worker), 'record_path': label + '_candidate.json', 'record_sha256': cid}
    for name, key in (('cases.json', 'cases'), ('evaluation.json', 'evaluation')):
        data = get(registry, p[key]) if fixtures is None else fixtures[key]
        write(name, data)
        manifest['cases' if key == 'cases' else 'evaluation'] = {'path': name, 'sha256': sha(data)}
    if fixtures is not None:
        manifest['r1']['fixture'] = {key: sha(value) for key, value in fixtures.items()}
        manifest['r1']['fixture']['scope'] = 'engineering-only; no comparison to R0 results'
    write('manifest.json', canonical(manifest))
    return out / 'manifest.json'


def verify_arm_sources(manifest, manifest_directory):
    """Verify all materialized candidate bindings before the runner starts a worker."""
    base = Path(manifest_directory).resolve()
    r1 = manifest['r1']
    if r1.get('schema') != 'rsi-candidate-manifest-v1':
        raise ValueError('unsupported candidate manifest schema')
    arms = manifest['arms']
    if set(arms) != {'A', 'B'}:
        raise ValueError('expected two candidate arms')
    records, paths = {}, {}
    for label in ('A', 'B'):
        spec = arms[label]
        def local(name):
            value = (base / name).resolve()
            if not value.is_relative_to(base) or not value.is_file():
                raise ValueError('invalid materialized artifact path')
            return value
        record_bytes = local(spec['record_path']).read_bytes()
        cid = r1['parent' if label == 'A' else 'child']
        if sha(record_bytes) != cid or spec['record_sha256'] != cid or spec['candidate_id'] != cid:
            raise ValueError('candidate record hash mismatch')
        rec = json.loads(record_bytes)
        if rec.get('type') != 'rsi-candidate-v1' or rec.get('parent') != spec['parent_id']:
            raise ValueError('invalid candidate record')
        worker = local(spec['worker_path'])
        if sha(worker.read_bytes()) != rec['worker'] or spec['worker_sha256'] != rec['worker']:
            raise ValueError('candidate worker hash mismatch')
        cfg = read_json(local(spec['path']))
        if sha(canonical(cfg)) != rec['config'] or spec['sha256'] != rec['config']:
            raise ValueError('candidate configuration hash mismatch')
        records[label], paths[label] = rec, worker
    a, b = records['A'], records['B']
    if b['parent'] != r1['parent'] or b.get('origin') != 'derived':
        raise ValueError('invalid candidate lineage')
    if any(not isinstance(b.get('notes', {}).get(k), str) or not b['notes'][k].strip()
           for k in ('observation', 'hypothesis', 'change', 'impact', 'falsification')):
        raise ValueError('missing candidate rationale')
    if any(a[k] != b[k] for k in ('template', 'cases', 'evaluation', 'run_manifest', 'run_results', 'run_versions')):
        raise ValueError('candidate provenance differs')
    template_path = (base / r1['template_path']).resolve()
    if not template_path.is_relative_to(base) or sha(template_path.read_bytes()) != a['template'] or r1['template_sha256'] != a['template']:
        raise ValueError('parent template hash mismatch')
    template = read_json(template_path)
    for key in ('fixed', 'budget', 'evaluator_version', 'base_commit', 'status', 'stop_conditions'):
        if manifest.get(key) != template.get(key):
            raise ValueError('frozen template field differs: ' + key)
    if 'fixture' in r1 and (set(r1['fixture']) != {'cases', 'evaluation', 'scope'} or
            r1['fixture']['scope'] != 'engineering-only; no comparison to R0 results'):
        raise ValueError('invalid engineering fixture declaration')
    source = b.get('observation_source', {})
    if not all(source.get(k) for k in ('run_id', 'qid', 'observation_id', 'results_sha256', 'feedback_sha256', 'observation')):
        raise ValueError('missing structured observation source')
    if source['observation'].get('run_id') != source['run_id'] or source['observation'].get('qid') != source['qid'] or source['observation'].get('observation_id') != source['observation_id']:
        raise ValueError('observation source mismatch')
    archived = {}
    for name, key in (('observation_manifest.json', 'archive_manifest'),
                      ('observation_results.json', 'archive_results'),
                      ('observation_feedback.json', 'archive_feedback')):
        data = (base / name).read_bytes()
        if sha(data) != source[key]:
            raise ValueError('observation archive hash mismatch')
        archived[key] = json.loads(data.decode('utf-8-sig'))
    if source['archive_results'] != source['results_sha256'] or source['archive_feedback'] != source['feedback_sha256']:
        raise ValueError('observation digest mismatch')
    feedback = archived['archive_feedback']
    obs = source['observation']
    if feedback.get('run_id') != source['run_id'] or feedback.get('observations', []).count(obs) != 1:
        raise ValueError('observation missing from archived feedback')
    obs_core = {k: v for k, v in obs.items() if k != 'observation_id'}
    if 'obs-' + sha(canonical(obs_core))[:24] != source['observation_id']:
        raise ValueError('observation id mismatch')
    rows = archived['archive_results']['rows']
    matching = [i for i, row in enumerate(rows) if row.get('qid') == source['qid']]
    refs = [ref for ref in obs.get('evidence', []) if ref.get('path') == 'results.json']
    if len(matching) != 1 or len(refs) != 1 or refs[0].get('sha256') != source['results_sha256'] or refs[0].get('field') != f'rows[{matching[0]}]':
        raise ValueError('observation row binding mismatch')
    archived_manifest = archived['archive_manifest']
    arm = source.get('arm', 'B')
    if arm not in ('A', 'B'):
        raise ValueError('invalid observation arm')
    archived_arm = archived_manifest['arms'][arm]
    if archived_arm['candidate_id'] == r1['parent'] and 'r1' in archived_manifest:
        if archived_arm.get('worker_sha256') != a['worker'] or archived_arm.get('sha256') != a['config']:
            raise ValueError('observation parent artifact mismatch')
    elif a.get('origin') == 'r0' and archived_arm['candidate_id'] == a.get('original_candidate_id'):
        if sha(canonical(archived_manifest)) != a['run_manifest'] or sha(canonical(archived['archive_results'])) != a['run_results']:
            raise ValueError('legacy observation origin mismatch')
    else:
        raise ValueError('observation parent mismatch')
    source_fixture = archived_manifest.get('r1', {}).get('fixture')
    if source_fixture is not None and r1.get('fixture') != source_fixture:
        raise ValueError('engineering observation requires the same explicit engineering fixture')
    for key in ('cases', 'evaluation'):
        ref = manifest[key]
        path = (base / ref['path']).resolve()
        if not path.is_relative_to(base) or sha(canonical(read_json(path))) != r1.get('fixture', a)[key] or ref['sha256'] != r1.get('fixture', a)[key]:
            raise ValueError('candidate input hash mismatch')
    validate_seam(paths['A'].read_bytes(), paths['B'].read_bytes())
    return paths


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--registry', required=True, type=Path)
    subs = parser.add_subparsers(dest='command', required=True)
    reg = subs.add_parser('register-r0'); reg.add_argument('--manifest', required=True, type=Path); reg.add_argument('--run', required=True, type=Path)
    der = subs.add_parser('derive'); der.add_argument('--parent', required=True); der.add_argument('--config', required=True, type=Path); workers = der.add_mutually_exclusive_group(required=True); workers.add_argument('--worker', type=Path); workers.add_argument('--worker-patch', type=Path)
    der.add_argument('--from-run', required=True, type=Path); der.add_argument('--observation-id', required=True)
    for flag in ('observation', 'hypothesis', 'change', 'impact', 'falsification'):
        der.add_argument('--' + flag, required=True)
    mat = subs.add_parser('materialize'); mat.add_argument('--parent', required=True); mat.add_argument('--child', required=True); mat.add_argument('--out', required=True, type=Path); mat.add_argument('--cases', type=Path); mat.add_argument('--evaluation', type=Path); mat.add_argument('--engineering-fixture', action='store_true')
    args = parser.parse_args()
    if args.command == 'register-r0':
        output = register(args.registry, args.manifest, args.run)
    elif args.command == 'derive':
        output = {'candidate_id': derive(args.registry, args.parent, args.config, args.worker,
                  {key: getattr(args, key) for key in ('observation', 'hypothesis', 'change', 'impact', 'falsification')}, args.from_run, args.observation_id, args.worker_patch)}
    else:
        if bool(args.cases or args.evaluation) != args.engineering_fixture:
            parser.error('fixture inputs require --engineering-fixture and vice versa')
        output = {'manifest': str(materialize(args.registry, args.parent, args.child, args.out, args.cases, args.evaluation))}
    print(json.dumps(output, ensure_ascii=False))


if __name__ == '__main__':
    main()

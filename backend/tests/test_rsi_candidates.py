import hashlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'scripts' / 'rsi_candidates.py'


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def write(path, value):
    path.write_bytes(canonical(value))


def call(registry, *args, ok=True):
    proc = subprocess.run([sys.executable, str(CLI), '--registry', str(registry), *map(str, args)],
                          capture_output=True, text=True)
    assert (proc.returncode == 0) == ok, proc.stderr
    return json.loads(proc.stdout) if ok else proc.stderr


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / 'inputs'; src.mkdir()
    run = tmp_path / 'run'; (run / 'implementation').mkdir(parents=True)
    worker = b'def select_member_indices(members, chunks, files, seed_ids, selection):\n    return list(range(len(members)))\n\ndef other():\n    return 1\n'
    (run / 'implementation' / 'rsi_delivery_worker.py').write_bytes(worker)
    values = {'all.json': {'selection': 'all'}, 'b.json': {'selection': 'exact_duplicate'},
              'cases.json': [{'qid': 'fixture'}], 'expected.json': {'fixture': {}}}
    for name, val in values.items(): write(src / name, val)
    for arm, name in [('A', 'all.json'), ('B', 'b.json')]: write(run / 'implementation' / (arm + '.json'), values[name])
    ref = lambda name: {'path': name, 'sha256': digest(canonical(values[name]))}
    manifest = {'status': 'READY', 'base_commit': 'a' * 40, 'evaluator_version': 'e', 'experiment_id': 'r0',
                'arms': {'A': {**ref('all.json'), 'candidate_id': 'a', 'parent_id': None},
                         'B': {**ref('b.json'), 'candidate_id': 'b', 'parent_id': 'a'}},
                'cases': ref('cases.json'), 'evaluation': ref('expected.json')}
    write(src / 'manifest.json', manifest); write(run / 'manifest.json', manifest)
    write(run / 'versions.json', {'base_commit': 'a' * 40, 'evaluator': 'e',
          'implementation': {'rsi_delivery_worker.py': digest(worker)}})
    write(run / 'results.json', {'summary': {'status': 'PASS', 'failed': 0, 'actual_external_model_calls': 0,
          'experiment_id': 'r0', 'total': 1}, 'rows': [{'qid': 'fixture', 'status': 'BLOCKED', 'meets_expectation': True}]})
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        from rsi_feedback import build_feedback
        feedback = build_feedback(run, 'test-run')
        write(run / 'feedback.json', feedback)
    finally:
        sys.path.pop(0)
    return src, run, worker


def test_cli_lineage_isolation_recovery_and_tamper(tmp_path, setup):
    src, run, worker = setup
    reg = tmp_path / 'registry'
    ids = call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run)
    assert ids == call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run)
    changed = tmp_path / 'worker.py'
    changed.write_bytes(worker.replace(b'return list(range(len(members)))', b'return list(range(len(members)))[::-1]'))
    args = ['derive', '--parent', ids['B'], '--worker', changed, '--config', src / 'b.json',
            '--from-run', run, '--observation-id', json.loads((run / 'feedback.json').read_text())['observations'][0]['observation_id'], '--observation', 'synthetic', '--hypothesis', 'reverse', '--change', 'reverse order',
            '--impact', 'order', '--falsification', 'same order']
    child = call(reg, *args)['candidate_id']
    out = tmp_path / 'bundle'
    call(reg, 'materialize', '--parent', ids['B'], '--child', child, '--out', out,
         '--engineering-fixture', '--cases', src / 'cases.json', '--evaluation', src / 'expected.json')
    assert (out / 'A_worker.py').read_bytes() == worker
    assert (out / 'B_worker.py').read_bytes() == changed.read_bytes()
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        from rsi_candidates import verify_arm_sources
        manifest = json.loads((out / 'manifest.json').read_text())
        assert set(verify_arm_sources(manifest, out)) == {'A', 'B'}
        altered = json.loads(json.dumps(manifest)); altered['evaluator_version'] = 'tampered'
        with pytest.raises(ValueError, match='frozen template field'):
            verify_arm_sources(altered, out)
        original_feedback = (out / 'observation_feedback.json').read_bytes()
        (out / 'observation_feedback.json').write_text('{}')
        with pytest.raises(ValueError, match='archive hash mismatch'):
            verify_arm_sources(manifest, out)
        (out / 'observation_feedback.json').write_bytes(original_feedback)
        (out / 'B_worker.py').write_text('tampered')
        with pytest.raises(ValueError, match='hash mismatch'):
            verify_arm_sources(manifest, out)
    finally:
        sys.path.pop(0)
    # All stored bytes are verified before re-materialization; no source file is required.
    changed.unlink()
    recovered = tmp_path / 'recovered'
    call(reg, 'materialize', '--parent', ids['B'], '--child', child, '--out', recovered)
    assert (recovered / 'B_worker.py').read_bytes() != worker
    (reg / 'objects' / child).write_text('{}')
    assert 'hash mismatch' in call(reg, 'materialize', '--parent', ids['B'], '--child', child,
                                   '--out', tmp_path / 'bad', ok=False)


def test_reject_bad_runs_and_outside_seam(tmp_path, setup):
    src, run, worker = setup
    reg = tmp_path / 'registry'
    result = json.loads((run / 'results.json').read_text()); result['summary']['status'] = 'FAIL'
    write(run / 'results.json', result)
    assert 'not a completed' in call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run, ok=False)
    result['summary']['status'] = 'PASS'; write(run / 'results.json', result)
    ids = call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run)
    changed = tmp_path / 'bad.py'; changed.write_bytes(worker.replace(b'return 1', b'return 2'))
    assert 'confined' in call(reg, 'derive', '--parent', ids['B'], '--worker', changed, '--config', src / 'b.json',
        '--from-run', run, '--observation-id', json.loads((run / 'feedback.json').read_text())['observations'][0]['observation_id'], '--observation', 'o', '--hypothesis', 'h', '--change', 'c', '--impact', 'i', '--falsification', 'f', ok=False)


def test_unsafe_policy_and_fixture_gate(tmp_path, setup):
    src, run, worker = setup
    reg = tmp_path / 'registry'
    ids = call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run)
    changed = tmp_path / 'unsafe.py'
    changed.write_bytes(worker.replace(b'return list(range(len(members)))', b'return eval("[]")'))
    error = call(reg, 'derive', '--parent', ids['B'], '--worker', changed, '--config', src / 'b.json',
        '--from-run', run, '--observation-id', json.loads((run / 'feedback.json').read_text())['observations'][0]['observation_id'], '--observation', 'o', '--hypothesis', 'h', '--change', 'c', '--impact', 'i', '--falsification', 'f', ok=False)
    assert 'unsafe selection policy' in error or 'unsupported call' in error
    error = call(reg, 'materialize', '--parent', ids['B'], '--child', ids['A'], '--out', tmp_path / 'x',
                 '--cases', src / 'cases.json', '--evaluation', src / 'expected.json', ok=False)
    assert 'require --engineering-fixture' in error


def test_next_generation_binds_child_run_and_rejects_wrong_run(tmp_path, setup):
    src, run, worker = setup
    reg = tmp_path / 'registry'
    ids = call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run)
    obs = json.loads((run / 'feedback.json').read_text())['observations'][0]['observation_id']
    patch = tmp_path / 'patch.json'; write(patch, {'old': 'return list(range(len(members)))', 'new': 'return list(range(len(members)))[:]'} )
    def derive(parent, source_run, observation, patch_path):
        return call(reg, 'derive', '--parent', parent, '--from-run', source_run, '--observation-id', observation,
                    '--worker-patch', patch_path, '--config', src / 'b.json', '--observation', 'o',
                    '--hypothesis', 'h', '--change', 'c', '--impact', 'i', '--falsification', 'f')
    child = derive(ids['B'], run, obs, patch)['candidate_id']
    second_run = tmp_path / 'second-run'; second_run.mkdir()
    old_manifest = json.loads((run / 'manifest.json').read_text())
    old_manifest['r1'] = {'schema': 'rsi-candidate-manifest-v1', 'fixture': {
        'cases': old_manifest['cases']['sha256'], 'evaluation': old_manifest['evaluation']['sha256'],
        'scope': 'engineering-only; no comparison to R0 results'}}
    old_manifest['arms']['B'] = {'candidate_id': child,
        'worker_sha256': digest((reg / 'objects' / child).read_bytes())}
    child_record = json.loads((reg / 'objects' / child).read_text())
    old_manifest['arms']['B'].update(worker_sha256=child_record['worker'], sha256=child_record['config'])
    (second_run / 'implementation').mkdir()
    (second_run / 'implementation' / 'B_worker.py').write_bytes((reg / 'objects' / child_record['worker']).read_bytes())
    (second_run / 'implementation' / 'B.json').write_bytes((reg / 'objects' / child_record['config']).read_bytes())
    write(second_run / 'manifest.json', old_manifest)
    write(second_run / 'results.json', {'summary': {'run_id': 'next-run'}, 'rows': [{'qid': 'next', 'status': 'BLOCKED'}]})
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        from rsi_feedback import build_feedback
        feedback = build_feedback(second_run)
    finally:
        sys.path.pop(0)
    write(second_run / 'feedback.json', feedback)
    next_obs = feedback['observations'][0]['observation_id']
    patch2 = tmp_path / 'patch2.json'; write(patch2, {'old': 'return list(range(len(members)))[:]', 'new': 'return list(range(len(members)))'})
    grandchild = derive(child, second_run, next_obs, patch2)['candidate_id']
    bundle = tmp_path / 'generation-two'
    error = call(reg, 'materialize', '--parent', child, '--child', grandchild, '--out', bundle, ok=False)
    assert 'same explicit engineering fixture' in error
    call(reg, 'materialize', '--parent', child, '--child', grandchild, '--out', bundle,
         '--engineering-fixture', '--cases', src / 'cases.json', '--evaluation', src / 'expected.json')
    error = call(reg, 'derive', '--parent', child, '--from-run', run, '--observation-id', obs,
                 '--worker-patch', patch2, '--config', src / 'b.json', '--observation', 'o',
                 '--hypothesis', 'h', '--change', 'c', '--impact', 'i', '--falsification', 'f', ok=False)
    assert 'does not evaluate parent' in error


def test_legacy_same_label_cannot_substitute_results_versions_or_artifacts(tmp_path, setup):
    import shutil
    src, run, worker = setup
    reg = tmp_path / 'registry'
    ids = call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run)
    for change in ('results', 'versions', 'worker', 'config'):
        other = tmp_path / change
        shutil.copytree(run, other)
        if change == 'results':
            result = json.loads((other / 'results.json').read_text()); result['rows'][0]['reason'] = 'different'
            write(other / 'results.json', result)
        elif change == 'versions':
            versions = json.loads((other / 'versions.json').read_text()); versions['extra'] = 'different'
            write(other / 'versions.json', versions)
        elif change == 'worker':
            (other / 'implementation' / 'rsi_delivery_worker.py').write_bytes(worker + b'\n# changed')
        else:
            write(other / 'implementation' / 'B.json', {'selection': 'all'})
        sys.path.insert(0, str(ROOT / 'scripts'))
        try:
            from rsi_feedback import build_feedback
            feedback = build_feedback(other, 'test-run')
        finally:
            sys.path.pop(0)
        write(other / 'feedback.json', feedback)
        obs = feedback['observations'][0]['observation_id']
        patch = tmp_path / 'patch.json'; write(patch, {'old': 'return list(range(len(members)))', 'new': 'return list(range(len(members)))[:]'})
        error = call(reg, 'derive', '--parent', ids['B'], '--from-run', other, '--observation-id', obs,
                     '--worker-patch', patch, '--config', src / 'b.json', '--observation', 'o',
                     '--hypothesis', 'h', '--change', 'c', '--impact', 'i', '--falsification', 'f', ok=False)
        assert 'legacy observation run differs from registered origin' in error


def test_original_baseline_a_is_valid_parent(tmp_path, setup):
    src, run, worker = setup
    reg = tmp_path / 'registry'
    ids = call(reg, 'register-r0', '--manifest', src / 'manifest.json', '--run', run)
    obs = json.loads((run / 'feedback.json').read_text())['observations'][0]['observation_id']
    patch = tmp_path / 'patch.json'
    write(patch, {'old': 'return list(range(len(members)))', 'new': 'return list(range(len(members)))[:]'})
    child = call(reg, 'derive', '--parent', ids['A'], '--from-run', run, '--observation-id', obs,
                 '--worker-patch', patch, '--config', src / 'all.json', '--observation', 'o',
                 '--hypothesis', 'h', '--change', 'c', '--impact', 'i', '--falsification', 'f')['candidate_id']
    record = json.loads((reg / 'objects' / child).read_text())
    assert record['observation_source']['arm'] == 'A'
    bundle = tmp_path / 'a-bundle'
    call(reg, 'materialize', '--parent', ids['A'], '--child', child, '--out', bundle)
    sys.path.insert(0, str(ROOT / 'scripts'))
    try:
        from rsi_candidates import verify_arm_sources
        assert set(verify_arm_sources(json.loads((bundle / 'manifest.json').read_text()), bundle)) == {'A', 'B'}
    finally:
        sys.path.pop(0)

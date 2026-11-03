"""Synthetic runner contract tests; no official case, worker, model or network replay."""
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / 'scripts' / 'run_rsi_offline.py'


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location('rsi_offline_runner_under_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pair_record():
    return {'qid': 'synthetic-contract-only', 'upstream_sha256': 'a' * 64,
            'fixed_config_sha256': 'b' * 64, 'evaluator_version': 'synthetic-v1',
            'final_request_sha256': 'c' * 64}


@pytest.mark.parametrize('key', ['qid', 'upstream_sha256', 'fixed_config_sha256', 'evaluator_version'])
@pytest.mark.parametrize('change', ['missing_left', 'missing_right', 'different', 'empty_both'])
def test_pair_rejects_missing_or_mismatched_frozen_identity(runner, key, change):
    left, right = pair_record(), pair_record()
    if change == 'missing_left':
        left.pop(key)
    elif change == 'missing_right':
        right.pop(key)
    elif change == 'different':
        right[key] = 'different'
    else:
        left[key] = right[key] = ''
    with pytest.raises(ValueError):
        runner.compare_pair(left, right)


def test_pair_allows_candidate_request_difference_without_mutating_records(runner):
    left, right = pair_record(), pair_record()
    right['final_request_sha256'] = 'd' * 64
    before = copy.deepcopy((left, right))
    runner.compare_pair(left, right)
    assert (left, right) == before


def test_common_runner_import_does_not_mutate_environment_or_socket():
    code = '''
import os, runpy, socket, sys
before_env = dict(os.environ)
names = ('socket', 'create_connection', 'getaddrinfo', 'socketpair')
before = {name: getattr(socket, name) for name in names}
connect = socket.socket.connect
connect_ex = socket.socket.connect_ex
runpy.run_path(sys.argv[1], run_name='import_only_contract_check')
assert dict(os.environ) == before_env, 'runner import changed environment'
assert all(getattr(socket, name) is value for name, value in before.items()), 'runner import changed socket'
assert socket.socket.connect is connect
assert socket.socket.connect_ex is connect_ex
'''
    completed = subprocess.run([sys.executable, '-S', '-c', code, str(SCRIPT)],
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.fixture
def synthetic_manifest(runner, tmp_path):
    """New one-case fixture; never load the published experiment samples."""
    case = {'question': 'Synthetic empty retrieval?', 'allowed_kb_ids': ['synthetic-kb'],
            'files': [], 'chunks': [], 'seed_ids': []}
    artifacts = {'cases': [{'qid': 'synthetic-contract-only', 'case': case}],
                 'evaluation': {'synthetic-contract-only': {'status': 'PAIRED', 'length_change': 'equal'}},
                 'A': {'selection': 'all'}, 'B': {'selection': 'exact_duplicate'}}
    refs = {}
    for name, value in artifacts.items():
        path = tmp_path / (name + '.json')
        path.write_text(json.dumps(value), encoding='utf-8')
        refs[name] = {'path': path.name, 'sha256': runner.fingerprint(value)}
    refs['A'].update(candidate_id='synthetic-baseline', parent_id=None)
    refs['B'].update(candidate_id='synthetic-candidate', parent_id='synthetic-baseline')
    head = subprocess.check_output(['git', '-C', str(SCRIPT.parents[1]), 'rev-parse', 'HEAD'], text=True).strip()
    manifest = {'experiment_id': 'synthetic-lifecycle-only', 'status': 'READY', 'base_commit': head,
                'evaluator_version': runner.EVALUATOR, 'fixed': runner.FIXED,
                'cases': refs['cases'], 'evaluation': refs['evaluation'],
                'arms': {'A': refs['A'], 'B': refs['B']},
                'budget': {'max_questions': 1, 'external_calls': 0, 'attempts_per_arm': 1}}
    path = tmp_path / 'manifest.json'
    path.write_text(json.dumps(manifest), encoding='utf-8')
    return path


def stub_child(runner, fail_candidate=False, corrupt_candidate=False):
    def child(stage, paths, cwd):
        if stage == 'prepare':
            result = {'status': 'OK', 'synthetic_snapshot': True}
        else:
            snapshot = runner.load(paths[0])
            selection = runner.load(paths[1])['selection']
            if selection == 'exact_duplicate' and fail_candidate:
                raise ValueError('synthetic candidate failure')
            upstream = runner.fingerprint(snapshot)
            if selection == 'exact_duplicate' and corrupt_candidate:
                upstream = 'wrong-frozen-snapshot'
            result = {'status': 'OK', 'upstream_sha256': upstream, 'selection': selection,
                      'members': [], 'context': '', 'messages': [], 'request_calls': 0}
        runner.save(paths[-1], result)
        return result
    return child


def tree_bytes(path):
    return {p.relative_to(path).as_posix(): p.read_bytes() for p in path.rglob('*') if p.is_file()}


def test_failure_keeps_evidence_and_recovery_requires_new_output(runner, synthetic_manifest, tmp_path, monkeypatch):
    failed_out, recovered_out = tmp_path / 'failed', tmp_path / 'recovered'
    manifest_bytes = synthetic_manifest.read_bytes()
    monkeypatch.setattr(runner, 'child', stub_child(runner, fail_candidate=True))
    failed = runner.run(synthetic_manifest, failed_out)
    assert (failed['status'], failed['total'], failed['paired'], failed['failed']) == ('FAIL', 1, 0, 1)
    row = runner.load(failed_out / 'case-001/result.json')
    assert row['status'] == 'FAILED' and 'synthetic candidate failure' in row['error']
    assert (failed_out / 'case-001/A/capture.json').is_file()
    assert (failed_out / 'manifest.json').is_file()
    assert (failed_out / 'versions.json').is_file()
    before = tree_bytes(failed_out)
    monkeypatch.setattr(runner, 'child', stub_child(runner))
    with pytest.raises(FileExistsError):
        runner.run(synthetic_manifest, failed_out)
    assert tree_bytes(failed_out) == before
    recovered = runner.run(synthetic_manifest, recovered_out)
    assert (recovered['status'], recovered['total'], recovered['paired'], recovered['failed']) == ('PASS', 1, 1, 0)
    assert tree_bytes(failed_out) == before
    assert synthetic_manifest.read_bytes() == manifest_bytes
    recovered_before = tree_bytes(recovered_out)
    with pytest.raises(FileExistsError):
        runner.run(synthetic_manifest, recovered_out)
    assert tree_bytes(recovered_out) == recovered_before


def test_mismatched_snapshot_excluded_from_pair_denominator(runner, synthetic_manifest, tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'child', stub_child(runner, corrupt_candidate=True))
    out = tmp_path / 'mismatched'
    summary = runner.run(synthetic_manifest, out)
    assert (summary['total'], summary['paired'], summary['failed']) == (1, 0, 1)
    assert summary['status'] == 'FAIL'
    row = runner.load(out / 'case-001/result.json')
    assert row['status'] == 'FAILED' and 'frozen inputs' in row['error']
    assert (out / 'case-001/B/capture.json').is_file()

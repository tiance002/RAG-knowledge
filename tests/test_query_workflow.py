"""Synthetic coordinator tests; no model, network, business source or containers."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rsik import query_workflow as q
from rsik.adjudicate import SLOTS, evaluate_candidate
from rsik.errors import StageFailure
from rsik.redact import LeakScanner
from rsik.state import canonical
from rsik.tests.test_workflow import FakeRoles, fake_rows, public_context


class Registry:
    def __init__(self):
        self.parent = {'candidate_id': 'parent', 'worker': b'parent', 'config': b'{}'}
        self.children = {}

    def require_not_quarantined(self, cid):
        assert cid == 'parent'

    def get(self, cid):
        return self.parent if cid == 'parent' else self.children[cid]

    def put(self, worker, config, **kwargs):
        cid = kwargs['evidence']['slot']
        self.children[cid] = {'candidate_id': cid, 'worker': worker, 'config': config, 'record': {'parent_id': 'parent'}}
        return cid


class Capsule:
    business_root = 'SYNTHETIC'
    def setup(self): pass
    def close(self): self.closed = True
    def child(self, stage, args, directory, **kwargs):
        Path(args[-1]).write_bytes(canonical({'status': 'PASS'}))
        return {'status': 'PASS'}


class Advisory:
    scanner = LeakScanner()
    def __init__(self): self.calls = []
    def call(self, role, public):
        assert 'selector_contract' not in public and 'experiment_intent' not in public
        self.calls.append(role)
        return {'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC'}


def setup(monkeypatch, tmp_path, *, p3_change=False, failed_slot=None):
    registry, advisory = Registry(), Advisory()
    monkeypatch.setattr(q, 'check_split', lambda *a, **k: {'frozen': True})
    monkeypatch.setattr(q, 'apply_patch', lambda *a: b'changed')
    monkeypatch.setattr(q, 'selector', lambda *a: (None, 'SYNTHETIC public selector'))
    monkeypatch.setattr(q, 'invariant_probe_source', lambda worker: b'SYNTHETIC probe')
    calls = []
    def replay(capsule, fixture, artifacts, output, **kwargs):
        slot = artifacts['B']['candidate_id']; calls.append((slot, str(fixture)))
        if slot == failed_slot:
            raise StageFailure('REPLAY_ERROR', 'SYNTHETIC_INFRA_FAILURE')
        output.mkdir()
        (output / 'manifest.json').write_bytes(canonical({'arms': {
            arm: {'candidate_id': artifact['candidate_id'], 'worker_sha256': q.bytes_hash(artifact['worker']),
                  'sha256': q.bytes_hash(artifact['config'])} for arm, artifact in artifacts.items()}}))
        (output / 'results.json').write_bytes(canonical({'rows': [{'status': 'PAIRED'}]}))
        for arm in ('A', 'B'):
            path = output / 'case-001' / arm
            path.mkdir(parents=True)
            (path / 'capture.json').write_bytes(canonical({'members': ['one'] if arm == 'A' or not p3_change else ['two']}))
        rows = fake_rows()
        return evaluate_candidate({'angle': 'P2_BUDGET', 'proposal_id': 'rsi-query-aware-engineering/v1',
            'candidate_id': slot, 'worker_hash': 'same', 'config_hash': 'same'}, rows,
            [r['qid'] for r in rows], total_questions=20)
    monkeypatch.setattr(q, 'replay_artifacts', replay)
    kwargs = dict(public=public_context(), roles=FakeRoles(same_patch=True, barrier=True), advisory_roles=advisory,
        parent=registry.parent, fixtures=[tmp_path / str(i) for i in range(4)], registry=registry,
        directory=tmp_path / 'round', scanner=LeakScanner(), capsule_factory=lambda output: Capsule())
    return kwargs, calls, advisory


def test_all_instances_ordinary_replayed_and_advisory_follows_archive(monkeypatch, tmp_path):
    kwargs, calls, advisory = setup(monkeypatch, tmp_path)
    before = deepcopy(kwargs['public'])
    result = q.run_query_round(**kwargs)
    assert len(calls) == 12 and {s for s, _ in calls} == set(SLOTS)
    assert result['family_size'] == 3 and result['adopted_proposal_id'] == 'SYNTHETIC-P1_DEDUP'
    assert len(result['candidate_decisions']) == 3
    assert Path(result['selected_training_directory'], 'round.json').exists()
    assert advisory.calls == ['curator', 'meta'] and kwargs['public'] == before


def test_order_role_cannot_change_selected_members(monkeypatch, tmp_path):
    kwargs, calls, advisory = setup(monkeypatch, tmp_path, p3_change=True)
    result = q.run_query_round(**kwargs)
    p3 = next(d for d in result['candidate_decisions'] if d['angle'] == 'P3_ORDER')
    assert p3['reason_codes'] == ['ANGLE_VIOLATION']
    assert result['adopted_candidate_id'] != 'P3_ORDER'
    assert advisory.calls == ['curator', 'meta']


def test_local_replay_failure_does_not_skip_other_instances(monkeypatch, tmp_path):
    kwargs, calls, _ = setup(monkeypatch, tmp_path, failed_slot='P2_BUDGET')
    result = q.run_query_round(**kwargs)
    assert result['attempted_replays'] == list(SLOTS)
    assert any(s == 'P3_ORDER' for s, _ in calls)
    assert next(d for d in result['candidate_decisions'] if d['angle'] == 'P2_BUDGET')['verdict'] == 'INCONCLUSIVE'


def test_single_profile_retains_fixed_three_comparison_slots(monkeypatch, tmp_path):
    kwargs, calls, _ = setup(monkeypatch, tmp_path)
    kwargs.update(roles=FakeRoles(same_patch=True), enabled_slots=SLOTS[:1])
    result = q.run_query_round(**kwargs)
    assert len(calls) == 4 and result['family_size'] == 3
    assert len(result['candidate_decisions']) == 3 and result['enabled_slots'] == list(SLOTS[:1])


def test_compatible_handbook_sections_retrieved_without_mutating_public(monkeypatch, tmp_path):
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    sections = []
    class Handbook:
        def retrieve(self, section, format, features, fingerprint):
            sections.append((section, fingerprint))
            return []
    kwargs.update(handbook=Handbook(), compatibility_fingerprint='SYNTHETIC-frozen-profile')
    q.run_query_round(**kwargs)
    assert sections == [(s, 'SYNTHETIC-frozen-profile') for s in ('dedup', 'budget', 'order', 'format')]


def test_registry_failure_is_global_and_cannot_select_partial_winner(monkeypatch, tmp_path):
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    def broken(*a, **k): raise ValueError('SYNTHETIC corrupt registry')
    kwargs['registry'].put = broken
    result = q.run_query_round(**kwargs)
    assert result['verdict'] == 'ROUND_ABORTED' and result['adopted_candidate_id'] is None


def test_curator_local_failure_does_not_skip_meta_or_rewrite_decision(monkeypatch, tmp_path):
    kwargs, _, advisory = setup(monkeypatch, tmp_path)
    original = advisory.call
    def call(role, public):
        if role == 'curator':
            advisory.calls.append(role)
            raise StageFailure('AGENT_ERROR', 'AGENT_UNAVAILABLE')
        return original(role, public)
    advisory.call = call
    result = q.run_query_round(**kwargs)
    assert result['verdict'] == 'ADOPT_CANDIDATE' and advisory.calls == ['curator', 'meta']
    assert q.load(kwargs['directory'] / 'advisory-summary.json')['role_failures'] == [
        {'role': 'curator', 'reason_code': 'AGENT_UNAVAILABLE'}]


def test_matched_comparison_normalizes_aliases_but_preserves_counts_and_code():
    a, b = public_context(), public_context()
    assert a != b and q.public_semantics(a) == q.public_semantics(b)
    b['observations'][0]['counts']['questions'] += 1
    assert q.public_semantics(a) != q.public_semantics(b)


def test_original_budget_path_required_and_cannot_create_fresh_ledger(tmp_path):
    query_registry = tmp_path / 'query-aware-v1' / 'registry'
    expected = tmp_path / 'task-model-budget.sqlite'
    with pytest.raises(ValueError, match='ORIGINAL_TASK_BUDGET'):
        q.bind_budget_path(query_registry, expected)
    expected.write_bytes(b'SYNTHETIC existing ledger')
    assert q.bind_budget_path(query_registry, expected) == expected
    with pytest.raises(ValueError, match='ORIGINAL_TASK_BUDGET'):
        q.bind_budget_path(query_registry, tmp_path / 'new-budget.sqlite')


def test_completed_recovery_recomputes_without_roles_or_capsules_and_rejects_tamper(monkeypatch, tmp_path):
    kwargs, _, _ = setup(monkeypatch, tmp_path)
    result = q.run_query_round(**kwargs)
    monkeypatch.setattr(q, 'read_only_quarantine_check', lambda *a: None)
    def read(run, fixture, cid, proposal, worker, config):
        index = int(run.name.split('-')[1])
        return q.load(run.parent / f'decision-{index}.json')
    monkeypatch.setattr(q, 'read_decision', read)
    before = {p: p.read_bytes() for p in kwargs['directory'].rglob('*') if p.is_file()}
    recovered = q.resume_query_round(directory=kwargs['directory'], registry=kwargs['registry'], fixtures=kwargs['fixtures'])
    assert recovered == result and before == {p: p.read_bytes() for p in before}
    (kwargs['directory'] / 'public-context.json').write_bytes(b'{}')
    with pytest.raises(ValueError, match='ARTIFACT_CHANGED'):
        q.resume_query_round(directory=kwargs['directory'], registry=kwargs['registry'], fixtures=kwargs['fixtures'])


def test_partial_round_recovery_fails_closed_without_restarting(tmp_path):
    with pytest.raises(FileNotFoundError):
        q.resume_query_round(directory=tmp_path, registry=Registry(), fixtures=[])

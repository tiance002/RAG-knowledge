"""Actual coordinator/readers with invented CONTROL sources and capture doubles.

No worker/backend/model executes. These archives cannot qualify real data or
production Gold; the double fabricates only explicitly labelled control captures.
"""
from pathlib import Path

import pytest

from portable_fixtures import ROOT, SyntheticCapsule, write
from rsik import query_preview
from rsik.baseline_qualification import qualify_baseline, verify_baseline_qualification
from rsik.execution_evidence import ExecutionEvidence
from rsik.experiment import load, bytes_hash
from rsik.query_aware import QueryRegistry, adapt_legacy
from rsik.query_verification import verify_training, verify_sealed
from rsik.query_workflow import run_query_round, resume_query_round
from rsik.redact import LeakScanner, make_visible, project_summary
from rsik.role_profiles import DEPENDENCY
from rsik.source_relations import PROFILE, check_fixture
from rsik.tests.test_query_learning_bridge import Proposers
from rsik.tests.test_source_relations import _build_fixture, _append_citations
from run_rsi_loop import selector


class Advisory:
    scanner = LeakScanner()
    def call(self, role, public):
        return {'status': 'ABSTAIN', 'result': None, 'reason': 'CONTROL_ONLY'}


def fixture_with_optional_member(path):
    fixture = _build_fixture(path, count=20)
    cases = load(fixture / 'cases.json')
    specs = load(fixture / 'relations.restricted.json')
    for item in cases:
        case, spec = item['case'], specs[item['qid']]
        sid = next(iter(spec['sources']))
        source = spec['sources'][sid]
        source_path = fixture / source['relative_path']
        original = source_path.read_text(encoding='utf-8')
        noise = 'CONTROL optional filler ' * 20
        source_path.write_bytes((original + '\n' + noise).encode())
        source['content_sha256'] = bytes_hash(source_path.read_bytes())
        case['chunks'].append({'id': 'm3', 'file_id': source['file_id'],
            'content': noise, 'content_sha256': bytes_hash(noise.encode())})
        spec['member_spans']['m3'] = {'source_id': sid, 'start': len(original) + 1,
                                     'end': len(original) + 1 + len(noise)}
    write(fixture / 'cases.json', cases)
    write(fixture / 'relations.restricted.json', specs)
    manifest = load(fixture / 'manifest.json')
    manifest['artifact_hashes'] = {name: bytes_hash((fixture / name).read_bytes())
                                  for name in ('cases.json', 'relations.restricted.json')}
    write(fixture / 'manifest.json', manifest)
    write(fixture / 'freeze.json', check_fixture(fixture, expected_count=20))
    return fixture


class RelationalCaptureDouble(SyntheticCapsule):
    """Explicit CONTROL-only delivery double; never claims code execution."""
    def __init__(self, fixture, *, compress=True):
        self.cases = load(fixture / 'cases.json')
        self.specs = load(fixture / 'relations.restricted.json')
        self.compress = compress
    def setup(self): pass
    def close(self): pass
    def child(self, stage, paths, cwd, **kwargs):
        if Path(cwd).name == 'probes':
            result = {'status': 'PASS', 'scope': 'CONTROL_NON_EXECUTION_DOUBLE'}
            write(paths[-1], result)
            return result
        result = super().child(stage, paths, cwd, **kwargs)
        if stage == 'deliver':
            index = int(Path(cwd).parent.name.removeprefix('case-')) - 1
            spec = self.specs[self.cases[index]['qid']]
            if self.compress and Path(kwargs['worker']).name == 'B_worker.py':
                result['members'] = [m for m in result['members'] if m['id'] != 'm3']
                result['context'] = '\n'.join(m['content'] for m in result['members'])
                result['messages'] = [{'content': result['context']}]
                result['proof'] = []
                offset = 0
                for member in result['members']:
                    end = offset + len(member['content'])
                    result['proof'].append({'id': member['id'], 'context_start': offset,
                        'context_end': end, 'prompt_start': offset, 'prompt_end': end})
                    offset = end + 1
            _append_citations(result, spec)
            write(paths[-1], result)
        return result


@pytest.fixture
def completed_relational(tmp_path):
    fixture = fixture_with_optional_member(tmp_path / 'fixture')
    registry = QueryRegistry(tmp_path / 'registry')
    pid = registry.put(adapt_legacy((ROOT / 'scripts/rsi_delivery_worker.py').read_bytes()),
        b'{"selection":"all"}', parent_id='CONTROL-root', origin='OWNER_SPECIFIED', evidence={})
    parent = registry.get(pid)
    public = make_visible([project_summary('REPLAY_SUMMARY', {'questions': 20}, include_count_evidence=True)],
        parent_code=selector(parent['worker'].decode())[1])
    result = run_query_round(evidence=ExecutionEvidence.synthetic_control(), public=public,
        roles=Proposers(), advisory_roles=Advisory(), parent=parent, fixtures=[fixture]*4,
        registry=registry, directory=tmp_path / 'round',
        capsule_factory=lambda _: RelationalCaptureDouble(fixture), scanner=LeakScanner(),
        role_profile=DEPENDENCY)
    return fixture, registry, result, tmp_path


def test_relational_coordinator_family_resume_training_and_preview(completed_relational):
    fixture, registry, result, root = completed_relational
    assert result['adopted_candidate_id'] is not None
    assert len(result['candidate_decisions']) == 3
    assert result['family_size'] == 3
    restored = resume_query_round(directory=root/'round', registry=registry,
        fixtures=[fixture]*4, evidence=ExecutionEvidence.synthetic_control())
    assert restored == result
    training = Path(result['selected_training_directory'])
    candidate, parent = verify_training(registry.directory, training, [fixture]*4,
        result['adopted_candidate_id'], evidence=ExecutionEvidence.synthetic_control())
    assert parent['candidate_id'] == result['parent_id']
    assert candidate['candidate_id'] == result['adopted_candidate_id']
    # CONTROL fixtures can exercise training but cannot masquerade as final fresh sources.
    with pytest.raises(ValueError, match='SEALED_FINAL_HOLDOUT_REQUIRED'):
        verify_sealed(registry=registry.directory, training=training,
            train_fixtures=[fixture]*4, sealed_fixtures=[fixture]*3, used_holdout=fixture,
            candidate_id=candidate['candidate_id'], trusted_id=parent['candidate_id'],
            capsule=RelationalCaptureDouble(fixture), output=root/'unqualified-sealed')
    assert not (root/'unqualified-sealed').exists()
    for decision in load(training/'round.json')['suite_decisions']:
        assert decision['evaluator_profile'] == PROFILE
        assert not decision['real_eligible']
        assert len(decision['per_question']) == 20
        assert all(row['candidate_facts'] == row['parent_facts'] for row in decision['per_question'])
    frame = query_preview.frames(training, [fixture]*4, evidence=ExecutionEvidence.synthetic_control())
    assert len(frame['entries']) == 80
    assert all(b['evaluator_profile'] == PROFILE for b in frame['baseline'].values())
    class RecordedBatchDouble:
        business_root = Path('CONTROL_NO_BACKEND')
        def child(self, stage, paths, cwd, **kwargs):
            value = ({'status': 'PASS'} if Path(cwd).name == 'unit' else
                {'captures': [{'key': k, 'capture': b['capture']} for k, b in frame['baseline'].items()]})
            write(paths[-1], value)
            return value
    diagnostic = query_preview.replay(RecordedBatchDouble(), frame, parent['worker'],
        root/'preview', 'CONTROL-preview')
    assert not diagnostic['formal_admission']
    assert all(all(d['gates']['G'+str(i)] for i in range(1,5)) for d in diagnostic['suite_decisions'])
    assert all(set(r['candidate_facts'].values()) == {'PRESENT'}
        for d in diagnostic['suite_decisions'] for r in d['per_question'])


def test_relational_baseline_qualification_uses_same_frozen_profile(tmp_path):
    fixture = fixture_with_optional_member(tmp_path / 'fixture')
    registry = QueryRegistry(tmp_path / 'registry')
    cid = registry.put(adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),
        b'{"selection":"all"}', parent_id='CONTROL-root', origin='OWNER_SPECIFIED', evidence={})
    output = tmp_path/'qualification'
    receipt = qualify_baseline(registry=registry.directory, candidate_id=cid, fixtures=[fixture]*4,
        capsule=RelationalCaptureDouble(fixture, compress=False), output=output, origin='OWNER_SPECIFIED')
    assert receipt['status'] == 'CONTROL_BASELINE_QUALIFIED'
    assert not receipt['real_eligible']
    assert all(d['evaluator_profile'] == PROFILE for d in receipt['suite_decisions'])
    candidate, verified = verify_baseline_qualification(registry=registry.directory,
        receipt=output/'baseline-qualification.json', fixtures=[fixture]*4,
        evidence=ExecutionEvidence.synthetic_control())
    assert candidate['candidate_id'] == cid and verified == receipt


def test_multisource_bundle_counts_independent_clusters_instead_of_raw_sources(tmp_path):
    from rsik.events import EventLog
    from rsik.query_verification import claim_bundle
    sources = {f'CONTROL-source-{i}' for i in range(40)}
    freezes = ['CONTROL-freeze-1', 'CONTROL-freeze-2', 'CONTROL-freeze-3']
    log = EventLog(tmp_path/'sealed.sqlite')
    with pytest.raises(ValueError, match='SEALED_BUNDLE_SHAPE_INVALID'):
        claim_bundle(log, 'CONTROL-child', sources, freezes, tmp_path/'legacy')
    claim_bundle(log, 'CONTROL-child', sources, freezes, tmp_path/'relations',
        statistical_denominator=20, auxiliary_cases=40)
    event = log.read()[-1]['payload']
    assert event['statistical_denominator'] == 20
    assert event['auxiliary_cases'] == 40
    assert len(event['source_hashes']) == 40
    with pytest.raises(ValueError, match='SEALED_SOURCES_ALREADY_USED'):
        claim_bundle(log, 'CONTROL-child', sources, freezes, tmp_path/'again',
            statistical_denominator=20)

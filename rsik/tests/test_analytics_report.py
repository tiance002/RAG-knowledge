"""Synthetic CLI controls; no benchmark Gold, network or production fixtures."""
import hashlib
import json
from pathlib import Path

import pytest

from rsik.analytics.reader import read_artifact
from rsik.analytics.report import build_report, main
from rsik.events import EventLog


@pytest.fixture
def event_file(tmp_path):
    path = tmp_path / 'events.sqlite'
    log = EventLog(path)
    log.append('start', 'MODEL_REQUEST_STARTED', {
        'request_key': 'synthetic-r-diagnose-0', 'role': 'diagnose', 'model': 'synthetic',
        'request_hash': 'a' * 64, 'experiment_id': 'e', 'epoch': '1', 'role_profile': 'v',
        'evidence_mode': 'SYNTHETIC_CONTROL', 'parent_id': 'p', 'candidate_id': 'c',
        'compatibility_fingerprint': 'f'})
    return path


def test_cli_repeat_is_byte_identical_and_read_only(event_file, tmp_path, monkeypatch):
    before = event_file.read_bytes()
    monkeypatch.setattr(EventLog, '__init__', lambda *a, **k: pytest.fail('write-capable constructor'))
    outputs = [tmp_path / 'first', tmp_path / 'second']
    for output in outputs:
        main(['--event-log', str(event_file), '--output', str(output)])
    assert event_file.read_bytes() == before
    assert sorted(p.name for p in outputs[0].iterdir()) == ['metrics-schema.json', 'report.json', 'report.zh.md']
    for name in ('report.json', 'report.zh.md', 'metrics-schema.json'):
        assert (outputs[0] / name).read_bytes() == (outputs[1] / name).read_bytes()
    assert not Path(str(event_file) + '-wal').exists()
    assert not Path(str(event_file) + '-shm').exists()


@pytest.mark.parametrize('field,value', [('experiment_id','e'), ('epoch','1'), ('role_profile','v'),
    ('agent_role','diagnose'), ('evidence_mode','SYNTHETIC_CONTROL'), ('parent_id','p'),
    ('candidate_id','c'), ('compatibility_fingerprint','f')])
def test_all_requested_filters(event_file, field, value):
    assert build_report([event_file], filters={field:value})['projection_count'] == 2
    assert build_report([event_file], filters={field:'not-present'})['projection_count'] == 0


def test_manifest_hash_mismatch_before_output(event_file, tmp_path):
    manifest = tmp_path / 'sources.json'
    manifest.write_text(json.dumps({'sources':[{'path':str(event_file),'sha256':'0'*64}]}))
    output = tmp_path / 'out'
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        main(['--source-manifest',str(manifest),'--output',str(output)])
    assert not output.exists()


def test_no_source_schema_created(tmp_path):
    path = tmp_path/'missing.sqlite'
    with pytest.raises(FileNotFoundError):
        main(['--event-log',str(path),'--output',str(tmp_path/'out')])
    assert not path.exists()


def test_registry_does_not_follow_nested_closed_sources(event_file, tmp_path):
    root = tmp_path/'registry'; root.mkdir()
    (root/'events.sqlite').write_bytes(event_file.read_bytes())
    closed=root/'holdout';closed.mkdir();(closed/'private.sqlite').write_bytes(b'not sqlite')
    main(['--registry',str(root),'--output',str(tmp_path/'out')])


def test_closed_resolved_target_is_rejected_before_body_read(tmp_path, monkeypatch):
    closed=tmp_path/'closed'/'round.json';closed.parent.mkdir();closed.write_bytes(b'private sentinel')
    public=tmp_path/'round.json'
    original=Path.resolve
    def resolve(path, **kwargs):
        return closed if path==public else original(path, **kwargs)
    monkeypatch.setattr(Path,'resolve',resolve)
    monkeypatch.setattr(Path,'read_bytes',lambda *a:pytest.fail('closed body read'))
    with pytest.raises(ValueError,match='CLOSED_ARTIFACT'):
        read_artifact(public,'a'*64,'round')


def test_duplicate_input_does_not_double_count(event_file):
    a=build_report([event_file]);b=build_report([event_file,event_file])
    assert a['projection_count']==b['projection_count']==2
    assert a['statistics']==b['statistics']


def test_s1_round_keeps_unselected_measurement_and_deduplicates_provider_usage(tmp_path):
    round_id = 'round-' + 'a' * 32
    ledger = tmp_path / 'budget.sqlite'
    log = EventLog(ledger)
    for role, usage in [('P2_BUDGET', {'input_tokens': 12, 'output_tokens': 3,
                                      'input_tokens_details': {'cached_tokens': 7}}), ('diagnose', None)]:
        key = f'{round_id}-{role}-0'
        log.append('start:' + key, 'MODEL_REQUEST_STARTED', {'request_key': key, 'role': role, 'model': 'test'})
        log.append('end:' + key, 'MODEL_REQUEST_COMPLETED', {'request_key': key, 'usage': usage,
                   'usage_status': 'KNOWN' if usage else 'UNKNOWN', 'status': 'RECEIVED'})
    log.append('finish', 'AUTONOMY_TEST_ENDED', {'experiment_id': 'run-1', 'execution_state': 'COMPLETED'})
    round_path = tmp_path / 'round.json'
    round_path.write_text(json.dumps({'round_id': round_id, 'evidence_mode': 'SYNTHETIC_CONTROL',
        'launch_run_id': 'run-1', 'execution_state': 'COMPLETED', 'adopted_candidate_id': None,
        'attempted_replays': ['P2_BUDGET'], 'candidate_decisions': [{'angle': 'P2_BUDGET',
        'candidate_id': 'loser', 'verdict': 'OBSERVED', 'eligible_count': 1,
        'per_question': [{'qid': 'q1', 'pair_status': 'PAIRED', 'pair_identity': True,
                          'execution': 'COMPLETED', 'chars_A': 100, 'chars_B': 80}]}]}), encoding='utf8')
    archived = round_path.read_bytes()
    artifact = {'kind': 'round', 'path': round_path, 'sha256': hashlib.sha256(archived).hexdigest()}
    copy = tmp_path / 'mirror.sqlite'
    copy.write_bytes(ledger.read_bytes())
    report = build_report([ledger, ledger, copy], [artifact])
    candidate, = [r for r in report['projections'] if r['projection_type'] == 'Candidate']
    assert candidate['payload']['adopted'] is False
    assert candidate['payload']['verdict'] == 'OBSERVED'
    assert candidate['payload']['context_measurements']['candidate_chars'] == 80
    assert candidate['payload']['context_measurements']['final_answer_quality_status'] == 'UNKNOWN'
    tokens = report['tokens']
    assert tokens['totals'] == {'requests': 2, 'known_requests': 1, 'unknown_requests': 1,
        'usage_status': 'PARTIAL', 'input_tokens': 12, 'output_tokens': 3, 'total_tokens': 15}
    assert tokens['by']['round'][round_id] == tokens['totals']
    assert tokens['completed_experiments']['run-1'] == tokens['totals']
    assert tokens['by']['candidate']['loser']['total_tokens'] == 15
    assert tokens['by']['stage']['diagnose']['total_tokens'] is None
    assert tokens['by']['model']['test'] == tokens['totals']
    assert tokens['efficiency_by_experiment']['run-1']['status'] == 'UNKNOWN'
    assert round_path.read_bytes() == archived


def test_s1_efficiency_requires_explicit_independent_improvement(tmp_path):
    ledger = tmp_path / 'events.sqlite'
    log = EventLog(ledger)
    log.append('start', 'MODEL_REQUEST_STARTED', {
        'request_key': 'k', 'role': 'diagnose', 'model': 'synthetic', 'experiment_id': 'e'})
    log.append('end', 'MODEL_REQUEST_COMPLETED', {'request_key': 'k', 'usage_status': 'KNOWN',
        'usage': {'input_tokens': 10, 'output_tokens': 0}})
    # Simulated evidence metadata exercises the report joins, not a live verification.
    log.append('adopted', 'CANDIDATE_ADOPTED', {'candidate_id': 'c', 'experiment_id': 'e'})
    assert build_report([ledger])['tokens']['efficiency_by_experiment']['e']['status'] == 'UNKNOWN'
    log.append('sealed', 'QUERY_SEALED_BUNDLE_FINISHED', {
        'candidate_id': 'c', 'experiment_id': 'e', 'verdict': 'VERIFIED', 'improvement': True})
    assert build_report([ledger])['tokens']['efficiency_by_experiment']['e'] == {
        'verified_improvements': 1, 'status': 'OK', 'tokens_per_verified_improvement': 10}


def test_repeat_budget_flag(event_file,tmp_path):
    main(['--budget-ledger',str(event_file),'--budget-ledger',str(event_file),'--output',str(tmp_path/'out')])
    assert json.loads((tmp_path/'out/report.json').read_bytes())['projection_count']==2

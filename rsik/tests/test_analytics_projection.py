"""Synthetic frozen logs only; no real ledger or closed fixtures."""
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest

from rsik.analytics.reader import read_event_log, read_artifact
from rsik.analytics.projections import project_sources
from rsik.events import EventLog


def checksum(path):
    return sha256(path.read_bytes()).hexdigest()


def test_read_only_missing_hash_and_chain(tmp_path):
    path = tmp_path / 'events.sqlite'
    with pytest.raises(FileNotFoundError):
        read_event_log(path)
    assert not path.exists()
    log = EventLog(path)
    log.append('one', 'ROUND_STARTED', {'round_id': 'synthetic'})
    before = checksum(path)
    source = read_event_log(path, before)
    assert source['events'][0]['payload']['round_id'] == 'synthetic'
    assert checksum(path) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ['events.sqlite']
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        read_event_log(path, '0' * 64)
    with sqlite3.connect(path) as db:
        db.execute('DROP TRIGGER events_no_update')
        db.execute("UPDATE events SET request_hash=?", ('0' * 64,))
    with pytest.raises(ValueError, match='CHAIN_INVALID'):
        read_event_log(path)


def test_wal_rejected_without_reader_side_effects(tmp_path):
    path = tmp_path / 'events.sqlite'
    EventLog(path)
    wal = Path(str(path) + '-wal')
    wal.write_bytes(b'synthetic unfinished journal')
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    with pytest.raises(ValueError, match='NOT_FROZEN'):
        read_event_log(path)
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir()}


def test_invocation_transport_not_semantic_success_and_cost(tmp_path):
    path = tmp_path / 'events.sqlite'
    log = EventLog(path)
    log.append('start', 'MODEL_REQUEST_STARTED', {'request_key': 'r-P1_DEDUP-0', 'role': 'P1_DEDUP', 'model': 'synthetic', 'request_hash': 'a' * 64})
    log.append('end', 'MODEL_REQUEST_COMPLETED', {'request_key': 'r-P1_DEDUP-0', 'status': 'RECEIVED', 'usage_status': 'UNKNOWN', 'cost_nano_upper': None, 'usage': None})
    source = read_event_log(path)
    records = project_sources([source, source])
    assert len(records) == 2
    invocation = next(r for r in records if r['projection_type'] == 'AgentInvocation')
    assert invocation['payload']['attempted'] is True
    assert invocation['payload']['received'] is True
    assert invocation['payload']['schema_valid'] == invocation['payload']['success'] == 'UNKNOWN'
    assert invocation['dimensions']['agent_role'] == 'P1_DEDUP'
    assert invocation['dimensions']['evidence_mode'] == 'UNKNOWN'
    cost = next(r for r in records if r['projection_type'] == 'CostEvent')
    assert cost['payload']['cost_nano_upper'] is None
    assert cost['payload']['input_tokens'] is None
    changed = {**source, 'events': []}
    with pytest.raises(ValueError, match='IDENTITY_CONFLICT'):
        project_sources([source, changed])


def test_event_identity_conflict_and_no_sensitive_payload(tmp_path):
    path = tmp_path / 'events.sqlite'
    log = EventLog(path)
    log.append('hold', 'HOLDOUT_FAILED', {'candidate_id': 'c', 'reason': 'UNKNOWN', 'question': 'SECRET', 'answers': ['SECRET'], 'evidence': 'SECRET'})
    source = read_event_log(path)
    record = project_sources([source])[0]
    assert record['payload']['verdict'] == 'HOLDOUT_INCONCLUSIVE'
    assert 'SECRET' not in json.dumps(record)
    event = {**source['events'][0], 'actor': 'different'}
    # Event IDs are scoped to their log: a different valid log may legitimately
    # reuse an idempotency key and payload at another timestamp/sequence.
    assert len(project_sources([source, {**source, 'source_id': 'other', 'events': [event]}])) == 2


def test_hash_bound_round_artifact_and_modes(tmp_path):
    path = tmp_path / 'round.json'
    path.write_text(json.dumps({'round_id': 'r', 'execution_state': 'COMPLETED', 'evidence_mode': 'SYNTHETIC_CONTROL',
        'parent_id': 'p', 'role_profile': {'version': 'v'}, 'proposal_instances': {'P1_DEDUP': {'proposal_id': 'pi', 'hypothesis': 'SECRET',
        'expected_impact': 'SECRET', 'worker_patch': {'old': 'OLD SECRET', 'new': 'NEW SECRET'}}},
        'candidate_decisions': [{'angle': 'P1_DEDUP', 'candidate_id': 'c', 'verdict': 'REJECTED', 'per_question': [{'question': 'SECRET'}]}]}), encoding='utf-8')
    artifact = read_artifact(path, checksum(path), 'round')
    records = project_sources([], [artifact, artifact])
    assert {r['projection_type'] for r in records} == {'Round', 'Proposal', 'Candidate', 'Replay'}
    assert all(r['dimensions']['evidence_mode'] == 'SYNTHETIC_CONTROL' for r in records)
    assert all(r['event_ids'] == [] and r['source_refs'][0]['sha256'] == checksum(path) for r in records)
    assert 'SECRET' not in json.dumps(records)
    proposal = next(r for r in records if r['projection_type'] == 'Proposal')
    assert len(proposal['payload']['patch_sha256']) == 64
    assert proposal['payload']['hypothesis_id'] == sha256(b'SECRET').hexdigest()
    assert proposal['payload']['ast_sha256'] == 'UNKNOWN'
    assert proposal['payload']['schema_valid'] == 'UNKNOWN'
    assert proposal['payload']['static_pass'] == 'UNKNOWN'
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        read_artifact(path, '0' * 64, 'round')
    closed = tmp_path / 'holdout' / 'round.json'
    with pytest.raises(ValueError, match='CLOSED_ARTIFACT'):
        read_artifact(closed, '0' * 64, 'round')


def test_retry_provenance_is_not_a_request_lifecycle(tmp_path):
    path = tmp_path / 'events.sqlite'
    EventLog(path).append('retry-proof', 'MODEL_REQUEST_EXPLICIT_RETRY_PROVENANCE',
        {'old_key': 'old', 'new_key': 'new', 'reason': 'OWNER_AUTHORIZED_RETRY'})
    records = project_sources([read_event_log(path)])
    assert len(records) == 1
    assert records[0]['projection_type'] == 'Experiment'
    assert records[0]['payload']['event_type'] == 'MODEL_REQUEST_EXPLICIT_RETRY_PROVENANCE'


def test_closed_schema_rejected_and_pending_cost_unknown(tmp_path):
    manifest = tmp_path / 'manifest.json'
    manifest.write_text(json.dumps({'run_id': 'r', 'arms': {}, 'schema': 'rsi-query-aware-sealed/v2.3'}), encoding='utf-8')
    with pytest.raises(ValueError, match='CLOSED_ARTIFACT'):
        read_artifact(manifest, checksum(manifest), 'replay')
    path = tmp_path / 'events.sqlite'
    EventLog(path).append('start', 'MODEL_REQUEST_STARTED', {'request_key': 'r-diagnose-0', 'role': 'diagnose'})
    cost = next(r for r in project_sources([read_event_log(path)]) if r['projection_type'] == 'CostEvent')
    assert cost['payload']['pending'] is True
    assert cost['payload']['cost_nano_upper'] is None
    assert cost['payload']['usage_status'] == 'UNKNOWN'


def test_modes_and_versions_are_not_merged(tmp_path):
    sources = []
    for i, mode in enumerate(('SYNTHETIC_CONTROL', 'REAL_REPLAY', 'ENGINEERING_DEMO')):
        path = tmp_path / f'{i}.sqlite'
        EventLog(path).append('one', 'CANDIDATE_ADOPTED', {'candidate_id': 'same', 'evidence_mode': mode, 'role_profile': f'v{i}'})
        sources.append(read_event_log(path))
    records = project_sources(sources)
    assert len(records) == 3
    assert {r['dimensions']['evidence_mode'] for r in records} == {'SYNTHETIC_CONTROL', 'REAL_REPLAY', 'ENGINEERING_DEMO'}
    assert len({r['dimensions']['role_profile'] for r in records}) == 3


def test_named_projection_types_full_family_and_archive_join(tmp_path):
    from rsik.family_decision import make_family
    from rsik.workflow import placeholder
    from rsik.adjudicate import SLOTS

    path = tmp_path / 'events.sqlite'
    log = EventLog(path)
    for kind in ('EXPERIMENT_STARTED', 'ROUND_STARTED', 'PROPOSAL_QUALIFIED', 'CANDIDATE_ADOPTED',
                 'PARENT_SELECTED', 'REPLAY_FINISHED', 'ENTRY_CREATED', 'QUERY_SEALED_BUNDLE_FINISHED', 'PROMOTED'):
        log.append(kind, kind, {'round_id': 'promotion-round' if kind == 'PROMOTED' else 'r', 'candidate_id': 'c', 'approval': {'signature': 'SECRET'},
            'current': {'version_id': 'c', 'mode': 'SIMULATION_ONLY'}})
    log.append('request', 'MODEL_REQUEST_STARTED', {'request_key': 'r-diagnose-0', 'role': 'diagnose'})
    log.append('complete', 'MODEL_REQUEST_COMPLETED', {'request_key': 'r-diagnose-0', 'usage_status': 'UNKNOWN'})
    family = make_family([placeholder(slot, None, 'SLOT_DISABLED') for slot in SLOTS], enabled_slots=())
    fpath = tmp_path / 'family-decision.json'
    fpath.write_text(json.dumps(family), encoding='utf-8')
    rpath = tmp_path / 'round.json'
    rpath.write_text(json.dumps({'round_id': 'r', 'execution_state': 'COMPLETED', 'evidence_mode': 'SYNTHETIC_CONTROL',
                               'role_profile': {'version': 'synthetic-v1'}}), encoding='utf-8')
    records = project_sources([read_event_log(path)], [read_artifact(fpath, checksum(fpath), 'family'),
                                                     read_artifact(rpath, checksum(rpath), 'round')])
    assert {r['projection_type'] for r in records} == {'Experiment', 'Round', 'Proposal', 'Candidate', 'Parent',
        'Replay', 'MemoryEvent', 'Holdout', 'Promotion', 'AgentInvocation', 'CostEvent', 'FamilyDecision'}
    invocation = next(r for r in records if r['projection_type'] == 'AgentInvocation')
    assert invocation['dimensions']['role_profile'] == 'synthetic-v1'
    assert len(invocation['source_refs']) == 2
    assert len(invocation['event_ids']) == 2
    projected_family = next(r for r in records if r['projection_type'] == 'FamilyDecision')
    assert projected_family['payload']['complete'] is True
    assert len(projected_family['payload']['decisions']) == 3
    assert 'SECRET' not in json.dumps(records)


def test_simulation_is_demo_and_histories_have_source_namespace(tmp_path):
    sources = []
    for name in ('first', 'second'):
        path = tmp_path / (name + '.sqlite')
        log = EventLog(path)
        log.append('promote', 'PROMOTED', {'mode': 'SIMULATION_ONLY',
            'current': {'version_id': 'B', 'operation_id': name, 'environment': name}})
        sources.append(read_event_log(path))
    records = project_sources(sources)
    assert {r['dimensions']['evidence_mode'] for r in records} == {'ENGINEERING_DEMO'}
    assert len({r['payload']['history_source_id'] for r in records}) == 2
    assert {r['payload']['history_environment'] for r in records} == {'first', 'second'}
    assert all(r['payload']['sequence'] == 1 for r in records)


def test_conflicting_simulation_label_rejected(tmp_path):
    path = tmp_path / 'events.sqlite'
    EventLog(path).append('one', 'PROMOTED', {'mode': 'SIMULATION_ONLY', 'evidence_mode': 'REAL_REPLAY'})
    with pytest.raises(ValueError, match='SIMULATION_MODE_CONFLICT'):
        project_sources([read_event_log(path)])

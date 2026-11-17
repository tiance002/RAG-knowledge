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


def test_repeat_budget_flag(event_file,tmp_path):
    main(['--budget-ledger',str(event_file),'--budget-ledger',str(event_file),'--output',str(tmp_path/'out')])
    assert json.loads((tmp_path/'out/report.json').read_bytes())['projection_count']==2

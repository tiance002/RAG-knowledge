from hashlib import sha256
import json

import pytest

from rsik.panel_resume import frozen_pause, publish_carried_receipt


@pytest.fixture
def pause(tmp_path):
    source = tmp_path / 'original'
    source.mkdir()
    (source / 'attempts').mkdir()
    binding = {'arm_id': 'code', 'config_sha256': 'config', 'panel_sha256': 'panel',
               'execution_sha256': 'execution'}
    cases = [{'item_id': str(i), 'ordinal': i} for i in (21, 22, 23)]

    def save(name, value):
        raw = json.dumps(value).encode()
        (source / name).write_bytes(raw)
        return sha256(raw).hexdigest()

    case_hash = save('21-case.json', {**binding, 'ordinal': 21, 'case_id': '21',
                                    'dev60_guard': {'complete': False}, 'completion_unknown': True})
    for ordinal in (22, 23):
        save(f'{ordinal}-state.json', {'ordinal': ordinal, 'case_id': str(ordinal), 'state': 'PENDING'})
    index_hash = save('case-files.json', {'cases': 1, 'files': {'21-case.json': case_hash}})
    digest = save('completion.json', {**binding, 'attempt_id': 'first', 'state': 'FAILED',
                  'case_count': 3, 'valid_case_count': 0, 'capacity_paused': True,
                  'cases_all_attempted': False, 'pending_ordinals': [22, 23],
                  'case_files_sha256': index_hash})
    return source, digest, binding, cases


def test_pause_preserves_unknown_and_resumes_only_unsent(pause, tmp_path):
    receipts, valid, pending = frozen_pause(*pause, 'second')
    source, _, _, cases = pause
    assert pending == cases[1:]
    assert valid == 0
    assert json.loads(receipts['21-case.json'])['completion_unknown'] is True
    destination = tmp_path / 'second'
    destination.mkdir()
    publish_carried_receipt(destination, '21-case.json', receipts['21-case.json'])
    assert (destination / '21-case.json').read_bytes() == (source / '21-case.json').read_bytes()


@pytest.mark.parametrize('artifact,raw', [
    ('21-case.json', b'{}'), ('case-files.json', b'{}'), ('completion.json', b'{}'),
    ('attempts/22-try1.json', b'{}'),
    ('22-state.json', b'{"ordinal":22,"case_id":"22","state":"RUNNING"}'),
])
def test_tampering_or_evidence_of_attempt_blocks_resume(pause, artifact, raw):
    (pause[0] / artifact).write_bytes(raw)
    with pytest.raises(ValueError):
        frozen_pause(*pause, 'second')


def test_resume_rejects_reused_attempt_id_and_changed_execution(pause):
    with pytest.raises(ValueError):
        frozen_pause(*pause, 'first')
    source, digest, binding, cases = pause
    with pytest.raises(ValueError):
        frozen_pause(source, digest, {**binding, 'execution_sha256': 'other'}, cases, 'second')

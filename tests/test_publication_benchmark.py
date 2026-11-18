"""Small portable contracts for the real publication experiment harness.

The child establishes local-only settings before importing any application
module. No test here runs the large performance fixture or calls a provider.
Both independently loaded arms use current public source. Historical P1/P2
parity is established by the separately frozen experiment, not these contracts.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESULT_PREFIX = 'PUBLICATION_CONTRACT_RESULT='


def _probe(tmp_path, mode, arm='both'):
    environment = dict(os.environ)
    environment.update(
        DATABASE_URL='sqlite://', REDIS_URL='memory://',
        CHROMA_HOST='', CHROMA_DB_DIR=str(tmp_path / 'unused-persistent-chroma'),
        RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false',
        ANONYMIZED_TELEMETRY='False', PYTHONDONTWRITEBYTECODE='1',
    )
    result = subprocess.run(
        [sys.executable, '-X', 'utf8', str(Path(__file__).resolve()),
         '--publication-probe', mode, arm, str(tmp_path)],
        cwd=ROOT, env=environment, capture_output=True, text=True,
        encoding='utf-8', errors='replace', timeout=90,
    )
    assert result.returncode == 0, (
        f'Publication contract child exited {result.returncode}\n'
        f'STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}'
    )
    reports = [line[len(RESULT_PREFIX):] for line in result.stdout.splitlines()
               if line.startswith(RESULT_PREFIX)]
    assert len(reports) == 1, result.stdout + '\n' + result.stderr
    report = json.loads(reports[0])
    assert report['status'] == 'PASS'
    assert not list(tmp_path.glob('*.db*')), 'Owned disposable SQLite files remain'
    assert not (tmp_path / 'unused-persistent-chroma').exists()
    return report


def test_two_arms_publish_real_full_equivalent_state(tmp_path):
    report = _probe(tmp_path, 'success')
    assert report['arms'] == ['reference', 'candidate']
    assert report['parent_count'] == 2
    assert report['new_children'] == 6
    assert report['full_semantics_match']


def test_archive_record_deduplicates_exact_bytes_and_preserves_bad_identity(tmp_path):
    report = _probe(tmp_path, 'archive')
    assert report['snapshot_files'] == 2
    assert report['same_payload_reused'] and report['original_unmodified']
    assert report['bad_declared_identity_preserved']


def test_explicit_source_directories_supply_both_arms_without_fallback(tmp_path):
    report = _probe(tmp_path, 'source-dirs')
    assert report['explicit_source_origins']
    assert report['database_connections'] == 0


@pytest.mark.parametrize('arm', ['reference', 'candidate'])
def test_corrupted_later_real_read_cannot_publish_sql_or_complete_receipt(tmp_path, arm):
    report = _probe(tmp_path, 'corrupt', arm)
    assert report['read_calls'] >= 2
    assert report['replacement_calls'] == report['claim_calls'] == 1
    assert report['original_sql_unchanged'] and report['staging_only']


@pytest.mark.parametrize('arm', ['reference', 'candidate'])
def test_unknown_read_propagates_original_error_without_retry(tmp_path, arm):
    report = _probe(tmp_path, 'unknown', arm)
    assert report['read_calls'] == 2
    assert report['replacement_calls'] == report['claim_calls'] == 1
    assert report['original_error_identity']
    assert report['original_sql_unchanged'] and report['staging_only']


def _child(mode, selected_arm, directory):
    # This branch is a fresh Python process. Do not move app imports to module
    # scope: the calling pytest process may already hold an unrelated engine.
    import importlib
    import copy
    from collections import Counter
    import socket

    # Establish hard guards before application imports as well as safe settings.
    # Only the per-sample SQLite files may open a SQLAlchemy connection. Chroma
    # is the installed in-process EphemeralClient and needs no network socket.
    blocked = {'network': 0, 'database': 0}

    def reject_network(*args, **kwargs):
        blocked['network'] += 1
        raise AssertionError('PUBLICATION_CONTRACT_OUTBOUND_CONNECTION')

    socket.socket.connect = reject_network
    socket.socket.connect_ex = reject_network
    socket.create_connection = reject_network
    from sqlalchemy import event
    from sqlalchemy.engine import Engine

    @event.listens_for(Engine, 'do_connect')
    def isolated_sqlite_only(dialect, connection_record, cargs, cparams):
        database = Path(cargs[0]) if cargs and cargs[0] != ':memory:' else None
        if (dialect.name != 'sqlite' or database is None or
                database.resolve().parent != directory.resolve() or database.suffix != '.db'):
            blocked['database'] += 1
            raise AssertionError('PUBLICATION_CONTRACT_UNOWNED_DATABASE_CONNECTION')

    sys.path.insert(0, str(ROOT / 'scripts'))
    helper = importlib.import_module('benchmark_publication_helpers')
    from sqlmodel import Session, select

    if mode == 'source-dirs':
        paths = {name: directory / name for name in ('reference', 'candidate')}
        expected = {}
        for name, path in paths.items():
            path.mkdir()
            expected[name] = {}
            for service in helper.SERVICE_NAMES:
                raw = f'# Explicit synthetic {name} {service} source\n'.encode('utf-8')
                (path / (service + '.py')).write_bytes(raw)
                expected[name][service] = raw
        actual = helper.freeze_sources(paths['reference'], paths['candidate'])
        assert actual == expected
        assert actual['reference'] != actual['candidate']
        assert blocked == {'network': 0, 'database': 0}, blocked
        return {'status': 'PASS', 'explicit_source_origins': True, 'database_connections': 0}

    if mode == 'archive':
        logical = {'chunks': [['new:0', '中文 evidence']], 'vectors': [[0.5, -0.25]]}
        digest = helper.digest(helper.canonical(logical).encode('utf-8'))
        run = {'arm': 'contract', 'total_ms': 1.25,
               'semantic': {'logical': logical, 'full_logical_sha256': digest}}
        original = copy.deepcopy(run)
        snapshotdir = directory / 'snapshots'
        first = helper.archive_record(run, snapshotdir)
        second = helper.archive_record(run, snapshotdir)
        assert first['semantic']['snapshot_ref'] == second['semantic']['snapshot_ref']
        path = Path(first['semantic']['snapshot_ref'])
        assert path.name == digest + '.json'
        assert path.read_bytes() == helper.canonical(logical).encode('utf-8')
        assert helper.digest(path.read_bytes()) == first['semantic']['snapshot_sha256'] == digest
        assert len(list(snapshotdir.glob('*.json'))) == 1
        assert 'logical' not in first['semantic'] and run == original
        changed = copy.deepcopy(run)
        changed['semantic']['logical']['vectors'][0][0] = 0.75
        changed['semantic']['full_logical_sha256'] = 'deliberately-wrong-declared-identity'
        changed_original = copy.deepcopy(changed)
        archived_changed = helper.archive_record(changed, snapshotdir)
        changed_path = Path(archived_changed['semantic']['snapshot_ref'])
        assert changed_path != path
        assert changed_path.read_bytes() == helper.canonical(changed['semantic']['logical']).encode('utf-8')
        assert helper.digest(changed_path.read_bytes()) == archived_changed['semantic']['snapshot_sha256']
        assert archived_changed['semantic']['full_logical_sha256'] == 'deliberately-wrong-declared-identity'
        assert changed == changed_original and run == original
        assert len(list(snapshotdir.glob('*.json'))) == 2
        assert blocked == {'network': 0, 'database': 0}, blocked
        return {'status': 'PASS', 'snapshot_files': 2, 'same_payload_reused': True,
                'original_unmodified': True, 'bad_declared_identity_preserved': True}

    current_sources = ROOT / 'backend/app/services'
    sources = helper.freeze_sources(current_sources)
    assert sources['reference'] == sources['candidate']
    assert all(raw == (current_sources / (name + '.py')).read_bytes()
               for raw_arm in sources.values() for name, raw in raw_arm.items())
    rows = helper.publication_fixture(parents=2, children_per_parent=3, vocabulary=20)
    original_rows = copy.deepcopy(rows)
    arms = ['reference', 'candidate'] if selected_arm == 'both' else [selected_arm]
    modules = {name: helper.load_arm(name, sources[name], directory) for name in arms}
    for name, arm in modules.items():
        assert arm['index_builds'].VectorStoreService is arm['vector_store'].VectorStoreService
        assert arm['index_builds'].get_bm25_index is arm['bm25'].get_bm25_index
        assert all(Path(module.__file__).read_bytes() == sources[name][key]
                   for key, module in arm.items())

    if mode == 'success':
        runs = []
        for name in arms:
            run = helper.run_one(name, modules[name], rows, directory, 'contract')
            helper.validate_run(run)
            logical = run['semantic']['logical']
            assert run['semantic']['parent_count'] == 2
            assert run['semantic']['child_count'] == 6
            assert logical['stats']['total_docs'] == len(logical['indices']) == 7
            assert len(logical['chunks']) == 9
            parents = {row[0] for row in logical['chunks'] if row[2] == 'parent'}
            children = {row[0]: row for row in logical['chunks'] if row[2] == 'child'}
            assert len(parents) == 2 and all(row[3] in parents for row in children.values())
            assert not parents.intersection(row[0] for row in logical['indices'])
            assert not parents.intersection(row[2] for row in logical['postings'])
            assert not parents.intersection(row[0] for row in logical['vectors'])
            assert {row[0] for row in logical['indices']} == set(children) | {'background:0'}
            assert logical['posting_df'] == dict(Counter(row[1] for row in logical['postings']))
            assert logical['queries']['prior'] == {'all': [], 'top20': []}
            assert len(logical['queries']['fresh']['all']) == 6
            assert logical['queries']['fresh']['top20'] == logical['queries']['fresh']['all']
            assert all(len(row[3]) == 256 for row in logical['vectors'])
            assert sum(row[0].startswith('new:') for row in logical['vectors']) == 6
            assert sum(row[0].startswith('old:') for row in logical['vectors']) == 6
            assert len(logical['vector_query_membership_distances']) == len(logical['vectors']) == 13
            expected_input_hash = helper.digest(helper.canonical(
                {'rows': rows, 'embeddings': [helper.embeddings_for(values) for values in rows]}).encode())
            assert run['input_sha256'] == expected_input_hash
            assert run['sqlite_policy']['journal_mode'] == 'wal'
            assert run['sqlite_policy']['wal_autocheckpoint'] == 0
            assert run['total_ms'] > 0
            stages = {phase['stage'] for phase in run['phases']}
            assert {'index_build_stage', 'index_build_vector_write', 'index_build_vector_verify',
                    'index_build_sql_replace', 'index_build_recount', 'index_build_commit'} <= stages
            assert run['ledger_calls'] == {'guard_calls': 1, 'guard_successes': 1,
                                           'complete_calls': 1, 'complete_successes': 1}
            assert all(run['cleanup'][key] for key in
                       ('collection_deleted', 'engine_disposed', 'database_removed'))
            assert rows == original_rows, 'Publication mutated the shared experiment input'
            runs.append(run)
        helper.assert_equal(runs[0]['semantic'], runs[1]['semantic'])
        assert runs[0]['semantic']['logical'] == runs[1]['semantic']['logical']
        # Verify the comparison uses the complete stored state rather than only
        # aggregate counts: changing one coordinate must change its identity.
        changed = copy.deepcopy(runs[1]['semantic'])
        changed['logical']['vectors'][0][3][0] += 0.125
        changed['full_logical_sha256'] = helper.digest(helper.canonical(changed['logical']).encode())
        try:
            helper.assert_equal(runs[0]['semantic'], changed)
        except AssertionError:
            pass
        else:
            raise AssertionError('Changed stored vector was accepted as equivalent')
        assert blocked == {'network': 0, 'database': 0}, blocked
        return {'status': 'PASS', 'arms': arms, 'parent_count': 2, 'new_children': 6,
                'full_semantics_match': True, 'denied_connections': blocked}

    assert mode in ('corrupt', 'unknown') and len(arms) == 1
    arm = modules[selected_arm]
    index, vector = arm['index_builds'], arm['vector_store']
    dispatch = helper.dispatch
    counters = {'reads': 0, 'replacement': 0, 'claims': 0}
    observation = {}
    original_publish = index.publish_index_build
    original_get = vector.VectorStoreService._ingest_get
    original_claim = dispatch.claim_task
    unknown = RuntimeError('TEST_PROVIDER_READ_UNKNOWN')
    unknown.provider_status = 'UNKNOWN'

    def sql_projection():
        with Session(dispatch.engine) as session:
            values = {}
            for model in (helper.DocumentChunk, helper.ChunkIndex, helper.BM25Posting,
                          helper.IndexStats, helper.FileDocument):
                records = [row.model_dump() for row in session.exec(select(model)).all()]
                values[model.__tablename__] = sorted(records, key=repr)
            values['existing_builds'] = sorted(
                [row.model_dump() for row in session.exec(select(helper.FileIndexBuild)).all()
                 if row.task_id != 'replacement'], key=repr)
            return values

    def observe_publish(claim, *args, **kwargs):
        if claim.task_id != 'replacement':
            return original_publish(claim, *args, **kwargs)
        counters['replacement'] += 1
        observation['before'] = sql_projection()
        try:
            return original_publish(claim, *args, **kwargs)
        except BaseException:
            observation['after'] = sql_projection()
            with Session(dispatch.engine) as session:
                receipt = session.get(dispatch.FileTaskReceipt, 'replacement')
                attempt = session.get(dispatch.FileTaskAttempt, ('replacement', 1))
                builds = session.exec(select(helper.FileIndexBuild)).all()
                assert receipt.status == attempt.outcome == 'RUNNING'
                assert receipt.result is None and receipt.attempt_no == 1
                assert receipt.owner_token == attempt.owner_token
                assert [(build.state, build.chunk_count) for build in builds
                        if build.task_id == 'replacement'] == [('STAGING', 8)]
                assert all(build.state == 'ACTIVE' for build in builds
                           if build.task_id != 'replacement')
            raise

    def observe_claim(task_id, *args, **kwargs):
        if task_id == 'replacement':
            counters['claims'] += 1
        return original_claim(task_id, *args, **kwargs)

    def later_read(self, collection, **kwargs):
        # Delegate to installed real Chroma, then damage only a later reply.
        output = original_get(self, collection, **kwargs)
        with Session(dispatch.engine) as session:
            receipt = session.get(dispatch.FileTaskReceipt, 'replacement')
            replacing = receipt is not None and receipt.status == 'RUNNING'
        if replacing:
            counters['reads'] += 1
            if counters['reads'] == 2:
                if mode == 'unknown':
                    raise unknown
                output['documents'][0] += ' TEST_LATER_READ_CORRUPTION'
        return output

    # Six child vectors cross three real add/get calls under a deliberately
    # small batch limit. This does not change the performance experiment.
    vector.VectorStoreService._batch_size = lambda self: 2
    vector.VectorStoreService._ingest_get = later_read
    index.publish_index_build = observe_publish
    dispatch.claim_task = observe_claim
    try:
        helper.run_one(selected_arm, arm, rows, directory, 'fault-contract')
    except BaseException as exc:
        if mode == 'unknown':
            assert exc is unknown and exc.provider_status == 'UNKNOWN'
        else:
            assert str(exc) == 'BUILD_VECTOR_VERIFICATION_FAILED'
        evidence = exc.publication_evidence
        assert evidence['terminal']['receipt_status'] == 'RUNNING'
        assert evidence['terminal']['active_build_unchanged']
        assert evidence['ledger_calls'] == {}, 'SQL fence/completion must not run before full vector verification'
        assert all(evidence['cleanup'][key] for key in
                   ('collection_deleted', 'engine_disposed', 'database_removed'))
    else:
        raise AssertionError('Fault was reported as successful publication')
    assert observation['before'] == observation['after']
    assert counters['replacement'] == counters['claims'] == 1
    assert counters['reads'] == 2
    assert rows == original_rows
    assert blocked == {'network': 0, 'database': 0}, blocked
    return {'status': 'PASS', 'arm': selected_arm, 'read_calls': counters['reads'],
            'replacement_calls': counters['replacement'], 'claim_calls': counters['claims'],
            'original_sql_unchanged': True, 'staging_only': True,
            'original_error_identity': mode == 'unknown', 'denied_connections': blocked}


if __name__ == '__main__':
    assert len(sys.argv) == 5 and sys.argv[1] == '--publication-probe'
    output = _child(sys.argv[2], sys.argv[3], Path(sys.argv[4]))
    print(RESULT_PREFIX + json.dumps(output, sort_keys=True))

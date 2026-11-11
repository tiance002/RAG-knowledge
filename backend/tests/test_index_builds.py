from types import SimpleNamespace

import chromadb
import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase
from app.services import index_builds
from app.services.bm25 import BM25Index
from app.services.vector_store import VectorStoreService
from app.tasks import dispatch


@pytest.fixture
def environment(monkeypatch):
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    client = chromadb.EphemeralClient()
    monkeypatch.setattr(index_builds, 'engine', engine)
    monkeypatch.setattr(VectorStoreService, '_client', client)
    monkeypatch.setattr(dispatch, 'guard_claim_in_session', lambda session, claim: claim.valid)
    monkeypatch.setattr(dispatch, 'complete_in_session', lambda session, claim, result: claim.valid)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='m', vlm_model='v'))
        session.add(FileDocument(id='f', kb_id='kb', name='f', local_path='f', size=1, desired_generation=1))
        session.add(DocumentChunk(id='old', file_id='f', content='old evidence'))
        session.commit()
        BM25Index().index_chunk(session, 'old', 'old evidence', 'kb')
        session.commit()
    VectorStoreService().add_documents('kb', [{'id':'old', 'content':'old evidence', 'metadata':{'file_id':'f'}}], [[1., 0.]])
    return engine


def claim(valid=True):
    return SimpleNamespace(file_id='f', task_id='t', target_generation=1, attempt_no=1, valid=valid)


def test_build_publication_and_failed_fence_preserve_old_projection(environment):
    with pytest.raises(RuntimeError, match='BUILD_CLAIM_LOST'):
        index_builds.publish_index_build(claim(False), [{'content':'staged evidence'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        assert [c.content for c in session.exec(select(DocumentChunk)).all()] == ['old evidence']
        assert session.get(FileDocument, 'f').active_build_id is None
        assert BM25Index().search(session, 'old', 'kb')
        assert session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'STAGING')).first()
    result = index_builds.publish_index_build(claim(), [{'content':'new evidence'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        chunks = session.exec(select(DocumentChunk)).all()
        assert [c.content for c in chunks] == ['new evidence']
        assert session.get(FileDocument, 'f').active_build_id == result['build_id']
        assert BM25Index().search(session, 'new', 'kb')
        assert not BM25Index().search(session, 'old', 'kb')
        assert session.get(FileIndexBuild, result['build_id']).state == 'ACTIVE'
        retired = session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'RETIRED')).first()
        assert retired
    index_builds.cleanup_retired_builds([retired.build_id], grace_seconds=0)
    with Session(environment) as session:
        assert session.get(FileIndexBuild, retired.build_id).state == 'CLEANED'


def test_vector_allowlist_filters_before_top_k(environment):
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'stale', 'content':'stale', 'metadata':{'file_id':'f'}}], [[0., 1.]])
    rows = store.query('kb', [0., 1.], top_k=1, allowed_ids=['old'])
    assert [r['id'] for r in rows] == ['old']
    assert store.query('kb', [0., 1.], top_k=1, allowed_ids=[]) == []


def test_abandoned_build_is_swept_again_after_late_write(environment):
    with pytest.raises(RuntimeError, match='BUILD_CLAIM_LOST'):
        index_builds.publish_index_build(claim(False), [{'content':'late'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        build = session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'STAGING')).one()
        import json
        vector_id = json.loads(build.manifest)['vector_ids'][0]
    index_builds.sweep_builds()
    collection = VectorStoreService()._existing_collection('kb')
    assert not collection.get(ids=[vector_id])['ids']
    collection.add(ids=[vector_id], documents=['late'], metadatas=[{'file_id':'f'}], embeddings=[[0., 1.]])
    index_builds.sweep_builds()
    assert not collection.get(ids=[vector_id])['ids']
    with Session(environment) as session:
        assert session.get(FileIndexBuild, build.build_id).state == 'ABANDONED'


def test_published_metadata_and_legacy_exact_cleanup(environment):
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'other', 'content':'keep', 'metadata':{'file_id':'different'}}], [[.5, .5]])
    result = index_builds.publish_index_build(claim(), [{'content':'new', 'page_number':4,
        'heading_text':'Heading', 'heading_level':2, 'image_path':'p'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        chunk = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        value = store._existing_collection('kb').get(ids=[chunk.id], include=['metadatas'])
        metadata = value['metadatas'][0]
        assert metadata['build_id'] == result['build_id']
        assert (metadata['page_number'], metadata['heading_text'], metadata['image_path']) == (4, 'Heading', 'p')
    collection = store._existing_collection('kb')
    assert collection.get(ids=['old'])['ids'] == ['old']  # grace for in-flight readers
    with Session(environment) as session:
        retired = session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'RETIRED')).one()
    index_builds.cleanup_retired_builds([retired.build_id], grace_seconds=0)
    assert not collection.get(ids=['old'])['ids']
    assert collection.get(ids=['other'])['ids'] == ['other']


def test_real_async_vector_route_filters_unpublished_candidates(environment, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.retrieval import RetrievalService
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'staged', 'content':'staged', 'metadata':{'file_id':'f'}}], [[0., 1.]])
    service = RetrievalService()
    monkeypatch.setattr(service, '_embed_query_for_kb', AsyncMock(return_value=[0., 1.]))
    with Session(environment) as session:
        rows, metrics = asyncio.run(service._vector_search(session, 'q', ['kb'], 1, 0.0))
    assert [row['id'] for row in rows] == ['old']
    assert metrics['vector_count'] == 1


def test_http_chroma_query_supports_id_allowlist(tmp_path, monkeypatch):
    import os
    import socket
    import subprocess
    import time
    from pathlib import Path
    executable = Path(os.sys.executable).parent / 'chroma.exe'
    if not executable.exists():
        pytest.skip('Chroma server executable is unavailable')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    log = tmp_path / 'chroma.log'
    with log.open('wb') as output:
        process = subprocess.Popen([str(executable), 'run', '--host', '127.0.0.1', '--port', str(port),
                                    '--path', str(tmp_path / 'chroma')], stdout=output, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            deadline = time.monotonic() + 20
            while True:
                try:
                    client = chromadb.HttpClient(host='127.0.0.1', port=port)
                    client.heartbeat()
                    break
                except Exception:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('Chroma server did not start: ' + log.read_text(errors='replace'))
                    time.sleep(.2)
            monkeypatch.setattr(VectorStoreService, '_client', client)
            store = VectorStoreService()
            store.add_documents('kb', [{'id':'live', 'content':'live', 'metadata':{'file_id':'f', 'build_id':'active'}},
                                       {'id':'stale', 'content':'stale', 'metadata':{'file_id':'f', 'build_id':'old'}}],
                                [[1., 0.], [0., 1.]])
            assert [r['id'] for r in store.query('kb', [0., 1.], top_k=1, allowed_ids=['live'])] == ['live']
            assert [r['id'] for r in store.query('kb', [0., 1.], top_k=1, allowed_build_ids=['active'], allowed_ids=[])] == ['live']
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


def test_verified_provenance_survives_unchanged_submit_only(environment, tmp_path):
    import json
    from app.services.pdf_provenance import write_manifest
    item = SimpleNamespace(content='new', page_number=4, content_type='text',
                           metadata={'source_locator': {'page':4, 'bbox':[1, 2, 3, 4]}})
    path = write_manifest(tmp_path, 'source-sha', [], [item])
    checkpoint = {'source_sha256':'source-sha', 'provenance_manifest':path}
    first = index_builds.publish_index_build(claim(), [{'content':'new', 'page_number':4}], [[0., 1.]], 'm', checkpoint)
    with Session(environment) as session:
        chunk = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        assert json.loads(chunk.source_metadata_json)['source_locators'] == [{'page':4, 'bbox':[1, 2, 3, 4]}]
        chunk_id = chunk.id
    index_builds.publish_index_build(claim(), [{'id':chunk_id, 'content':'new', 'page_number':4}], [[0., 1.]], 'm')
    with Session(environment) as session:
        chunk = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one()
        assert json.loads(chunk.source_metadata_json)['source_sha256'] == 'source-sha'
        chunk_id = chunk.id
    index_builds.publish_index_build(claim(), [{'id':chunk_id, 'content':'edited', 'page_number':4}], [[0., 1.]], 'm')
    with Session(environment) as session:
        assert session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'f')).one().source_metadata_json is None


def test_late_worker_cannot_publish_after_delete(environment):
    from datetime import datetime
    with Session(environment) as session:
        doc = session.get(FileDocument, 'f')
        doc.deleted_at = datetime.utcnow()
        doc.desired_generation = 2
        session.add(doc)
        session.commit()
    with pytest.raises(RuntimeError, match='BUILD_GENERATION_CHANGED'):
        index_builds.publish_index_build(claim(), [{'content':'resurrection'}], [[0., 1.]], 'm')
    with Session(environment) as session:
        doc = session.get(FileDocument, 'f')
        assert doc.deleted_at is not None and doc.active_build_id is None
        from app.services.retrieval import RetrievalService
        assert RetrievalService._vector_scope(session, 'kb') == ([], [])
        assert [c.content for c in session.exec(select(DocumentChunk)).all()] == ['old evidence']


def test_batched_vector_operations_merge_build_and_legacy_results(environment, monkeypatch):
    store = VectorStoreService()
    monkeypatch.setattr(store, '_batch_size', lambda: 2)
    docs = [{'id':f'new-{i}', 'content':f'new {i}',
             'metadata':{'file_id':'f', 'build_id':f'b-{i}'}} for i in range(5)]
    vectors = [[float(i) / 10, 1.] for i in range(5)]
    store.add_documents('kb', docs, vectors)
    fetched = store.get_documents('kb', [d['id'] for d in docs], include=['metadatas','embeddings'])
    assert set(fetched['ids']) == {d['id'] for d in docs}
    # More build IDs and legacy IDs than one batch; stale closer hits excluded.
    result = store.query('kb', [0., 1.], top_k=3, allowed_build_ids=['b-1','b-3','b-4'],
                         allowed_ids=['missing-1','old','missing-2'])
    assert [r['id'] for r in result] == ['new-1','new-3','new-4']
    store.delete_ids('kb', [d['id'] for d in docs])
    assert store.get_documents('kb', [d['id'] for d in docs])['ids'] == []
    store.delete_collection('kb')
    store.delete_ids('kb', ['already-absent'])


def test_mixed_legacy_and_versioned_retrieval_uses_sql_scope(environment, monkeypatch):
    import asyncio
    from unittest.mock import AsyncMock
    from app.services.retrieval import RetrievalService
    result = index_builds.publish_index_build(claim(), [{'content':'versioned alpha'}], [[0.,1.]], 'm')
    with Session(environment) as session:
        session.add(FileDocument(id='legacy', kb_id='kb', name='legacy', local_path='legacy', size=1))
        session.add(DocumentChunk(id='legacy-id', file_id='legacy', content='legacy beta'))
        session.commit()
    store = VectorStoreService()
    store.add_documents('kb', [{'id':'legacy-id','content':'legacy beta','metadata':{'file_id':'legacy'}},
                               {'id':'unpublished','content':'unpublished','metadata':{'file_id':'f','build_id':'other'}}],
                        [[1.,0.], [0.,1.]])
    svc = RetrievalService()
    monkeypatch.setattr(svc, '_embed_query_for_kb', AsyncMock(return_value=[0.,1.]))
    with Session(environment) as session:
        assert svc._vector_scope(session, 'kb') == (['legacy-id'], [result['build_id']])
        rows, _ = asyncio.run(svc._vector_search(session, 'q', ['kb'], 3, 0.0))
    assert {r['content'] for r in rows} == {'versioned alpha', 'legacy beta'}


class PublicationVectors:
    """Full readback substitute; provider/model requests are never made."""
    def __init__(self, on_add=None, damage=False, failure=None):
        self.on_add, self.damage, self.failure = on_add, damage, failure
        self.documents, self.vectors = [], []
        self.add_calls = 0

    def validate_dimension(self, kb_id, vector):
        assert kb_id == 'kb' and len(vector) == 2

    def add_documents(self, kb_id, documents, vectors):
        self.add_calls += 1
        if self.on_add: self.on_add()
        self.documents, self.vectors = list(documents), list(vectors)
        if self.failure: raise self.failure

    def get_documents(self, kb_id, ids, include):
        result = {'ids': [row['id'] for row in self.documents],
                  'documents': [row['content'] for row in self.documents],
                  'metadatas': [dict(row['metadata']) for row in self.documents],
                  'embeddings': self.vectors}
        assert result['ids'] == ids and include == ['documents', 'metadatas', 'embeddings']
        if self.damage: result['metadatas'][-1]['build_id'] = 'wrong-build'
        return result


def _large_parent_child_rows():
    from app.services.parent_child import build_parent_child_chunks, ParentChildConfig
    from app.services.parser import ParsedChunk
    source = []
    for parent_index in range(32):
        children = []
        for child_index in range(32):
            prefix = f'section{parent_index:02d} child{child_index:02d} '
            children.append((prefix + 'alpha beta gamma delta ' * 100)[:2048])
        source.append(''.join(children))
    text = ''.join(source)
    config = ParentChildConfig(parent_chunk_size=65536, parent_chunk_overlap=0,
                               child_chunk_size=2048, child_chunk_overlap=0)
    parsed = build_parent_child_chunks([ParsedChunk(text, None, 'text')], config)
    rows, parent_offsets, offset = [], {}, 0
    for piece in parsed:
        row = {**piece.metadata, 'content': piece.content, 'content_type': piece.content_type}
        if row['chunk_type'] == 'parent':
            parent_offsets[row['id']] = offset
            start = offset; offset += len(piece.content)
        else:
            start = parent_offsets[row['parent_id']] + row['parent_local_start']
        # Exact offsets are known only because this fixture's parsed text IS the
        # synthetic source bytes (ASCII); this does not infer PDF source offsets.
        row.update(start_offset=start, end_offset=start + len(piece.content))
        rows.append(row)
    return rows, text


def test_large_parent_child_bulk_publication_and_optional_historical_ab(tmp_path, monkeypatch, request):
    """2MiB source/1024children; compare real publication with identical peers."""
    import hashlib
    import importlib.util
    import json
    import subprocess
    from pathlib import Path
    from time import perf_counter
    from sqlalchemy import event, func
    from app.models import ChunkIndex, ContentType, IndexStats
    from app.services import observability

    repo = Path(__file__).resolve().parents[2]
    baseline_source, baseline_origin = None, 'NOT_RUN_HISTORICAL_BASELINE_UNAVAILABLE'
    # Fresh clones do not contain excluded var/archives. Prefer the pinned public
    # Git blob, offline; its LF bytes equal the historical snapshot modulo CRLF.
    try:
        historical = subprocess.run(['git', 'show',
            '2041bfb5355bab5c0a4e123d413dabcbd619125a:backend/app/services/index_builds.py'],
            cwd=repo, capture_output=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        historical = None
    if historical is not None and historical.returncode == 0:
        baseline_source, baseline_origin = historical.stdout, 'PUBLIC_GIT_2041bfb'
    else:
        archived = repo / 'var/ecs-readiness-20261109/minimal/backend/app/services/index_builds.py'
        if archived.is_file():
            baseline_source, baseline_origin = archived.read_bytes(), 'OPTIONAL_LOCAL_HISTORICAL_SNAPSHOT'
    implementations = []
    if baseline_source is not None:
        assert hashlib.sha256(baseline_source.replace(b'\r\n', b'\n')).hexdigest() == 'f9619ad526593ead65ef6ff5e3bce3bb7da72951105391cd3ee72d3d1adb9658'
        original_path = tmp_path / 'public_historical_index_builds.py'
        original_path.write_bytes(baseline_source)
        spec = importlib.util.spec_from_file_location('publication_public_baseline', original_path)
        baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
        implementations.append(('snapshot', baseline))
    implementations.append(('candidate', index_builds))  # Core large regression is NEVER skipped.
    rows, text = _large_parent_child_rows()
    source = tmp_path / 'large-source.txt'; source.write_text(text, encoding='utf-8')
    source_sha = hashlib.sha256(source.read_bytes()).hexdigest()
    assert source.stat().st_size == 2 * 1024 * 1024
    reports = []
    for label, module in implementations:
        database = tmp_path / (label + '.db')
        engine = create_engine('sqlite:///' + str(database))
        with engine.connect() as connection:
            assert connection.exec_driver_sql('PRAGMA journal_mode=WAL').scalar() == 'wal'
            connection.exec_driver_sql('PRAGMA wal_autocheckpoint=0')
        SQLModel.metadata.create_all(engine)
        keeper = engine.raw_connection()  # Keep WAL observable across commits.
        with Session(engine) as session:
            session.add(KnowledgeBase(id='kb', name='kb', embedding_model='m', vlm_model='v', chunk_count=987654))
            session.add(FileDocument(id='f', kb_id='kb', name='large-source.txt', local_path=str(source),
                                     size=source.stat().st_size, desired_generation=1))
            session.commit()
            session.execute(DocumentChunk.__table__.insert(), [dict(id=row['id'], file_id='f', content=row['content'],
                content_type=ContentType.TEXT, chunk_type=row['chunk_type'], parent_id=row.get('parent_id'),
                start_offset=row['start_offset'], end_offset=row['end_offset'], original_index=index,
                source_metadata_json=json.dumps({'source_sha256': source_sha, 'source_locators': [
                    {'start_char': row['start_offset'], 'end_char': row['end_offset']}]})) for index, row in enumerate(rows)])
            session.commit()
        sql, loaded, timings = [], [], []
        def statement(connection, cursor, value, parameters, context, executemany):
            normalized = value.lower()
            if 'document_chunks' in normalized:
                sql.append((normalized, len(parameters) if executemany else 1, executemany))
        def materialized(session, instance):
            if isinstance(instance, DocumentChunk): loaded.append(instance.id)
        event.listen(engine, 'before_cursor_execute', statement)
        event.listen(Session, 'loaded_as_persistent', materialized)
        monkeypatch.setattr(observability, 'event', lambda name, **data: timings.append((name, data)))
        def staged_before_external():
            with Session(engine) as session:
                assert session.exec(select(func.count()).select_from(DocumentChunk)).one() == 1056
                assert session.get(FileDocument, 'f').active_build_id is None
                assert session.exec(select(FileIndexBuild).where(FileIndexBuild.state == 'STAGING')).one()
        vectors = PublicationVectors(on_add=staged_before_external)
        monkeypatch.setattr(module, 'engine', engine)
        monkeypatch.setattr(module, 'VectorStoreService', lambda: vectors)
        monkeypatch.setattr(dispatch, 'guard_claim_in_session', lambda session, owner: True)
        monkeypatch.setattr(dispatch, 'complete_in_session', lambda session, owner, result: True)
        wal = Path(str(database) + '-wal'); wal_before = wal.stat().st_size if wal.exists() else 0
        started = perf_counter()
        result = module.publish_index_build(claim(), rows, [[1., 0.]] * 1024, 'm', {'source_sha256': source_sha})
        elapsed = perf_counter() - started
        wal_after = wal.stat().st_size if wal.exists() else 0
        event.remove(engine, 'before_cursor_execute', statement)
        event.remove(Session, 'loaded_as_persistent', materialized)
        lookups = [value for value, _, _ in sql if value.startswith('select') and
                   ('document_chunks.id in (' in value or 'document_chunks.id = ?' in value)]
        insert_batches = [count for value, count, _ in sql if value.startswith('insert into document_chunks')]
        with Session(engine) as session:
            actual = session.exec(select(DocumentChunk)).all(); by_id = {row.id: row for row in actual}
            parents = [row for row in actual if row.chunk_type == 'parent']; children = [row for row in actual if row.chunk_type == 'child']
            assert (len(parents), len(children), len(actual)) == (32, 1024, 1056)
            assert all(row.parent_id in by_id and by_id[row.parent_id].chunk_type == 'parent'
                and row.content in by_id[row.parent_id].content for row in children)
            assert all(text[row.start_offset:row.end_offset] == row.content for row in actual)
            assert all(json.loads(row.source_metadata_json)['source_sha256'] == source_sha for row in actual)
            assert session.get(FileDocument, 'f').active_build_id == result['build_id']
            assert session.get(KnowledgeBase, 'kb').chunk_count == 1056  # Recount repairs stale legacy count.
            indexed = set(session.exec(select(ChunkIndex.chunk_id)).all())
            assert indexed == {row.id for row in children}
            assert session.get(IndexStats, 'kb').total_docs == 1024
            assert {row['id'] for row in vectors.documents} == {row.id for row in children}
            assert not ({row.id for row in parents} & indexed)
        report = {'implementation': label, 'source_bytes': source.stat().st_size, 'source_sha256': source_sha,
                  'historical_baseline_origin': baseline_origin,
                  'parent_count': 32, 'child_count': 1024, 'vector_count': len(vectors.vectors),
                  'graph_builder': 'actual build_parent_child_chunks',
                  'fixture_chunk_configuration': {'parent_size': 65536, 'parent_overlap': 0, 'child_size': 2048, 'child_overlap': 0},
                  'prior_lookup_statements': len(lookups), 'sql_insert_batches': insert_batches,
                  'materialized_prior_chunk_orm_objects': len(loaded), 'publication_seconds': elapsed,
                  'wal_before_bytes': wal_before, 'wal_after_bytes': wal_after, 'wal_growth_bytes': wal_after - wal_before,
                  'vector_backend': 'full-readback substitute; no model/provider',
                  'phase_timings': [data for name, data in timings if name == 'performance.stage' and data.get('stage', '').startswith('index_build_')]}
        reports.append(report)
        keeper.close(); engine.dispose()
    candidate = next(report for report in reports if report['implementation'] == 'candidate')
    if len(reports) == 2:
        assert reports[0]['prior_lookup_statements'] == 1056
        assert reports[0]['materialized_prior_chunk_orm_objects'] >= 1056
    assert candidate['prior_lookup_statements'] == 3
    assert candidate['materialized_prior_chunk_orm_objects'] == 0
    assert candidate['sql_insert_batches'] == [500, 500, 56]
    assert any(phase['stage'] == 'index_build_recount' for phase in candidate['phase_timings'])
    assert any(phase['stage'] == 'index_build_commit' for phase in candidate['phase_timings'])
    request.node.user_properties.append(('large_publication_ab', json.dumps(reports)))


@pytest.mark.parametrize('failure_phase', ['vector_verify', 'sql_insert', 'bm25_finish', 'final_fence', 'external_unknown'])
def test_bulk_publication_failures_keep_prior_projection(environment, monkeypatch, failure_phase):
    import json
    from sqlalchemy import event
    from app.models import IndexStats
    failure = RuntimeError('EXTERNAL_WRITE_UNCONFIRMED')
    failure.provider_status = 'UNKNOWN'
    vectors = PublicationVectors(damage=failure_phase == 'vector_verify',
                                 failure=failure if failure_phase == 'external_unknown' else None)
    monkeypatch.setattr(index_builds, 'VectorStoreService', lambda: vectors)
    if failure_phase == 'bm25_finish':
        monkeypatch.setattr(BM25Index, 'finish_file_replacement', lambda *args: (_ for _ in ()).throw(RuntimeError('BM25_INJECTED_FAILURE')))
    if failure_phase == 'final_fence':
        monkeypatch.setattr(dispatch, 'complete_in_session', lambda *args: False)
    def fail_insert(connection, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith('insert into document_chunks'):
            raise RuntimeError('SQL_INSERT_INJECTED_FAILURE')
    if failure_phase == 'sql_insert': event.listen(environment, 'before_cursor_execute', fail_insert)
    try:
        with pytest.raises(RuntimeError) as caught:
            index_builds.publish_index_build(claim(), [{'content': 'new isolated alpha'}], [[0., 1.]], 'm')
    finally:
        if failure_phase == 'sql_insert': event.remove(environment, 'before_cursor_execute', fail_insert)
    if failure_phase == 'external_unknown': assert caught.value is failure and caught.value.provider_status == 'UNKNOWN'
    with Session(environment) as session:
        assert [row.content for row in session.exec(select(DocumentChunk)).all()] == ['old evidence']
        assert session.get(FileDocument, 'f').active_build_id is None
        assert session.get(IndexStats, 'kb').total_docs == 1
        assert BM25Index().search(session, 'old', 'kb') and not BM25Index().search(session, 'isolated', 'kb')
        builds = session.exec(select(FileIndexBuild)).all()
        assert len(builds) == 1 and builds[0].state == 'STAGING'
        assert set(json.loads(builds[0].manifest)['vector_ids']) == {row['id'] for row in vectors.documents}
    assert vectors.add_calls == 1  # Uncertain external effects are never replayed.


def test_batched_prior_provenance_preserves_exact_match_and_file_scope(environment):
    from app.models import ContentType
    with Session(environment) as session:
        original = session.get(DocumentChunk, 'old')
        original.page_number, original.source_metadata_json = 4, '{"trusted":true}'
        session.add(FileDocument(id='foreign-file', kb_id='kb', name='other', local_path='other', size=1))
        session.add(DocumentChunk(id='foreign', file_id='foreign-file', content='old evidence', page_number=4,
                                  content_type=ContentType.TEXT, source_metadata_json='{"foreign":true}'))
        session.add(original); session.commit()
        valid = {'id': 'old', 'content': 'old evidence', 'page_number': 4}
        originals = [valid, {**valid, 'content': 'edited'}, {**valid, 'page_number': 5},
                     {**valid, 'content_type': 'image'}, {**valid, 'id': 'foreign'},
                     {**valid, 'id': 'absent'}, {**valid, 'source_metadata_json': '{"forged":true}'}]
        remapped = [{'id': str(index)} for index in range(len(originals))]
        assert index_builds._prior_chunk_provenance(session, 'f', originals, remapped) == {
            '0': '{"trusted":true}', '6': '{"trusted":true}'}

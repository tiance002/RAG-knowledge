"""Offline stages use isolated SQLite/artifacts; providers and Chroma are replaced."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlmodel import SQLModel, Session, create_engine, select

from app.models import CustomModel, DocumentChunk, FileDocument, FileStatus, KnowledgeBase, ModelType
from app.tasks import dispatch, staged_pipeline as pipeline
from app.services import runtime_settings


@pytest.fixture
def stages(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'tasks.db'))
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(dispatch, 'engine', engine)
    monkeypatch.setattr(pipeline, '_artifact_sweep_cursor', None)
    settings = pipeline.get_settings().model_copy(update={
        'UPLOAD_DIR': str(tmp_path / 'uploads'), 'IMAGE_DIR': str(tmp_path / 'images'),
        'FILE_TASK_ARTIFACT_DIR': str(tmp_path / 'artifacts')})
    monkeypatch.setattr(pipeline, 'get_settings', lambda: settings)
    monkeypatch.setattr(runtime_settings, 'get_runtime_settings', lambda session=None: SimpleNamespace(
        revision=1, chunk_size=500, chunk_overlap=50, default_vlm_model=None))
    monkeypatch.setattr(dispatch.execute_file_operation, 'apply_async', lambda **kwargs: None)
    source = tmp_path / 'document.txt'
    source.write_text('Alpha source paragraph.\n\nBeta source paragraph.', encoding='utf-8')
    with Session(engine) as session:
        session.add(KnowledgeBase(id='kb', name='kb', embedding_model='embedding', vlm_model=''))
        session.add(CustomModel(id='embedding', name='embedding', model_type=ModelType.EMBEDDING,
            base_url='https://provider.invalid/v1', model_name='frozen-model', api_key='private-test-key'))
        session.add(FileDocument(id='file', kb_id='kb', name='document.txt', local_path=str(source),
                                 size=source.stat().st_size))
        session.commit()
    yield engine, source, settings
    engine.dispose()


def submit(stage, payload):
    result = dispatch.dispatch_file_task('file', stage, payload)
    claim = dispatch.claim_task(result['task_id'], 'parse' if stage == 'parse' else 'embed')
    assert claim is not None
    return claim


def test_preview_completes_without_embedding_or_active_index(stages, monkeypatch):
    from app.services.embedding import EmbeddingService
    async def forbidden(*args, **kwargs):
        pytest.fail('plain parse preview must not call embedding')
    monkeypatch.setattr(EmbeddingService, '_embed', forbidden)
    claim = submit('parse', {})
    assert 'private-test-key' not in json.dumps(claim.payload)
    result = pipeline.execute_stage(claim)
    assert result['status'] == 'pending_confirm'
    with Session(stages[0]) as session:
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        assert receipt.status == 'COMPLETED' and receipt.active_file is None
        doc = session.get(FileDocument, 'file')
        assert doc.status == FileStatus.PENDING_CONFIRM
        assert doc.active_build_id is None
        assert session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'file')).all()
    assert stages[1].read_text() == 'Alpha source paragraph.\n\nBeta source paragraph.'


def test_auto_parse_hands_off_full_checkpoint_then_embed(stages, monkeypatch):
    from app.services.embedding import EmbeddingService
    calls = []
    async def embedding(self, contents, model_id):
        calls.append((list(contents), model_id))
        return [[1.0, 0.0] for _ in contents]
    monkeypatch.setattr(EmbeddingService, '_embed', embedding)
    claim = submit('parse', {'auto_vectorize': True, 'parent_child': True,
                             'child_chunk_size':20,'child_chunk_overlap':0})
    pipeline.execute_stage(claim)
    with Session(stages[0]) as session:
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        assert receipt.status == 'QUEUED' and receipt.stage == 'embed'
        assert receipt.owner_token is None
        assert session.exec(select(DocumentChunk)).all() == []
        original = pipeline.read_artifact(claim.task_id, receipt.checkpoint_ref)
        assert original['source_sha256'] == pipeline.source_digest(stages[1])
        assert any(c['chunk_type'] == 'parent' for c in original['chunks_data'])
    embed_claim = dispatch.claim_task(claim.task_id, 'embed')
    pipeline.execute_stage(embed_claim)
    with Session(stages[0]) as session:
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        assert receipt.stage == 'index' and receipt.status == 'QUEUED'
        checkpoint = pipeline.read_artifact(claim.task_id, receipt.checkpoint_ref)
        assert len(checkpoint['embeddings']) == sum(c['chunk_type'] != 'parent' for c in checkpoint['chunks_data'])
        assert checkpoint['dimension'] == 2
        assert session.exec(select(DocumentChunk)).all() == []
        effects = session.exec(select(dispatch.FileTaskEffect)).all()
        assert len(effects) == 1 and effects[0].state == 'COMPLETED'
        assert pipeline.verify_checkpoint_for_resume(receipt, effects)
    assert calls and all(call[1] == 'frozen-model' for call in calls)


def test_effect_result_reused_and_pending_effect_never_replayed(stages):
    claim = submit('index', {'chunks_data': [{'content': 'a'}]})
    runner = pipeline.EffectRunner(claim)
    calls = []
    async def good():
        calls.append('good')
        return [[1.0]]
    identity = {'inputs': ['a'], 'model': 'frozen-model'}
    assert asyncio.run(runner('embedding', identity, good)) == [[1.0]]
    assert asyncio.run(runner('embedding', identity, good)) == [[1.0]]
    assert calls == ['good']
    async def ambiguous():
        calls.append('ambiguous')
        raise TimeoutError('response lost')
    with pytest.raises(pipeline.UnconfirmedEffect):
        asyncio.run(runner('embedding', {'inputs': ['b']}, ambiguous))
    with pytest.raises(pipeline.UnconfirmedEffect, match='PREVIOUS_OUTCOME'):
        asyncio.run(runner('embedding', {'inputs': ['b']}, ambiguous))
    assert calls == ['good', 'ambiguous']


def test_source_and_model_drift_fail_before_provider_or_active_writes(stages, monkeypatch):
    claim = submit('parse', {'auto_vectorize': True})
    stages[1].write_text('changed outside application', encoding='utf-8')
    with pytest.raises(ValueError, match='TASK_SOURCE_CHANGED'):
        pipeline.execute_stage(claim)
    with Session(stages[0]) as session:
        assert session.exec(select(DocumentChunk)).all() == []
    stages[1].write_text('Alpha source paragraph.\n\nBeta source paragraph.', encoding='utf-8')
    with Session(stages[0]) as session:
        model = session.get(CustomModel, 'embedding')
        model.model_name = 'new-provider-model'
        session.add(model); session.commit()
    with pytest.raises(ValueError, match='MODEL_CONFIGURATION_CHANGED'):
        pipeline.execute_stage(claim)


def test_artifacts_reject_path_escape_and_corruption(stages):
    task_id = str(uuid4())
    reference = pipeline.write_artifact(task_id, {'state': 'saved'})
    assert pipeline.read_artifact(task_id, reference) == {'state': 'saved'}
    with pytest.raises(ValueError, match='PATH_INVALID'):
        pipeline.read_artifact(str(uuid4()), reference)
    Path(reference).write_text('{"state":"corrupted"}', encoding='utf-8')
    with pytest.raises(ValueError, match='HASH_MISMATCH'):
        pipeline.read_artifact(task_id, reference)


def test_stale_preview_cannot_replace_existing_chunks(stages):
    claim = submit('parse', {})
    with Session(stages[0]) as session:
        session.add(DocumentChunk(id='old-chunk', file_id='file', content='old preview'))
        doc = session.get(FileDocument, 'file')
        doc.desired_generation += 1
        session.add(doc); session.commit()
    with pytest.raises(pipeline.StaleStageClaim):
        pipeline.execute_stage(claim)
    with Session(stages[0]) as session:
        assert [c.id for c in session.exec(select(DocumentChunk)).all()] == ['old-chunk']


def test_invalid_vectors_are_unknown_and_not_cached_as_completed(stages):
    claim = submit('index', {'chunks_data': [{'content': 'a'}]})
    async def malformed():
        return [[float('nan')]]
    with pytest.raises(pipeline.UnconfirmedEffect):
        asyncio.run(pipeline.EffectRunner(claim)('embedding', {'inputs': ['a']}, malformed))
    with Session(stages[0]) as session:
        effect = session.exec(select(dispatch.FileTaskEffect)).one()
        assert effect.state == 'PENDING' and effect.result_ref is None


def test_image_paths_are_attempt_owned(stages):
    from app.services.parser import FileParser
    first = FileParser('kb', image_namespace='tasks/first', parser_settings=stages[2])
    second = FileParser('kb', image_namespace='tasks/second', parser_settings=stages[2])
    a = first._save_image(b'first', 'same-file', 1, 1)
    b = second._save_image(b'second', 'same-file', 1, 1)
    assert a != b
    assert (Path(stages[2].IMAGE_DIR) / a).read_bytes() == b'first'
    assert (Path(stages[2].IMAGE_DIR) / b).read_bytes() == b'second'


def test_legacy_worker_delivery_cannot_bypass_receipts(stages):
    # Celery caches task functions by name across module reloads; isolate this
    # import so older task-reload tests retain their own engine monkeypatches.
    import subprocess
    import sys
    script = "from app.tasks.vectorize_tasks import vectorize_file_task as t; t.push_request(called_directly=False); assert t.run('file')['error'] == 'LEGACY_TASK_DISABLED_USE_FILE_TASK_RECEIPT'"
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_rejected_429_uses_retry_after_but_partial_batch_is_unknown(stages):
    import httpx
    from app.services.llm import invoke_provider_http_async
    claim = submit('index', {'chunks_data': [{'content': 'a'}]})
    request = httpx.Request('POST', 'https://provider.invalid/embeddings')
    response = httpx.Response(429, request=request, headers={'Retry-After': '125'})
    async def rejected():
        async def send():
            return response
        return await invoke_provider_http_async(send, stage='embedding', model_name='frozen-model')
    with pytest.raises(dispatch.RetryableStageError) as error:
        asyncio.run(pipeline.EffectRunner(claim)('embedding', {'inputs': ['a']}, rejected))
    assert error.value.delay_ms == 125000
    with Session(stages[0]) as session:
        assert session.exec(select(dispatch.FileTaskEffect)).one().state == 'RETRYABLE'
    failure = httpx.HTTPStatusError('rate limit', request=request, response=response)
    assert pipeline._safe_retry_delay(failure, [{'status_code': 200}, {'status_code': 429}]) is None
    assert pipeline._safe_retry_delay(failure, [{'status_code': 429}]) == 125000
    preconnect = httpx.ConnectError('not connected')
    preconnect.provider_status = 'PRE_CONNECT_FAILED'
    assert pipeline._safe_retry_delay(preconnect, []) == 10000
    assert pipeline._safe_retry_delay(preconnect, [{'status_code': 200}]) is None


def test_semantic_parser_must_propagate_uncertain_effect(stages):
    from app.services.parser import StructuredSplitter
    splitter = StructuredSplitter(use_semantic=False)
    def uncertain(*args, **kwargs):
        raise pipeline.UnconfirmedEffect('provider outcome not confirmed')
    splitter.use_semantic = True
    splitter.semantic_splitter = SimpleNamespace(create_documents=uncertain)
    with pytest.raises(pipeline.UnconfirmedEffect):
        splitter._split_content('source sentence.', ignore_limit_check=True)


def test_complete_staged_ingestion_publishes_verified_build(stages, monkeypatch):
    from app.services import index_builds
    from app.services.embedding import EmbeddingService
    monkeypatch.setattr(index_builds, 'engine', stages[0])
    stored = {}
    class Store:
        def validate_dimension(self, kb_id, vector):
            assert len(vector) == 2
        def add_documents(self, kb_id, documents, embeddings):
            for doc, vector in zip(documents, embeddings):
                stored[doc['id']] = (doc, vector)
        def _existing_collection(self, kb_id):
            return self
        def get(self, ids, include):
            return dict(ids=ids, documents=[stored[i][0]['content'] for i in ids],
                metadatas=[stored[i][0]['metadata'] for i in ids], embeddings=[stored[i][1] for i in ids])
        def get_documents(self, kb_id, ids, include):
            return self.get(ids, include)
        def delete_ids(self, kb_id, ids):
            for key in ids:
                stored.pop(key, None)
    monkeypatch.setattr(index_builds, 'VectorStoreService', Store)
    async def embedding(self, contents, model_id):
        return [[1.0, 0.0] for _ in contents]
    monkeypatch.setattr(EmbeddingService, '_embed', embedding)
    claim = submit('parse', {'auto_vectorize': True})
    pipeline.execute_stage(claim)
    pipeline.execute_stage(dispatch.claim_task(claim.task_id, 'embed'))
    result = pipeline.execute_stage(dispatch.claim_task(claim.task_id, 'index'))
    assert result['status'] == 'parsed' and result['build_id']
    with Session(stages[0]) as session:
        doc = session.get(FileDocument, 'file')
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        assert doc.active_build_id == result['build_id'] and doc.status == FileStatus.PARSED
        assert receipt.status == 'COMPLETED' and receipt.active_file is None
        chunks = session.exec(select(DocumentChunk)).all()
        assert len(chunks) == len(stored) == result['chunk_count']
        assert all(json.loads(c.source_metadata_json)['source_sha256'] == claim.payload['_source_sha256'] for c in chunks)


def test_preview_refuses_historical_bm25_even_when_vectors_and_build_pointer_missing(stages):
    from app.models import ChunkIndex
    claim = submit('parse', {})
    with Session(stages[0]) as session:
        session.add(DocumentChunk(id='historical', file_id='file', content='still searchable'))
        session.flush()
        session.add(ChunkIndex(id='old-posting', chunk_id='historical', kb_id='kb'))
        session.commit()
    with pytest.raises(ValueError, match='PREVIEW_CANNOT_REPLACE_ACTIVE_INDEX'):
        pipeline.execute_stage(claim)
    with Session(stages[0]) as session:
        assert session.get(DocumentChunk, 'historical').content == 'still searchable'
        assert session.get(ChunkIndex, 'old-posting') is not None


def test_write_before_effect_cas_can_resume_without_provider_replay(stages, monkeypatch):
    from app.services.embedding import EmbeddingService
    calls = []
    async def embedding(self, contents, model_id):
        calls.append(list(contents))
        return [[1.0, 0.0] for _ in contents]
    monkeypatch.setattr(EmbeddingService, '_embed', embedding)
    claim = submit('index', {'chunks_data': [{'content': 'durable response'}]})
    original_mark = dispatch.mark_effect_result
    monkeypatch.setattr(dispatch, 'mark_effect_result', lambda *args, **kwargs: False)
    with pytest.raises(pipeline.UnconfirmedEffect):
        pipeline.execute_stage(claim)
    monkeypatch.setattr(dispatch, 'mark_effect_result', original_mark)
    with Session(stages[0]) as session:
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        receipt.status, receipt.owner_token, receipt.lease_until = 'UNKNOWN', None, None
        effects = session.exec(select(dispatch.FileTaskEffect)).all()
        assert effects[0].state == 'PENDING'
        assert receipt.checkpoint_ref is None
        session.add(receipt); session.commit()
    monkeypatch.setattr(dispatch, 'notify_task', lambda *args: None)
    from app.tasks.file_task_recovery import scan_once
    result = scan_once()
    assert result['retried'] == result['sent'] == 1
    with Session(stages[0]) as session:
        assert session.get(dispatch.FileTaskReceipt, claim.task_id).status == 'RETRY_WAIT'
        assert session.exec(select(dispatch.FileTaskEffect)).one().state == 'COMPLETED'
    pipeline.execute_stage(dispatch.claim_task(claim.task_id, 'embed'))
    assert calls == [['durable response']]


def test_pending_effect_without_materialized_result_cannot_resume(stages):
    claim = submit('index', {'chunks_data': [{'content': 'lost response'}]})
    async def lost():
        raise TimeoutError('no response')
    with pytest.raises(pipeline.UnconfirmedEffect):
        asyncio.run(pipeline.EffectRunner(claim)('embedding', {'inputs': ['lost response']}, lost))
    with Session(stages[0]) as session:
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        effects = session.exec(select(dispatch.FileTaskEffect)).all()
        assert not pipeline.recover_materialized_effects_for_resume(session, receipt, effects)
        assert not pipeline.verify_checkpoint_for_resume(receipt, effects)
        assert effects[0].state == 'PENDING'
        # A self-hashed local file with the wrong accepted input identity is
        # insufficient evidence for settling an otherwise uncertain request.
        pipeline.write_artifact(claim.task_id, dict(schema='task-effect-v1', task_id=claim.task_id,
            stage=claim.stage, effect_key=effects[0].effect_key, input_hash='0' * 64,
            kind='embedding', input_count=1, result=[[1.0]]))
        assert not pipeline.recover_materialized_effects_for_resume(session, receipt, effects)
        assert effects[0].state == 'PENDING'


@pytest.mark.parametrize('kind', ['embedding', 'vlm'])
@pytest.mark.parametrize('outcome', ['late_before_send', 'late_after_send', 'second_timeout'])
def test_uncertain_effect_replays_at_most_once(stages, monkeypatch, kind, outcome):
    from app.tasks.file_task_recovery import scan_once

    calls = []
    monkeypatch.setattr(dispatch, 'notify_task', lambda *args: None)
    old = submit('index', {'chunks_data': [{'content': 'durable source'}]})
    cached = {'inputs': ['already complete']} if kind == 'embedding' else {'image_sha256': 'a'}
    uncertain = {'inputs': ['uncertain']} if kind == 'embedding' else {'image_sha256': 'b'}
    old_result, new_result = ([[1., 0.]], [[0., 1.]]) if kind == 'embedding' else ('old caption', 'new caption')
    key, identity_hash = pipeline._effect_identity(old, kind, uncertain)

    def store_late_result():
        reference = pipeline.write_artifact(old.task_id, dict(schema='task-effect-v1', task_id=old.task_id,
            stage=old.stage, attempt_no=old.attempt_no, effect_key=key, input_hash=identity_hash,
            kind=kind, input_count=1, result=old_result))
        assert not dispatch.mark_effect_result(old, key, reference)

    async def previously_completed():
        calls.append('completed')
        return old_result

    async def lost():
        calls.append('original')
        raise TimeoutError('response lost')

    assert asyncio.run(pipeline.EffectRunner(old)(kind, cached, previously_completed)) == old_result
    with pytest.raises(pipeline.UnconfirmedEffect) as exc:
        asyncio.run(pipeline.EffectRunner(old)(kind, uncertain, lost))
    dispatch._fail_claim(old, exc.value)
    assert scan_once()['retried'] == 1
    if outcome == 'late_before_send':
        store_late_result()
    new = dispatch.claim_task(old.task_id, 'embed')
    assert new is not None
    assert asyncio.run(pipeline.EffectRunner(new)(kind, cached, previously_completed)) == old_result

    async def replay():
        calls.append('replay')
        if outcome == 'second_timeout':
            raise TimeoutError('second response lost')
        if outcome == 'late_after_send':
            store_late_result()
        return new_result

    if outcome == 'second_timeout':
        with pytest.raises(pipeline.UnconfirmedEffect) as exc:
            asyncio.run(pipeline.EffectRunner(new)(kind, uncertain, replay))
        dispatch._fail_claim(new, exc.value)
        assert scan_once()['retried'] == 0
        assert scan_once()['sent'] == 0
        with Session(stages[0]) as session:
            terminal = session.get(dispatch.FileTaskReceipt, old.task_id)
            assert terminal.status == 'FAILED' and terminal.active_file is None
            assert dispatch.receipt_view(terminal)['resolution_available'] is True
        assert dispatch.resolve_unknown(old.file_id, old.task_id, 'terminate', 'user cancelled')['status'] == 'CANCELLED'
    else:
        expected = old_result if outcome == 'late_before_send' else new_result
        assert asyncio.run(pipeline.EffectRunner(new)(kind, uncertain, replay)) == expected
        with Session(stages[0]) as session:
            effect = session.get(dispatch.FileTaskEffect, key)
            assert effect.state == 'COMPLETED'
            assert pipeline.read_artifact(old.task_id, effect.result_ref)['result'] == expected
    assert calls == ['completed', 'original'] + ([] if outcome == 'late_before_send' else ['replay'])


def test_multipart_embedding_does_not_resend_a_completed_item(stages, monkeypatch):
    from app.services.embedding import EmbeddingService
    from app.services.task_effect_context import effect_scope
    from app.tasks.file_task_recovery import scan_once

    calls = []
    async def request(self, inputs, model_id):
        calls.append(inputs)
        if inputs == ['second'] and calls.count(inputs) == 1:
            raise TimeoutError('second item timed out')
        return [[1., 0.]]

    monkeypatch.setattr(EmbeddingService, '_embed', request)
    monkeypatch.setattr(dispatch, 'notify_task', lambda *args: None)
    service = EmbeddingService(base_url='https://provider.invalid/api/embeddings', model='test')
    old = submit('index', {'chunks_data': [{'content': 'durable source'}]})
    with effect_scope(pipeline.EffectRunner(old)), pytest.raises(pipeline.UnconfirmedEffect) as exc:
        asyncio.run(service.embed_documents(['first', 'second']))
    dispatch._fail_claim(old, exc.value)
    assert scan_once()['retried'] == 1
    with effect_scope(pipeline.EffectRunner(dispatch.claim_task(old.task_id, 'embed'))):
        assert asyncio.run(service.embed_documents(['first', 'second'])) == [[1., 0.], [1., 0.]]
    assert calls == [['first'], ['second'], ['second']]


def test_artifact_sweep_keeps_live_preview_and_effect_evidence_and_unknown(stages):
    claim = submit('parse', {})
    pipeline.execute_stage(claim)
    directory = pipeline._task_directory(claim.task_id)
    orphan = directory / 'uncommitted.tmp'
    orphan.write_text('unreferenced intermediate')
    live_image = Path(stages[2].IMAGE_DIR) / 'kb' / 'tasks' / Path(claim.task_id).name.replace('-', '') / '1' / 'live.png'
    live_image.parent.mkdir(parents=True, exist_ok=True)
    live_image.write_bytes(b'live')
    dead_image = live_image.with_name('dead.png')
    dead_image.write_bytes(b'dead')
    with Session(stages[0]) as session:
        chunk = session.exec(select(DocumentChunk)).first()
        chunk.image_path = live_image.relative_to(Path(stages[2].IMAGE_DIR)).as_posix()
        session.add(chunk)
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        reference = receipt.checkpoint_ref
        session.commit()
    result = pipeline.sweep_task_artifacts(grace_seconds=0)
    assert result['files_deleted'] == 2 and not result['errors']
    assert not orphan.exists() and not dead_image.exists() and live_image.exists()
    checkpoint = pipeline.read_artifact(claim.task_id, reference)
    assert Path(checkpoint['provenance_manifest']).exists()
    assert stages[1].exists()
    # A later UNKNOWN task protects even unreferenced in-progress intermediates.
    new = submit('index', {'chunks_data': [{'content': 'next'}]})
    protected = Path(pipeline.write_artifact(new.task_id, {'uncommitted': True}))
    with Session(stages[0]) as session:
        receipt = session.get(dispatch.FileTaskReceipt, new.task_id)
        receipt.status = 'UNKNOWN'; session.add(receipt); session.commit()
    pipeline.sweep_task_artifacts(grace_seconds=0)
    assert protected.exists()


def test_verified_upload_hash_and_untrusted_provenance_are_checked(stages):
    with Session(stages[0]) as session:
        doc = session.get(FileDocument, 'file')
        doc.sha256 = '0' * 64
        session.add(doc); session.commit()
    with pytest.raises(ValueError, match='UPLOAD_HASH_MISMATCH'):
        submit('index', {'chunks_data': [{'content': 'input'}]})
    with Session(stages[0]) as session:
        doc = session.get(FileDocument, 'file')
        doc.sha256 = pipeline.source_digest(stages[1])
        session.add(doc); session.commit()
    claim = submit('index', {'chunks_data': [{'content': 'edited',
        'metadata': {'source_locator': {'page': 99}}, 'source_metadata_json': '{"forged":true}',
        'source_locator_json': '{"forged":true}', 'provenance_manifest': 'untrusted.json'}]})
    assert claim.payload['chunks_data'] == [{'content': 'edited'}]


def test_changed_pinned_image_is_rejected_before_embedding(stages, monkeypatch):
    from app.services.embedding import EmbeddingService
    image = Path(stages[2].IMAGE_DIR) / 'kb' / 'source.png'
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(b'original image fixture')
    with Session(stages[0]) as session:
        session.add(DocumentChunk(id='image-chunk', file_id='file', content='image', image_path='kb/source.png'))
        session.commit()
    claim = submit('index', {'chunks_data': [{'id': 'image-chunk', 'content': 'image', 'image_path': 'kb/source.png'}]})
    async def forbidden(*args, **kwargs):
        pytest.fail('changed image must be rejected before paid embedding')
    monkeypatch.setattr(EmbeddingService, '_embed', forbidden)
    image.write_bytes(b'changed image fixture')
    with pytest.raises(ValueError, match='IMAGE_HASH_MISMATCH'):
        pipeline.execute_stage(claim)


def test_artifact_sweep_rotates_with_bounded_tasks_and_read_budget(stages):
    identifiers = [str(__import__('uuid').UUID(int=i)) for i in (1, 2, 3)]
    artifacts = []
    with Session(stages[0]) as session:
        for task_id in identifiers:
            session.add(dispatch.FileTaskReceipt(task_id=task_id, file_id='file', operation='parse',
                stage='parse', status='FAILED', payload=json.dumps({'_kb_id': 'kb'})))
            artifacts.append(Path(pipeline.write_artifact(task_id, {'orphan': task_id})))
        session.commit()
    for expected_remaining in (2, 1, 0):
        result = pipeline.sweep_task_artifacts(grace_seconds=0, max_tasks=1)
        assert result['tasks_examined'] == 1 and result['files_deleted'] == 1
        assert sum(path.exists() for path in artifacts) == expected_remaining
    reference = Path(pipeline.write_artifact(identifiers[0], {'large': 'x' * 500}))
    result = pipeline.sweep_task_artifacts(grace_seconds=0, max_tasks=1, max_read_bytes=10)
    assert result['tasks_deferred'] == 1 and result['bytes_read'] == 0
    assert reference.exists()

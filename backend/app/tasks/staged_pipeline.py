"""Resumable parse/embedding stages. Only the publisher changes active indexes.

Artifacts are immutable, content-addressed JSON. Original uploads are reused
read-only and checked against the accepted digest, rather than copied per retry.
Provider effects are deliberately conservative: an interrupted batch is unknown
even when its provider may have processed only part of the batch.
"""
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import tempfile
from uuid import UUID, uuid4

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import ContentType, DocumentChunk, FileDocument, FileStatus, KnowledgeBase
from app.services.model_bindings import resolve_model
from app.services.task_effect_context import effect_scope, capture_provider_ids


class UnconfirmedEffect(RuntimeError):
    provider_status = 'UNKNOWN'
    ingest_effect_error = True


class StaleStageClaim(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _digest(value):
    return hashlib.sha256(value).hexdigest()


def source_digest(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def artifact_root():
    settings = get_settings()
    configured = getattr(settings, 'FILE_TASK_ARTIFACT_DIR', None)
    return Path(configured or Path(settings.UPLOAD_DIR).parent / 'task-artifacts').resolve()


def _task_directory(task_id):
    # IDs become directory names only after validation, never raw payload paths.
    UUID(str(task_id))
    return artifact_root() / str(task_id)


def write_artifact(task_id, value):
    raw = _json(value)
    directory = _task_directory(task_id)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (_digest(raw) + '.json')
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        # Atomic publication; identical content may safely share the same name.
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != raw:
                raise ValueError('TASK_ARTIFACT_HASH_CONFLICT')
        if os.name != 'nt':
            descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def read_artifact(task_id, reference):
    path = Path(reference).resolve()
    if path.parent != _task_directory(task_id).resolve() or path.suffix != '.json':
        raise ValueError('TASK_ARTIFACT_PATH_INVALID')
    raw = path.read_bytes()
    if path.stem != _digest(raw):
        raise ValueError('TASK_ARTIFACT_HASH_MISMATCH')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError('TASK_ARTIFACT_SCHEMA_INVALID')
    return value


def _binding_snapshot(binding):
    if binding is None:
        return None
    return dict(binding_id=binding.binding_id, model_name=binding.model_name,
                base_url=binding.base_url, model_type=binding.model_type,
                context_length=binding.context_length, source=binding.source,
                credential_sha256=_digest(binding.api_key.encode()))


def freeze_payload(session, file_doc, operation, payload):
    """Resolve mutable defaults at acceptance, never persist plaintext credentials."""
    if file_doc is None:
        raise ValueError('FILE_NOT_FOUND')
    payload = dict(payload)
    kb = session.get(KnowledgeBase, file_doc.kb_id)
    if kb is None:
        raise ValueError('KNOWLEDGE_BASE_NOT_FOUND')
    path = Path(file_doc.local_path).resolve()
    if not path.is_file():
        raise ValueError('FILE_SOURCE_UNAVAILABLE')
    accepted_source_hash = source_digest(path)
    if getattr(file_doc, 'sha256', None) and accepted_source_hash != file_doc.sha256:
        raise ValueError('TASK_SOURCE_UPLOAD_HASH_MISMATCH')
    if operation == 'index':
        if not isinstance(payload.get('chunks_data'), list):
            raise ValueError('TASK_INDEX_CHUNKS_INVALID')
        cleaned = []
        for incoming in payload['chunks_data']:
            if not isinstance(incoming, dict):
                raise ValueError('TASK_INDEX_CHUNKS_INVALID')
            row = {key: value for key, value in incoming.items() if key not in (
                'metadata', 'source_metadata_json', 'source_locator_json', 'provenance_manifest')}
            if row.get('image_path'):
                prior = session.get(DocumentChunk, row.get('id')) if row.get('id') else None
                if not prior or prior.file_id != file_doc.id or prior.image_path != row['image_path']:
                    raise ValueError('TASK_CHUNK_IMAGE_REFERENCE_INVALID')
            cleaned.append(row)
        payload['chunks_data'] = cleaned
        payload['_image_artifacts'] = _image_manifest(cleaned)
    from app.services.runtime_settings import get_runtime_settings
    defaults = get_runtime_settings(session)
    if operation == 'parse':
        for key, value in dict(chunk_mode='custom', chunk_size=defaults.chunk_size,
                               chunk_overlap=defaults.chunk_overlap, heading_level=2).items():
            if payload.get(key) is None:
                payload[key] = value
        size, overlap = int(payload['chunk_size']), int(payload['chunk_overlap'])
        if size <= 0 or not 0 <= overlap < size:
            raise ValueError('CHUNK_CONFIGURATION_INVALID')
    needs_vector = operation == 'index' or payload.get('auto_vectorize', False)
    semantic = operation == 'parse' and (payload.get('embedding_model') or
                        payload.get('chunk_mode') in ('semantic', 'structure'))
    # Legacy retrieval uses the deployment embedding when a KB has no model.
    # Freeze that same identity so later settings changes cannot alter a task.
    identities = {'vector': payload.get('vector_model') or kb.embedding_model or 'sys_embedding',
                  'semantic': payload.get('embedding_model') or kb.embedding_model or 'sys_embedding',
                  'vlm': payload.get('vlm_model_id')}
    if needs_vector and payload.get('vector_model') and identities['vector'] != kb.embedding_model:
        # Reject an incompatible existing collection before any paid stage.
        from app.tasks.parse_tasks import _preflight_embedding
        _preflight_embedding(session, file_doc, identities['vector'])
    bindings = {}
    for role, needed, model_type in [('vector', needs_vector or semantic, 'embedding'),
                                    ('semantic', semantic, 'embedding'),
                                    ('vlm', operation == 'parse' and bool(identities['vlm']), 'vlm')]:
        bindings[role] = _binding_snapshot(resolve_model(session, model_type, identities[role],
                          use_default=False)) if needed else None
    payload.update(_operation=operation, _source_path=str(path),
                   _source_sha256=accepted_source_hash, _kb_id=file_doc.kb_id,
                   _model_snapshots=bindings)
    settings = get_settings()
    payload['_parser_settings'] = {key: getattr(settings, key) for key in (
        'PDF_PARSER_MODE', 'PDF_TESSDATA_DIR', 'PDF_OCR_DPI',
        'PDF_SCANNED_TABLES_ENABLED', 'PDF_TABLES_ENABLED')}
    payload['_vlm_prompt'] = settings.VLM_PROMPT
    payload['_vlm_max_tokens'] = getattr(settings, 'VLM_MAX_TOKENS', 1000)
    return payload


def _resolve_frozen(session, snapshot):
    if snapshot is None:
        return None
    binding = resolve_model(session, snapshot['model_type'], snapshot['binding_id'], use_default=False)
    if _binding_snapshot(binding) != snapshot:
        raise ValueError('TASK_MODEL_CONFIGURATION_CHANGED')
    return binding


def _assert_source(payload):
    path = Path(payload['_source_path'])
    if source_digest(path) != payload['_source_sha256']:
        raise ValueError('TASK_SOURCE_CHANGED')
    return path


def _validate_vectors(vectors, count):
    if not isinstance(vectors, list) or len(vectors) != count:
        raise ValueError('EMBEDDING_COUNT_MISMATCH')
    dimension = len(vectors[0]) if vectors else 0
    if count and not dimension:
        raise ValueError('EMBEDDING_DIMENSION_INVALID')
    if any(not isinstance(v, list) or len(v) != dimension or
           any(isinstance(n, bool) or not isinstance(n, (int, float)) or not math.isfinite(n)
               for n in v) for v in vectors):
        raise ValueError('EMBEDDING_VECTOR_INVALID')
    return dimension


def _effect_identity(claim, kind, identity):
    identity_hash = _digest(_json({'kind': kind, 'identity': identity,
        'source_sha256': claim.payload['_source_sha256'],
        'models': claim.payload['_model_snapshots']}))
    return _digest(_json([claim.task_id, claim.stage, kind, identity_hash])), identity_hash


def _vlm_request_identity(claim, image_hash, service):
    budget = claim.payload.get('_vlm_max_tokens', 1000)
    if isinstance(budget, bool) or not isinstance(budget, int) or not 128 <= budget <= 32768:
        raise ValueError('VLM_MAX_TOKENS_INVALID')
    identity = dict(image_sha256=image_hash, base_url=service.base_url,
                    model=service.model, prompt=service.prompt,
                    max_tokens=budget)
    if claim.payload.get('_vlm_reuse_completed_legacy') and identity['max_tokens'] != 1000:
        # Explicitly authorized legacy recovery can reuse complete captions.
        # Their original request identity and immutable artifact remain intact.
        from app.tasks import dispatch
        legacy = {**identity, 'max_tokens': 1000}
        key, input_hash = _effect_identity(claim, 'vlm', legacy)
        with Session(dispatch.engine) as session:
            effect = session.get(dispatch.FileTaskEffect, key)
            if (effect is not None and effect.task_id == claim.task_id and
                    effect.stage == claim.stage and effect.input_hash == input_hash and
                    effect.state == 'COMPLETED'):
                return legacy
    return identity


class EffectRunner:
    def __init__(self, claim):
        self.claim = claim

    async def __call__(self, kind, identity, callback):
        from app.tasks import dispatch
        key, identity_hash = _effect_identity(self.claim, kind, identity)
        cache_only = kind == 'vlm' and self.claim.payload.get('_vlm_cache_only', False)
        if cache_only:
            with Session(dispatch.engine) as session:
                cached = session.get(dispatch.FileTaskEffect, key)
                if (cached is None or cached.state != 'COMPLETED' or
                        cached.task_id != self.claim.task_id or cached.stage != self.claim.stage or
                        cached.input_hash != identity_hash):
                    raise UnconfirmedEffect('VLM_CACHE_ONLY_RESULT_REQUIRED')
        state = dispatch.begin_effect(self.claim, key, identity_hash)
        if state == 'COMPLETED':
            # The result location is deterministic; recover a completed call even
            # if the stage checkpoint was never committed.
            from app.tasks.dispatch import FileTaskEffect
            with Session(dispatch.engine) as session:
                effect = session.get(FileTaskEffect, key)
                if effect is None or not effect.result_ref:
                    raise UnconfirmedEffect('EFFECT_RESULT_MISSING')
                value = read_artifact(self.claim.task_id, effect.result_ref)
            if value.get('effect_key') != key:
                raise ValueError('EFFECT_RESULT_IDENTITY_MISMATCH')
            return value['result']
        if state != 'NEW':
            raise UnconfirmedEffect('EFFECT_PREVIOUS_OUTCOME_UNCONFIRMED')
        if cache_only:
            # A concurrent ledger change must never turn a cache import into a paid call.
            raise UnconfirmedEffect('VLM_CACHE_ONLY_RESULT_REQUIRED')
        try:
            with capture_provider_ids() as provider_ids:
                result = await callback()
            if kind == 'embedding':
                _validate_vectors(result, len(identity['inputs']))
            ids = [call['request_id'] for call in provider_ids if call.get('request_id')]
            provider_id = ids[0] if len(ids) == 1 else json.dumps(ids) if ids else None
            reference = write_artifact(self.claim.task_id, {'schema': 'task-effect-v1',
                'task_id': self.claim.task_id, 'stage': self.claim.stage,
                'input_hash': identity_hash, 'effect_key': key, 'kind': kind,
                'input_count': len(identity['inputs']) if kind == 'embedding' else None,
                'provider_request_id': provider_id, 'result': result})
            if not dispatch.mark_effect_result(self.claim, key, reference, provider_request_id=provider_id):
                raise UnconfirmedEffect('EFFECT_RESULT_OWNERSHIP_LOST')
            return result
        except BaseException as exc:
            # Intent already persisted. A response, including partially returned
            # multi-request batches, must never be replayed implicitly.
            if isinstance(exc, StaleStageClaim):
                raise
            evidence = getattr(exc, 'response_evidence', None)
            if kind == 'vlm' and isinstance(evidence, dict):
                try:
                    write_artifact(self.claim.task_id, {'schema': 'task-effect-rejected-v1',
                        'task_id': self.claim.task_id, 'stage': self.claim.stage,
                        'input_hash': identity_hash, 'effect_key': key, 'kind': kind,
                        'status': getattr(exc, 'status', 'UNKNOWN'),
                        'provider_calls': provider_ids, 'response': evidence})
                except (OSError, ValueError, TypeError):
                    # A failed evidence write still leaves the paid intent PENDING.
                    # It must not authorize replay or turn the caption into success.
                    pass
            delay = _safe_retry_delay(exc, provider_ids)
            if delay is not None:
                code = 'PROVIDER_REJECTED_429' if provider_ids else 'PROVIDER_PRE_CONNECT_FAILED'
                if not dispatch.mark_effect_retryable(self.claim, key, delay, code):
                    raise UnconfirmedEffect('EFFECT_RETRY_OWNERSHIP_LOST') from exc
                retryable = dispatch.RetryableStageError(delay, code)
                retryable.ingest_effect_error = True
                raise retryable from exc
            raise UnconfirmedEffect('EFFECT_RESULT_UNCONFIRMED:' + type(exc).__name__) from exc


def _safe_retry_delay(exc, calls):
    """A batch with any earlier accepted response is never a safe retry."""
    import httpx
    if not calls and getattr(exc, 'provider_status', None) == 'PRE_CONNECT_FAILED':
        return 10000
    if (isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 429 and
            len(calls) == 1 and calls[0].get('status_code') == 429):
        value = exc.response.headers.get('Retry-After', '').strip()
        if value:
            try:
                seconds = float(value)
                if math.isfinite(seconds) and seconds >= 0:
                    return math.ceil(seconds * 1000)
            except ValueError:
                from email.utils import parsedate_to_datetime
                from datetime import datetime, timezone
                try:
                    target = parsedate_to_datetime(value)
                    if target.tzinfo is None:
                        target = target.replace(tzinfo=timezone.utc)
                    return max(0, math.ceil((target - datetime.now(timezone.utc)).total_seconds() * 1000))
                except (TypeError, ValueError, OverflowError):
                    pass
        return 10000
    return None


def _chunk_data(chunk):
    metadata = dict(chunk.metadata or {})
    return dict(id=metadata.get('id') or str(uuid4()), content=chunk.content,
        page_number=chunk.page_number, content_type=chunk.content_type,
        image_path=chunk.image_path, token_count=chunk.token_count, metadata=metadata,
        **{key: metadata.get(key) for key in ('parent_id', 'heading_level', 'heading_text',
                                            'start_offset', 'end_offset')},
        chunk_type=metadata.get('chunk_type', 'content'))


def _image_manifest(chunks):
    root = Path(get_settings().IMAGE_DIR).resolve()
    found = {}
    for chunk in chunks:
        if not chunk.get('image_path'):
            continue
        image = (root / chunk['image_path']).resolve()
        if not image.is_relative_to(root) or not image.is_file():
            raise ValueError('TASK_IMAGE_REFERENCE_INVALID')
        found[chunk['image_path']] = source_digest(image)
    return found


def _checkpoint(claim, chunks, **extra):
    return dict(schema='file-stage-v1', task_id=claim.task_id, file_id=claim.file_id,
                generation=claim.target_generation, kb_id=claim.payload['_kb_id'],
                source_sha256=claim.payload['_source_sha256'],
                source_path=claim.payload['_source_path'],
                models=claim.payload['_model_snapshots'], chunks_data=chunks,
                image_artifacts=(claim.payload.get('_image_artifacts', {}) if claim.payload.get('_operation') == 'index'
                                 else _image_manifest(chunks)), **extra)


def _load_checkpoint(claim):
    value = read_artifact(claim.task_id, claim.checkpoint_ref)
    if (value.get('schema') != 'file-stage-v1' or value.get('task_id') != claim.task_id or
        value.get('file_id') != claim.file_id or value.get('generation') != claim.target_generation or
        value.get('source_sha256') != claim.payload['_source_sha256'] or
        value.get('models') != claim.payload['_model_snapshots']):
        raise ValueError('TASK_CHECKPOINT_IDENTITY_MISMATCH')
    _verify_checkpoint_artifacts(claim.task_id, value)
    return value


def _verify_checkpoint_artifacts(task_id, checkpoint):
    if _image_manifest(checkpoint['chunks_data']) != checkpoint.get('image_artifacts', {}):
        raise ValueError('TASK_IMAGE_HASH_MISMATCH')
    reference = checkpoint.get('provenance_manifest')
    if reference:
        path = Path(reference).resolve()
        if not path.is_relative_to(_task_directory(task_id).resolve()):
            raise ValueError('TASK_PROVENANCE_PATH_INVALID')
        raw = path.read_bytes()
        if path.stem != _digest(raw):
            raise ValueError('TASK_PROVENANCE_HASH_MISMATCH')
        value = json.loads(raw)
        if value.get('source_sha256') != checkpoint['source_sha256']:
            raise ValueError('TASK_PROVENANCE_SOURCE_MISMATCH')


def _relevant_resume_effects(row, effects):
    # A manual budget recovery retains its original intent as REPLAY_AUTHORIZED.
    # Only an explicitly mapped successor can settle that historical intent;
    # ordinary authorized replays and unrelated states retain stock semantics.
    payload = json.loads(row.payload)
    recovery = payload.get('_vlm_recovery')
    if not recovery or row.stage != 'parse':
        return effects
    if (not isinstance(recovery, dict) or recovery.get('max_tokens') != payload.get('_vlm_max_tokens') or
            recovery.get('max_tokens') != 2048 or not isinstance(recovery.get('superseded'), dict)):
        raise ValueError('VLM_RECOVERY_MAPPING_INVALID')
    by_key = {effect.effect_key: effect for effect in effects}
    excluded = set()
    for old_key, mapping in recovery['superseded'].items():
        old = by_key.get(old_key)
        successor = by_key.get(mapping.get('successor_effect_key')) if isinstance(mapping, dict) else None
        if (old is None or successor is None or old is successor or
                old.state != 'REPLAY_AUTHORIZED' or old.stage != 'parse' or
                old.input_hash != mapping.get('input_hash') or
                successor.input_hash != mapping.get('successor_input_hash') or
                successor.state not in ('PENDING', 'COMPLETED')):
            raise ValueError('VLM_RECOVERY_SUCCESSOR_UNSETTLED')
        excluded.add(old_key)
    return [effect for effect in effects if effect.effect_key not in excluded]


def verify_checkpoint_for_resume(row, effects):
    """UNKNOWN may resume only from an intact checkpoint and settled effects."""
    from app.tasks.dispatch import Claim
    try:
        effects = _relevant_resume_effects(row, effects)
        if any(effect.state != 'COMPLETED' for effect in effects):
            return False
        payload = json.loads(row.payload)
        if not row.checkpoint_ref:
            if row.stage != 'parse' and not (row.stage == 'embed' and payload.get('_operation') == 'index'):
                return False
            _assert_source(payload)
            if row.stage == 'embed' and not isinstance(payload.get('chunks_data'), list):
                return False
            if row.stage == 'embed' and _image_manifest(payload['chunks_data']) != payload.get('_image_artifacts', {}):
                return False
            # Frozen settings/model references are required; credentials remain
            # outside artifacts and will be compared before any resumed call.
            if not isinstance(payload.get('_model_snapshots'), dict):
                return False
            for effect in effects:
                stored = read_artifact(row.task_id, effect.result_ref)
                if stored.get('effect_key') != effect.effect_key:
                    return False
            return True
        claim = Claim(row.task_id, row.file_id, row.target_generation, row.owner_token or '',
                      row.attempt_no, row.stage, payload, row.checkpoint_ref)
        value = _load_checkpoint(claim)
        if row.stage == 'index':
            _validate_vectors(value['embeddings'], sum(c.get('chunk_type') != 'parent' for c in value['chunks_data']))
        for effect in effects:
            stored = read_artifact(row.task_id, effect.result_ref)
            if stored.get('effect_key') != effect.effect_key:
                return False
        return True
    except (ValueError, OSError, TypeError, KeyError):
        return False


def recover_materialized_effects_for_resume(session, row, effects):
    """Recover write-before-CAS responses inside the caller's resume transaction.

    No provider is called. A pending intent is settled only by exactly one
    self-hashed, task-local, identity-matched result envelope. An absent or
    conflicting result remains UNKNOWN. Caller must roll back on False.
    """
    from app.tasks import dispatch
    try:
        directory = _task_directory(row.task_id)
        effects = _relevant_resume_effects(row, effects)
        pending = [effect for effect in effects if effect.state == 'PENDING']
        if any(effect.state not in ('PENDING', 'COMPLETED') for effect in effects):
            return False
        materialized = {}
        for path in directory.glob('*.json'):
            try:
                value = read_artifact(row.task_id, str(path))
            except (ValueError, OSError):
                continue
            if value.get('schema') != 'task-effect-v1':
                continue
            materialized.setdefault(value.get('effect_key'), []).append((str(path), value))
        settled = []
        for effect in pending:
            matches = []
            for reference, value in materialized.get(effect.effect_key, []):
                if (value.get('task_id') != row.task_id or value.get('stage') != effect.stage or
                        value.get('input_hash') != effect.input_hash or
                        _digest(_json([row.task_id, effect.stage, value.get('kind'), effect.input_hash])) != effect.effect_key):
                    continue
                if value.get('kind') == 'embedding':
                    _validate_vectors(value['result'], value['input_count'])
                elif value.get('kind') != 'vlm' or not isinstance(value.get('result'), str) or not value['result'].strip():
                    continue
                matches.append((reference, value))
            if len(matches) != 1:
                return False
            settled.append((effect, *matches[0]))
        for effect, reference, value in settled:
            effect.state, effect.result_ref = 'COMPLETED', reference
            effect.provider_request_id = value.get('provider_request_id')
            effect.updated_at = dispatch.now_ms()
            session.add(effect)
        session.flush()
        return True
    except (ValueError, OSError, TypeError, KeyError):
        return False


def _handoff(claim, stage, checkpoint):
    from app.tasks.dispatch import handoff
    reference = write_artifact(claim.task_id, checkpoint)
    if not handoff(claim, stage, reference):
        raise StaleStageClaim('TASK_HANDOFF_OWNERSHIP_LOST')
    return {'stage': stage, 'checkpoint_ref': reference}


def _preview(claim, checkpoint):
    from app.tasks import dispatch
    from app.services.bm25 import get_bm25_index
    from app.services.parent_child import remap_and_validate_graph
    from app.services.index_builds import verified_provenance
    rows = remap_and_validate_graph(checkpoint['chunks_data'], claim.file_id)
    provenance = verified_provenance(checkpoint, rows)
    reference = write_artifact(claim.task_id, checkpoint)
    with Session(dispatch.engine) as session:
        if not dispatch.guard_claim_in_session(session, claim):
            raise StaleStageClaim('TASK_PREVIEW_OWNERSHIP_LOST')
        doc = session.get(FileDocument, claim.file_id)
        from app.models import ChunkIndex
        historical_bm25 = session.exec(select(ChunkIndex.id).join(
            DocumentChunk, DocumentChunk.id == ChunkIndex.chunk_id).where(
                DocumentChunk.file_id == claim.file_id).limit(1)).first()
        if (getattr(doc, 'active_build_id', None) or historical_bm25 or
                doc.status in (FileStatus.PARSED, FileStatus.READY)):
            raise ValueError('PREVIEW_CANNOT_REPLACE_ACTIVE_INDEX')
        old = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == claim.file_id)).all()
        bm25 = get_bm25_index()
        for chunk in old:
            bm25.delete_chunk_index(session, chunk.id, doc.kb_id)
            session.delete(chunk)
        session.flush()
        for index, row in enumerate(rows):
            session.add(DocumentChunk(**{key: row[key] for key in (
                'id', 'content', 'page_number', 'image_path', 'parent_id', 'heading_level',
                'heading_text', 'chunk_type', 'token_count', 'start_offset', 'end_offset')},
                file_id=claim.file_id, original_index=index,
                source_metadata_json=provenance.get(row['id']),
                content_type=ContentType(row.get('content_type', 'text'))))
        doc.status, doc.progress, doc.error_message = FileStatus.PENDING_CONFIRM, 50, None
        session.add(doc)
        receipt = session.get(dispatch.FileTaskReceipt, claim.task_id)
        receipt.checkpoint_ref = reference
        session.add(receipt)
        session.flush()
        result = dict(file_id=claim.file_id, chunk_count=len(rows), status='pending_confirm',
                      image_descriptions=checkpoint.get('image_descriptions', {}))
        if not dispatch.complete_in_session(session, claim, result):
            raise StaleStageClaim('TASK_PREVIEW_OWNERSHIP_LOST')
        session.commit()
        return result


def _validate_vlm_cache_only_coverage(claim, chunks, service, settings):
    if not claim.payload.get('_vlm_cache_only'):
        return
    expected = claim.payload.get('_vlm_cache_expected_inputs')
    suffixes = claim.payload.get('_vlm_cache_expected_suffixes')
    locators = claim.payload.get('_vlm_cache_expected_locators')
    if (service is None or not isinstance(expected, list) or len(expected) != 21 or
            any(not isinstance(value, str) or len(value) != 64 for value in expected) or
            len(set(expected)) != 21 or not isinstance(suffixes, dict) or set(suffixes) != set(expected) or
            not isinstance(locators, dict) or set(locators) != set(expected)):
        raise UnconfirmedEffect('VLM_CACHE_ONLY_COVERAGE_INVALID')
    actual = []
    root = Path(settings.IMAGE_DIR).resolve()
    for chunk in chunks:
        if chunk.content_type != 'image':
            continue
        if not chunk.image_path:
            raise UnconfirmedEffect('VLM_CACHE_ONLY_IMAGE_MISSING')
        image = (root / chunk.image_path).resolve()
        if not image.is_relative_to(root) or not image.is_file():
            raise UnconfirmedEffect('VLM_CACHE_ONLY_IMAGE_MISSING')
        identity = _vlm_request_identity(claim, source_digest(image), service)
        _, input_hash = _effect_identity(claim, 'vlm', identity)
        if image.suffix != suffixes.get(input_hash):
            raise UnconfirmedEffect('VLM_CACHE_ONLY_COVERAGE_MISMATCH')
        locator = locators.get(input_hash)
        if (not isinstance(locator, dict) or not isinstance(locator.get('locator'), str) or
                not image.stem.endswith(locator['locator']) or
                chunk.page_number != locator.get('page_number')):
            raise UnconfirmedEffect('VLM_CACHE_ONLY_LOCATOR_MISMATCH')
        actual.append(input_hash)
    if len(actual) != 21 or len(set(actual)) != 21 or set(actual) != set(expected):
        raise UnconfirmedEffect('VLM_CACHE_ONLY_COVERAGE_MISMATCH')


def _parse(claim):
    from app.tasks import dispatch
    from app.services.parser import FileParser
    from app.services.vlm import TaskVLMService
    from app.services.parent_child import (ParentChildConfig, build_parent_child_chunks,
        derive_image_description_chunk)
    payload = claim.payload
    source = _assert_source(payload)
    with Session(dispatch.engine) as session:
        semantic = _resolve_frozen(session, payload['_model_snapshots']['semantic'])
        vector = _resolve_frozen(session, payload['_model_snapshots']['vector'])
        vlm = _resolve_frozen(session, payload['_model_snapshots']['vlm'])
    settings = get_settings()
    audit = _task_directory(claim.task_id) / ('a' + str(claim.attempt_no))
    parser_settings = settings.model_copy(update={**payload['_parser_settings'], 'PDF_AUDIT_DIR': str(audit)})
    parser = FileParser(payload['_kb_id'], image_namespace=f'tasks/{UUID(claim.task_id).hex}/{claim.attempt_no}',
                        parser_settings=parser_settings, audit_flat=True)
    parent_child = payload.get('parent_child', False)
    pc = ParentChildConfig(payload.get('parent_chunk_size', 4096), payload.get('parent_chunk_overlap', 0),
                           payload.get('child_chunk_size', 384), payload.get('child_chunk_overlap', 48))
    if parent_child:
        pc.validate()
    chunks = parser.parse(str(source), chunk_mode=payload['chunk_mode'],
        chunk_size=pc.parent_chunk_size if parent_child else payload['chunk_size'],
        chunk_overlap=0 if parent_child else payload['chunk_overlap'],
        separator=payload.get('separator', '\n\n'), heading_level=payload['heading_level'],
        use_semantic=semantic is not None,
        embedding_model=semantic.model_name if semantic else None,
        embedding_base_url=semantic.base_url if semantic else None,
        embedding_api_key=semantic.api_key if semantic else None,
        embedding_max_tokens=vector.context_length if vector else settings.EMBEDDING_MAX_CONTEXT_LENGTH,
        semantic_max_tokens=semantic.context_length if semantic else None)
    summary = {'COMPLETED': 0, 'NOT_CONFIGURED': 0}
    runner = EffectRunner(claim)
    service = TaskVLMService(base_url=vlm.base_url, api_key=vlm.api_key, model=vlm.model_name,
                         prompt=payload['_vlm_prompt']) if vlm else None
    _validate_vlm_cache_only_coverage(claim, chunks, service, settings)
    parsed_with_descriptions, derived_descriptions = [], set()
    for chunk in chunks:
        parsed_with_descriptions.append(chunk)
        if chunk.content_type != 'image' or not chunk.image_path:
            continue
        if service is None:
            summary['NOT_CONFIGURED'] += 1
            continue
        image = (Path(settings.IMAGE_DIR) / chunk.image_path).resolve()
        if not image.is_relative_to(Path(settings.IMAGE_DIR).resolve()):
            raise ValueError('TASK_IMAGE_PATH_INVALID')
        identity = _vlm_request_identity(claim, source_digest(image), service)
        service.max_tokens = identity['max_tokens']
        caption = asyncio.run(runner('vlm', identity, lambda: service.describe_image(str(image))))
        description = {'status':'COMPLETED', 'image_sha256':identity['image_sha256'], 'model':service.model}
        if parent_child and source.suffix.lower() == '.pdf':
            # Original IMAGE bytes/placeholder stay original; derived text gets
            # its own authenticated manifest record and searchable child IDs.
            chunk.metadata = {**(chunk.metadata or {}), 'image_description': description}
            locator = (chunk.metadata or {}).get('source_locator') or {}
            key = (chunk.page_number, identity['image_sha256'],
                json.dumps(locator, sort_keys=True) if locator else chunk.image_path)
            if key not in derived_descriptions:
                effect_key, _ = _effect_identity(claim, 'vlm', identity)
                derived = derive_image_description_chunk(chunk, caption,
                    source_sha256=payload['_source_sha256'], image_sha256=identity['image_sha256'],
                    model=service.model, prompt_sha256=hashlib.sha256(service.prompt.encode()).hexdigest(),
                    max_tokens=identity['max_tokens'], effect_key=effect_key)
                derived.token_count = parser.splitter.count_tokens(derived.content)
                parsed_with_descriptions.append(derived)
                derived_descriptions.add(key)
        else:
            chunk.content += '\n[图片描述（模型生成，需核对原图）]\n' + caption
            chunk.metadata = {**(chunk.metadata or {}), 'content_origin':'model_generated_image_description',
                               'image_description': description}
            chunk.token_count = parser.splitter.count_tokens(chunk.content)
        summary['COMPLETED'] += 1
    if parent_child:
        chunks = build_parent_child_chunks(parsed_with_descriptions, pc, count_tokens=parser.splitter.count_tokens)
    _assert_source(payload)
    from app.services.pdf_provenance import write_manifest
    final_manifest = write_manifest(str(audit), payload['_source_sha256'],
        getattr(parser, 'pdf_page_records', []), chunks, schema='ingest-parse-v1', flat=True)
    checkpoint = _checkpoint(claim, [_chunk_data(chunk) for chunk in chunks],
        image_descriptions=summary, provenance_manifest=final_manifest,
        artifact_paths={'audit': str(audit), 'images': str(Path(parser.image_dir).resolve())})
    if payload.get('auto_vectorize'):
        return _handoff(claim, 'embed', checkpoint)
    return _preview(claim, checkpoint)


def _embed(claim):
    from app.tasks import dispatch
    from app.services.embedding import EmbeddingService
    _assert_source(claim.payload)
    checkpoint = _load_checkpoint(claim) if claim.checkpoint_ref else _checkpoint(
        claim, claim.payload['chunks_data'])
    _verify_checkpoint_artifacts(claim.task_id, checkpoint)
    with Session(dispatch.engine) as session:
        binding = _resolve_frozen(session, checkpoint['models']['vector'])
    if binding is None:
        raise ValueError('TASK_VECTOR_MODEL_MISSING')
    contents = [row.get('content', '') for row in checkpoint['chunks_data'] if row.get('chunk_type') != 'parent']
    service = EmbeddingService(base_url=binding.base_url, api_key=binding.api_key, model=binding.model_name)
    vectors = asyncio.run(service.embed_documents(contents, model_id=binding.model_name)) if contents else []
    checkpoint.update(embeddings=vectors, dimension=_validate_vectors(vectors, len(contents)))
    return _handoff(claim, 'index', checkpoint)


def execute_stage(claim):
    """Called by the durable executor while its independent heartbeat runs."""
    with effect_scope(EffectRunner(claim)):
        if claim.stage == 'parse':
            return _parse(claim)
        if claim.stage == 'embed':
            return _embed(claim)
        if claim.stage == 'index':
            from app.services.index_builds import publish_index_build
            _assert_source(claim.payload)
            checkpoint = _load_checkpoint(claim)
            chunks = checkpoint['chunks_data']
            _validate_vectors(checkpoint['embeddings'], sum(row.get('chunk_type') != 'parent' for row in chunks))
            return publish_index_build(claim, chunks, checkpoint['embeddings'],
                vector_model=claim.payload.get('vector_model'), manifest=checkpoint)
        raise ValueError('TASK_STAGE_INVALID')


_artifact_sweep_cursor = None


class _SweepBudgetExhausted(Exception):
    pass


def sweep_task_artifacts(grace_seconds=3600, max_tasks=25, max_files_per_task=2000,
                         max_read_bytes=16 * 1024 * 1024):
    """Collect verified task-owned orphans; retain ledger evidence and live images.

    Explicit checkpoint/effect/attempt references and their audit manifests are
    retained even after completion. These are recovery and provider-call evidence,
    not disposable caches. Large images cease to be protected once no live SQL
    chunk references them and the task is terminal. Never touches source uploads,
    shared manifests, another task's directory, or an active/UNKNOWN task.
    """
    import time
    from itertools import islice
    from app.tasks import dispatch
    from app.models import FileIndexBuild
    global _artifact_sweep_cursor
    stats = dict(files_deleted=0, bytes_deleted=0, tasks_examined=0,
                 tasks_deferred=0, tasks_without_artifacts=0, bytes_read=0, errors=[])
    cutoff = time.time() - max(0, grace_seconds)
    settings = get_settings()
    image_root = Path(settings.IMAGE_DIR).resolve()
    cache = {}

    def read_bounded(task_id, reference):
        path = str(Path(reference).resolve())
        if path in cache:
            return cache[path]
        size = Path(path).stat().st_size
        if stats['bytes_read'] + size > max_read_bytes:
            raise _SweepBudgetExhausted()
        stats['bytes_read'] += size
        cache[path] = read_artifact(task_id, path)
        return cache[path]

    with Session(dispatch.engine) as session:
        query = select(dispatch.FileTaskReceipt).where(dispatch.FileTaskReceipt.status.in_(
            ['COMPLETED', 'FAILED', 'CANCELLED'])).order_by(dispatch.FileTaskReceipt.task_id).limit(max(1, max_tasks))
        tasks = session.exec(query.where(dispatch.FileTaskReceipt.task_id > _artifact_sweep_cursor)
                             if _artifact_sweep_cursor else query).all()
        if not tasks and _artifact_sweep_cursor:
            _artifact_sweep_cursor = None
            tasks = session.exec(query).all()
        for task in tasks:
            _artifact_sweep_cursor = task.task_id
            stats['tasks_examined'] += 1
            try:
                task_path = _task_directory(task.task_id)
                directory = task_path.resolve()
                if task_path.is_symlink() or directory.parent != artifact_root():
                    raise ValueError('TASK_ARTIFACT_CLEANUP_PATH_INVALID')
                # UUID validation and the ledger row prove this task namespace.
                payload = json.loads(task.payload)
                raw_image_task = image_root / payload['_kb_id'] / 'tasks' / UUID(task.task_id).hex
                image_task = raw_image_task.resolve()
                if (not image_task.is_relative_to(image_root) or raw_image_task.is_symlink() or
                        any(parent.is_symlink() for parent in raw_image_task.parents if parent.is_relative_to(image_root))):
                    raise ValueError('TASK_IMAGE_CLEANUP_PATH_INVALID')
                if not directory.exists() and not image_task.exists():
                    stats['tasks_without_artifacts'] += 1
                    continue
                attempts = session.exec(select(dispatch.FileTaskAttempt).where(
                    dispatch.FileTaskAttempt.task_id == task.task_id).limit(max_files_per_task + 1)).all()
                effects = session.exec(select(dispatch.FileTaskEffect).where(
                    dispatch.FileTaskEffect.task_id == task.task_id).limit(max_files_per_task + 1)).all()
                if len(attempts) > max_files_per_task or len(effects) > max_files_per_task:
                    raise _SweepBudgetExhausted()
                references = {task.checkpoint_ref} if task.checkpoint_ref else set()
                references.update(a.artifact_ref for a in attempts if a.artifact_ref)
                # Effect result bodies can be large. Their references protect
                # them without loading vectors during every garbage sweep.
                protected = {str(Path(e.result_ref).resolve()) for e in effects if e.result_ref}
                effect_keys = {effect.effect_key for effect in effects}
                # A response materialized before its ledger CAS is still useful
                # evidence when an UNKNOWN task is explicitly terminated.
                root_json = list(islice(directory.glob('*.json'), max_files_per_task + 1))
                if len(root_json) > max_files_per_task:
                    raise _SweepBudgetExhausted()
                for candidate in root_json:
                    if str(candidate.resolve()) in protected:
                        continue
                    try:
                        envelope = read_bounded(task.task_id, str(candidate))
                    except (ValueError, OSError):
                        continue
                    if (envelope.get('schema') in ('task-effect-v1', 'task-effect-rejected-v1') and
                            envelope.get('task_id') == task.task_id and
                            envelope.get('effect_key') in effect_keys):
                        protected.add(str(candidate.resolve()))
                live_builds = session.exec(select(FileIndexBuild).where(
                    FileIndexBuild.file_id == task.file_id, FileIndexBuild.task_id == task.task_id,
                    FileIndexBuild.state.in_(['ACTIVE', 'STAGING'])).limit(max_files_per_task + 1)).all()
                if len(live_builds) > max_files_per_task:
                    raise _SweepBudgetExhausted()
                for build in live_builds:
                    manifest = json.loads(build.manifest)
                    if manifest.get('checkpoint_ref'):
                        references.add(manifest['checkpoint_ref'])
                    if manifest.get('provenance_manifest'):
                        protected.add(str(Path(manifest['provenance_manifest']).resolve()))
                for reference in references:
                    value = read_bounded(task.task_id, reference)
                    protected.add(str(Path(reference).resolve()))
                    if value.get('provenance_manifest'):
                        protected.add(str(Path(value['provenance_manifest']).resolve()))
                # A live or staging build can still require additional image
                # resources. Preserve the whole image tree until retirement.
                targets = [(directory, protected)]
                if not live_builds and image_task.exists():
                    prefix = image_task.relative_to(image_root).as_posix() + '/'
                    # image_path has an additive index. Bound the query to this
                    # namespace rather than reading unrelated corpus locators.
                    values = session.exec(select(DocumentChunk.image_path).where(
                        DocumentChunk.image_path >= prefix, DocumentChunk.image_path < prefix + '\uffff')
                        .limit(max_files_per_task + 1)).all()
                    if len(values) > max_files_per_task:
                        raise _SweepBudgetExhausted()
                    live_images = {str((image_root / value).resolve()) for value in values}
                    targets.append((image_task, live_images))
                prepared = []
                for root, retained in targets:
                    if not root.exists() or root.is_symlink():
                        continue
                    candidates = list(islice(root.rglob('*'), max_files_per_task + 1))
                    if len(candidates) > max_files_per_task:
                        raise _SweepBudgetExhausted()
                    prepared.append((root, retained, candidates))
                for root, retained, candidates in prepared:
                    # A terminal task cannot regain a lease; new user submissions
                    # receive another task ID and another artifact namespace.
                    for candidate in candidates:
                        real = candidate.resolve()
                        if (candidate.is_symlink() or not real.is_relative_to(root) or
                                not candidate.is_file() or str(real) in retained):
                            continue
                        info = candidate.stat()
                        if info.st_mtime > cutoff:
                            continue
                        candidate.unlink()
                        stats['files_deleted'] += 1
                        stats['bytes_deleted'] += info.st_size
                    for child in sorted(candidates, key=lambda p: len(p.parts), reverse=True):
                        if (child.is_dir() and not child.is_symlink() and child.resolve().is_relative_to(root)):
                            try:
                                child.rmdir()
                            except OSError:
                                pass
            except _SweepBudgetExhausted:
                # Defer the entire namespace before deleting anything when its
                # references cannot be established within this sweep's budget.
                stats['tasks_deferred'] += 1
            except (ValueError, TypeError, KeyError, OSError) as exc:
                # Unreadable references mean insufficient evidence for deletion.
                stats['errors'].append(dict(task_id=task.task_id, error_type=type(exc).__name__))
    return stats

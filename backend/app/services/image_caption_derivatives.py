"""Trusted, additive caption snapshots; no client metadata grants provenance.

The operator registers verified preview SQL rows in a new JSON file. Publication
recovers their provenance by exact ID/content/page/type, then fences the original
PDF dependencies in the same SQLite write transaction as activation. This module
does not create tasks, invoke providers, or mutate source files.
"""
import hashlib
import json
from pathlib import Path
import re
from collections import Counter
from uuid import UUID, uuid5

from sqlmodel import select
from sqlalchemy import or_

from app.models import DocumentChunk, FileDocument, FileIndexBuild, FileStatus


SCHEMA = 'registered-image-caption-json-v1'
DERIVATION = 'image-caption-text-v1'
LABEL = '[图片描述（模型生成，需核对原图）]'
NAMESPACE = UUID('ca73dd23-9cf8-4a86-b34f-df2a7b316e5e')
_SHA = re.compile('[0-9a-f]{64}')
_FIELDS = ('original_file_id', 'original_build_id', 'original_generation',
           'original_source_sha256', 'source_manifest_sha256',
           'original_chunk_id', 'original_chunk_sha256', 'page',
           'source_image_path', 'image_sha256')


def _require(value, reason):
    if not value:
        raise ValueError(reason)


def _registered_identity(file_doc):
    return bool(file_doc.sha256) and file_doc.id == str(uuid5(
        NAMESPACE, file_doc.kb_id + ':' + file_doc.sha256))


def _digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def _artifact_identity(path):
    info = Path(path).stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _verify_artifact(path, expected):
    path = Path(path).resolve(strict=True)
    before = _artifact_identity(path)
    _require(_digest_file(path) == expected and _artifact_identity(path) == before,
             'CAPTION_ORIGINAL_ARTIFACT_CHANGED')
    return dict(path=str(path), identity=before, sha256=expected)


def guard_caption_artifact_identities(artifacts):
    """Cheap final fence after staging byte verification; no hashing under lock."""
    for artifact in artifacts:
        _require(_artifact_identity(artifact['path']) == artifact['identity'],
                 'CAPTION_ORIGINAL_ARTIFACT_CHANGED')


def caption_dependencies(file_doc, provenances, source_sha256, rows=None):
    """Extract dependencies only from exact trusted SQL provenance reuse.

    Callers must not pass incoming request metadata. Native PDF captions and
    ordinary files keep their existing behavior. Derived JSON cannot claim the
    original PDF's digest or pagination as its own.
    """
    dependencies = {}
    # Registration IDs commit to the derived file's own immutable bytes. This
    # marker survives lost preview metadata and display-name edits, so malformed
    # source JSON cannot downgrade a registered derivative to an ordinary file.
    registered = _registered_identity(file_doc)
    provenance_hint = any(isinstance(value := json.loads(raw), dict) and value.get('provenance_schema') == SCHEMA
                          for raw in provenances.values())
    source_body = {}
    if registered or provenance_hint:
        try:
            source_body = json.loads(Path(file_doc.local_path).read_bytes())
        except (ValueError, OSError):
            source_body = {}
        _require(isinstance(source_body, dict) and source_body.get('schema') == SCHEMA and
                 _digest_file(file_doc.local_path) == source_sha256 == file_doc.sha256,
                 'CAPTION_DERIVED_SOURCE_SCHEMA_OR_HASH_CHANGED')
    derived = registered or provenance_hint
    if derived:
        _require(rows is not None and len(rows) == len(provenances) and rows and
                 all(row['id'] in provenances for row in rows), 'CAPTION_DERIVED_PROVENANCE_COVERAGE_MISSING')
        snapshot = source_body['source_snapshot']
        expected = Counter((item['kind'], item['text'], item['provenance']['original_chunk_id'])
                           for item in [*snapshot['proposed_parents'], *snapshot['proposed_children']])
        actual = Counter()
        row_by_id = {row['id']: row for row in rows}
        for row in rows:
            metadata = json.loads(provenances[row['id']])
            descriptions = metadata.get('image_descriptions') or []
            _require(metadata.get('provenance_schema') == SCHEMA and len(descriptions) == 1 and
                     isinstance(descriptions[0], dict) and row.get('page_number') is None and
                     row.get('image_path') is None and row.get('content_type') == 'text' and
                     descriptions[0].get('indexed_text_sha256') == hashlib.sha256(row.get('content', '').encode('utf-8')).hexdigest(),
                     'CAPTION_DERIVED_PROVENANCE_COVERAGE_INVALID')
            actual[(row.get('chunk_type'), row.get('content'), descriptions[0].get('original_chunk_id'))] += 1
            if row.get('chunk_type') == 'child':
                parent = row_by_id.get(row.get('parent_id'))
                _require(parent is not None and parent.get('chunk_type') == 'parent' and
                         json.loads(provenances[parent['id']])['image_descriptions'][0].get('original_chunk_id') ==
                         descriptions[0].get('original_chunk_id'), 'CAPTION_DERIVED_PARENT_ORIGIN_INVALID')
        _require(actual == expected, 'CAPTION_DERIVED_GRAPH_COVERAGE_INVALID')
    for raw in provenances.values():
        metadata = json.loads(raw)
        _require(isinstance(metadata, dict), 'CAPTION_DERIVED_PROVENANCE_INVALID')
        if metadata.get('provenance_schema') != SCHEMA:
            _require(not derived, 'CAPTION_DERIVED_PROVENANCE_COVERAGE_MISSING')
            continue
        _require(Path(file_doc.local_path).suffix.lower() == '.json' and
                 file_doc.name.startswith(LABEL) and
                 metadata.get('source_sha256') == source_sha256 == file_doc.sha256 and
                 _SHA.fullmatch(source_sha256 or '') and
                 metadata.get('content_origins') == ['model_generated_image_description'],
                 'CAPTION_DERIVED_SOURCE_IDENTITY_INVALID')
        locators = metadata.get('source_locators') or []
        descriptions = metadata.get('image_descriptions') or []
        _require(len(locators) == len(descriptions) == 1, 'CAPTION_DERIVED_PROVENANCE_AMBIGUOUS')
        locator, description = locators[0], descriptions[0]
        _require(isinstance(locator, dict) and isinstance(description, dict), 'CAPTION_DERIVED_PROVENANCE_INVALID')
        _require(locator.get('kind') == 'pdf-image-description' and
                 locator.get('derivation') == description.get('derivation') == DERIVATION and
                 locator.get('derived_document_sha256') == source_sha256 and
                 description.get('status') == 'COMPLETED' and
                 description.get('requires_original_verification') is True and
                 _SHA.fullmatch(description.get('indexed_text_sha256') or '') and
                 isinstance(description.get('model'), str) and description['model'].strip(),
                 'CAPTION_DERIVED_PROVENANCE_INVALID')
        dependency = {key: locator.get(key) for key in _FIELDS}
        _require(all(dependency.get(key) for key in _FIELDS) and
                 isinstance(dependency['original_generation'], int) and
                 not isinstance(dependency['original_generation'], bool) and
                 isinstance(dependency['page'], int) and not isinstance(dependency['page'], bool) and
                 dependency['page'] > 0 and
                 all(_SHA.fullmatch(dependency[key]) for key in
                     ('original_source_sha256', 'source_manifest_sha256',
                      'original_chunk_sha256', 'image_sha256')) and
                 dependency['original_file_id'] != file_doc.id,
                 'CAPTION_DEPENDENCY_INVALID')
        for key in ('original_source_sha256', 'original_file_id', 'original_build_id',
                    'original_generation', 'original_chunk_id', 'image_sha256', 'source_image_path'):
            _require(description.get(key) == dependency[key], 'CAPTION_DESCRIPTION_IDENTITY_MISMATCH')
        _require(description.get('source_page') == dependency['page'], 'CAPTION_DESCRIPTION_PAGE_MISMATCH')
        identity = (dependency['original_file_id'], dependency['original_chunk_id'])
        _require(identity not in dependencies or dependencies[identity] == dependency,
                 'CAPTION_DEPENDENCY_CONFLICT')
        dependencies[identity] = dependency
    _require(not derived or dependencies, 'CAPTION_DERIVED_PROVENANCE_COVERAGE_MISSING')
    return [dependencies[key] for key in sorted(dependencies)]


def guard_caption_dependencies(session, file_doc, dependencies, *, verify_files=True):
    """Fail closed if any original changes; callers choose the staging/fence mode.

    Staging verifies immutable physical bytes before acquiring a write lock.
    Final publication calls with verify_files=False after the regular task guard
    acquires SQLite's write lock, and separately restats staged artifact identities.
    That prevents application task dispatch/deletion/reindex from changing source
    rows before commit. Host-level mutation outside the application remains
    outside SQLite's guarantees.
    """
    _require(isinstance(dependencies, list) and all(isinstance(row, dict) for row in dependencies),
             'CAPTION_DEPENDENCIES_INVALID')
    def load(model, column, keys):
        result = {}
        keys = sorted(set(keys))
        for start in range(0, len(keys), 400):
            records = session.exec(select(model).where(column.in_(keys[start:start + 400]))
                                   .execution_options(populate_existing=True)).all()
            result.update((getattr(record, column.key), record) for record in records)
        return result
    sources = load(FileDocument, FileDocument.id, [d['original_file_id'] for d in dependencies])
    builds = load(FileIndexBuild, FileIndexBuild.build_id, [d['original_build_id'] for d in dependencies])
    chunks = load(DocumentChunk, DocumentChunk.id, [d['original_chunk_id'] for d in dependencies])
    superseded = set()
    source_ids = sorted(sources)
    for start in range(0, len(source_ids), 400):
        superseded.update(session.exec(select(FileDocument.supersedes_file_id).where(
            FileDocument.supersedes_file_id.in_(source_ids[start:start + 400]),
            FileDocument.kb_id == file_doc.kb_id, FileDocument.deleted_at.is_(None),
            or_(FileDocument.active_build_id.is_not(None),
                FileDocument.status.in_([FileStatus.PARSED, FileStatus.READY])))).all())
    artifacts = {}
    for dependency in dependencies:
        source_id = dependency['original_file_id']
        source = sources.get(source_id)
        _require(source is not None and source.kb_id == file_doc.kb_id and
                 source_id not in superseded and
                 source.deleted_at is None and
                 source.desired_generation == dependency['original_generation'] and
                 source.active_build_id == dependency['original_build_id'] and
                 (source.sha256 is None or source.sha256 == dependency['original_source_sha256']),
                 'CAPTION_ORIGINAL_FILE_CHANGED')
        build = builds.get(dependency['original_build_id'])
        _require(build is not None and build.file_id == source_id and
                 build.kb_id == file_doc.kb_id and build.state == 'ACTIVE' and
                 build.generation == dependency['original_generation'] and
                 build.manifest_hash == dependency['source_manifest_sha256'] and
                 hashlib.sha256(build.manifest.encode('utf-8')).hexdigest() == build.manifest_hash and
                 json.loads(build.manifest).get('source_sha256') == dependency['original_source_sha256'],
                 'CAPTION_ORIGINAL_BUILD_CHANGED')
        if verify_files and source.local_path not in artifacts:
            artifacts[source.local_path] = _verify_artifact(source.local_path, dependency['original_source_sha256'])
        chunk = chunks.get(dependency['original_chunk_id'])
        _require(chunk is not None and chunk.file_id == source_id and
                 hashlib.sha256(chunk.content.encode('utf-8')).hexdigest() == dependency['original_chunk_sha256'] and
                 chunk.page_number == dependency['page'] and chunk.image_path == dependency['source_image_path'] and
                 getattr(chunk.content_type, 'value', chunk.content_type) == 'image',
                 'CAPTION_ORIGINAL_CHUNK_CHANGED')
        original = json.loads(chunk.source_metadata_json or '{}')
        descriptions = original.get('image_descriptions') or []
        _require(original.get('source_sha256') == dependency['original_source_sha256'] and
                 'model_generated_image_description' in (original.get('content_origins') or []) and
                 len(descriptions) == 1 and isinstance(descriptions[0], dict) and
                 descriptions[0].get('status') == 'COMPLETED' and
                 descriptions[0].get('image_sha256') == dependency['image_sha256'],
                 'CAPTION_ORIGINAL_PROVENANCE_CHANGED')
        if verify_files:
            from app.core.config import get_settings
            root = Path(get_settings().IMAGE_DIR).resolve()
            image = (root / dependency['source_image_path']).resolve()
            _require(image.is_relative_to(root) and image.is_file(), 'CAPTION_ORIGINAL_IMAGE_CHANGED')
            if str(image) not in artifacts:
                artifacts[str(image)] = _verify_artifact(image, dependency['image_sha256'])
    return list(artifacts.values())


def inactive_caption_derivative_ids(session, allowed_kb_ids=None, *, candidate_file_ids=None):
    """Hide derivatives after application-managed original reindex/deletion.

    Active dependency builds and committed JSON identities are examined. Read
    eligibility checks SQL identity; expensive byte checks happen at publication.
    Malformed dependency records fail closed for that derived file. Ordinary
    builds lacking the dependency key retain their previous eligibility.
    """
    from app.services.file_versions import _require_flushed_eligibility_session
    _require_flushed_eligibility_session(session)
    statement = select(FileDocument, FileIndexBuild).join(
        FileIndexBuild, FileIndexBuild.build_id == FileDocument.active_build_id).where(
            FileDocument.deleted_at.is_(None),
            or_(FileIndexBuild.manifest.contains('"image_caption_dependencies"'),
                FileDocument.local_path.endswith('.json')))
    if allowed_kb_ids is not None:
        statement = statement.where(FileDocument.kb_id.in_(allowed_kb_ids))
    inactive = set()
    candidates = sorted(set(candidate_file_ids)) if candidate_file_ids is not None else None
    batches = [None] if candidates is None else [candidates[start:start + 500] for start in range(0, len(candidates), 500)]
    for batch in batches:
        selected = statement if batch is None else statement.where(FileDocument.id.in_(batch))
        for file_doc, build in session.exec(selected.execution_options(populate_existing=True)).all():
            if not (_registered_identity(file_doc) or
                    '"image_caption_dependencies"' in build.manifest):
                continue
            try:
                _require(hashlib.sha256(build.manifest.encode('utf-8')).hexdigest() == build.manifest_hash,
                         'CAPTION_BUILD_MANIFEST_CHANGED')
                dependencies = json.loads(build.manifest).get('image_caption_dependencies')
                _require(isinstance(dependencies, list) and dependencies, 'CAPTION_DEPENDENCIES_MISSING')
                guard_caption_dependencies(session, file_doc, dependencies, verify_files=False)
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                inactive.add(file_doc.id)
    return inactive


def guard_caption_model_at_acceptance(session, file_doc, payload):
    """Bind a registered caption source to its exact embedding identity.

    Called inside the official dispatch writer transaction, before generation or
    receipt mutation. No client-supplied expected snapshot is trusted. A later
    model edit is rejected independently by the worker's existing _resolve_frozen.
    """
    if not _registered_identity(file_doc):
        return
    path = Path(file_doc.local_path)
    raw = path.read_bytes()
    _require(path.suffix.lower() == '.json' and hashlib.sha256(raw).hexdigest() ==
             file_doc.sha256 == payload.get('_source_sha256'), 'TASK_CAPTION_SOURCE_IDENTITY_CHANGED')
    source = json.loads(raw)
    _require(isinstance(source, dict) and source.get('schema') == SCHEMA,
             'TASK_CAPTION_SOURCE_SCHEMA_CHANGED')
    source_snapshot = source.get('source_snapshot')
    model_snapshots = payload.get('_model_snapshots')
    _require(isinstance(source_snapshot, dict) and isinstance(model_snapshots, dict),
             'TASK_CAPTION_MODEL_BINDING_CHANGED')
    expected = source_snapshot.get('existing_embedding_binding')
    frozen = model_snapshots.get('vector')
    _require(isinstance(expected, dict) and expected == frozen and
             expected.get('binding_id') == payload.get('vector_model'),
             'TASK_CAPTION_MODEL_BINDING_CHANGED')
    from app.services.model_bindings import resolve_model
    from app.tasks.staged_pipeline import _binding_snapshot
    actual = _binding_snapshot(resolve_model(session, 'embedding', expected['binding_id'], use_default=False))
    _require(actual == expected, 'TASK_CAPTION_MODEL_BINDING_CHANGED')

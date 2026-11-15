"""Fenced publication of isolated vector builds and live SQL projection."""
import hashlib
import json
import math
import os
from pathlib import Path
from datetime import datetime
from uuid import uuid4

from sqlalchemy import func
from sqlmodel import Session, select

from app.core.database import engine
from app.models import (FileDocument, FileIndexBuild, DocumentChunk, ContentType,
                        FileStatus, KnowledgeBase)
from app.services.vector_store import VectorStoreService
from app.services.bm25 import get_bm25_index
from app.services.parent_child import remap_and_validate_graph


def _manifest(data):
    raw = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def verified_provenance(checkpoint, rows):
    """Recover locators only from an integrity-checked source-matched manifest."""
    from app.services.pdf_provenance import _manifest_index, chunk_key
    path_value = checkpoint.get('provenance_manifest')
    if not path_value:
        return {}
    path = Path(path_value)
    stat = path.stat()
    source_hash, index = _manifest_index(str(path), stat.st_size, stat.st_mtime_ns)
    if source_hash != checkpoint.get('source_sha256'):
        raise ValueError('PROVENANCE_SOURCE_MISMATCH')
    found = {}
    for row in rows:
        matches = index.get(chunk_key(row.get('content', ''), row.get('page_number'), row.get('content_type', 'text')), [])
        if not matches:
            continue
        provenance = {'source_sha256': source_hash,
                      'source_locators': [m['source_locator'] for m in matches if m.get('source_locator')]}
        origins = sorted({m['content_origin'] for m in matches if m.get('content_origin')})
        if origins:
            provenance['content_origins'] = origins
        descriptions = [m['image_description'] for m in matches if m.get('image_description')]
        if descriptions:
            provenance['image_descriptions'] = descriptions
        found[row['id']] = json.dumps(provenance, ensure_ascii=False)
    return found


def retire_file_builds(session, file_id):
    """Record exact live IDs for deferred cleanup; caller owns and commits transaction."""
    doc = session.get(FileDocument, file_id)
    if doc is None:
        return []
    ids = list(session.exec(select(DocumentChunk.id).where(DocumentChunk.file_id == file_id)).all())
    retired = []
    if doc.active_build_id:
        build = session.get(FileIndexBuild, doc.active_build_id)
        if build:
            build.state, build.retired_at = "RETIRED", datetime.utcnow()
            session.add(build)
            retired.append(build.build_id)
    elif ids:
        # Historical IDs are the actual SQL projection, never inferred from names.
        raw, digest = _manifest({"vector_ids": ids, "legacy": True})
        build = FileIndexBuild(build_id=str(uuid4()), file_id=file_id, kb_id=doc.kb_id,
            generation=doc.desired_generation or 0, task_id="legacy", attempt_no=0,
            state="RETIRED", manifest=raw, manifest_hash=digest, chunk_count=len(ids),
            retired_at=datetime.utcnow())
        session.add(build)
        retired.append(build.build_id)
    doc.active_build_id = None
    session.add(doc)
    return retired


def cleanup_retired_builds(build_ids, *, grace_seconds=None):
    """Remove recorded retired IDs only; failed cleanup remains durable for retry."""
    service = VectorStoreService()
    grace = float(os.getenv('FILE_INDEX_RETIRE_GRACE_SECONDS', '300')) if grace_seconds is None else grace_seconds
    for build_id in build_ids:
        with Session(engine) as session:
            build = session.get(FileIndexBuild, build_id)
            if not build or build.state != "RETIRED":
                continue
            if build.retired_at is None or (datetime.utcnow() - build.retired_at).total_seconds() < grace:
                continue
            if session.exec(select(FileDocument.id).where(FileDocument.active_build_id == build_id)).first():
                continue
            ids = json.loads(build.manifest).get("vector_ids", [])
            try:
                service.delete_ids(build.kb_id, ids)
            except Exception:
                continue
            build.state = "CLEANED"
            session.add(build)
            session.commit()


def sweep_builds():
    """Reconcile exact abandoned IDs repeatedly; a delayed writer may re-add them."""
    from app.tasks.dispatch import FileTaskReceipt, now_ms
    with Session(engine) as session:
        retired = [b.build_id for b in session.exec(select(FileIndexBuild).where(
            FileIndexBuild.state == 'RETIRED')).all()]
        abandoned = []
        for build in session.exec(select(FileIndexBuild).where(
                FileIndexBuild.state.in_(['STAGING', 'ABANDONED']))).all():
            receipt = session.get(FileTaskReceipt, build.task_id)
            running = (receipt is not None and receipt.status == 'RUNNING' and
                       receipt.attempt_no == build.attempt_no and receipt.lease_until and
                       receipt.lease_until > now_ms())
            if running:
                continue
            build.state = 'ABANDONED'
            session.add(build)
            abandoned.append((build.kb_id, json.loads(build.manifest).get('vector_ids', [])))
        session.commit()
    cleanup_retired_builds(retired)
    service = VectorStoreService()
    for kb_id, ids in abandoned:
        try:
            service.delete_ids(kb_id, ids)
        except Exception:
            pass  # Keep ABANDONED forever for the next reconciliation pass.


def publish_index_build(claim, chunks_data, embeddings, vector_model=None, manifest=None):
    """Write isolated vectors then atomically fence and publish live SQL projection."""
    from app.tasks.dispatch import guard_claim_in_session, complete_in_session

    rows = remap_and_validate_graph(chunks_data, claim.file_id)
    searchable = [row for row in rows if row.get("chunk_type") != "parent"]
    if len(searchable) != len(embeddings) or any(not vector for vector in embeddings):
        raise ValueError("EMBEDDING_COUNT_MISMATCH")
    if embeddings and len({len(v) for v in embeddings}) != 1:
        raise ValueError("EMBEDDING_DIMENSION_MISMATCH")
    if any(not math.isfinite(float(value)) for vector in embeddings for value in vector):
        raise ValueError("EMBEDDING_NONFINITE")
    build_id = str(uuid4())
    with Session(engine, expire_on_commit=False) as session:
        doc = session.get(FileDocument, claim.file_id)
        if not doc:
            raise ValueError("FILE_NOT_FOUND")
        kb_id, file_name, source_path = doc.kb_id, doc.name, doc.local_path
        prior_provenance = {}
        for original, remapped in zip(chunks_data, rows):
            prior = session.get(DocumentChunk, original.get('id')) if original.get('id') else None
            if (prior and prior.file_id == claim.file_id and prior.source_metadata_json
                    and prior.content == original.get('content', '') and prior.page_number == original.get('page_number')
                    and prior.content_type.value == original.get('content_type', 'text')):
                prior_provenance[remapped['id']] = prior.source_metadata_json
        checkpoint = manifest or {}
        from app.services.image_caption_derivatives import (
            caption_dependencies, guard_caption_dependencies, guard_caption_artifact_identities)
        caption_sources = caption_dependencies(doc, prior_provenance, checkpoint.get('source_sha256'), rows)
        caption_artifacts = guard_caption_dependencies(session, doc, caption_sources)
        payload = {"vector_ids": [r["id"] for r in searchable], "chunk_ids": [r["id"] for r in rows],
                   "embedding_model": vector_model, "dimension": len(embeddings[0]) if embeddings else 0,
                   "checkpoint_ref": claim.checkpoint_ref if hasattr(claim, 'checkpoint_ref') else None,
                   "source_sha256": checkpoint.get('source_sha256'),
                   "provenance_manifest": checkpoint.get('provenance_manifest'),
                   "embedding_sha256": hashlib.sha256(json.dumps(embeddings, separators=(',', ':'),
                       allow_nan=False).encode('utf-8')).hexdigest(),
                   "content_sha256": [hashlib.sha256(r.get('content', '').encode('utf-8')).hexdigest() for r in searchable]}
        if caption_sources:
            payload['image_caption_dependencies'] = caption_sources
            payload['image_caption_source_artifacts'] = caption_artifacts
        raw, digest = _manifest(payload)
        build = FileIndexBuild(build_id=build_id, file_id=claim.file_id, kb_id=kb_id,
            generation=claim.target_generation, task_id=claim.task_id, attempt_no=claim.attempt_no,
            state="STAGING", manifest=raw, manifest_hash=digest, chunk_count=len(rows))
        session.add(build)
        session.commit()  # ownership record precedes any external write

    from app.services.pdf_provenance import index_provenance
    provenances = {**prior_provenance, **verified_provenance(checkpoint, rows)}
    documents = []
    for index, row in enumerate(rows):
        if row.get('chunk_type') == 'parent':
            continue
        try:
            content_type = ContentType(row.get('content_type', 'text'))
        except ValueError:
            content_type = ContentType.TEXT
        chunk = DocumentChunk(id=row['id'], file_id=claim.file_id, content=row.get('content', ''),
            page_number=row.get('page_number'), content_type=content_type)
        metadata = {'file_id': claim.file_id, 'file_name': file_name, 'build_id': build_id,
            'chunk_index': index, 'page_number': row.get('page_number') or 0,
            'content_type': content_type.value, 'image_path': row.get('image_path') or '',
            'location_info': f"Page {row.get('page_number') or 1} | Chunk #{index + 1}",
            'parent_id': row.get('parent_id') or '', 'heading_level': row.get('heading_level') or 0,
            'heading_text': row.get('heading_text') or '', 'chunk_type': row.get('chunk_type') or 'content'}
        if row['id'] in provenances:
            metadata['source_locator_json'] = provenances[row['id']]
        else:
            metadata.update(index_provenance(source_path, chunk))
            provenances[row['id']] = metadata.get('source_locator_json')
        documents.append({'id': row['id'], 'content': chunk.content, 'metadata': metadata})
    service = VectorStoreService()
    if embeddings:
        service.validate_dimension(kb_id, embeddings[0])
        service.add_documents(kb_id, documents, embeddings)
        actual = service.get_documents(kb_id, payload["vector_ids"], include=["documents", "metadatas", "embeddings"])
        observed = {key: (content, metadata, vector) for key, content, metadata, vector in zip(
            actual["ids"], actual["documents"], actual["metadatas"], actual["embeddings"])}
        if len(observed) != len(documents) or any(
            d["id"] not in observed or observed[d["id"]][0] != d["content"] or
            observed[d["id"]][1] != d['metadata'] or
            len(observed[d['id']][2]) != len(vector) or
            any(not math.isclose(float(a), float(b), rel_tol=1e-6, abs_tol=1e-7)
                for a, b in zip(observed[d['id']][2], vector))
            for d, vector in zip(documents, embeddings)):
            raise RuntimeError("BUILD_VECTOR_VERIFICATION_FAILED")

    bm25 = get_bm25_index()
    prepared_bm25 = bm25.prepare_file_index(rows)
    retired = []
    with Session(engine, expire_on_commit=False) as session:
        if not guard_claim_in_session(session, claim):
            session.rollback()
            raise RuntimeError("BUILD_CLAIM_LOST")
        doc = session.get(FileDocument, claim.file_id, populate_existing=True)
        if not doc or doc.deleted_at is not None or doc.desired_generation != claim.target_generation:
            session.rollback()
            raise RuntimeError("BUILD_GENERATION_CHANGED")
        guard_caption_dependencies(session, doc, caption_sources, verify_files=False)
        guard_caption_artifact_identities(caption_artifacts)
        kb = session.get(KnowledgeBase, kb_id)
        if vector_model and kb and kb.embedding_model != vector_model:
            other_indexed = session.exec(select(DocumentChunk.id).join(FileDocument).where(
                FileDocument.kb_id == kb_id, FileDocument.id != claim.file_id,
                FileDocument.deleted_at.is_(None),
                FileDocument.status.in_([FileStatus.PARSED, FileStatus.READY])).limit(1)).first()
            own_active = doc.active_build_id or doc.status in (FileStatus.PARSED, FileStatus.READY)
            if other_indexed or own_active:
                raise ValueError('EMBEDDING_MODEL_MISMATCH')
            kb.embedding_model = vector_model
        retired = retire_file_builds(session, claim.file_id)
        removed_bm25 = bm25.begin_file_replacement(session, claim.file_id, kb_id)
        old_chunks = session.exec(select(DocumentChunk).where(DocumentChunk.file_id == claim.file_id)).all()
        for chunk in old_chunks:
            session.delete(chunk)
        session.flush()
        new_chunks = []
        for index, row in enumerate(rows):
            try:
                content_type = ContentType(row.get("content_type", "text"))
            except ValueError:
                content_type = ContentType.TEXT
            chunk = DocumentChunk(id=row["id"], file_id=claim.file_id, content=row.get("content", ""),
                page_number=row.get("page_number"), content_type=content_type,
                image_path=row.get("image_path"), original_index=index, parent_id=row.get("parent_id"),
                heading_level=row.get("heading_level"), heading_text=row.get("heading_text"),
                chunk_type=row.get("chunk_type") or "content", token_count=row.get("token_count") or 0,
                start_offset=row.get("start_offset"), end_offset=row.get("end_offset"),
                source_metadata_json=provenances.get(row['id']))
            session.add(chunk)
            new_chunks.append(chunk)
        session.flush()
        bm25.finish_file_replacement(session, kb_id, removed_bm25, prepared_bm25)
        build = session.get(FileIndexBuild, build_id)
        build.state = "ACTIVE"
        doc.active_build_id = build_id
        doc.status, doc.progress, doc.error_message = FileStatus.PARSED, 100, None
        doc.updated_at = datetime.utcnow()
        kb = session.get(KnowledgeBase, kb_id)
        if kb:
            session.flush()
            kb.chunk_count = session.exec(select(func.count(DocumentChunk.id)).join(FileDocument).where(
                FileDocument.kb_id == kb_id, FileDocument.deleted_at.is_(None))).one()
            kb.updated_at = datetime.utcnow()
            session.add(kb)
        session.add(build)
        session.add(doc)
        result = {"file_id": claim.file_id, "build_id": build_id, "chunk_count": len(rows), "status": "parsed"}
        if not complete_in_session(session, claim, result):
            session.rollback()
            raise RuntimeError("BUILD_CLAIM_LOST")
        session.commit()
    # Retired vectors stay available for in-flight readers until the sweeper grace elapses.
    return result

"""On-demand source context; scope and active publication are SQL-authoritative."""
import json
import uuid
from typing import List, Optional

from langchain_core.tools import tool
from sqlmodel import Session

from app.core.database import engine
from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase
from app.services.file_versions import inactive_file_ids
from app.services.retrieval import _verified_build_manifest
from app.services.rerank import expand_context
from app.tools.retrieval_tool import _serialize_evidence_results


def _unavailable(chunk_id):
    return json.dumps({'context': 'Requested source is unavailable in the current selected scope.',
        'citations': [], 'retrieval_status': 'NO_RESULTS', 'requested_chunk_id': chunk_id,
        'source_mapping_status': 'VERIFIED', 'expansion_delivery_status': 'NOT_REQUESTED'},
        ensure_ascii=False)


def _active_source(session, chunk_id, kb_ids):
    chunk = session.get(DocumentChunk, chunk_id)
    doc = session.get(FileDocument, chunk.file_id) if chunk else None
    kb = session.get(KnowledgeBase, doc.kb_id) if doc else None
    if (not doc or doc.kb_id not in kb_ids or doc.deleted_at is not None
            or not kb or kb.is_deleted or doc.id in inactive_file_ids(session, kb_ids)):
        return None
    if doc.active_build_id:
        build = session.get(FileIndexBuild, doc.active_build_id)
        if (not build or build.file_id != doc.id or build.kb_id != doc.kb_id
                or build.state != 'ACTIVE'):
            return None
        manifest = _verified_build_manifest(build.build_id, build.manifest, build.manifest_hash, {})
        if manifest is None or chunk.id not in manifest['chunk_ids']:
            return None
    return chunk, doc, kb


@tool
async def read_knowledge_context(source_id: int | str, window: int = 1) -> str:
    """Read surroundings of a source returned by knowledge search.

    Args:
        source_id: Exact short source alias from the current request, such as s1. Legacy source numbers are also accepted. Never guess an alias.
        window: Adjacent chunk window, from 0 to 2. A verified parent may supply context.
    """
    raise ValueError('read_knowledge_context requires server-side source registry dispatch')


async def read_registered_context(source: dict, kb_ids: Optional[List[str]], window: int = 1) -> str:
    """Read only a server-resolved registry source; UUID and scope are never model arguments."""
    registry_source = source
    chunk_id = source.get('chunk_id')
    # Previously expanded citations may refer to a parent. Resolve only their
    # recorded seed IDs, never infer a seed from file names or surrounding text.
    seed_ids = source.get('seed_child_ids') or []
    if seed_ids:
        with Session(engine) as session:
            current = session.get(DocumentChunk, chunk_id) if chunk_id else None
            if current and current.chunk_type == 'parent':
                chunk_id = seed_ids[0]
    # The workflow additionally requires the ID to exist in its evidence registry.
    # An absent scope must never broaden into all knowledge bases.
    if not kb_ids or type(window) is not int or not 0 <= window <= 2:
        return _unavailable(chunk_id)
    with Session(engine) as session:
        source = _active_source(session, chunk_id, kb_ids)
        if source is None or source[0].chunk_type == 'parent':
            return _unavailable(chunk_id)
        chunk, doc, kb = source
        if ((registry_source.get('fileId') and registry_source['fileId'] != doc.id)
                or (registry_source.get('kb_id') and registry_source['kb_id'] != kb.id)):
            return _unavailable(chunk_id)
        from app.services.pdf_provenance import source_metadata
        metadata = (json.loads(chunk.source_metadata_json) if chunk.source_metadata_json else
                    source_metadata(doc.local_path, chunk.content, chunk.page_number, chunk.content_type))
        row = {'id': chunk.id, 'file_id': doc.id, 'file_name': doc.display_name or doc.name,
            'kb_id': kb.id, 'kb_name': kb.name, 'content': chunk.content,
            'chunk_type': chunk.chunk_type, 'parent_id': chunk.parent_id,
            'original_index': chunk.original_index, 'page_number': chunk.page_number,
            'content_type': chunk.content_type, 'heading_text': chunk.heading_text,
            'source_metadata': metadata}
        rows = expand_context([row], session, window_size=window,
            allowed_kb_ids=kb_ids, include_source_members=True, max_parent_contexts=1)
        # Expansion is shared with Normal; re-authorize every delivered source here,
        # including parents, against the current publication manifest before delivery.
        for expanded in rows:
            members = (expanded.get('context_group') or {}).get('source_members') or []
            for member in members:
                actual = _active_source(session, member.get('id'), kb_ids)
                if (actual is None or actual[1].id != doc.id
                        or actual[0].content != member.get('content')):
                    return _unavailable(chunk_id)
        result = json.loads(_serialize_evidence_results(rows, 'SUCCESS' if rows else 'NO_RESULTS',
            {}, uuid.uuid4().hex))
        result['requested_chunk_id'] = chunk_id
        return json.dumps(result, ensure_ascii=False)

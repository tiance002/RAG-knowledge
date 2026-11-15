"""Server-owned conversational history and source reuse, shared by chat and Agent.

Stored answers are dialogue, not original evidence. References must be checked
against the current file/scope before any historical body can enter a new call.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import re

from sqlmodel import Session

_capture = ContextVar('conversation_delivery', default=None)


def _hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@contextmanager
def delivery_capture_scope(state):
    token = _capture.set(state)
    try:
        yield state
    finally:
        _capture.reset(token)


def record_understanding(value):
    state = _capture.get()
    if state is not None:
        state['understanding'] = dict(value)


def register_request_sources(citations):
    state = _capture.get()
    if state is not None:
        source_engine = state.get('source_engine')
        if source_engine is not None:
            # Freeze while constructing this request, not after a potentially
            # long model call during which the document may be re-indexed.
            with Session(source_engine) as db:
                allowed = set(state.get('kb_ids', []))
                for c in citations:
                    valid = _validate_reference(db, c, allowed) if c.get('source_type') != 'web' else None
                    if valid:
                        for key in ('source_sha256', 'index_build_id', 'verification_status', 'content_hash'):
                            c[key] = valid[key]
        current = state.setdefault('citations', [])
        seen = {(c.get('chunk_id'), c.get('fileId') or c.get('file_id'), c.get('url'), c.get('content')) for c in current}
        for c in citations:
            key = (c.get('chunk_id'), c.get('fileId') or c.get('file_id'), c.get('url'), c.get('content'))
            if key not in seen:
                current.append(c)
                seen.add(key)


def record_provider_request(messages, stage, citations=None):
    """Capture identities of the actual application request, never model receipt.

    LangChain rows are explicitly marked as a content projection. Do not persist
    complete prompts in the chat database or claim an SDK call has returned.
    """
    state = _capture.get()
    if state is None:
        return
    rows, texts = [], []
    for item in messages:
        role = item.get('role') if isinstance(item, dict) else getattr(item, 'type', None)
        content = item.get('content') if isinstance(item, dict) else getattr(item, 'content', '')
        rows.append({'role': role, 'content': content})
        if isinstance(content, str):
            texts.append(content)
            try:
                data = json.loads(content)
                texts.extend(c.get('content', '') for c in data.get('citations', []) if isinstance(c, dict))
            except (ValueError, AttributeError, TypeError):
                pass
    # Tool JSON may carry source bodies with escaped newlines; decoded bodies
    # above are inspected as well as ordinary system/user content.
    source_rows = list(citations if citations is not None else state.get('citations', []))
    present = []
    for source in source_rows:
        body = source.get('content')
        if isinstance(body, str) and body and any(body in text for text in texts):
            present.append({'chunk_id': source.get('chunk_id'),
                            'file_id': source.get('fileId') or source.get('file_id'),
                            'content_hash': _hash(body), 'url': source.get('url')})
    serialized = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str)
    state.setdefault('provider_requests', []).append({
        'stage': stage, 'messages_hash': _hash(serialized),
        'history_message_count': len(rows), 'evidence': present,
        'delivery_status': 'PROVIDER_REQUEST_PREPARED',
        'is_exact_provider_payload': False, 'capture_kind': 'application_message_projection',
    })


def _validate_reference(db, reference, allowed):
    from app.models import DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase
    cid = reference.get('chunk_id') or reference.get('chunkId')
    if not isinstance(cid, str) or not cid:
        return None
    chunk = db.get(DocumentChunk, cid) if cid else None
    if chunk is None:
        return None
    file = db.get(FileDocument, chunk.file_id)
    if file is None or file.deleted_at or file.kb_id not in allowed:
        return None
    kb = db.get(KnowledgeBase, file.kb_id)
    if kb is None or kb.is_deleted:
        return None
    build_row = db.get(FileIndexBuild, file.active_build_id) if file.active_build_id else None
    if (build_row is None or build_row.state != 'ACTIVE' or build_row.file_id != file.id
            or build_row.kb_id != file.kb_id):
        return None
    try:
        active_manifest = json.loads(build_row.manifest)
    except (ValueError, TypeError):
        return None
    if not isinstance(active_manifest, dict) or not isinstance(active_manifest.get('chunk_ids'), list):
        return None
    if (_hash(build_row.manifest) != build_row.manifest_hash
            or chunk.id not in active_manifest.get('chunk_ids', [])):
        return None
    # Legacy documents may predate FileDocument.sha256. Only the verified,
    # server-owned ACTIVE manifest may supply the missing source identity.
    manifest_hash = active_manifest.get('source_sha256')
    if manifest_hash is not None and (not isinstance(manifest_hash, str) or not manifest_hash):
        return None
    if file.sha256 and manifest_hash and file.sha256 != manifest_hash:
        return None
    current_source_hash = file.sha256 or manifest_hash
    claimed_file = reference.get('file_id') or reference.get('fileId')
    claimed_kb = reference.get('kb_id')
    if (claimed_file and claimed_file != file.id) or (claimed_kb and claimed_kb != file.kb_id):
        return None
    metadata = reference.get('source_metadata') or {}
    if not isinstance(metadata, dict):
        return None
    source_hash = reference.get('source_sha256') or metadata.get('source_sha256')
    build = reference.get('index_build_id')
    if source_hash and source_hash != current_source_hash:
        return None
    if build and build != file.active_build_id:
        return None
    body = reference.get('content')
    if not isinstance(body, str) or not body:
        return None
    # A reference's VERIFIED label and self-hash do not prove an expanded
    # snapshot still matches the current source. Reuse exact current bodies.
    if _hash(body) != _hash(chunk.content):
        return None
    try:
        current_metadata = json.loads(chunk.source_metadata_json or '{}')
        if not isinstance(current_metadata, dict):
            return None
    except (ValueError, TypeError):
        return None
    # Model-generated caption provenance must survive historical reuse.
    metadata = {**metadata, **current_metadata}
    if current_source_hash:
        metadata['source_sha256'] = current_source_hash
    from pathlib import PurePath
    page = chunk.page_number if PurePath(file.name).suffix.lower() == '.pdf' else None
    sheet = current_metadata.get('sheet') or current_metadata.get('sheet_name')
    location = (f'第 {page} 页' if page is not None else f'工作表: {sheet}' if sheet
                else chunk.heading_text or '未知位置')
    return {**reference, 'chunk_id': chunk.id, 'fileId': file.id, 'file_id': file.id,
            'kb_id': file.kb_id, 'kb_name': kb.name, 'fileName': file.name,
            'page_number': page, 'location': location,
            'content': body, 'content_hash': _hash(body), 'source_metadata': metadata,
            'source_sha256': current_source_hash, 'index_build_id': file.active_build_id,
            'origin': 'HISTORY_REUSE', 'origin_reference_id': reference.get('id'),
            'verification_status': 'VERIFIED', 'source_mapping_status': 'VERIFIED',
            'delivery_status': 'CANDIDATE'}


def prepare_history(store, session_id, before_sequence, previous_rounds, kb_ids, *, allow_web=False):
    from app.schemas import Message, MessageRole
    allowed = set(kb_ids)
    turns = store.history_turns(session_id, before_sequence, previous_rounds)
    messages, candidates, selected, excluded = [], [], [], []
    with Session(getattr(store, 'source_engine', store.engine)) as db:
        for turn in turns:
            refs = turn.get('citations') or []
            old_scope = (turn.get('effective_config') or {}).get('kb_ids')
            if old_scope is not None and not set(old_scope).issubset(allowed):
                excluded.append({'turn_id': turn['id'], 'reason': 'SCOPE_CHANGED'})
                continue
            if any(isinstance(r.get('kb_id'), str) and r['kb_id'] not in allowed for r in refs):
                excluded.append({'turn_id': turn['id'], 'reason': 'REFERENCE_OUT_OF_SCOPE'})
                continue
            question = turn.get('user_content', '')
            answer = turn.get('assistant_content') or ''
            complete = (turn.get('status') == 'COMPLETED'
                        and turn.get('generation_status') == 'COMPLETED')
            legacy = turn.get('status') == 'LEGACY_IMPORTED'
            legacy_assistant = (turn.get('legacy_metadata') or {}).get('assistant', {})
            if legacy and (legacy_assistant.get('streaming') or legacy_assistant.get('status')):
                legacy = False
            if not complete and not legacy:
                answer = ''
            if question:
                messages.append(Message(role=MessageRole.USER, content=question))
            if answer:
                # Historical [1] never denotes the new request's source [1].
                projected = re.sub(r'\[s?(\d+)\]', lambda m: f'(historical source {turn["sequence_no"]}:{m[1]})', answer)
                messages.append(Message(role=MessageRole.ASSISTANT, content=projected))
            selected.append(turn['id'])
            for ref in refs if complete or legacy else []:
                if ref.get('source_type') == 'web':
                    # Only server-captured completed snapshots; browser imports and
                    # search snippets are never asserted to be live page verification.
                    from app.services.web_source_registry import validate_web_snapshot
                    validated = (validate_web_snapshot(ref) if allow_web and complete
                                 and ref.get('trust_status') == 'CAPTURED_SEARCH_RESULT' else None)
                    if validated:
                        validated = {**validated, 'origin': 'HISTORY_WEB_SNAPSHOT',
                                     'history_citation_index': ref.get('citation_index')}
                else:
                    validated = _validate_reference(db, ref, allowed)
                if validated:
                    validated['history_turn_id'] = turn['id']
                    candidates.append(validated)
    return messages, candidates, {'history_turn_ids': selected, 'excluded_turns': excluded,
                                  'history_reference_candidates': len(candidates),
                                  'history_round_limit': previous_rounds}


def merge_history_citations(current, candidates, query):
    """Supplement fresh results from the newest validated reference-bearing turn.

    WeKnora-style token overlap, discounted history scores and max three sources.
    Current hit order is authoritative here: no raw BM25/vector/old-rerank score
    is mixed with the current ranking. Discount metadata is diagnostic, not a
    claim that Rerank executed again. Original and discounted scores stay distinct.
    """
    if not candidates:
        return list(current), []
    from app.core.config import get_settings
    from app.services.bm25 import tokenize
    import math
    tokens = set(tokenize(query))
    if not tokens:
        return list(current), []
    # Candidates arrive in server-owned chronological order. Retain all only
    # for older call sites without turn identity; never infer it from a filename.
    latest_turn = next((c.get('history_turn_id') for c in reversed(candidates)
                        if c.get('history_turn_id') is not None), None)
    if latest_turn is not None:
        candidates = [c for c in candidates if c.get('history_turn_id') == latest_turn]
    existing = {(c.get('fileId') or c.get('file_id'), c.get('chunk_id'), _hash(c.get('content') or '')) for c in current}
    output, added = list(current), []
    used = sum(len(c.get('content') or '') for c in current)
    for candidate in candidates:
        if len(added) >= 3:
            break
        body = candidate.get('content') or ''
        identity = (candidate.get('fileId') or candidate.get('file_id'), candidate.get('chunk_id'), _hash(body))
        if identity in existing or not body:
            continue
        body_tokens = set(tokenize(body))
        if not body_tokens:
            continue
        overlap = len(tokens & body_tokens) / min(len(tokens), len(body_tokens))
        if overlap < 0.3 or used + len(body) > get_settings().CONTEXT_MAX_CHARS:
            continue
        selected = {**candidate, 'match_type': 'history', 'history_similarity': overlap,
                    'history_score_discount': 0.6,
                    'history_selection_rule': 'latest-reference-turn-overlap-v2'}
        score = candidate.get('score')
        if type(score) in (int, float) and math.isfinite(score):
            selected['history_original_score'] = score
            selected['history_discounted_score'] = score * 0.6
        else:
            selected['history_original_score'] = None
            selected['history_discounted_score'] = None
        # Keep original retrieval/rerank scores untouched and truthfully named.
        output.append(selected)
        added.append(selected)
        existing.add(identity)
        used += len(body)
    return output, added

"""Source-linked ordinary-chat context selection; never a retriever or judge."""
import hashlib
from pathlib import PurePath


def content_hash(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def source_member(chunk, seed, relation):
    """Describe an actually selected DB chunk; unknown locators stay unknown."""
    is_seed = str(chunk.id) == str(seed.get('id'))
    metadata = dict(seed.get('source_metadata') or {}) if is_seed else {}
    # Office/TXT index page placeholders are not verified source pagination.
    page = chunk.page_number if PurePath(seed.get('file_name', '')).suffix.lower() == '.pdf' else None
    if page is not None:
        metadata['page'] = page
    elif 'page' in metadata:
        metadata['indexed_page_number'] = metadata.pop('page')
    return {
        'id': str(chunk.id), 'content': chunk.content, 'content_hash': content_hash(chunk.content),
        'file_id': chunk.file_id, 'file_name': seed.get('file_name', ''),
        'kb_id': seed.get('kb_id'), 'kb_name': seed.get('kb_name', ''),
        'page_number': page, 'indexed_page_number': chunk.page_number, 'source_metadata': metadata,
        'content_type': chunk.content_type, 'image_path': chunk.image_path,
        'heading_text': getattr(chunk, 'heading_text', None),
        'heading_level': getattr(chunk, 'heading_level', None),
        'parent_id': getattr(chunk, 'parent_id', None), 'relation': 'seed' if is_seed else relation,
        'original_index': getattr(chunk, 'original_index', None),
    }


def _verified_members(row):
    group = row.get('context_group') or {}
    members = group.get('source_members') or []
    if not members or group.get('context_group_id') != 'group:' + str(row.get('id')):
        return None
    if group.get('original_expanded_hash') != content_hash(row.get('content', '')):
        return None
    for member in members:
        if (not isinstance(member, dict) or not member.get('id')
                or member.get('file_id') != row.get('file_id')
                or not isinstance(member.get('content'), str)
                or member.get('content_hash') != content_hash(member['content'])):
            return None
    if '\n'.join(m['content'] for m in members if m['content']) != row.get('content', ''):
        return None
    # A single original ID must not stand for conflicting source bytes/locations.
    identities = {}
    for m in members:
        identity = (m['content_hash'], m.get('file_id'), m.get('page_number'))
        if m['id'] in identities and identities[m['id']] != identity:
            return None
        identities[m['id']] = identity
    return members


def build_chat_context(results, format_citation, strategy='EXPANDED_CONTENT'):
    """Return real member citations and a body-free context diagnostic summary.

    ORIGINAL_CONTENT preserves the historical A arm. EXPANDED_CONTENT consumes
    only members annotated by the real expander, never inferred adjacent chunks.
    Missing provenance falls back to the known seed and reports UNKNOWN.
    No input clipping rule or token budget is added here.
    """
    if strategy not in ('ORIGINAL_CONTENT', 'EXPANDED_CONTENT'):
        raise ValueError('unsupported context strategy')
    from app.services.agent_workflow import evidence_text
    citations, groups, changes = [], [], []
    delivered_by_id = {}
    omitted_images = omitted_expanded = mapping_unknown = False
    seed_ids, expanded_ids = [], []
    for row in results:
        seed = row.get('id')
        if seed and seed not in seed_ids:
            seed_ids.append(seed)
        expanded = bool(row.get('is_expanded') or (row.get('metadata') or {}).get('is_expanded'))
        group = row.get('context_group') or {}
        members = _verified_members(row) if expanded else None
        group_id = 'group:' + str(seed) if expanded else None
        if expanded:
            for m in members or []:
                if m['id'] != seed and m['id'] not in expanded_ids:
                    expanded_ids.append(m['id'])
        mapping_status = ('UNKNOWN' if row.get('source_mapping_status') == 'UNKNOWN'
                          else 'VERIFIED' if not expanded or members is not None else 'UNKNOWN')
        mapping_unknown |= mapping_status == 'UNKNOWN'
        if expanded and strategy == 'EXPANDED_CONTENT' and members is not None:
            selected = members
        elif expanded:
            original = row.get('original_content')
            selected = [{**row, 'content': original}] if isinstance(original, str) and original.strip() else []
            omitted_expanded |= not bool(selected)
        else:
            selected = [row]
        member_ids = []
        for member in selected:
            text, has_image = evidence_text(member)
            has_image |= member.get('content_type') == 'image'
            omitted_images |= has_image
            mid = member.get('id')
            if not mid or not text or has_image:
                continue
            if mid in delivered_by_id:
                existing = delivered_by_id[mid]
                if (existing['content'] != text or existing.get('fileId') != member.get('file_id')
                        or existing.get('page_number') != member.get('page_number')):
                    mapping_unknown = True
                    continue
                if group_id and group_id not in existing.get('context_group_ids', []):
                    existing.setdefault('context_group_ids', []).append(group_id)
                member_ids.append(mid)
                continue
            mapped = {**row, **member}
            if expanded and mid != seed:
                # Group membership is not an independent retrieval/rerank score.
                mapped.update(score=None, rerank_score=None, vector_score=None, bm25_score=None)
            citation = {**format_citation(mapped, text), 'content': text,
                        'page_number': member.get('page_number')}
            if group_id and strategy == 'EXPANDED_CONTENT' and members is not None:
                citation.update(context_group_id=group_id, context_group_ids=[group_id],
                                seed_evidence_ids=[seed], parent_chunk_id=member.get('parent_id'),
                                evidence_relation=member.get('relation', 'seed'),
                                content_hash=content_hash(text), source_mapping_status=mapping_status)
            citations.append(citation)
            delivered_by_id[mid] = citation
            member_ids.append(mid)
        if expanded:
            groups.append({
                'context_group_id': group_id, 'seed_evidence_ids': [seed],
                'expanded_evidence_ids': list(dict.fromkeys(m['id'] for m in members or [] if m['id'] != seed)),
                'delivered_evidence_ids': list(dict.fromkeys(member_ids)),
                'source_members': [{k: m.get(k) for k in ('id', 'content_hash', 'file_id', 'page_number',
                    'parent_id', 'relation', 'original_index')} for m in members or []],
                'source_locations': [{'evidence_id': m['id'], 'page': m.get('page_number'),
                    'position': m.get('source_metadata') or {}} for m in members or []],
                'content_hash': content_hash(row.get('content', '')),
                'source_mapping_status': mapping_status,
                'truncation_status': group.get('truncation_status', 'UNKNOWN'),
            })
        changes.append({'candidate_id': seed,
            'reason_code': ('missing_source' if not selected else 'replaced_by_original_content' if expanded and strategy == 'ORIGINAL_CONTENT'
                            else 'source_mapping_unknown' if mapping_status == 'UNKNOWN' else None),
            'selected_content_sha256': content_hash(row.get('content', '')),
            'delivered_evidence_ids': list(dict.fromkeys(member_ids)),
            'delivered_content_sha256': (content_hash(delivered_by_id[member_ids[0]]['content']) if len(member_ids) == 1 else None),
            'used_original_content': bool(expanded and (strategy == 'ORIGINAL_CONTENT' or members is None)),
            'omitted_from_llm': not bool(member_ids)})
    context = '\n\n'.join(c['content'] for c in citations)
    summary = {
        'context_strategy': strategy, 'seed_evidence_ids': seed_ids,
        'expanded_evidence_ids': expanded_ids, 'delivered_evidence_ids': list(delivered_by_id),
        'context_groups': groups, 'context_group_count': len(groups),
        'context_char_count': len(context), 'content_hash': content_hash(context),
        'context_token_count': None, 'token_count_status': 'UNKNOWN',
        'token_count_method': 'model_tokenizer_not_configured',
        'truncation_status': ('CHAR_LIMIT_APPLIED' if any(g['truncation_status'] == 'CHAR_LIMIT_APPLIED' for g in groups)
                              else 'UNKNOWN' if any(g['truncation_status'] == 'UNKNOWN' for g in groups) else 'NOT_APPLIED'),
        'delivery_status': 'SELECTED_NOT_YET_SENT',
        'source_mapping_status': 'UNKNOWN' if mapping_unknown else 'VERIFIED',
        'omitted_images': omitted_images, 'omitted_expanded': omitted_expanded, 'changes': changes,
    }
    return citations, summary

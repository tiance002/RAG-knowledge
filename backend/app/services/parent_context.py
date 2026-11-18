"""Request-local parent retrieval. No inferred historical links or persistent flags."""
from sqlmodel import select
from app.models import DocumentChunk, FileDocument
from app.core.config import get_settings
from app.services.context_builder import source_member, content_hash
from app.services.observability import event
from app.services.parent_child import supported_child


def expand_parent_context(top_chunks, session, *, window_size=None,
                          include_source_members=False, allowed_kb_ids=None, max_context_chars=None,
                          max_parent_contexts=None):
    settings = get_settings()
    window = settings.CONTEXT_WINDOW_SIZE if window_size is None else max(0, window_size)
    parent_limit = (getattr(settings, 'MAX_PARENT_CONTEXTS', 6)
                    if max_parent_contexts is None else max_parent_contexts)
    parent_limit = max(0, int(parent_limit))
    # Older deployed Settings omit this newer aggregate budget. Keep the same
    # bounded default as current Settings, while honoring explicit zero budgets.
    remaining = getattr(settings, 'CONTEXT_MAX_CHARS', 24000) if max_context_chars is None else max_context_chars
    seed_ids = list(dict.fromkeys(r.get('id') for r in top_chunks if r.get('id')))
    seeds = {c.id: c for c in session.exec(select(DocumentChunk).where(DocumentChunk.id.in_(seed_ids))).all()}
    file_ids = {c.file_id for c in seeds.values()}
    files = {f.id: f for f in session.exec(select(FileDocument).where(FileDocument.id.in_(file_ids))).all()}
    parent_ids = {c.parent_id for c in seeds.values() if c.parent_id}
    parents = {c.id: c for c in session.exec(select(DocumentChunk).where(DocumentChunk.id.in_(parent_ids))).all()} if parent_ids else {}
    from app.services.rerank import candidate_rank_key
    groups = {}
    for input_rank, row in enumerate(top_chunks):
        seed = seeds.get(row.get('id'))
        doc = files.get(seed.file_id) if seed else None
        if (not doc or (allowed_kb_ids is not None and doc.kb_id not in allowed_kb_ids)
                or (row.get('file_id') and row['file_id'] != seed.file_id)
                or (row.get('kb_id') and row['kb_id'] != doc.kb_id) or seed.chunk_type == 'parent'):
            event('retrieval.context_rejected', chunk_id=row.get('id'), reason='source_scope_mismatch')
            continue
        parent = parents.get(seed.parent_id)
        reason = None
        if seed.parent_id and (not parent or parent.file_id != seed.file_id or parent.chunk_type != 'parent'):
            reason = 'INVALID_PARENT_LINK'
            parent = None
        key = parent.id if parent else seed.id
        group = groups.setdefault(key, {'parent': parent, 'seeds': [], 'row': row, 'file': doc,
                                        'reason': reason, 'first_rank': input_rank})
        if seed.id not in {c.id for c in group['seeds']}:
            group['seeds'].append(seed)
        # Aggregate seed relevance only. Parent has never been independently scored.
        relevance = candidate_rank_key
        if relevance(row) > relevance(group['row']):
            group['row'] = row
    # Rank each parent by its strongest child hit. Upstream ordering is normally
    # already score based, but a parent can collect multiple hits over the pass.
    relevance = candidate_rank_key

    ranked_groups = sorted(
        groups.values(),
        key=lambda group: (relevance(group['row']), -group['first_rank']),
        reverse=True,
    )
    parent_rank = 0
    for group in ranked_groups:
        if group['parent']:
            parent_rank += 1
            group['parent_selection_rank'] = parent_rank
            group['parent_selected'] = parent_rank <= parent_limit
            group['parent_selection_reason'] = 'SELECTED_BY_CHILD_SCORE' if group['parent_selected'] else 'PARENT_LIMIT'
        else:
            group['parent_selection_rank'] = None
            group['parent_selected'] = False
            group['parent_selection_reason'] = 'NO_VERIFIED_PARENT'
        score_row = group['row']
        group['parent_selection_score_kind'] = 'composite_score' if score_row.get('composite_score') is not None else 'rerank_score' if score_row.get('rerank_score') is not None else 'score'
        group['parent_selection_score'] = score_row.get('composite_score') if score_row.get('composite_score') is not None else score_row.get('rerank_score') if score_row.get('rerank_score') is not None else score_row.get('score')

    # Batch neighborhood candidates, then select by order within the authorized file.
    neighbors = {}
    if window:
        from sqlalchemy import or_, and_
        ranges = [and_(DocumentChunk.file_id == c.file_id,
                       DocumentChunk.original_index >= c.original_index-window,
                       DocumentChunk.original_index <= c.original_index+window)
                  for g in groups.values() for c in g['seeds']]
        if ranges:
            for c in session.exec(select(DocumentChunk).where(or_(*ranges), DocumentChunk.chunk_type != 'parent')).all():
                neighbors.setdefault(c.file_id, []).append(c)
    output, delivered = [], set()
    all_seed_ids = {c.id for g in groups.values() for c in g['seeds']}
    future_seed_cost = sum(len(c.content)+2 for g in groups.values() for c in g['seeds'])
    configured_allowance = remaining
    mandatory_overrun = future_seed_cost > configured_allowance
    # The allowance constrains optional expansion, never independent seed bodies.
    remaining = max(remaining, future_seed_cost)
    for group in ranked_groups:
        row, doc, parent, hits = group['row'], group['file'], group['parent'], group['seeds']
        hit_ids = [c.id for c in hits]
        score_child_id = row['id']
        # As in WeKnora's document-order merge, identity is independent of score.
        primary = min(hits, key=lambda c: (c.original_index or 0, c.id))
        group_id = 'group:' + str(parent.id if parent else primary.id)
        hit_children = [source_member(c, {**row, 'kb_id': doc.kb_id}, 'seed', doc.local_path)
                        for c in sorted(hits, key=lambda c: (c.original_index or 0, c.id))]
        future_seed_cost -= sum(len(c.content)+2 for c in hits)
        reason = group['reason']
        # Whole parent or whole seeds: preserve exact citation bytes, never attribute a
        # clipped excerpt to an unmodified DB chunk.
        parent_selected = bool(parent and group['parent_selected'])
        selected = [(parent, 'parent')] if parent_selected else [(c, 'seed') for c in hits]
        original_length = len(parent.content) if parent else sum(len(c.content) for c in hits)
        if parent_selected and (len(parent.content)+2 > remaining-future_seed_cost
                       or any(not supported_child(c, parent) for c in hits)):
            selected = [(c, 'seed') for c in hits]
            reason = 'PARENT_OVER_BUDGET' if all(supported_child(c, parent) for c in hits) else 'SEED_NOT_IN_PARENT'
        members, omitted = [], []
        for c, relation in selected:
            if c.id in delivered:
                continue
            cost = len(c.content) + 2
            if cost > remaining:
                omitted.append(c.id)
                continue
            members.append(source_member(c, {**row, 'kb_id': doc.kb_id}, relation, doc.local_path))
            delivered.add(c.id)
            remaining -= cost
        # Neighbor expansion never crosses a known heading/parent boundary. Unknown
        # boundaries are explicitly reported; no semantic relevance claim is made.
        neighbor_ids = []
        parent_delivered = bool(parent_selected and any(m['id'] == parent.id for m in members))
        for c in sorted(neighbors.get(doc.id, []), key=lambda c: c.original_index):
            if c.id in delivered or c.id in all_seed_ids:
                continue
            if parent_delivered:
                continue  # Parent already supplies its structural neighborhood.
            seed = hits[0]
            if parent and (reason != 'PARENT_OVER_BUDGET' or c.parent_id != parent.id
                           or not supported_child(c, parent)):
                continue  # Budget fallback only; do not trust an invalid parent/body link.
            if not parent and (seed.parent_id or c.parent_id):
                continue
            if seed.heading_text and c.heading_text != seed.heading_text:
                continue
            if not any(abs(c.original_index-h.original_index) <= window for h in hits):
                continue
            if len(c.content)+2 > remaining-future_seed_cost:
                continue
            members.append(source_member(c, {**row, 'kb_id': doc.kb_id}, 'neighbor', doc.local_path))
            neighbor_ids.append(c.id)
            delivered.add(c.id)
            remaining -= len(c.content)+2
        members.sort(key=lambda m: (m.get('original_index') or 0, m['id']))
        content = '\n'.join(m['content'] for m in members)
        status = 'SEED_OMITTED_OVER_LIMIT' if omitted else 'CHAR_LIMIT_APPLIED' if reason == 'PARENT_OVER_BUDGET' else 'NOT_APPLIED'
        event('retrieval.context_expansion', seed_ids=hit_ids, parent_id=parent.id if parent else None,
              neighbor_ids=neighbor_ids, original_chars=original_length, delivered_chars=len(content),
              omitted_ids=omitted, truncation_status=status, degradation_reason=reason,
              parent_selection_rank=group['parent_selection_rank'],
              parent_selection_score_kind=group['parent_selection_score_kind'],
              parent_selection_score=group['parent_selection_score'], parent_limit=parent_limit,
              parent_selected=parent_selected, parent_selection_reason=group['parent_selection_reason'],
              boundary_status='KNOWN' if parent or hits[0].heading_text else 'UNKNOWN')
        selection_fallback = bool(parent and not parent_selected)
        parent_selection = {'rank': group['parent_selection_rank'],
            'score_kind': group['parent_selection_score_kind'],
            'score': group['parent_selection_score'], 'limit': parent_limit,
            'selected': parent_selected, 'reason': group['parent_selection_reason']}
        # Source identity follows the document-order representative, while the
        # ranking fields still explicitly refer to score_child_id.
        primary_source = next(m for m in hit_children if m['id'] == primary.id)
        representative_fields = {k: primary_source.get(k) for k in (
            'file_id', 'file_name', 'kb_id', 'kb_name', 'page_number',
            'indexed_page_number', 'source_metadata', 'content_type', 'image_path',
            'heading_text', 'heading_level', 'original_index', 'parent_id', 'chunk_type')}
        result = {**row, **representative_fields, 'id': primary.id,
                  'context_group_id': group_id, 'representative_child_id': primary.id,
                  'score_child_id': score_child_id, 'hit_children': hit_children,
                  'citation_sources': members,
                  'content': content, 'original_content': '' if primary.id in omitted else primary.content,
                  'delivery_budget': {'context_max_chars': configured_allowance,
                                      'mandatory_overrun': mandatory_overrun},
                  'seed_child_ids': hit_ids, 'matched_child_ids': hit_ids,
                  'parent_chunk_id': parent.id if parent else None,
                  'score_origin': 'best_seed_rerank' if row.get('rerank_score') is not None else 'best_seed_retrieval',
                  'source_mapping_status': 'UNKNOWN' if reason in ('INVALID_PARENT_LINK', 'SEED_NOT_IN_PARENT') else 'VERIFIED',
                  'expansion_delivery_status': 'OMITTED' if omitted else 'SEED_ONLY' if reason or selection_fallback else 'DELIVERED',
                  'parent_selection': parent_selection,
                  'metadata': {**row.get('metadata', {}), 'is_expanded': True},
                  'context_group': {'context_group_id': group_id,
                      'representative_child_id': primary.id, 'score_child_id': score_child_id,
                      'hit_children': hit_children,
                      'seed_evidence_ids': hit_ids, 'seed_child_ids': hit_ids,
                      'parent_chunk_id': parent.id if parent else None,
                      'selected_parent_id': parent.id if parent_delivered else None,
                      'parent_selection': parent_selection,
                      'source_members': members, 'expanded_evidence_ids': [m['id'] for m in members if m['id'] not in hit_ids],
                      'original_expanded_hash': content_hash(content), 'truncation_status': status,
                      'degradation_reason': reason, 'omitted_seed_ids': omitted}}
        if not include_source_members:
            result.pop('context_group')
        output.append(result)
    return output

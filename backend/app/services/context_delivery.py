"""Bound optional delivery expansion without clipping protected source bodies."""
from copy import deepcopy
from hashlib import sha256


def _identity(member):
    text = member.get('content')
    if not member.get('id') or not member.get('file_id') or not isinstance(text, str):
        raise ValueError('CONTEXT_MEMBER_SOURCE_INVALID')
    digest = sha256(text.encode('utf-8')).hexdigest()
    if member.get('content_hash') != digest:
        raise ValueError('CONTEXT_MEMBER_CONTENT_MISMATCH')
    return (member['file_id'], member.get('kb_id'), digest, member.get('page_number'),
            member.get('original_index'), member.get('parent_id'))


def _structural(member):
    metadata = member.get('source_metadata') or {}
    return (bool(member.get('heading_text')) or member.get('content_type') == 'table'
            or member.get('source_type') in ('native_source_expansion', 'source_native_table_expansion')
            or any(metadata.get(key) for key in
                   ('headers', 'footnotes', 'notes', 'unit', 'units', 'table_bbox', 'cell', 'sheet', 'row')))


def bound_context_delivery(rows, verified_seeds, max_chars):
    """Reserve all verified seeds/known structure, then whole optional members.

    The allowance counts unique source-member characters, not model tokens or
    provider prompt overhead. Unknown mappings stay whole and explicitly unknown.
    """
    allowance = max(0, int(max_chars))
    output = deepcopy(rows)
    identities, locators, required, known, unknown_cost = {}, {}, {}, [], 0

    def register(member):
        identity = _identity(member)
        mid = member['id']
        if mid in identities and identities[mid] != identity:
            raise ValueError('CONTEXT_MEMBER_IDENTITY_CONFLICT')
        identities[mid] = identity
        metadata = member.get('source_metadata') or {}
        previous = locators.setdefault(mid, {})
        for key in ('source_sha256', 'source_locators', 'page', 'table_bbox', 'sheet', 'row', 'cell'):
            if key in metadata:
                if key in previous and previous[key] != metadata[key]:
                    raise ValueError('CONTEXT_MEMBER_LOCATOR_CONFLICT')
                previous[key] = metadata[key]

    for mid, member in verified_seeds.items():
        if mid != member.get('id'):
            raise ValueError('CONTEXT_SEED_IDENTITY_MISMATCH')
        register(member)
        required[mid] = member
    for row in output:
        group = row.get('context_group') or {}
        members = group.get('source_members')
        if row.get('source_mapping_status') == 'UNKNOWN' or not members:
            # The selector cannot invent provenance or reinterpret unknown rows.
            unknown_cost += len(row.get('content') or '') + 2
            row['delivery_budget_source_status'] = 'UNKNOWN_RETAINED_WHOLE'
            if row.get('id') in verified_seeds:
                row['original_content'] = verified_seeds[row['id']]['content']
            continue
        for member in members:
            register(member)
            if member['file_id'] != row.get('file_id'):
                raise ValueError('CONTEXT_MEMBER_FILE_MISMATCH')
            if member.get('kb_id') != row.get('kb_id'):
                raise ValueError('CONTEXT_MEMBER_KB_MISMATCH')
            if _structural(member):
                required[member['id']] = member
        for sid in group.get('seed_evidence_ids') or [row.get('id')]:
            seed = verified_seeds.get(sid)
            if seed is None or seed['file_id'] != row.get('file_id'):
                raise ValueError('CONTEXT_VERIFIED_SEED_MISSING')
        known.append((row, group, members))

    mandatory_chars = sum(len(m['content']) + 2 for m in required.values()) + unknown_cost
    remaining = max(0, allowance - mandatory_chars)
    chosen = dict(required)
    for _, _, members in known:
        for member in members:
            mid = member['id']
            if mid in chosen:
                continue
            cost = len(member['content']) + 2
            if cost <= remaining:
                chosen[mid] = member
                remaining -= cost

    for row, group, members in known:
        before_ids = [m['id'] for m in members]
        kept = [deepcopy(chosen[m['id']]) for m in members if m['id'] in chosen]
        present = {m['id'] for m in kept}
        for sid in group.get('seed_evidence_ids') or [row['id']]:
            if sid not in present:
                kept.append(deepcopy(verified_seeds[sid]))
        if any(m['id'] not in before_ids for m in kept):
            kept.sort(key=lambda m: (m.get('original_index') or 0, m['id']))
        omitted = [mid for mid in before_ids if mid not in chosen]
        content = '\n'.join(m['content'] for m in kept)
        row['content'] = content
        row['original_content'] = verified_seeds[row['id']]['content']
        group.update(source_members=kept,
                     original_expanded_hash=sha256(content.encode('utf-8')).hexdigest(),
                     omitted_optional_ids=omitted, omitted_seed_ids=[],
                     expanded_evidence_ids=[m['id'] for m in kept
                                            if m['id'] not in verified_seeds])
        if omitted:
            group['truncation_status'] = 'CHAR_LIMIT_APPLIED'
            group['degradation_reason'] = 'OPTIONAL_EXPANSION_OVER_CONTEXT_CHAR_ALLOWANCE'
    summary = {'context_max_chars': allowance, 'mandatory_chars': mandatory_chars,
               'mandatory_overrun': mandatory_chars > allowance,
               'delivered_unique_member_chars': sum(len(m['content']) + 2 for m in chosen.values()) + unknown_cost,
               'verified_seed_ids': list(verified_seeds),
               'omitted_optional_ids': list(dict.fromkeys(
                   mid for _, group, _ in known for mid in group.get('omitted_optional_ids', []))),
               'unknown_mapping_rows': sum('delivery_budget_source_status' in row for row in output),
               'unit_dependency_status': 'KNOWN_METADATA_PRESERVED_EXTERNAL_DEPENDENCIES_UNVERIFIED'}
    for row in output:
        row['delivery_budget'] = deepcopy(summary)
    return output

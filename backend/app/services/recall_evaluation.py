"""Separate recalled seed identity from expanded, citable source identity."""
import hashlib


def _as_dict(row):
    return row.model_dump() if hasattr(row, 'model_dump') else row


def _verified_sources(sources):
    if not isinstance(sources, list):
        return None
    verified = {}
    for source in sources:
        if not isinstance(source, dict):
            return None
        source_id, body = source.get('id'), source.get('content')
        if (not isinstance(source_id, str) or not source_id or not isinstance(body, str)
                or source.get('content_hash') != hashlib.sha256(body.encode('utf-8')).hexdigest()):
            return None
        identity = (source['content_hash'], source.get('file_id'), source.get('kb_id'))
        if source_id in verified and verified[source_id] != identity:
            return None
        verified[source_id] = identity
    return verified


def _coherent_group(row, seed_ids, hits, sources):
    """Validate identity linkage in addition to each source body's own hash."""
    representative = row.get('representative_child_id')
    score_child = row.get('score_child_id')
    parent = row.get('parent_chunk_id')
    group_id = row.get('context_group_id')
    if (not isinstance(representative, str) or not isinstance(score_child, str)
            or (parent is not None and not isinstance(parent, str))
            or not isinstance(group_id, str)
            or representative not in hits or score_child not in hits
            or row.get('chunkId', row.get('id')) != representative
            or group_id != 'group:' + str(parent or representative)):
        return False
    # A parent-context group cannot mix documents or knowledge bases merely
    # because every individual body has a valid self-hash.
    scopes = {(identity[1], identity[2]) for identity in hits.values()}
    if len(scopes) != 1 or any(not all(scope) for scope in scopes):
        return False
    if sources is None or any((identity[1], identity[2]) not in scopes for identity in sources.values()):
        return False
    group = row.get('context_group')
    if group is not None:
        nested_seeds = group.get('seed_child_ids') if isinstance(group, dict) else None
        if (not isinstance(group, dict) or group.get('context_group_id') != group_id
                or group.get('parent_chunk_id') != parent
                or not isinstance(nested_seeds, list)
                or not all(isinstance(value, str) for value in nested_seeds)
                or set(nested_seeds) != set(seed_ids)
                or group.get('representative_child_id') != representative
                or group.get('score_child_id') != score_child):
            return False
        nested_members = _verified_sources(group.get('source_members'))
        if nested_members is None or nested_members != sources:
            return False
    return True


def evaluate_recall_identity(results):
    """Count explicit hit children, never representative IDs or expanded parents.

    UNKNOWN rows retain any verified source IDs for diagnostics but are excluded
    from asserted seed coverage. This also supports old saved responses without
    inventing a parent/child relation from their representative chunkId.
    """
    matched, citations, unknown = [], [], []
    hit_identity, citation_identity = {}, {}
    conflicting_hits, conflicting_citations = set(), set()
    for index, value in enumerate(results):
        row = _as_dict(value)
        seed_ids = row.get('seed_child_ids')
        hits = _verified_sources(row.get('hit_children'))
        sources = _verified_sources(row.get('citation_sources'))
        mapping_ok = (isinstance(seed_ids, list) and bool(seed_ids)
                      and all(isinstance(value, str) and value for value in seed_ids)
                      and hits is not None and set(seed_ids) == set(hits)
                      and row.get('source_mapping_status') == 'VERIFIED'
                      and _coherent_group(row, seed_ids, hits, sources))
        if mapping_ok:
            for source_id, identity in hits.items():
                if source_id in hit_identity and hit_identity[source_id] != identity:
                    mapping_ok = False
                    conflicting_hits.add(source_id)
                hit_identity[source_id] = identity
        if mapping_ok:
            matched.extend(seed_ids)
        else:
            unknown.append({'result_index': index, 'chunk_id': row.get('chunkId', row.get('id')),
                            'reason': 'MISSING_OR_UNVERIFIED_HIT_MAPPING'})
        if sources is not None:
            for source_id, identity in sources.items():
                if source_id in citation_identity and citation_identity[source_id] != identity:
                    conflicting_citations.add(source_id)
                    unknown.append({'result_index': index, 'chunk_id': source_id,
                                    'reason': 'CONFLICTING_CITATION_SOURCE'})
                citation_identity[source_id] = identity
            citations.extend(sources)
        elif row.get('citation_sources') is not None:
            unknown.append({'result_index': index, 'chunk_id': row.get('chunkId', row.get('id')),
                            'reason': 'INVALID_CITATION_SOURCE'})
    matched = [value for value in dict.fromkeys(matched) if value not in conflicting_hits]
    citations = [value for value in dict.fromkeys(citations) if value not in conflicting_citations]
    return {'matched_seed_ids': matched,
            'citation_source_ids': citations,
            'context_group_count': len(results),
            'matched_seed_count': len(set(matched)),
            'citation_source_count': len(set(citations)),
            'mapping_status': 'UNKNOWN' if unknown else 'VERIFIED',
            'unknown_mappings': unknown}


def evaluate_seed_qrels(results, relevant_seed_ids):
    """Compute seed Recall only when both qrels and hit identity are explicit."""
    summary = evaluate_recall_identity(results)
    gold = set(relevant_seed_ids)
    hits = gold.intersection(summary['matched_seed_ids'])
    known = summary['mapping_status'] == 'VERIFIED' and bool(gold)
    return {**summary, 'gold_seed_count': len(gold),
            'matched_gold_seed_ids': sorted(hits),
            'seed_recall': len(hits) / len(gold) if known else None,
            'seed_recall_status': 'VERIFIED' if known else 'UNKNOWN'}


def recall_identity_fields(row):
    """Serialize the expander's explicit identity fields without inferring IDs."""
    group = row.get('context_group') or {}
    return {'context_group_id': group.get('context_group_id'),
            'representative_child_id': row.get('representative_child_id'),
            'score_child_id': row.get('score_child_id'),
            'hit_children': row.get('hit_children', []),
            'citation_sources': row.get('citation_sources', [])}

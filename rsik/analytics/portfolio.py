"""Deterministic proposal portfolio; parsing never executes candidate code."""
import ast
import hashlib
import json


def _hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _body(record):
    return record.get('payload', {})


def analyze_portfolio(records):
    instances = []
    seen = set()
    for record in records:
        if record.get('kind', record.get('projection_type', record.get('type'))) != 'Proposal':
            continue
        payload = _body(record)
        identity = record.get('record_id') or record.get('id') or record.get('event_id') or payload.get('proposal_id')
        # Mirrored source records are one instance; distinct slots remain distinct.
        key = (identity, payload.get('slot', payload.get('angle')), payload.get('round_id'))
        if identity is not None and key in seen:
            continue
        seen.add(key)
        patch = payload.get('worker_patch', payload.get('patch'))
        byte_hash = payload.get('patch_sha256', payload.get('patch_hash'))
        ast_hash = payload.get('ast_sha256', payload.get('ast_hash'))
        if byte_hash in ('UNKNOWN', 'N/A', ''): byte_hash = None
        if ast_hash in ('UNKNOWN', 'N/A', ''): ast_hash = None
        parse_status = 'UNKNOWN'
        if isinstance(patch, (dict, str)):
            serialized = json.dumps(patch, ensure_ascii=False, sort_keys=True, separators=(',', ':')) if isinstance(patch, dict) else patch
            byte_hash = _hash(serialized)
            # A replacement fragment cannot establish whole strategy equivalence:
            # the old anchor and parent determine its actual meaning.
            if isinstance(patch, dict):
                parse_status = 'REPLACEMENT_SCOPE_UNKNOWN'
            elif payload.get('full_candidate_source') is True:
                try:
                    ast_hash = _hash(ast.dump(ast.parse(patch), include_attributes=False))
                    parse_status = 'PARSED_SOURCE'
                except (SyntaxError, TypeError, ValueError, RecursionError):
                    parse_status = 'UNPARSABLE'
            else:
                parse_status = 'SOURCE_SCOPE_UNKNOWN'
        full_identity = 'normalized_ast_sha256' in payload
        if full_identity:
            ast_hash = payload['normalized_ast_sha256']
            if not ast_hash or ast_hash == 'UNKNOWN':
                ast_hash = None
            parse_status = 'FULL_WORKER' if ast_hash else 'UNKNOWN'
        instances.append({
            'proposal_id': payload.get('proposal_id', identity),
            'slot': payload.get('slot', payload.get('angle')),
            'agent_role': record.get('dimensions', {}).get('agent_role', payload.get('agent_role', 'UNKNOWN')),
            'round_id': payload.get('round_id'), 'schema_valid': payload.get('schema_valid'),
            'static_pass': payload.get('static_pass'), 'patch_byte_hash': byte_hash,
            **({'worker_sha256': payload.get('worker_sha256'),
                'normalized_ast_sha256': ast_hash} if full_identity else {}),
            'ast_hash': ast_hash, 'ast_scope': parse_status,
            'hypothesis_identity': payload.get('hypothesis_id'),
            'expected_impact_identity': payload.get('expected_impact_id'),
            'modified_paths': sorted(payload.get('modified_paths') or []),
        })
    instances.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
    def summary(field):
        known = [item[field] for item in instances if item[field] is not None]
        return {'known_instances': len(known), 'unknown_instances': len(instances)-len(known),
                'unique_count': len(set(known)) if known else None,
                'duplicate_instances': len(known)-len(set(known)) if known else None}
    return {'proposal_instances': len(instances) if instances else None,
            'observed_proposal_instances_lower_bound': len(instances), 'instances': instances,
            'patch_bytes': summary('patch_byte_hash'), 'ast_structure': summary('ast_hash'),
            'hypothesis': summary('hypothesis_identity'), 'expected_impact': summary('expected_impact_identity'),
            'interpretation': 'Exact identities only; natural-language equivalence is UNKNOWN. Replacement AST is not full candidate AST.'}

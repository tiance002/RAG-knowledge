"""Read-only Token attribution for hash-bound objective request receipts."""
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path

from .metrics import token_statistics
from .projections import project_sources
from .reader import digest, read_event_log
from ..contracts import ANGLES
from ..locking import atomic_write
from ..objectives import validate_prompt_objective
from ..state import canonical


def objective_usage(ledger, receipt_paths):
    source = read_event_log(ledger)
    starts = defaultdict(list)
    for event in source['events']:
        if event['event_type'] == 'MODEL_REQUEST_STARTED':
            starts[event['payload']['request_key']].append(event['payload'])
    statistics = token_statistics(project_sources([source]))
    usage = {row['request_key']: row for row in statistics['requests']}
    grouped = defaultdict(list)
    seen = {}
    for path in receipt_paths:
        path = Path(path)
        receipt = json.loads(path.read_bytes())
        if receipt.get('schema') != 'rsi-request-input-receipt/v1':
            raise ValueError('INVALID_OBJECTIVE_USAGE_RECEIPT')
        key = receipt['request_key']
        if path.name != key + '.input-receipt.json' or receipt['request_file'] != key + '.request.json':
            raise ValueError('INVALID_OBJECTIVE_USAGE_RECEIPT_IDENTITY')
        if key in seen:
            if seen[key] != receipt:
                raise ValueError('CONFLICTING_OBJECTIVE_USAGE_RECEIPT')
            continue
        seen[key] = receipt
        if key not in starts:
            continue  # prepared locally, no actual request start
        wire = path.with_name(receipt['request_file']).read_bytes()
        body = json.loads(wire)
        if (len(starts[key]) != 1 or receipt['role'] != starts[key][0]['role']
                or starts[key][0]['request_hash'] != digest(body) or receipt['request_hash'] != digest(body)
                or receipt['request_wire_sha256'] != sha256(wire).hexdigest()
                or receipt['request_body_bytes'] != len(wire)):
            raise ValueError('OBJECTIVE_USAGE_REQUEST_MISMATCH')
        public = json.loads(body['input'][1]['content'])['public']
        objective = public.get('objective')
        if objective is not None:
            validate_prompt_objective(objective)
            if (receipt.get('objective_prompt_sha256') != digest(objective)
                    or receipt.get('objective_profile_id') != objective['objective_profile_id']
                    or receipt.get('objective_version') != objective['version']):
                raise ValueError('OBJECTIVE_USAGE_PROFILE_MISMATCH')
        profile_key = (objective['objective_profile_id'], objective['version'], digest(objective)) if objective else (
            'UNSCOPED', None, None)
        grouped[profile_key].append((usage[key], public, receipt['role']))

    def summarize(counts):
        known = [count for count in counts if count is not None]
        return {'status': 'N/A' if not counts else 'UNKNOWN' if not known else
                'PARTIAL' if len(known) < len(counts) else 'KNOWN',
                'tokens': sum(known) if known else None, 'unknown_requests': len(counts) - len(known)}

    groups = []
    for (profile_id, version, profile_hash), rows in sorted(grouped.items(), key=lambda item: str(item[0])):
        groups.append({
            'objective_profile_id': profile_id, 'version': version, 'objective_prompt_sha256': profile_hash,
            'requests': len(rows), 'input_tokens': summarize([r['input_tokens'] for r, _, _ in rows]),
            'output_tokens': summarize([r['output_tokens'] for r, _, _ in rows]),
            'proposal_input_tokens': summarize([r['input_tokens'] for r, _, role in rows if role in ANGLES]),
            # Aggregate provider usage contains no per-section attribution.
            # A section absent from the exact request contributes zero.
            'search_memory_tokens': summarize([
                None if public.get('search_memory', {}).get('entries') else 0 for _, public, _ in rows]),
            'handbook_tokens': summarize([
                None if public.get('handbook_entries') else 0 for _, public, _ in rows]),
        })
    return {'source_sha256': source['sha256'], 'groups': groups,
            'unattributed_request_count': len(set(usage) - seen.keys()),
            'all_usage': statistics['totals']}


def main():
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ledger', required=True, type=Path)
    parser.add_argument('--input-receipt', required=True, action='append', type=Path)
    parser.add_argument('--output', type=Path)
    options = parser.parse_args()
    result = objective_usage(options.ledger, options.input_receipt)
    if options.output:
        atomic_write(options.output, canonical(result), exclusive=True)
    else:
        print(canonical(result).decode('utf-8'))


if __name__ == '__main__':
    main()

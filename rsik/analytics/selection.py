"""Opt-in selection diagnostics, independent of frozen S3/S6 definitions."""
from collections import Counter, defaultdict
import json


SELECTION_CONTRACT_VERSION = 'rsi-selection-metrics/m1b1'
SELECTION_DEFINITIONS = {
    'selection_success_rate': {'description': 'Selection Success Rate',
        'numerator': 'completed valid operations returning one parent',
        'denominator': 'completed valid operations including empty results'},
    'selected_parent_diversity': {'description': 'Selected Parent Diversity',
        'numerator': 'distinct parents returned by completed valid operations',
        'denominator': 'completed valid operations returning one parent'},
    'conditional_parent_hhi': {'description': 'Conditional Parent HHI',
        'numerator': 'sum of squared parent selection counts',
        'denominator': 'square of completed valid operations returning one parent'},
}


def analyze_selection(records):
    """Consume validated m1a1 Selection projections within one compatibility group.

    Repeated semantic operation snapshots count once. Conflicting snapshots make
    that operation UNKNOWN, never an empty success. Unfinished operations stay
    outside the completed-operation denominator, with explicit coverage counts.
    """
    operations = defaultdict(list)
    refs = set()
    unidentified = 0
    for record in records:
        if record.get('kind', record.get('projection_type', record.get('type'))) != 'Selection':
            continue
        p = record['payload']
        if p.get('metric_contract_version') != 'rsi-metrics/m1a1':
            continue
        for ref in record.get('source_refs', []) + record.get('event_ids', []):
            refs.add(json.dumps(ref, sort_keys=True, ensure_ascii=False))
        identity = p.get('selection_id')
        if not isinstance(identity, str) or identity in ('', 'UNKNOWN', 'N/A'):
            unidentified += 1
            continue
        source = record.get('history_source_id') or p.get('history_source_id')
        if not source:
            source = tuple(sorted(str(r.get('source_id')) for r in record.get('source_refs', []) if isinstance(r, dict)))
        operations[(str(source), identity)].append(p)
    completed, counts, unknown = 0, Counter(), unidentified
    conflicts = 0
    for snapshots in operations.values():
        signatures = {json.dumps({k: p.get(k) for k in
            ('completed', 'valid', 'selected_parent_id', 'returned_count')}, sort_keys=True)
            for p in snapshots}
        if len(signatures) != 1:
            conflicts += 1
            unknown += 1
            continue
        p = snapshots[0]
        parent = p.get('selected_parent_id')
        valid_parent = isinstance(parent, str) and parent not in ('', 'UNKNOWN', 'N/A')
        explicit_empty = 'selected_parent_id' in p and parent is None
        returned = p.get('returned_count', 1 if valid_parent else 0)
        if (p.get('completed') is not True or p.get('valid') is not True or
                not (valid_parent or explicit_empty) or type(returned) is not int or
                returned != int(valid_parent)):
            unknown += 1
            continue
        completed += 1
        if valid_parent:
            counts[parent] += 1
    successful = sum(counts.values())
    metrics = []
    for mid, numerator, denominator in (
            ('selection_success_rate', successful, completed),
            ('selected_parent_diversity', len(counts), successful),
            ('conditional_parent_hhi', sum(n*n for n in counts.values()), successful**2)):
        status = 'UNKNOWN' if not completed else 'N/A' if not denominator else 'OK'
        metrics.append({'metric_id': mid, 'status': status,
            'numerator': numerator if completed else None,
            'denominator': denominator if completed else None,
            'value': numerator / denominator if status == 'OK' else None,
            'unit': 'HHI' if mid == 'conditional_parent_hhi' else 'ratio'})
    return {'schema': SELECTION_CONTRACT_VERSION, 'definitions': SELECTION_DEFINITIONS,
        'metrics': metrics, 'observed_operations': len(operations) + unidentified,
        'completed_valid_operations': completed, 'successful_operations': successful,
        'empty_operations': completed-successful, 'unknown_operations': unknown,
        'conflicting_operations': conflicts, 'parent_counts': dict(sorted(counts.items())),
        'evidence_refs': [json.loads(ref) for ref in sorted(refs)],
        'scope': 'Observed completed valid operations only; unknown operations excluded; no causal or quality claim.'}

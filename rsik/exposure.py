"""Workflow-owned source-family and exposure events; no freshness from renaming.

A hash chain establishes local integrity, not independent execution attestation.
Family assertions are reviewable metadata, not automatic semantic deduplication.
"""
from .events import EventLog, digest
from pathlib import Path
import re

PARTITIONS = {'CONTROL', 'DEV', 'ITERATIVE_VALIDATION', 'FINAL_HOLDOUT'}
RELATIONS = {'UNKNOWN', 'ORIGINAL', 'VERSION_OF', 'REPRINT_OF', 'EXCERPT_OF', 'DERIVED_FROM'}
REVIEW_POLICIES = {
    'research-source-family/v1': {'AI_REVIEWED', 'HUMAN_REVIEWED'},
    'production-source-family/v1': {'HUMAN_REVIEWED'},
}


def _review(review, subject):
    if (not isinstance(review, dict) or not isinstance(review.get('status'), str) or review.get('status') not in
            {'UNREVIEWED', 'AI_REVIEWED', 'HUMAN_REVIEWED'}
            or review.get('subject_sha256') != digest(subject)
            or not review.get('reviewer') or not review.get('reviewed_at')):
        raise ValueError('SOURCE_REVIEW_BINDING_INVALID')


class ExposureLedger:
    def __init__(self, path):
        self.log = EventLog(path)

    def sources(self):
        result = {}
        for event in self.log.read():
            if event['event_type'] == 'SOURCE_REGISTERED':
                p = event['payload']
                if p['source_id'] in result and result[p['source_id']] != p:
                    raise ValueError('SOURCE_IDENTITY_CONFLICT')
                result[p['source_id']] = p
        return result

    def register(self, source, *, review):
        fields = {'source_id', 'document_id', 'version', 'content_sha256', 'source_family_id',
                  'origin', 'relation', 'related_source_ids', 'exposure_status'}
        if not isinstance(source, dict) or set(source) != fields or any(not source.get(k) for k in
                ('source_id', 'document_id', 'version', 'content_sha256', 'source_family_id', 'origin')):
            raise ValueError('SOURCE_IDENTITY_INCOMPLETE')
        if (any(not isinstance(source[k], str) for k in
                ('source_id', 'document_id', 'version', 'content_sha256', 'source_family_id', 'origin', 'relation', 'exposure_status'))
                or not re.fullmatch(r'[0-9a-f]{64}', source['content_sha256']) or source['relation'] not in RELATIONS
                or source['exposure_status'] not in {'KNOWN_HISTORY', 'EXPOSURE_UNKNOWN'}
                or not isinstance(source['related_source_ids'], list)
                or any(not isinstance(sid, str) or not sid for sid in source['related_source_ids'])
                or len(set(source['related_source_ids'])) != len(source['related_source_ids'])
                or source['source_id'] in source['related_source_ids']):
            raise ValueError('SOURCE_IDENTITY_INCOMPLETE')
        _review(review, source)
        known = self.sources()
        if source['relation'] == 'UNKNOWN':
            if source['related_source_ids'] or source['exposure_status'] != 'EXPOSURE_UNKNOWN':
                raise ValueError('UNKNOWN_RELATION_REQUIRES_UNKNOWN_EXPOSURE')
        elif source['relation'] != 'ORIGINAL' and not source['related_source_ids']:
            raise ValueError('DERIVATION_PARENT_REQUIRED')
        if any(sid not in known for sid in source['related_source_ids']):
            raise ValueError('DERIVATION_PARENT_UNKNOWN')
        # Exact bytes or declared derivation cannot be relabeled independent.
        relatives = [s for s in known.values() if s['content_sha256'] == source['content_sha256']
                     or s['source_id'] in source['related_source_ids']
                     or s['document_id'] == source['document_id']]
        if any(s['source_family_id'] != source['source_family_id'] for s in relatives):
            raise ValueError('SOURCE_FAMILY_REPACKAGING')
        payload = {**source, 'review': review}
        self.log.append('source:' + source['source_id'], 'SOURCE_REGISTERED', payload,
                        evidence_hashes=[source['content_sha256'], review['subject_sha256']])
        return source['source_id']

    def expose(self, source_ids, *, partition, experiment_id, actor, evidence_ref, occurred_at,
               event_key):
        known = self.sources()
        if (not isinstance(partition, str) or partition not in PARTITIONS
                or not isinstance(source_ids, (list, tuple)) or not source_ids
                or any(not isinstance(sid, str) or not sid for sid in source_ids)
                or len(set(source_ids)) != len(source_ids)
                or any(sid not in known for sid in source_ids)
                or not all(isinstance(value, str) and value.strip()
                           for value in (experiment_id, actor, evidence_ref, occurred_at, event_key))):
            raise ValueError('EXPOSURE_EVENT_INVALID')
        payload = {'source_ids': sorted(source_ids), 'source_family_ids': sorted({
            known[s]['source_family_id'] for s in source_ids}),
            'dependency_cluster_ids': sorted({cluster_id for s in source_ids
                for cluster_id in ((known[s]['source_family_id'], '__UNKNOWN_SOURCE_FAMILY__')
                    if known[s]['relation'] == 'UNKNOWN' else (known[s]['source_family_id'],))}),
            'partition': partition,
            'experiment_id': experiment_id, 'actor': actor, 'evidence_ref': evidence_ref,
            'occurred_at': occurred_at}
        self.log.append('exposure:' + event_key, 'SOURCE_EXPOSED', payload)
        return payload

    def final_eligibility(self, source_ids, *, experiment_id,
                          review_policy='production-source-family/v1'):
        if (not isinstance(source_ids, (list, tuple)) or not source_ids
                or any(not isinstance(sid, str) or not sid for sid in source_ids)
                or len(set(source_ids)) != len(source_ids) or not experiment_id
                or not isinstance(review_policy, str) or review_policy not in REVIEW_POLICIES):
            raise ValueError('FINAL_ELIGIBILITY_INPUT_INVALID')
        known = self.sources()
        blockers = []
        production_blockers = []
        families = set()
        for sid in source_ids:
            source = known.get(sid)
            if source is None:
                blockers.append('SOURCE_UNREGISTERED')
                production_blockers.append('SOURCE_UNREGISTERED')
                continue
            families.add(source['source_family_id'])
            if source['exposure_status'] != 'KNOWN_HISTORY':
                blockers.append('EXPOSURE_UNKNOWN')
                production_blockers.append('EXPOSURE_UNKNOWN')
            if source['relation'] == 'UNKNOWN':
                blockers.append('SOURCE_FAMILY_UNKNOWN')
                production_blockers.append('SOURCE_FAMILY_UNKNOWN')
            if source['review']['status'] not in REVIEW_POLICIES[review_policy]:
                blockers.append('SOURCE_FAMILY_REVIEW_NOT_ACCEPTED_BY_POLICY')
            if source['review']['status'] != 'HUMAN_REVIEWED':
                production_blockers.append('SOURCE_FAMILY_NOT_HUMAN_REVIEWED')
        dependency_clusters = {cluster_id for sid in source_ids if sid in known
            for cluster_id in ((known[sid]['source_family_id'], '__UNKNOWN_SOURCE_FAMILY__')
                if known[sid]['relation'] == 'UNKNOWN' else (known[sid]['source_family_id'],))}
        # Any use by development, a previous final, or another actor/experiment
        # consumes freshness. Reservation is a distinct event, not exposure.
        for event in self.log.read():
            if event['event_type'] != 'SOURCE_EXPOSED':
                continue
            payload = event['payload']
            clusters = payload.get('dependency_cluster_ids')
            if clusters is None:
                clusters = payload.get('source_family_ids', [])
            if dependency_clusters.intersection(clusters):
                blockers.append('SOURCE_FAMILY_ALREADY_EXPOSED')
                production_blockers.append('SOURCE_FAMILY_ALREADY_EXPOSED')
        return {'eligible': not blockers, 'blocking_reasons': sorted(set(blockers)),
                'review_policy': review_policy,
                'dependency_cluster_ids': sorted(dependency_clusters),
                'production_review_eligible': not production_blockers,
                'production_blocking_reasons': sorted(set(production_blockers)),
                'source_family_ids': sorted(families), 'experiment_id': experiment_id,
                'ledger_frontier': self.frontier()}

    def snapshot(self, source_ids, *, experiment_id,
                 review_policy='production-source-family/v1'):
        """Return a plan-bound read-only view of source identity and exposure state."""
        if (not isinstance(source_ids, (list, tuple)) or not source_ids
                or any(not isinstance(sid, str) or not sid for sid in source_ids)
                or len(set(source_ids)) != len(source_ids) or not experiment_id):
            raise ValueError('SOURCE_SNAPSHOT_SET_INVALID')
        sources = self.sources()
        missing = sorted(set(source_ids) - set(sources))
        if missing:
            raise ValueError('SOURCE_UNREGISTERED')
        eligibility = self.final_eligibility(source_ids, experiment_id=experiment_id,
                                             review_policy=review_policy)
        selected = []
        for source_id in sorted(source_ids):
            source = sources[source_id]
            selected.append({k: source[k] for k in (
                'source_id', 'document_id', 'version', 'content_sha256', 'source_family_id',
                'origin', 'relation', 'related_source_ids', 'exposure_status')}
                | {'review_status': source['review']['status'],
                   'family_resolution_status': ('UNKNOWN' if source['relation'] == 'UNKNOWN' else 'DECLARED')})
        family_ids = set(eligibility['dependency_cluster_ids'])
        history = [{'sequence': event['sequence'], 'event_hash': event['event_hash'],
                    'event_type': event['event_type'], 'payload': event['payload']}
                   for event in self.log.read()
                   if event['event_type'] == 'SOURCE_EXPOSED'
                   and family_ids.intersection(event['payload'].get(
                       'dependency_cluster_ids', event['payload']['source_family_ids']))]
        return {'schema': 'rsi-source-exposure-snapshot/v1',
                'ledger_path': str(Path(self.log.path).resolve()),
                'experiment_id': experiment_id,
                'review_policy': review_policy,
                'frontier': self.frontier(), 'sources': selected,
                'exposure_history': history,
                'eligible': eligibility['eligible'],
                'blocking_reasons': eligibility['blocking_reasons'],
                'production_review_eligible': eligibility['production_review_eligible'],
                'production_qualified': False,
                'high_risk_review_pending': any(
                    sources[source_id]['review']['status'] != 'HUMAN_REVIEWED'
                    for source_id in source_ids),
                'production_blocking_reasons': eligibility['production_blocking_reasons'],
                'source_family_ids': eligibility['source_family_ids'],
                'dependency_cluster_ids': eligibility['dependency_cluster_ids']}

    @staticmethod
    def verify_snapshot(snapshot, *, source_ids, experiment_id,
                        review_policy='production-source-family/v1'):
        """Re-read the bound ledger and reject changed history or source metadata."""
        if (not isinstance(snapshot, dict)
                or snapshot.get('schema') != 'rsi-source-exposure-snapshot/v1'
                or snapshot.get('experiment_id') != experiment_id
                or snapshot.get('review_policy') != review_policy
                or not snapshot.get('ledger_path')):
            raise ValueError('SOURCE_EXPOSURE_SNAPSHOT_INVALID')
        path = Path(snapshot['ledger_path'])
        if not path.is_file():
            raise ValueError('SOURCE_EXPOSURE_LEDGER_MISSING')
        # Bypass EventLog.__init__: verification must never initialize or alter
        # an absent/empty ledger as a side effect.
        ledger = object.__new__(ExposureLedger)
        ledger.log = object.__new__(EventLog)
        ledger.log.path = path
        current = ledger.snapshot(source_ids, experiment_id=experiment_id,
                                  review_policy=review_policy)
        if current != snapshot:
            raise ValueError('SOURCE_EXPOSURE_SNAPSHOT_CHANGED')
        return current

    def frontier(self):
        events = self.log.read()
        return {'sequence': len(events), 'event_hash': events[-1]['event_hash'] if events else '0' * 64}

    def dependency_clusters(self, cases):
        """Connected components merge any cases sharing any source family."""
        known = self.sources()
        parents = {case['qid']: case['qid'] for case in cases}
        if len(parents) != len(cases):
            raise ValueError('DUPLICATE_CASE_ID')
        def find(q):
            while parents[q] != q:
                q = parents[q]
            return q
        owner = {}
        for case in cases:
            for sid in case['source_ids']:
                if sid not in known:
                    raise ValueError('SOURCE_UNREGISTERED')
                family_ids = ((known[sid]['source_family_id'], '__UNKNOWN_SOURCE_FAMILY__')
                    if known[sid]['relation'] == 'UNKNOWN' else (known[sid]['source_family_id'],))
                for family in family_ids:
                    if family in owner:
                        a, b = sorted((find(case['qid']), find(owner[family])))
                        parents[b] = a
                    owner[family] = case['qid']
        groups = {}
        for qid in parents:
            groups.setdefault(find(qid), []).append(qid)
        return {'clusters': sorted(sorted(g) for g in groups.values()),
                'statistical_denominator': len(groups), 'case_count': len(cases)}


def validate_snapshot(record, *, require_final=False,
                      review_policy='production-source-family/v1'):
    """Validate a frozen source record against its current immutable ledger.

    Development snapshots may include seen or exposure-unknown sources. Only a
    final-holdout check applies the freshness and human-review admission rule.
    """
    if isinstance(record, (str, Path)):
        import json
        record = json.loads(Path(record).read_text(encoding='utf-8'))
    if not isinstance(record, dict):
        raise ValueError('SOURCE_EXPOSURE_SNAPSHOT_INVALID')
    snapshot = record.get('snapshot', record)
    sources = snapshot.get('sources') if isinstance(snapshot, dict) else None
    if isinstance(sources, dict):
        source_ids = sorted(sources)
    elif isinstance(sources, list):
        source_ids = sorted(source.get('source_id') for source in sources
                             if isinstance(source, dict) and source.get('source_id'))
        if len(source_ids) != len(sources):
            raise ValueError('SOURCE_EXPOSURE_SNAPSHOT_INVALID')
    else:
        raise ValueError('SOURCE_EXPOSURE_SNAPSHOT_INVALID')
    current = ExposureLedger.verify_snapshot(snapshot, source_ids=source_ids,
        experiment_id=snapshot.get('experiment_id'), review_policy=review_policy)
    if require_final and not current['eligible']:
        raise ValueError('SOURCE_EXPOSURE_FINAL_INELIGIBLE')
    return current

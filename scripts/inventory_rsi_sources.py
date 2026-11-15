"""Read-only inventory of existing DEV source facts and exposure histories.

This intentionally does not invent source families, reviewed relation atoms, or
negative controls. It records the older frozen materials and reports gaps for
the separate source-relations/v1 profile.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sqlite3
from datetime import datetime, timezone


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / 'var/rsi-v23-s21-s7-20261115/sources/read-only-inventory.json'
CATEGORIES = [
    'NONSEED_NECESSITY', 'CROSS_MEMBER_RELATION', 'NEGATION_CONDITION',
    'UNIT_SCOPE_OWNERSHIP', 'MULTI_SOURCE_SUPPORT', 'CITATION_SUPPORT',
]
QUERY_RUNS = [
    'bigram-ratio-luna-r5', 'overlap-conservative-luna-r2', 'overlap-luna-r1',
    'overlap-zero-fallback-luna-r3', 'trigram-luna-r4',
]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path):
    return json.loads(path.read_text(encoding='utf-8'))


def inspect_dev_fixture(path: Path):
    path = path.resolve()
    sources_manifest = read_json(path / 'sources.manifest.json')
    cases = read_json(path / 'cases.json')
    provenance = read_json(path / 'provenance.restricted.json')
    source_rows = []
    source_bytes = {}
    exact_member_spans = 0
    member_spans = 0
    for row in sources_manifest:
        source_hash = row.get('source_sha256')
        relative = row.get('source_relative_path')
        if not relative:
            # Foundation bundles map content-addressed source files by digest.
            relative = f"sources/{source_hash}.md"
        source_path = (path / relative).resolve()
        if not source_path.is_relative_to(path):
            raise ValueError('INVENTORY_SOURCE_PATH_ESCAPES_FIXTURE')
        data = source_path.read_bytes()
        actual_hash = sha(data)
        source_bytes[source_hash] = data
        source_rows.append({
            'source_id': source_hash,
            'document_id': row.get('original_path') or row.get('source_path') or source_path.name,
            'version': actual_hash,
            'content_sha256': actual_hash,
            'source_group': row.get('source_group'),
            'source_path': str(source_path),
            'bytes': len(data),
            'hash_matches_manifest': actual_hash == source_hash,
            'family_relationship_status': 'UNKNOWN',
            'historical_exposure_status': 'SEEN_OR_UNKNOWN',
        })
    for item in cases:
        qid = item['qid']
        record = provenance[qid]
        data = source_bytes[record['source_sha256']]
        text = data.decode('utf-8')
        for member_id, span in record['member_spans'].items():
            member_spans += 1
            chunk = next((c for c in item['case']['chunks'] if c['id'] == member_id), None)
            if chunk and type(span.get('start')) is int and type(span.get('end')) is int:
                exact_member_spans += int(text[span['start']:span['end']] == chunk['content'])
    fact_count = 0
    exact_facts = 0
    legacy_kinds = Counter()
    for qid, record in provenance.items():
        data = source_bytes[record['source_sha256']]
        text = data.decode('utf-8')
        for fact in record.get('facts', []):
            fact_count += 1
            legacy_kinds[fact.get('kind', 'UNKNOWN')] += 1
            if type(fact.get('start')) is int and type(fact.get('end')) is int:
                exact_facts += int(text[fact['start']:fact['end']] == fact.get('text'))
    family_ids = sorted({r.get('source_group') for r in source_rows if r.get('source_group')})
    return {
        'fixture_path': str(path),
        'purpose': 'EXISTING_FROZEN_DEV',
        'question_count': len(cases),
        'source_count': len(source_rows),
        'source_family_count_by_legacy_group': len(family_ids),
        'legacy_source_families': family_ids,
        'sources': source_rows,
        'source_hash_mismatches': [r['source_id'] for r in source_rows if not r['hash_matches_manifest']],
        'legacy_fact_count': fact_count,
        'legacy_exact_fact_spans_verified': exact_facts,
        'legacy_fact_kinds': dict(sorted(legacy_kinds.items())),
        'legacy_member_spans_verified': exact_member_spans,
        'legacy_member_span_count': member_spans,
        'new_profile_atom_count': 0,
        'new_profile_reviewed_atom_count': 0,
        'source_family_relationships': 'UNKNOWN; legacy source_group identity does not encode reprint/derivation links',
        'exposure_history': 'PARTIAL; prior query-aware decisions are enumerated below; this bundle has no source-relations exposure ledger snapshot',
    }


def inspect_query_history(path: Path):
    path = path.resolve()
    histories = []
    usage = Counter()
    question_uses = 0
    run_paths = [path / name for name in QUERY_RUNS if (path / name).is_dir()]
    for run_path in run_paths:
        for decision_path in sorted(run_path.rglob('decision-*.json')):
            decision = read_json(decision_path)
            rows = decision.get('per_question', [])
            groups = Counter(row.get('source_group', 'UNKNOWN') for row in rows)
            for group, count in groups.items():
                if group != 'UNKNOWN':
                    usage[group] += count
            question_uses += len(rows)
            histories.append({
                'record_path': str(decision_path.resolve()),
                'candidate_id': decision.get('candidate_id'),
                'proposal_id': decision.get('proposal_id'),
                'question_count': len(rows),
                'source_group_usage': dict(sorted(groups.items())),
                'verdict': decision.get('verdict'),
                'reason_codes': decision.get('reason_codes', []),
            })
    run_manifests = []
    for run_path in run_paths:
        for manifest_path in sorted(run_path.rglob('manifest.json')):
            try:
                value = read_json(manifest_path)
            except (PermissionError, OSError, json.JSONDecodeError):
                continue
            if value.get('evaluator_version') or value.get('experiment_id'):
                run_manifests.append({
                    'record_path': str(manifest_path.resolve()),
                    'experiment_id': value.get('experiment_id'),
                    'status': value.get('status'),
                    'evaluator_version': value.get('evaluator_version'),
                    'base_commit': value.get('base_commit'),
                    'budget': value.get('budget'),
                })
    return {
        'history_root': str(path),
        'inspected_run_directories': [str(p.resolve()) for p in run_paths],
        'decision_record_count': len(histories),
        'run_manifest_count': len(run_manifests),
        'question_use_count': question_uses,
        'distinct_source_groups_seen': len(usage),
        'source_group_use_counts': dict(sorted(usage.items())),
        'decisions': histories,
        'run_manifests': run_manifests,
    }


def inspect_event_ledger(path: Path):
    path = path.resolve()
    if not path.is_file():
        return {'path': str(path), 'status': 'MISSING', 'source_events': None}
    uri = f'file:{path.as_posix()}?mode=ro'
    try:
        with sqlite3.connect(uri, uri=True) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'events' not in names:
                return {'path': str(path), 'status': 'NO_EVENTS_TABLE', 'source_events': None}
            rows = db.execute('SELECT event_json FROM events ORDER BY sequence').fetchall()
        source_events = []
        for (raw,) in rows:
            event = json.loads(raw)
            if event.get('event_type') in {'SOURCE_REGISTERED', 'SOURCE_EXPOSED'}:
                source_events.append({'event_type': event['event_type'], 'event_hash': event.get('event_hash'),
                                      'payload': event.get('payload')})
        return {'path': str(path), 'status': 'READ_ONLY_SCANNED',
                'event_count': len(rows), 'source_events': source_events}
    except (sqlite3.Error, OSError, json.JSONDecodeError) as exc:
        return {'path': str(path), 'status': 'READ_FAILED', 'error': type(exc).__name__, 'source_events': None}


def build_inventory():
    train = ROOT / 'var/rsi-v22-20261115/foundation-r1/train'
    query_history = ROOT / 'var/rsi-v22-20261115/query-aware-v1'
    if not train.is_dir() or not query_history.is_dir():
        raise FileNotFoundError('REQUIRED_V22_DEV_OR_QUERY_HISTORY_NOT_FOUND')
    dev = inspect_dev_fixture(train)
    history = inspect_query_history(query_history)
    sources_by_group = {row.get('source_group'): row for row in dev['sources']}
    history_group_ids = set(history['source_group_use_counts'])
    unknown_groups = sorted(history_group_ids - set(sources_by_group))
    for row in dev['sources']:
        row['historical_query_uses'] = history['source_group_use_counts'].get(row.get('source_group'), 0)
        row['exposure_status'] = 'SEEN' if row['historical_query_uses'] else 'HISTORY_NOT_FOUND'
    matrix = {category: {'status': 'BLOCKED_DATA', 'reviewed_positive_families': 0,
                         'negative_control_families': 0,
                         'reason': 'No source-relations/v1 fixture or paired member-removal control in the inspected frozen DEV corpus.'}
              for category in CATEGORIES}
    return {
        'schema': 'rsi-source-read-only-inventory/v1',
        'created_at_utc': datetime.now(timezone.utc).isoformat(),
        'read_only': True,
        'source_inputs': [str(train.resolve())] + [str((query_history / name).resolve())
                                                    for name in QUERY_RUNS if (query_history / name).is_dir()],
        'reused_existing_data': True,
        'copied_corpus_or_index': False,
        'dev_source_facts': dev,
        'historical_source_usage': history,
        'historical_groups_missing_from_dev_manifest': unknown_groups,
        'source_exposure_ledger': inspect_event_ledger(ROOT / 'var/rsi-v22-20261115/a1/events.sqlite'),
        'source_relations_profile_coverage': {
            'profile': 'source-relations/v1', 'status': 'BLOCKED_DATA',
            'matrix': matrix,
            'reason': 'Legacy exact source intervals are recorded, but they do not supply the reviewed object/value/unit/condition relation semantics, source-derivation links, or paired negative controls required by the new profile.'},
        'holdout_access': 'NOT_READ; restricted holdout paths are excluded from this inventory.',
        'claim_limit': 'Inventory evidence only. It does not qualify a real baseline or make a final-holdout freshness claim.',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    report = build_inventory()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output.resolve()),
                      'source_count': report['dev_source_facts']['source_count'],
                      'legacy_fact_count': report['dev_source_facts']['legacy_fact_count'],
                      'legacy_exact_fact_spans_verified': report['dev_source_facts']['legacy_exact_fact_spans_verified'],
                      'historical_decisions': report['historical_source_usage']['decision_record_count'],
                      'historical_question_uses': report['historical_source_usage']['question_use_count'],
                      'coverage_status': report['source_relations_profile_coverage']['status']}, ensure_ascii=False))


if __name__ == '__main__':
    main()

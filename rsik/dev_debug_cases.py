"""Freeze DEV native fixture inputs without Gold-table or holdout access.

The member universe is the archived native fixture chunk pool, not an historical
SSE expansion. No retrieval, provider, database, or model is invoked here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from pathlib import Path

SCHEMA = 'rsi-dev-debug-cases/v1'


def _read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def _binding(path):
    path = Path(path).resolve()
    raw = path.read_bytes()
    return {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}


def load_cases(path):
    """Load the private frozen bundle and reject changed or invalid native inputs."""
    value = _read(path)
    if value.get('schema') != SCHEMA or value.get('partition') != 'DEV':
        raise ValueError('DEV_CASE_BUNDLE_REQUIRED')
    cases = value.get('cases')
    if not isinstance(cases, list) or len(cases) != 14:
        raise ValueError('DEV_EXPECTED_14_CASES')
    if len({c['case_id'] for c in cases}) != 14:
        raise ValueError('DEV_DUPLICATE_CASE')
    if sum(c['cohort'] == 'failed' for c in cases) != 8 or sum(c['cohort'] == 'correct' for c in cases) != 6:
        raise ValueError('DEV_COHORT_DENOMINATOR')
    for case in cases:
        ids = [m['id'] for m in case['members']]
        if len(ids) != len(set(ids)) or not set(case['seed_ids']) <= set(ids):
            raise ValueError('DEV_NATIVE_SEED_OR_MEMBER_BINDING')
        for member in case['members']:
            chunk = case['chunks'][member['id']]
            if member['content'] != chunk['content'] or member['file_id'] != chunk['file_id']:
                raise ValueError('DEV_MEMBER_CONTENT_BINDING')
            digest = hashlib.sha256(member['content'].encode('utf-8')).hexdigest()
            if chunk.get('content_sha256') and digest != chunk['content_sha256']:
                raise ValueError('DEV_NATIVE_CONTENT_HASH')
        if case['historical_label'] not in {'CORRECT', 'PARTIAL', 'INCORRECT'}:
            raise ValueError('DEV_HISTORICAL_LABEL')
    return cases


def freeze_native_cases(fixture, selection_csv, output, metadata_output):
    """Select exactly 8 historical non-correct and 6 correct retrieval DEV rows.

    Only explicit DEV paths are read. No FINAL file or mixed Gold dataset is
    opened. Historic labels describe answers; they do not label member retention.
    """
    fixture, output, metadata_output = map(Path, (fixture, output, metadata_output))
    manifest_path = fixture / 'manifest.json'
    manifest = _read(manifest_path)
    if manifest.get('partition') != 'DEV':
        raise ValueError('DEV_FIXTURE_REQUIRED')
    if 'DEV' not in Path(selection_csv).name:
        raise ValueError('EXPLICIT_DEV_SELECTION_REQUIRED')
    source_cases_path = fixture / 'cases.json'
    expected = manifest.get('artifact_hashes', {}).get('cases.json')
    if expected != _binding(source_cases_path)['sha256']:
        raise ValueError('DEV_FIXTURE_CASES_HASH_CHANGED')
    source_cases = {r['qid']: r['case'] for r in _read(source_cases_path)}
    with Path(selection_csv).open(encoding='utf-8-sig', newline='') as stream:
        labels = list(csv.DictReader(stream))
    retrieval = [r for r in labels if r['item_id'] in source_cases and source_cases[r['item_id']]['seed_ids']]
    if len(retrieval) != 17:
        raise ValueError('DEV_EXPECTED_17_NATIVE_RETRIEVAL_CASES')
    failed = [r for r in retrieval if r['existing_answer_label'] != 'CORRECT'][:8]
    correct = [r for r in retrieval if r['existing_answer_label'] == 'CORRECT'][:6]
    if len(failed) != 8 or len(correct) != 6:
        raise ValueError('DEV_INSUFFICIENT_HISTORICAL_COHORTS')
    bindings = [_binding(manifest_path), _binding(source_cases_path), _binding(selection_csv)]
    cases = []
    for row in failed + correct:
        qid = row['item_id']
        native = source_cases[qid]
        files = {f['id']: f for f in native['files']}
        chunks = {c['id']: c for c in native['chunks']}
        if len(chunks) != len(native['chunks']):
            raise ValueError('DEV_NATIVE_DUPLICATE_CHUNK')
        members = [{**c, 'kb_id': files[c['file_id']]['kb_id']} for c in native['chunks']]
        cohort = 'correct' if row['existing_answer_label'] == 'CORRECT' else 'failed'
        cases.append({'case_id': qid, 'query': native['question'], 'cohort': cohort,
                      'historical_label': row['existing_answer_label'],
                      'historical_citation_label': row['existing_citation_label'],
                      'members': members, 'chunks': chunks, 'files': files,
                      'seed_ids': native['seed_ids'], 'native_case': native,
                      'selection': {'baseline': 'all'}, 'atoms': [],
                      'predicate_status': 'MISSING_ADMITTED_ATOM_PREDICATES',
                      'selector_input_status': 'AVAILABLE',
                      'member_universe': 'ARCHIVED_NATIVE_FIXTURE_CHUNK_POOL',
                      'original_historical_sse_pool_reproduction': False,
                      'source_bindings': bindings, 'selection_row': row})
    free_before = shutil.disk_usage(output.resolve().anchor).free
    value = {'schema': SCHEMA, 'partition': 'DEV', 'cases': cases,
             'selection_policy': 'DEV selection order; first 8 non-CORRECT and first 6 CORRECT retrieval rows',
             'source_bindings': bindings, 'lineage_id': manifest['lineage_id'],
             'source_fixture_review_status': manifest.get('review_status'),
             'formal_admission': False, 'model_calls': 0, 'production_changed': False,
             'limitations': ['Native chunk pool, not historical SSE expansion or original selector pool.',
                            'Historical answer labels are cohort labels only.',
                            'Source relation review is not admitted Gold atom retention authority; retention UNKNOWN.',
                            'No current retrieval coverage, generation correctness, or production qualification claim.']}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    checked = load_cases(output)
    metadata = {'schema': 'rsi-dev-debug-case-freeze/v1', 'status': 'FROZEN_NATIVE_DIAGNOSTIC_INPUTS',
                'private_bundle': _binding(output), 'source_bindings': bindings,
                'partition': 'DEV', 'case_count': len(checked), 'failed_cases': 8, 'correct_cases': 6,
                'retrieval_case_pool': 17, 'business_controls_excluded': 3,
                'member_universe': 'ARCHIVED_NATIVE_FIXTURE_CHUNK_POOL',
                'original_historical_sse_pool_reproduction': False,
                'audited_atom_retention': 'UNKNOWN', 'holdout_content_reads': 0,
                'mixed_gold_table_reads': 0, 'model_calls': 0, 'production_changed': False,
                'cases': [{'case_id': c['case_id'], 'cohort': c['cohort'],
                           'historical_label': c['historical_label'], 'members': len(c['members']),
                           'seeds': len(c['seed_ids']), 'native_seed_members_present': True} for c in checked],
                'resources': {'reused': ['existing DEV native fixture and DEV selection CSV', 'existing Python runtime'],
                              'created': [str(output), str(metadata_output)], 'deleted': [],
                              'new_database_index_environment_container': False,
                              'retention_reason': 'Frozen DEV diagnostic payload and auditable provenance',
                              'free_bytes_before': free_before,
                              'free_bytes_after': shutil.disk_usage(output.resolve().anchor).free},
                'limitations': value['limitations']}
    metadata_output.parent.mkdir(parents=True, exist_ok=True)
    metadata_output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return metadata


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fixture', required=True)
    parser.add_argument('--dev-selection', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--metadata-output', required=True)
    args = parser.parse_args()
    result = freeze_native_cases(args.fixture, args.dev_selection, args.output, args.metadata_output)
    print(json.dumps({'status': result['status'], 'cases': result['case_count'],
                      'failed': result['failed_cases'], 'correct': result['correct_cases'],
                      'retention': result['audited_atom_retention']}, ensure_ascii=True))


if __name__ == '__main__':
    main()

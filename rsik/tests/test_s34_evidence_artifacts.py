"""Portable checks for S34 evidence artifacts; no parsing, network or replay."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'docs' / 'evidence'


def load(name):
    return json.loads((EVIDENCE / (name + '.json')).read_bytes())


def test_native_sources_bind_existing_and_acquired_hashes_without_assigning_seed():
    inventory = load('s34-native-map')
    native = inventory['sources']
    acquisition = {r['source_id']: r['sha256'] for r in load('s34-new-sources')['sources']}
    assert len(acquisition) == inventory['source_count'] - 2 == 3
    assert {s['source_id']: s['source_sha256'] for s in native
            if s['source_id'] in acquisition} == acquisition
    assert {s['source_id']: len(s['members']) for s in native} == {
        'S34-PROM': 59, 'S34-OTEL': 21, 'S34-PYLOG': 113, 'OLD-DSM': 26, 'OLD-JZG': 2}
    members = [m for s in native for m in s['members']]
    assert len({m['member_id'] for m in members}) == len(members) == 221
    assert {m['seed'] for m in members} == {'UNKNOWN_NOT_RUN'}
    assert all(s['parse_repeat_identical'] for s in native)
    assert {s['effective_splitter'] for s in native} == {'NATIVE_PARSER_CHARACTER_FALLBACK'}
    assert all(inventory[k] == 0 for k in ('network_calls', 'model_calls', 'formal_index_writes'))


def test_localization_matches_are_separate_from_qualifying_controls():
    inventory = load('s34-native-map')
    counts = Counter()
    for source in inventory['sources']:
        ids = {member['member_id'] for member in source['members']}
        for span in source['span_checks']:
            assert {m['member_id'] for m in span['matches']} <= ids
            assert span['support_review'] == 'UNKNOWN_HUMAN_REVIEW_REQUIRED'
            counts[bool(span['matches'])] += 1
    assert counts == {True: 20, False: 3}
    coverage = load('s34-coverage')
    assert coverage['status'] == 'BLOCKED_DATA'
    mapping = coverage['S34_native_inventory']
    assert mapping['sha256'] == sha256((EVIDENCE / 's34-native-map.json').read_bytes()).hexdigest()
    assert mapping['qualifying_pair_executions'] == 0

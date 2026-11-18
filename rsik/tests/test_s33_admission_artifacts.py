"""Portable consistency checks for S3.3 review artifacts; no private data or execution."""
import csv
from collections import Counter, defaultdict
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'docs' / 'evidence'
PREFIX = 's3-v24-s33-'


def rows(name):
    with (EVIDENCE / (PREFIX + name + '.csv')).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def blob(name):
    return json.loads((EVIDENCE / (PREFIX + name + '.json')).read_text(encoding='utf-8-sig'))


def test_drafts_preserve_separate_twelve_question_denominator():
    audit = rows('question-audit')
    assert len(audit) == 12
    assert {row['question_id'] for row in audit} == {
        *(f'DRAFT-DSM-{n:02d}' for n in range(1, 9)),
        *(f'DRAFT-JZG-{n:02d}' for n in range(1, 5)),
    }
    assert {row['status'] for row in audit} == {'DRAFT_UNGRADED_NOT_ADMITTED'}
    assert {row['seed'] for row in audit} == {'UNKNOWN'}
    assert {row['independent_review_status'] for row in audit} == {'UNKNOWN_NOT_REVIEWED'}
    assert Counter(row['native_binding_status'] for row in audit) == {
        'UNKNOWN_TARGET_MEMBER_NOT_IN_CAPTURE_WINDOW': 8,
        'VERIFIED_SINGLE_SUPPORT_MEMBER_IN_CAPTURE': 4,
    }
    assert all(json.loads(row['exact_source_span_locators_json']) for row in audit)


def test_frozen_dev_gaps_preserve_both_grains_and_irreducible_subset():
    gaps = rows('atom-control-gap')
    assert Counter(g['suite'] for g in gaps) == {
        'main': 25, 'question-paraphrase': 25, 'forward-seed': 27, 'backward-seed': 15}
    assert {g['gap_status'] for g in gaps} == {'MISSING_NEGATIVE_CONTROL'}
    assert {g['fixture_partition'] for g in gaps} == {'DEV'}
    assert {g['control_execution_status'] for g in gaps} == {'NOT_EXECUTED'}
    assert len({(g['suite'], g['qid'], g['atom_id']) for g in gaps}) == 92
    assert len({(g['qid'], g['atom_id']) for g in gaps}) == 32
    hard = defaultdict(set)
    for gap in gaps:
        if gap['structural_classification'] == 'IRREDUCIBLE_SINGLE_CHUNK_SEED_BOUND':
            hard[gap['qid'], gap['atom_id']].add(gap['suite'])
    assert len(hard) == 9 and len({qid for qid, _ in hard}) == 4
    assert all(len(suites) == 4 for suites in hard.values())


def test_baseline_identity_receipt_is_not_an_execution_qualification():
    receipt = blob('baseline-readiness')
    assert receipt['status'] == 'BLOCKED_DATA' and receipt['execution'] == 'NOT_RUN'
    assert all(receipt[k] == 0 for k in ('new_qualifications','new_candidates','real_replays','paid_calls','original_budget_writes'))
    assert len(receipt['fixture_manifests']) == 4
    for part in receipt['fixture_manifests'].values():
        assert part['partition'] == 'DEV' and part['production_qualified'] is False
        counts = part['case_domain_counts']
        assert counts['retrieval_questions'] == 17 and counts['business_controls'] == 3
    assert receipt['original_budget_sha256'] == 'b1cbb1b5d178d6c716a508ed42bdb575370e9790f45c5b4bdfcd728b4ad3be52'


def test_feasibility_and_family_map_remain_data_blocked():
    feasibility, families = blob('dev20-r2-feasibility'), blob('source-family-map')
    assert feasibility['status'] == families['status'] == 'BLOCKED_DATA'
    assert feasibility['denominators']['fixed_selected_DEV20']['total'] == 20
    counts = families['source_eligibility_counts']
    assert counts['total'] == counts['unknown'] == 31
    assert counts['eligible'] == counts['blocked'] == 0
    assert feasibility['source_eligibility_counts'] == counts
    assert len(feasibility['class_deficits']) == 6
    assert all(record['minimum_additional_qualified_families_for_class'] == 2
               for record in feasibility['class_deficits'])
    assert feasibility['lower_bounds']['family_category_incidences_at_least'] == 12

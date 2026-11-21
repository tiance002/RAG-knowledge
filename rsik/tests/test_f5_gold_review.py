from hashlib import sha256
import json
from pathlib import Path

import pytest

from rsik.f5_gold_review import apply_gold_review, load_gold_review


def test_checked_in_review_binds_all_historically_flagged_cases():
    docs = Path(__file__).resolve().parents[2] / 'docs' / 'rsi-f5-audit-20261121'
    eligibility = json.loads((docs / 'dev200-eligibility.json').read_bytes())
    flagged = {row['qid']: row['ordinal'] for row in eligibility['rows'] if row['split'] == 'BLOCKED'}
    path = docs / 'dev200-gold-review.json'
    pin = sha256(path.read_bytes()).hexdigest()
    assert pin == 'b413e41b8816a20b9f693c0203d1b9a9ca65373c3fa802b66877476d46aaa27c'
    reviewed = load_gold_review(path, pin, eligibility['nested_manifest_sha256'],
                                eligibility['historical_population_sha256'], flagged)
    assert len(reviewed) == 12 and min(item['ordinal'] for item in reviewed.values()) > 60
    with pytest.raises(ValueError, match='hash mismatch'):
        load_gold_review(path, '0' * 64, eligibility['nested_manifest_sha256'],
                         eligibility['historical_population_sha256'], flagged)
    with pytest.raises(ValueError, match='exactly'):
        load_gold_review(path, pin, eligibility['nested_manifest_sha256'],
                         eligibility['historical_population_sha256'], {})


def test_reviewed_rubric_distinguishes_scoring_request_without_editing_gold():
    private = {'gold': 'reference only', 'delivered_citations': ['evidence']}
    item = {'private_input': private, 'rubric': 'fixed rubric',
            'public_receipt': {'input_sha256': 'input', 'case_receipt_sha256': 'answer'}}
    scored = apply_gold_review(item, 'Scope the factual claim.', 'review-pin')
    assert scored['private_input'] == private
    assert scored['public_receipt']['case_receipt_sha256'] == 'answer'
    assert scored['public_receipt']['input_sha256'] != item['public_receipt']['input_sha256']
    assert scored['public_receipt']['gold_review_sha256'] == 'review-pin'
    assert scored['rubric'].endswith('Scope the factual claim.')
    assert item['rubric'] == 'fixed rubric'

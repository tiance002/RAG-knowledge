"""Bind reviewed DEV200 scoring guidance to a frozen panel and issue rows."""

from hashlib import sha256
import json


def load_gold_review(path, expected_sha256, panel_sha256, population_sha256, flagged):
    raw = path.read_bytes()
    if not expected_sha256 or sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('Gold review hash mismatch')
    report = json.loads(raw)
    if (report.get('schema') != 'F5_DEV200_GOLD_REVIEW_V1'
            or report.get('panel_sha256') != panel_sha256
            or report.get('population_sha256') != population_sha256):
        raise ValueError('Gold review panel binding mismatch')
    rows = report['rows']
    if set(rows) != set(flagged):
        raise ValueError('Gold review must cover exactly the flagged cases')
    for key, row in rows.items():
        if (not isinstance(row, dict) or set(row) != {'ordinal', 'rule'}
                or type(row['ordinal']) is not int or row['ordinal'] != flagged[key]
                or not isinstance(row['rule'], str) or not 0 < len(row['rule'].strip()) <= 1000):
            raise ValueError('Gold review case binding or rule mismatch')
    return rows


def apply_gold_review(item, rule, review_sha256):
    """Add judge instructions while retaining the frozen Gold and delivered citations."""
    rubric = item['rubric'] + '\n\nReviewed scoring guidance for this question:\n' + rule
    rubric_sha256 = sha256(rubric.encode()).hexdigest()
    public = item['public_receipt']
    input_sha256 = sha256((public['input_sha256'] + rubric_sha256).encode()).hexdigest()
    return {**item, 'rubric': rubric, 'public_receipt': {
        **public, 'input_sha256': input_sha256, 'rubric_sha256': rubric_sha256,
        'known_source_or_gold_issue': True, 'gold_review_sha256': review_sha256,
    }}

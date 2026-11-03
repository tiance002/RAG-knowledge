"""SYNTHETIC accounting checks; no provider or private dataset required."""
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('affinity_analysis', ROOT / 'scripts/analyze_router_affinity.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_missing_expensive_arm_cannot_be_replaced_with_cheap_score():
    assert module.outcome('EXPENSIVE', 'PASS', 'UNKNOWN') == 'UNKNOWN'
    assert module.outcome('UNKNOWN', 'PASS', 'PASS') == 'UNKNOWN'


def test_unknown_is_separate_from_known_denominator():
    result = module.summarize([{'score': label} for label in ['PASS', 'PARTIAL', 'FAIL', 'UNKNOWN']], 'score')
    assert result['n'] == 4
    assert result['known_denominator'] == 3
    assert result['core_pass_rate_known'] == 1 / 3
    assert result['confirmed_pass_per_planned'] == 1 / 4


def test_duplicate_qid_is_rejected_instead_of_overwriting():
    with pytest.raises(ValueError, match='duplicate identity'):
        module.unique([{'qid': 'synthetic'}, {'qid': 'synthetic'}])


def test_missing_captured_features_stay_unknown():
    assert module.replay('Compare two things', {}, 'AFFINITY_V2')['role'] == 'UNKNOWN'

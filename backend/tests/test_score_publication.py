"""Score export must not turn absent judgments into numeric zero."""
import importlib.util
from pathlib import Path

import pytest


def test_score_export_unknowns_and_idempotency():
    path = Path(__file__).resolve().parents[2] / 'scripts' / 'publish_p0_scores.py'
    spec = importlib.util.spec_from_file_location('publish_p0_scores', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record = {'trace_id': 'trace', 'experiment_version': 'v1',
              'evidence': {'raw': 'UNKNOWN', 'context': 'PARTIAL'},
              'scores': {'core_correctness': None, 'empty': True, 'cost': None,
                         'evidence_recall': .5, 'tokens': {'total_tokens': 42}}}
    first = module.score_payloads([record])
    assert first == module.score_payloads([record])
    by_name = {row['name']: row for row in first}
    assert 'p0.core_correctness' not in by_name and 'p0.cost' not in by_name
    assert by_name['p0.evidence.raw']['value'] == 'UNKNOWN'
    assert by_name['p0.empty']['value'] == 1.
    assert by_name['p0.generation.total_tokens']['value'] == 42.
    with pytest.raises(ValueError, match='Duplicate score identity'):
        module.score_payloads([record, record])

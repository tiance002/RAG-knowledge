"""Deterministic evidence accounting; missing observations are never inferred."""
from enum import Enum

class EvidenceState(str, Enum):
    PRESENT = 'PRESENT'
    PARTIAL = 'PARTIAL'
    ABSENT = 'ABSENT'
    UNKNOWN = 'UNKNOWN'

STAGES = ('raw', 'merged', 'reranked', 'context', 'cited')
SCORE_FIELDS = ('core_correctness', 'faithfulness', 'citation_correctness', 'citation_recall', 'evidence_recall', 'empty', 'skipped', 'truncated', 'latency_ms', 'tokens', 'cost', 'remediation_success')

def evidence_state(gold, observed):
    if not gold or observed is None:
        return EvidenceState.UNKNOWN.value
    matched = set(gold) & set(observed)
    return (EvidenceState.ABSENT if not matched else EvidenceState.PRESENT if matched == set(gold) else EvidenceState.PARTIAL).value

def score_record(qid, experiment_version, gold, stages, scores=None):
    states = {stage: evidence_state(gold, stages.get(stage)) for stage in STAGES}
    coverage = {stage: (None if not gold or stages.get(stage) is None else len(set(gold) & set(stages[stage])) / len(set(gold))) for stage in STAGES}
    transitions = {}
    for left, right in zip(STAGES, STAGES[1:]):
        a, b = states[left], states[right]
        known = a != 'UNKNOWN' and b != 'UNKNOWN'
        transitions[f'{left}_to_{right}'] = {
            'coverage_delta': None if not known else coverage[right] - coverage[left],
            'retained': None if not known else a == 'PRESENT' and b == 'PRESENT',
            'recovered': None if not known else a in ('ABSENT', 'PARTIAL') and b == 'PRESENT',
            'lost': None if not known else a == 'PRESENT' and b != 'PRESENT',
        }
    return {'qid': qid, 'experiment_version': experiment_version, 'evidence': states,
            'coverage': coverage, 'transitions': transitions, 'scores': {k: (scores or {}).get(k) for k in SCORE_FIELDS}}

def normalize_location(source_format, location=None):
    location = location or {}
    fmt = (source_format or '').lower().lstrip('.')
    keys = ('page', 'bbox', 'sheet', 'row', 'column', 'cell', 'cell_range', 'heading', 'heading_path', 'table_row', 'line', 'source_sha256', 'index_version')
    return {'format': fmt or None, 'location': {**location, **{k: location.get(k) for k in keys}}}


def compare_records(records):
    result = {}
    for record in records:
        key = (record['qid'], record['experiment_version'])
        if key in result:
            raise ValueError(f'duplicate comparison key: {key}')
        result[key] = record
    return result

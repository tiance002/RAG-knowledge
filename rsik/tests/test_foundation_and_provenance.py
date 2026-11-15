"""Synthetic guard tests; no model calls, real corpus or sandbox execution."""
import copy
import hashlib
import importlib.util
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from rsik.provenance import assess_provenance
from rsik.foundation import check_split, source_separation
from run_rsi_offline import assess

spec = importlib.util.spec_from_file_location('foundation_builder', ROOT / 'scripts/build_rsi_v22_foundation.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


def fixture():
    text = 'A: 120 yuan, annual.\nB: 120 yuan, monthly.'
    boundary = text.index('B:')
    digest = hashlib.sha256(text.encode()).hexdigest()
    chunks = [{'id': cid, 'file_id': 'file', 'content': body,
               'content_sha256': hashlib.sha256(body.encode()).hexdigest()}
              for cid, body in [('a', text[:boundary]), ('b', text[boundary:])]]
    case = {'files': [{'id': 'file', 'kb_id': 'kb'}], 'chunks': chunks,
            'allowed_kb_ids': ['kb'], 'seed_ids': ['b']}
    source = {'source_sha256': digest, 'file_id': 'file', 'kb_id': 'kb',
              'member_spans': {'a': {'source_sha256': digest, 'start': 0, 'end': boundary},
                               'b': {'source_sha256': digest, 'start': boundary, 'end': len(text)}},
              'facts': [{'fact_id': 'a-value', 'file_id': 'file', 'kb_id': 'kb',
                         'source_sha256': digest, 'text': '120 yuan', 'start': 3, 'end': 11,
                         'supporting_chunk_ids': ['a'], 'normalization': 'identity-unicode-codepoints-v1',
                         'rule': 'exact_clause_in_source_bound_member'}]}
    def capture(cid):
        body = next(c['content'] for c in chunks if c['id'] == cid)
        return {'status': 'OK', 'members': [{'id': cid, 'file_id': 'file', 'kb_id': 'kb', 'content': body}],
                'context': body, 'messages': [{'content': body}], 'request_calls': 1,
                'proof': [{'id': cid, 'context_start': 0, 'context_end': len(body),
                           'prompt_start': 0, 'prompt_end': len(body)}]}
    return text.encode(), case, source, capture


def test_source_bound_success_covers_actual_offsets():
    raw, case, source, capture = fixture()
    result = assess_provenance(case, capture('a'), source, raw)
    assert result['status'] == 'PRESENT'
    assert result['facts'][0]['supporting_member_ids'] == ['a']


def test_same_file_other_object_and_condition_cannot_fake_fact():
    raw, case, source, capture = fixture()
    delivered = capture('b')
    expected = {'facts': [{'file_id': 'file', 'kind': 'value', 'text': '120 yuan'}]}
    # Demonstrates the real protected evaluator's limited substring semantics.
    original = assess(case, delivered, expected, 'B')
    assert original['facts'][0]['status'] == 'PRESENT' and not original['faults']
    additional = assess_provenance(case, delivered, source, raw)
    assert additional['status'] == 'ABSENT'
    assert additional['facts'][0]['supporting_member_ids'] == []


@pytest.mark.parametrize('fault', ['wrong_file', 'wrong_kb', 'forged_text', 'missing_proof',
                                  'wrong_prompt_offset', 'wrong_context_offset', 'wrong_source_hash',
                                  'unknown_rule', 'unknown_normalization', 'duplicate_delivery'])
def test_untrusted_evidence_never_becomes_present(fault):
    raw, case, source, capture = fixture()
    delivered = capture('a')
    if fault == 'wrong_file': delivered['members'][0]['file_id'] = 'wrong'
    elif fault == 'wrong_kb': delivered['members'][0]['kb_id'] = 'wrong'
    elif fault == 'forged_text': delivered['members'][0]['content'] += ' forged'
    elif fault == 'missing_proof': delivered['proof'] = []
    elif fault == 'wrong_prompt_offset': delivered['proof'][0]['prompt_start'] = 2
    elif fault == 'wrong_context_offset': delivered['proof'][0]['context_end'] = 2
    elif fault == 'wrong_source_hash': raw = b'not original'
    elif fault == 'unknown_rule': source['facts'][0]['rule'] = 'llm'
    elif fault == 'unknown_normalization': source['facts'][0]['normalization'] = 'strip-numbers'
    elif fault == 'duplicate_delivery':
        delivered['members'] *= 2
        delivered['proof'] *= 2
    assert assess_provenance(case, delivered, source, raw)['status'] == 'UNKNOWN'


def test_builder_never_splits_required_clauses_or_drops_source_characters():
    text = 'a' * 17 + 'NECESSARY_CLAUSE' + 'b' * 20
    spans = builder.segment(text, [(17, 33)], target=20)
    assert ''.join(text[start:end] for start, end in spans) == text
    assert all(not (start < 17 < end < 33) for start, end in spans)
    assert any(start <= 17 and 33 <= end for start, end in spans)


def test_builder_handles_overlapping_source_predicates():
    text = 'x' * 80
    spans = builder.segment(text, [(15, 35), (30, 50)], target=20)
    assert spans[0] == (0, 50)
    assert spans[-1][-1] == len(text)


def synthetic_foundation(tmp_path):
    items, sources = [], {}
    for index in range(20):
        body = f'  SYNTHETIC source {index}: value {index}.  '
        data = body.encode()
        digest = hashlib.sha256(data).hexdigest()
        left = body.index('value')
        right = body.index('.') + 1
        item = {'content': {'qid': f'SYNTHETIC-{index}', 'question': f'SYNTHETIC question {index}',
                            'evidence': [{'quote': body[left:right], 'original_location':
                                         {'char_start_0based': left, 'char_end_exclusive_0based': right}}]},
                'core_sha256': 'SYNTHETIC', 'content_review': {'decision': 'PASS', 'reviewer': 'SYNTHETIC'}}
        source = {'sha256': digest, 'file_id': f'file-{index}', 'kb_id': 'synthetic',
                  'name': f'source-{index}.md', 'source_path': 'SYNTHETIC'}
        items.append((item, source))
        sources[digest] = data
    builder.build_split(items, sources, tmp_path, 'DEV')
    return tmp_path


def test_builder_strips_only_boundary_whitespace_with_exact_source_coordinates(tmp_path):
    import json
    directory = synthetic_foundation(tmp_path)
    cases = json.loads((directory / 'cases.json').read_text())
    provenance = json.loads((directory / 'provenance.restricted.json').read_text())
    result = check_split(directory, expected_count=20)
    assert result['questions'] == result['source_groups'] == 20
    for item in cases:
        proof = provenance[item['qid']]
        source = (directory / proof['source_relative_path']).read_text()
        for chunk in item['case']['chunks']:
            loc = proof['member_spans'][chunk['id']]
            assert chunk['content'] == chunk['content'].strip() == source[loc['start']:loc['end']]
    assert result['human_review'] is False


@pytest.mark.parametrize('artifact', ['cases.json', 'evaluation.json'])
def test_freeze_rejects_changed_json_even_if_it_still_parses(tmp_path, artifact):
    import json
    directory = synthetic_foundation(tmp_path)
    value = json.loads((directory / artifact).read_text())
    if artifact == 'cases.json': value[0]['case']['question'] = 'tampered'
    else: value[next(iter(value))]['forbidden'] = ['tampered']
    (directory / artifact).write_text(json.dumps(value))
    with pytest.raises(ValueError, match='FIXTURE_TAMPERED'):
        check_split(directory, expected_count=20)


def test_independence_screen_ignores_blank_lines_but_rejects_same_document(tmp_path):
    a, b = tmp_path / 'train/sources', tmp_path / 'holdout/sources'
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / 'one.md').write_text('\n\n# Same actual title\nDifferent body.')
    (b / 'two.md').write_text('\n# Same actual title\nOther body.')
    result = source_separation(a.parent, b.parent)
    assert result['status'] == 'BLOCKED' and result['collisions'] == 1

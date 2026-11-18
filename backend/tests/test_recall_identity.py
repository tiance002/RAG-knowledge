"""Recall counts use original seed IDs, while citations use actual source bodies."""
import hashlib

import pytest

from app.schemas import RecallResult, RecallTestResponse
from app.services.recall_evaluation import evaluate_recall_identity, evaluate_seed_qrels, recall_identity_fields


def source(source_id, content=None):
    body = content if content is not None else 'body ' + source_id
    return {'id': source_id, 'content': body,
            'content_hash': hashlib.sha256(body.encode()).hexdigest(),
            'file_id': 'file', 'kb_id': 'kb'}


def result(seed_ids, parent_id):
    return RecallResult(chunkId=seed_ids[0], seed_child_ids=seed_ids,
        parent_chunk_id=parent_id, context_group_id='group:' + parent_id,
        representative_child_id=seed_ids[0], score_child_id=seed_ids[-1],
        hit_children=[source(x) for x in seed_ids],
        citation_sources=[source(parent_id)], source_mapping_status='VERIFIED',
        score=.8, content='expanded context', fileName='source.txt', kbName='kb', location='')


def test_ten_seed_hits_in_six_groups_are_not_six_recalled_chunks():
    groups = [result(['s0', 's1', 's2', 's3', 's4'], 'p0')]
    groups.extend(result(['s' + str(n)], 'p' + str(n)) for n in range(5, 10))
    summary = evaluate_recall_identity(groups)
    assert summary['mapping_status'] == 'VERIFIED'
    assert summary['matched_seed_ids'] == ['s' + str(n) for n in range(10)]
    assert summary['matched_seed_count'] == 10
    assert summary['context_group_count'] == summary['citation_source_count'] == 6
    assert set(summary['matched_seed_ids']).isdisjoint(summary['citation_source_ids'])
    response = RecallTestResponse(results=groups, query_time=1, evaluation=summary)
    serialized = response.model_dump()
    assert serialized['results'][0]['representative_child_id'] == 's0'
    assert serialized['results'][0]['score_child_id'] == 's4'
    assert serialized['results'][0]['citation_sources'][0]['id'] == 'p0'
    assert serialized['results'][0]['hit_children'][4]['id'] == 's4'


def test_legacy_representative_id_cannot_establish_seed_coverage():
    summary = evaluate_recall_identity([{'chunkId': 's0', 'content': 'parent body',
        'source_metadata': {'source_type': 'context_group', 'members': [{'chunk_id': 'parent'}]}}])
    assert summary['mapping_status'] == 'UNKNOWN'
    assert summary['matched_seed_ids'] == summary['citation_source_ids'] == []
    assert len(summary['unknown_mappings']) == 1


def test_hash_mismatch_or_missing_seed_is_not_a_verified_hit():
    row = result(['s0', 's1'], 'parent').model_dump()
    row['hit_children'][1]['content'] = 'altered text'
    summary = evaluate_recall_identity([row])
    assert summary['mapping_status'] == 'UNKNOWN' and summary['matched_seed_ids'] == []
    row = result(['s0', 's1'], 'parent').model_dump()
    row['hit_children'].pop()
    assert evaluate_recall_identity([row])['matched_seed_ids'] == []


def test_seed_fallback_cites_seed_not_unselected_parent():
    row = result(['s0'], 'parent').model_dump()
    row['citation_sources'] = row['hit_children']
    summary = evaluate_recall_identity([row])
    assert summary['matched_seed_ids'] == summary['citation_source_ids'] == ['s0']


def test_identity_fields_preserve_group_not_best_score_identity():
    row = {'id': 's0', 'representative_child_id': 's0', 'score_child_id': 's1',
           'context_group': {'context_group_id': 'group:parent'},
           'hit_children': [source('s0'), source('s1')], 'citation_sources': [source('parent')]}
    fields = recall_identity_fields(row)
    assert fields['context_group_id'] == 'group:parent'
    assert fields['representative_child_id'] != fields['score_child_id']
    assert fields['citation_sources'] == [source('parent')]


def test_qrels_recall_uses_seed_denominator_and_refuses_legacy_guess():
    row = result(['s0', 's1'], 'parent')
    report = evaluate_seed_qrels([row], ['s0', 's1', 'absent'])
    assert report['gold_seed_count'] == 3 and report['seed_recall'] == 2 / 3
    assert 'parent' not in report['matched_gold_seed_ids']
    legacy = evaluate_seed_qrels([{'chunkId': 's0'}], ['s0'])
    assert legacy['seed_recall'] is None and legacy['seed_recall_status'] == 'UNKNOWN'


def test_conflicting_same_id_source_is_excluded_from_asserted_coverage():
    left = result(['s0'], 'parent').model_dump()
    right = result(['s0'], 'parent').model_dump()
    right['hit_children'] = [source('s0', 'different original body')]
    report = evaluate_recall_identity([left, right])
    assert report['mapping_status'] == 'UNKNOWN' and report['matched_seed_ids'] == []


@pytest.mark.parametrize('field,value', [
    ('context_group_id', 'group:unrelated'),
    ('representative_child_id', 'unrelated'),
    ('score_child_id', 'unrelated'),
    ('parent_chunk_id', 'unrelated'),
    ('chunkId', 'parent'),
])
def test_group_identity_tampering_cannot_report_verified_seed_coverage(field, value):
    row = result(['s0', 's1'], 'parent').model_dump()
    row[field] = value
    report = evaluate_recall_identity([row])
    assert report['mapping_status'] == 'UNKNOWN' and report['matched_seed_ids'] == []


@pytest.mark.parametrize('source_kind', ['hit_children', 'citation_sources'])
@pytest.mark.parametrize('scope_field', ['file_id', 'kb_id'])
def test_valid_self_hash_does_not_authorize_cross_scope_mapping(source_kind, scope_field):
    row = result(['s0', 's1'], 'parent').model_dump()
    row[source_kind][-1][scope_field] = 'another-scope'
    report = evaluate_recall_identity([row])
    assert report['mapping_status'] == 'UNKNOWN' and report['matched_seed_ids'] == []


def test_nested_group_source_mapping_must_agree_with_public_sources():
    row = result(['s0', 's1'], 'parent').model_dump()
    row['context_group'] = {'context_group_id': 'group:parent', 'parent_chunk_id': 'parent',
        'representative_child_id': 's0', 'score_child_id': 's1', 'seed_child_ids': ['s0', 's1'],
        'source_members': [source('another-parent')]}
    assert evaluate_recall_identity([row])['mapping_status'] == 'UNKNOWN'

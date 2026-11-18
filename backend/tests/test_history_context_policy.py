"""History reference policy: real text/tokenizer, no external providers."""
from types import SimpleNamespace
from app.services.conversation_context import merge_history_citations


def candidate(cid, body='Atlas budget is 420 units', turn='latest', score=0.9):
    return {'fileId': 'file', 'chunk_id': cid, 'content': body,
            'history_turn_id': turn, 'score': score, 'origin': 'HISTORY_REUSE'}


def test_only_newest_reference_bearing_turn_is_supplemented():
    current = [candidate('fresh')]
    refs = [candidate('old', turn='old'), candidate('latest', turn='latest')]
    merged, added = merge_history_citations(current, refs, 'Atlas budget')
    assert [x['chunk_id'] for x in merged] == ['fresh', 'latest']
    assert len(added) == 1 and added[0]['match_type'] == 'history'
    assert current == [candidate('fresh')]


def test_discount_does_not_forge_new_rerank_score():
    original = candidate('history', score=0.8)
    original['rerank_score'] = 0.7
    _, added = merge_history_citations([], [original], 'Atlas budget')
    chosen = added[0]
    assert chosen['history_original_score'] == 0.8
    assert chosen['history_discounted_score'] == 0.48
    assert chosen['rerank_score'] == 0.7 and chosen['score'] == 0.8
    assert 'history_score_discount' not in original


def test_max_three_dedup_and_independent_current_preserved():
    rows = [candidate('c' + str(n)) for n in range(6)]
    current = [rows[0]]
    merged, added = merge_history_citations(current, rows, 'Atlas budget')
    assert merged[0] == rows[0]
    assert [c['chunk_id'] for c in added] == ['c1', 'c2', 'c3']


def test_unrelated_refs_not_selected_and_unknown_score_not_fabricated():
    assert merge_history_citations([], [candidate('x')], 'satellite telemetry')[1] == []
    _, added = merge_history_citations([], [candidate('x', score=None)], 'Atlas')
    assert added[0]['history_discounted_score'] is None


def test_smaller_set_overlap_matches_reference_mechanism():
    _, added = merge_history_citations([], [candidate('x', body='Atlas budget')],
        'Atlas budget astronomy satellite telemetry propulsion insurance administration')
    assert len(added) == 1 and added[0]['history_similarity'] == 1.0


def test_existing_body_budget_is_not_exceeded(monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(CONTEXT_MAX_CHARS=5))
    current = [candidate('fresh',body='Atlas')]
    merged, added = merge_history_citations(current, [candidate('history')], 'Atlas')
    assert merged == current and added == []

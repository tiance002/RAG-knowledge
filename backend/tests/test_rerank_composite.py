"""Offline ranking contracts; no model calls."""
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from app.services.rerank import batch_rerank, apply_composite_scores


def test_composite_topk_can_prefer_stronger_retrieval_without_overwriting_scores():
    rows=[{'id':'a','content':'first','score':.1}, {'id':'b','content':'second','score':.9}]
    service=SimpleNamespace(rerank=Mock(return_value=[{'index':0,'score':.9},{'index':1,'score':.8}]))
    selected=batch_rerank('q',rows,service,None,top_k=1)
    assert selected[0]['id']=='b'
    assert selected[0]['composite_score']==pytest.approx(.85)
    assert selected[0]['rerank_score']==.8 and selected[0]['score']==.9
    # Existing model threshold remains meaningful even with strong base scores.
    selected=batch_rerank('q',rows,service,None,top_k=2,score_threshold=.85)
    assert [r['id'] for r in selected]==['a']


def test_raw_lexical_scale_and_missing_model_score_are_explicit():
    rows=[{'score':10.,'rerank_score':.8}, {'score':30.,'rerank_score':.8},
          {'score':20.,'rerank_score':None}]
    apply_composite_scores(rows,SimpleNamespace())
    assert [r['retrieval_base_score'] for r in rows]==[0.,1.,.5]
    assert rows[1]['composite_score']>rows[0]['composite_score']
    assert rows[2]['composite_score'] is None
    assert rows[2]['composite_score_status']=='UNKNOWN_MODEL_SCORE'

"""Cached results lacking index/model provenance must never be reused."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.modules.setdefault('chromadb', MagicMock())
sys.modules.setdefault('chromadb.config', MagicMock())
from app.services.retrieval import RetrievalService, RetrievalCache


def make_service():
    service = RetrievalService.__new__(RetrievalService)
    service.cache = RetrievalCache(enabled=True)
    return service


def run(service, session, **options):
    return asyncio.run(service.search(session, 'question', options.pop('kb_ids', ['kb']),
                      search_mode=options.pop('search_mode', 'hybrid'), top_k=5,
                      use_cache=True, log_retrieval=False, **options))


def test_repeated_success_reexecutes_and_preserves_status_and_metrics():
    service = make_service()
    service._hybrid_search = AsyncMock(return_value=([{'id':'fresh','content':'evidence','score':0.8}],
                                                      {'vector_count': 1, 'bm25_count': 1}, False))
    # Even a legacy cache entry with the same key cannot override current work.
    service.cache.set('question', ['kb'], 'hybrid', 5, [{'id':'old','content':'stale','score':1.0}])
    first, m1 = run(service, None)
    second, m2 = run(service, None)
    assert [r['id'] for r in first] == [r['id'] for r in second] == ['fresh']
    assert m1['retrieval_status'] == m2['retrieval_status'] == 'SUCCESS'
    assert m1['vector_count'] == m2['vector_count'] == 1
    assert service._hybrid_search.await_count == 2
    assert service.cache.get('question', ['kb'], 'hybrid', 5)[0]['id'] == 'old'


def test_changes_in_scope_parameters_model_and_index_are_not_reused():
    service = make_service()
    state = SimpleNamespace(model='model-a', index='index-a', content='first')
    async def search(session, query, kb_ids, top_k, threshold, vector_weight, bm25_weight):
        text = f'{kb_ids[0]}|{threshold}|{vector_weight}|{bm25_weight}|{session.model}|{session.index}|{session.content}'
        return [{'id':text, 'content':text, 'score':0.8}], {'vector_count':1}, False
    service._hybrid_search = AsyncMock(side_effect=search)
    queries = [run(service, state, score_threshold=0.1, vector_weight=0.7, bm25_weight=0.3)[0][0]['id'],
               run(service, state, score_threshold=0.2, vector_weight=0.7, bm25_weight=0.3)[0][0]['id'],
               run(service, state, score_threshold=0.1, vector_weight=0.5, bm25_weight=0.5)[0][0]['id'],
               run(service, state, kb_ids=['other'], score_threshold=0.1, vector_weight=0.7, bm25_weight=0.3)[0][0]['id']]
    state.model='model-b'
    queries.append(run(service, state, score_threshold=0.1, vector_weight=0.7, bm25_weight=0.3)[0][0]['id'])
    state.index='index-b'
    queries.append(run(service, state, score_threshold=0.1, vector_weight=0.7, bm25_weight=0.3)[0][0]['id'])
    state.content='changed'
    queries.append(run(service, state, score_threshold=0.1, vector_weight=0.7, bm25_weight=0.3)[0][0]['id'])
    assert len(set(queries)) == len(queries) == 7
    assert service._hybrid_search.await_count == 7


def test_partial_failure_and_unknown_do_not_poison_later_success():
    service = make_service()
    rows = [{'id':'c', 'content':'evidence','score':0.8}]
    service._hybrid_search = AsyncMock(side_effect=[(rows, {'vector_count':0}, True),
                                                      (rows, {'vector_count':1}, False)])
    _, failed = run(service, None)
    _, recovered = run(service, None)
    assert failed['retrieval_status'] == 'RETRIEVAL_ERROR'
    assert recovered['retrieval_status'] == 'SUCCESS'
    assert service._hybrid_search.await_count == 2
    service._hybrid_search = AsyncMock(side_effect=TimeoutError('provider outcome unknown'))
    service._vector_search = AsyncMock()
    try:
        run(service, None)
        assert False, 'unknown provider failure must propagate'
    except TimeoutError:
        pass
    service._vector_search.assert_not_awaited()

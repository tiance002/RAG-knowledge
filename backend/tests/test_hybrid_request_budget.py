"""Real SQLite/postings; embedding and vector boundaries are controlled doubles."""
import asyncio
import hashlib
import json
from datetime import datetime
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app.models import CustomModel, DocumentChunk, FileDocument, FileIndexBuild, KnowledgeBase, ModelType
from app.services import retrieval
from app.services.bm25 import BM25Index
from app.services.bounded_lexical_executor import managed_lexical_executor
from app.services.retrieval_budget import RetrievalBudget, RequestQueryVectors, fair_depths


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setenv('RAG_OBSERVABILITY_ENABLED', 'false')
    monkeypatch.setenv('RAG_CHROMA_EXECUTOR_ENABLED', 'false')
    monkeypatch.delenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', raising=False)
    for name in ('VECTOR_DEPTH', 'BM25_INITIAL_DEPTH', 'BM25_SUPPLEMENT_DEPTH',
                 'MAX_VARIANTS', 'MAX_VARIANT_TARGET_PAIRS', 'TARGET_INFLIGHT', 'DEADLINE_SECONDS'):
        monkeypatch.delenv('RAG_RETRIEVAL_' + name, raising=False)


@pytest.fixture
def corpus(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'corpus.db'),
                           connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(engine)
    index = BM25Index()
    with Session(engine) as db:
        db.add(CustomModel(id='embedding', name='controlled', model_type=ModelType.EMBEDDING,
            base_url='http://embedding.invalid/v1', api_key='synthetic-never-emitted',
            model_name='same-space', context_length=4096))
        for kb in ('a', 'b', 'outside'):
            db.add(KnowledgeBase(id=kb, name=kb, embedding_model='embedding', vlm_model=''))
            db.add(FileDocument(id='file-' + kb, kb_id=kb, name=kb + '.txt', local_path='fixture', size=0))
            for number, content in enumerate(('alpha original 100', 'alpha original 200',
                                               'beta 300', 'gamma 400')):
                db.add(DocumentChunk(id=f'{kb}-{number}', file_id='file-' + kb, content=content))
        db.commit()
        for kb in ('a', 'b', 'outside'):
            for number in range(4):
                chunk = db.get(DocumentChunk, f'{kb}-{number}')
                index.index_chunk(db, chunk.id, chunk.content, kb)
        db.commit()
    svc = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    svc.bm25_index = index
    svc._embed_query_for_kb = AsyncMock(return_value=[.1, .2])
    calls = []
    async def vector(**kwargs):
        calls.append(kwargs)
        kb = kwargs['kb_id']
        return [{'id': kb + '-0', 'score': .9}, {'id': kb + '-1', 'score': .8}][:kwargs['top_k']]
    svc._query_vector_store = vector
    yield engine, svc, calls
    engine.dispose()


async def search(corpus, *, top_k=3, original_query=None, kb_ids=('a', 'b')):
    engine, svc, calls = corpus
    with Session(engine) as db:
        return await svc.search(db, 'alpha', list(kb_ids), top_k=top_k,
            original_query=original_query, log_retrieval=False)


def test_request_totals_and_deterministic_allocations_cover_base_and_supplement(corpus, monkeypatch):
    # Explicit whole-request overrides exercise aggressive small-budget behavior
    # independently of the production scope-aware default policy.
    monkeypatch.setenv('RAG_RETRIEVAL_VECTOR_DEPTH', '150')
    monkeypatch.setenv('RAG_RETRIEVAL_BM25_INITIAL_DEPTH', '50')
    _, svc, calls = corpus
    raw_search = svc.bm25_index.search
    svc.bm25_index.search = Mock(wraps=raw_search)
    monkeypatch.setattr(retrieval, 'expand_queries', lambda _: ['beta', 'gamma', 'delta', 'epsilon', 'zeta'])
    async def run():
        async with managed_lexical_executor():
            return await search(corpus, top_k=20, original_query='original', kb_ids=('b', 'a', 'a'))
    rows, metrics = asyncio.run(run())
    assert sum(call['top_k'] for call in calls) == 150
    assert {call['kb_id']: call['top_k'] for call in calls} == {'a': 75, 'b': 75}
    searches = svc.bm25_index.search.call_args_list
    initial = [call.kwargs for call in searches if call.kwargs['query'] in ('alpha', 'original')]
    supplemental = [call.kwargs for call in searches if call.kwargs['query'] not in ('alpha', 'original')]
    assert sum(call['top_k'] for call in initial) == 50
    assert sum(call['top_k'] for call in supplemental) == 50
    assert len(initial) == 4 and len(supplemental) == 8
    assert len({(call['query'], call['kb_id']) for call in initial + supplemental}) == 12
    assert metrics['expansion_pairs_capped'] and metrics['expansion_variant_target_pairs'] == 8
    assert sum(row['bm25_supplement_depth'] for row in metrics['target_allocations']) == 50
    assert all(row['kb_id'] in ('a', 'b') for row in rows)
    assert svc._embed_query_for_kb.await_count == 2


def test_one_kb_one_base_keeps_150_50_scores_and_rankings(corpus, monkeypatch):
    engine, svc, calls = corpus
    monkeypatch.setattr(retrieval, 'expand_queries', Mock(side_effect=AssertionError('sufficient union')))
    rows, metrics = asyncio.run(search(corpus, top_k=2, kb_ids=('a',)))
    with Session(engine) as db:
        lexical = svc._bm25_query_results(db, ['alpha'], ['a'], 50)
        reference = retrieval.weighted_rrf([
            {'id': 'a-0', 'score': .9, 'kb_id': 'a'},
            {'id': 'a-1', 'score': .8, 'kb_id': 'a'},
        ], retrieval.aggregate_bm25_results(lexical))
        reference = retrieval.deduplicate_results(svc._enrich_results(db, reference[:2], ['a']))
    assert rows == reference and calls[0]['top_k'] == 150
    assert metrics['bm25_initial_request_depth'] == 50


@pytest.mark.parametrize('targets,base_queries,vector_total,lexical_total', [
    (1, 1, 150, 50), (2, 1, 300, 100), (10, 1, 1500, 500),
    (1, 2, 150, 100), (2, 2, 300, 200), (10, 2, 1500, 1000),
])
def test_scope_aware_defaults_preserve_independent_route_depths(targets, base_queries, vector_total, lexical_total):
    budget = RetrievalBudget.from_environment(target_count=targets, base_query_count=base_queries)
    assert (budget.vector_depth, budget.lexical_initial_depth) == (vector_total, lexical_total)
    assert budget.lexical_supplement_depth == 50
    assert budget.max_variant_target_pairs == min(64, max(8, targets))
    assert budget.target_inflight == 2 and budget.timeout_seconds == 30
    assert budget.metrics()['retrieval_target_count'] == targets
    assert budget.metrics()['retrieval_base_query_count'] == base_queries
    # Direct construction retains its existing one-target dataclass defaults.
    assert (RetrievalBudget().vector_depth, RetrievalBudget().lexical_initial_depth) == (150, 50)


def test_explicit_depths_override_scope_defaults_as_totals(monkeypatch):
    monkeypatch.setenv('RAG_RETRIEVAL_VECTOR_DEPTH', '200')
    monkeypatch.setenv('RAG_RETRIEVAL_BM25_INITIAL_DEPTH', '75')
    budget = RetrievalBudget.from_environment(target_count=10, base_query_count=2)
    assert budget.vector_depth == 200 and budget.lexical_initial_depth == 75


def test_hybrid_passes_unique_target_and_distinct_base_counts_to_defaults(corpus, monkeypatch):
    _, svc, calls = corpus
    raw_search = svc.bm25_index.search
    svc.bm25_index.search = Mock(wraps=raw_search)
    monkeypatch.setattr(retrieval, 'expand_queries', lambda _: [])
    _, metrics = asyncio.run(search(corpus, top_k=20, original_query='original', kb_ids=('b', 'a', 'a')))
    assert sum(call['top_k'] for call in calls) == 300
    assert sum(call.kwargs['top_k'] for call in svc.bm25_index.search.call_args_list) == 200
    assert metrics['retrieval_target_count'] == 2 and metrics['retrieval_base_query_count'] == 2
    assert metrics['retrieval_default_policy'] == 'baseline_depths_bounded_initial_v3'
    calls.clear()
    svc.bm25_index.search.reset_mock()
    _, metrics = asyncio.run(search(corpus, top_k=20, original_query='  ALPHA  ', kb_ids=('b', 'a', 'a')))
    assert sum(call['top_k'] for call in calls) == 300
    assert sum(call.kwargs['top_k'] for call in svc.bm25_index.search.call_args_list) == 100
    assert metrics['retrieval_base_query_count'] == 1


def test_invalid_sources_cannot_suppress_expansion_or_take_final_topk(corpus, monkeypatch):
    engine, svc, calls = corpus
    with Session(engine) as db:
        db.add(FileDocument(id='deleted', kb_id='a', name='deleted', local_path='x', size=0,
                            deleted_at=datetime.now()))
        db.add(DocumentChunk(id='deleted-hit', file_id='deleted', content='alpha 999'))
        db.add(DocumentChunk(id='parent-hit', file_id='file-a', content='alpha 998', chunk_type='parent'))
        db.commit()
    async def vector(**kwargs):
        return [{'id': ident, 'score': 1.0} for ident in
                ('outside-0', 'parent-hit', 'deleted-hit', 'missing', 'a-0')]
    svc._query_vector_store = vector
    expand = Mock(return_value=['beta'])
    monkeypatch.setattr(retrieval, 'expand_queries', expand)
    rows, metrics = asyncio.run(search(corpus, top_k=3, kb_ids=('a',)))
    assert metrics['initial_candidate_count'] == 2
    assert metrics['expansion_triggered'] and expand.call_count == 1
    assert {row['id'] for row in rows} == {'a-0', 'a-1', 'a-2'}


def test_concurrency_one_two_keep_parallel_candidates_and_allocations(corpus, monkeypatch):
    _, svc, calls = corpus
    original_vector = svc._query_vector_store
    active = peak = 0
    async def vector(**kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(.02 if kwargs['kb_id'] == 'a' else .01)
            return await original_vector(**kwargs)
        finally:
            active -= 1
    svc._query_vector_store = vector
    async def run():
        async with managed_lexical_executor():
            parallel, pmetrics = await search(corpus)
            monkeypatch.setenv('RAG_RETRIEVAL_TARGET_INFLIGHT', '1')
            single, ometrics = await search(corpus)
        assert parallel == single
        assert pmetrics['target_allocations'] == ometrics['target_allocations']
        assert pmetrics['hybrid_initial_parallel'] and ometrics['hybrid_initial_parallel']
        assert ometrics['hybrid_execution_reason'] == 'managed_independent_reader'
        assert pmetrics['initial_routes_overlap_ms'] > 0
        assert ometrics['initial_routes_overlap_ms'] > 0
    asyncio.run(run())
    assert peak == 2 and active == 0


def test_deadline_returns_completed_valid_evidence_and_keeps_real_lexical_capacity(corpus, monkeypatch):
    _, svc, calls = corpus
    started, release = threading.Event(), threading.Event()
    original = svc.bm25_index.search
    def lexical(**kwargs):
        started.set()
        release.wait(2)
        return original(**kwargs)
    svc.bm25_index.search = lexical
    # Allow Windows/SQLite setup jitter before the controlled vector boundary;
    # this verifies cancellation ownership, not a business latency target.
    monkeypatch.setenv('RAG_RETRIEVAL_DEADLINE_SECONDS', '.5')
    async def run():
        try:
            async with managed_lexical_executor() as manager:
                # The manager is lifespan-owned; API caller deadline does not
                # wait for a shielded SQLite read to finish.
                began = time.perf_counter()
                rows, metrics = await search(corpus, top_k=2, kb_ids=('a',))
                assert time.perf_counter() - began < 1.25
                assert rows and metrics['retrieval_deadline_exhausted']
                assert metrics['degraded'] and started.is_set()
                from app.services import bounded_lexical_executor
                assert bounded_lexical_executor._manager.slots._value == 1
                release.set()
        finally:
            release.set()
    asyncio.run(run())


def test_memo_scope_identity_and_budget_validation(monkeypatch):
    from app.services.embedding import EmbeddingService
    from app.services.model_bindings import ModelBinding
    binding = ModelBinding('m', 'model', 'http://embedding.invalid/v1', 'synthetic',
                           model_type='embedding', context_length=4096)
    service = EmbeddingService(base_url=binding.base_url, api_key=binding.api_key, model=binding.model_name)
    a = retrieval._query_embedding_reuse_key(service, binding, 'q', RequestQueryVectors(['a']).authorization_scope)
    b = retrieval._query_embedding_reuse_key(service, binding, 'q', RequestQueryVectors(['b']).authorization_scope)
    assert a != b
    assert fair_depths(5, ['b', 'a', 'b']) == {'b': 3, 'a': 2}
    monkeypatch.setenv('RAG_RETRIEVAL_DEADLINE_SECONDS', 'nan')
    with pytest.raises(ValueError, match='INVALID_RAG_RETRIEVAL_DEADLINE_SECONDS'):
        RetrievalBudget.from_environment()


def test_deadline_without_completed_routes_is_unknown_not_no_results(corpus, monkeypatch):
    _, svc, calls = corpus
    started, release = threading.Event(), threading.Event()
    async def vector(**kwargs):
        await asyncio.Event().wait()
    def lexical(**kwargs):
        started.set()
        release.wait(2)
        return []
    svc._query_vector_store = vector
    svc.bm25_index.search = lexical
    monkeypatch.setenv('RAG_RETRIEVAL_DEADLINE_SECONDS', '.04')
    async def run():
        try:
            async with managed_lexical_executor():
                rows, metrics = await search(corpus, kb_ids=('a',))
                assert rows == [] and metrics['retrieval_status'] == 'UNKNOWN'
                assert metrics['retrieval_deadline_exhausted']
                assert metrics['fallback_used'] and started.is_set()
                release.set()
        finally:
            release.set()
    asyncio.run(run())


def test_active_manifest_stale_chunk_and_deleted_kb_filter_before_union(corpus, monkeypatch):
    engine, svc, calls = corpus
    from sqlalchemy import event
    with Session(engine) as db:
        file = db.get(FileDocument, 'file-a')
        file.active_build_id = 'active-build'
        kb = db.get(KnowledgeBase, 'b')
        kb.is_deleted = True
        raw = json.dumps({'chunk_ids': ['a-0', 'a-2'], 'vector_ids': ['a-0', 'a-2']})
        db.add(FileIndexBuild(build_id='active-build', file_id='file-a', kb_id='a',
            generation=1, task_id='fixture', attempt_no=1, state='ACTIVE', manifest=raw,
            manifest_hash=hashlib.sha256(raw.encode()).hexdigest(), chunk_count=2))
        db.add_all([file, kb])
        db.commit()
    statements = []
    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', record)
    try:
        with Session(engine) as db:
            rows = svc._authorized_route_rows(db, [
                {'id': 'a-0', 'kb_id': 'a'}, {'id': 'a-1', 'kb_id': 'a'},
                {'id': 'b-0', 'kb_id': 'b'}, {'id': 'a-0', 'kb_id': 'b'}], ['a', 'b'])
            assert rows == [{'id': 'a-0', 'kb_id': 'a'}]
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert not any('document_chunks.content' in statement for statement in statements)
    monkeypatch.setattr(retrieval, 'expand_queries', Mock(return_value=['beta']))
    rows, metrics = asyncio.run(search(corpus, top_k=2))
    assert metrics['initial_candidate_count'] == 1 and metrics['expansion_triggered']
    assert {row['id'] for row in rows} == {'a-0', 'a-2'}


@pytest.mark.parametrize('defect', ['missing', 'retired', 'wrong_owner', 'bad_hash', 'bad_json'])
def test_invalid_active_manifest_fails_closed(corpus, defect):
    engine, svc, calls = corpus
    with Session(engine) as db:
        file = db.get(FileDocument, 'file-a')
        file.active_build_id = 'active-build'
        db.add(file)
        if defect != 'missing':
            raw = '{bad json' if defect == 'bad_json' else json.dumps({'chunk_ids': ['a-0']})
            db.add(FileIndexBuild(build_id='active-build', file_id='wrong' if defect == 'wrong_owner' else 'file-a',
                kb_id='a', generation=1, task_id='fixture', attempt_no=1,
                state='RETIRED' if defect == 'retired' else 'ACTIVE', manifest=raw,
                manifest_hash='invalid' if defect == 'bad_hash' else hashlib.sha256(raw.encode()).hexdigest(), chunk_count=1))
        db.commit()
        assert svc._authorized_route_rows(db, [{'id': 'a-0', 'kb_id': 'a'}], ['a']) == []


@pytest.mark.parametrize('shape,expected_calls,expected_dimensions', [
    ('same', 1, [3, 3]), ('different', 2, [3, 4]),
    ('legacy', 2, [None, None]), ('mixed', 2, [None, 3]),
    ('wrong_length', 2, [3, 3]), ('bad_hash', 2, [None, 3]),
])
def test_actual_target_dimensions_guard_request_memo(corpus, monkeypatch, shape, expected_calls, expected_dimensions):
    engine, svc, calls = corpus
    monkeypatch.setenv('RAG_QUERY_EMBEDDING_REUSE_ENABLED', 'true')
    from app.services.embedding import EmbeddingService
    with Session(engine) as db:
        if shape != 'legacy':
            for kb in ('a', 'b'):
                file = db.get(FileDocument, 'file-' + kb)
                file.active_build_id = 'build-' + kb
                db.add(file)
                raw = json.dumps({'chunk_ids': [f'{kb}-{i}' for i in range(4)],
                    'dimension': 4 if shape == 'different' and kb == 'b' else 3})
                db.add(FileIndexBuild(build_id='build-' + kb, file_id='file-' + kb, kb_id=kb,
                    generation=1, task_id='fixture', attempt_no=1, state='ACTIVE', manifest=raw,
                    manifest_hash='invalid' if shape == 'bad_hash' and kb == 'a' else hashlib.sha256(raw.encode()).hexdigest(),
                    chunk_count=4))
            if shape == 'mixed':
                raw = json.dumps({'chunk_ids': ['a-extra'], 'dimension': 4})
                db.add(FileDocument(id='mixed-file', kb_id='a', name='mixed', local_path='x', size=0,
                                    active_build_id='mixed-build'))
                db.add(DocumentChunk(id='a-extra', file_id='mixed-file', content='alpha 999'))
                db.add(FileIndexBuild(build_id='mixed-build', file_id='mixed-file', kb_id='a',
                    generation=1, task_id='fixture', attempt_no=1, state='ACTIVE', manifest=raw,
                    manifest_hash=hashlib.sha256(raw.encode()).hexdigest(), chunk_count=1))
            db.commit()
    send = AsyncMock(return_value=[.1, .2] if shape == 'wrong_length' else [.1, .2, .3])
    monkeypatch.setattr(EmbeddingService, 'embed_query', send)
    svc._embed_query_for_kb = retrieval.RetrievalService._embed_query_for_kb.__get__(svc)
    rows, metrics = asyncio.run(search(corpus, top_k=2))
    assert send.await_count == expected_calls
    assert [row['embedding_dimension'] for row in metrics['target_allocations']] == expected_dimensions
    assert all(call.args == ('alpha',) and call.kwargs == {'model_id': 'same-space'}
               for call in send.call_args_list)

"""Actual file-SQLite sessions, deterministic barriers; provider responses are doubles."""
import asyncio
import threading
import time
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, create_engine
from app.services import retrieval
from app.services.bounded_lexical_executor import managed_lexical_executor, BoundedLexicalExecutor


def service():
    svc = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    svc._vector_scope = Mock(return_value=([], []))
    # Scheduling/ranking boundary double; real source authorization is covered
    # by test_hybrid_request_budget with genuine SQLite rows and manifests.
    svc._authorized_route_rows = Mock(side_effect=lambda session, rows, allowed: rows)
    svc._enrich_results = Mock(side_effect=lambda session, rows, allowed: rows)
    svc._embed_query_for_kb = AsyncMock(return_value=[.1])
    svc.vector_store = Mock()
    svc.vector_store.query.return_value = [{'id': 'v', 'score': .9}]
    svc.bm25_index = Mock()
    svc.bm25_index.search.return_value = [{'chunk_id': 'b', 'bm25_score': 3}]
    return svc


def test_parallel_barrier_session_ownership_and_ranking_parity(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'), connect_args={'check_same_thread': False})
    svc = service()
    worker_started, vector_started = threading.Event(), threading.Event()
    caller_thread = threading.get_ident()
    observed = []
    def lexical(**kw):
        observed.append((threading.get_ident(), kw['session']))
        worker_started.set()
        assert vector_started.wait(2), 'vector must start before lexical completes'
        return [{'chunk_id': 'b', 'bm25_score': 3}]
    async def vector(*a, **kw):
        vector_started.set()
        assert await asyncio.to_thread(worker_started.wait, 2), 'lexical must start before vector completes'
        return [.1]
    svc.bm25_index.search.side_effect = lexical
    svc._embed_query_for_kb = AsyncMock(side_effect=vector)
    async def run():
        with Session(engine) as session:
            async with managed_lexical_executor():
                parallel = await svc._hybrid_search(session, 'q', ['kb'], 2, 0, .7, .3)
            assert all(t != caller_thread and reader is not session for t, reader in observed)
            svc.bm25_index.search.side_effect = None
            svc._embed_query_for_kb = AsyncMock(return_value=[.1])
            serial = await svc._hybrid_search(session, 'q', ['kb'], 2, 0, .7, .3)
            assert parallel[0] == serial[0]
            assert parallel[1]['initial_candidate_count'] == 2
            assert parallel[1]['hybrid_initial_parallel'] is True
    asyncio.run(run())
    engine.dispose()


def test_initial_union_deduplicates_and_reuses_bm25_before_expansion(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    svc = service()
    svc.vector_store.query.return_value = [{'id': 'same', 'score': .9}]
    queries = []
    def lexical(**kw):
        queries.append(kw['query'])
        return [{'chunk_id': 'extra' if kw['query'] == 'keyword' else 'same', 'bm25_score': 3}]
    svc.bm25_index.search.side_effect = lexical
    monkeypatch.setattr(retrieval, 'expand_queries', lambda _: ['keyword'])
    async def run():
        async with managed_lexical_executor():
            with Session(engine) as session:
                rows, metrics, fallback = await svc._hybrid_search(session, 'q', ['kb'], 2, 0, .7, .3)
                assert metrics['initial_candidate_count'] == 1
                assert metrics['expansion_query_count'] == 1
                assert {r['id'] for r in rows} == {'same', 'extra'}
    asyncio.run(run())
    assert queries == ['q', 'keyword']
    svc._embed_query_for_kb.assert_awaited_once()
    engine.dispose()


def test_sufficient_union_skips_variants_and_real_sessions_stay_scoped(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    svc = service()
    monkeypatch.setattr(retrieval, 'expand_queries', Mock(side_effect=AssertionError('unneeded expansion')))
    async def run():
        async with managed_lexical_executor():
            with Session(engine) as session:
                _, metrics, _ = await svc._hybrid_search(session, 'q', ['kb'], 2, 0, .7, .3)
                assert metrics['expansion_triggered'] is False
    asyncio.run(run())
    svc.bm25_index.search.assert_called_once()
    engine.dispose()


@pytest.mark.parametrize('route', ['vector', 'bm25'])
def test_unknown_route_is_terminal_no_supplement_or_paid_retry(tmp_path, monkeypatch, route):
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    svc = service()
    if route == 'vector':
        svc._embed_query_for_kb.side_effect = TimeoutError('unknown effect')
    else:
        svc.bm25_index.search.side_effect = TimeoutError('unknown effect')
    monkeypatch.setattr(retrieval, 'expand_queries', Mock(side_effect=AssertionError('must not expand')))
    async def run():
        async with managed_lexical_executor():
            with Session(engine) as session:
                with pytest.raises(TimeoutError):
                    await svc._hybrid_search(session, 'q', ['kb'], 5, 0, .7, .3)
    asyncio.run(run())
    svc._embed_query_for_kb.assert_awaited_once()
    engine.dispose()


def test_local_route_failure_preserves_other_route(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    svc = service()
    svc.bm25_index.search.side_effect = RuntimeError('local index unavailable')
    async def run():
        async with managed_lexical_executor():
            with Session(engine) as session:
                rows, _, fallback = await svc._hybrid_search(session, 'q', ['kb'], 1, 0, .7, .3)
                assert [r['id'] for r in rows] == ['v'] and fallback
    asyncio.run(run())
    engine.dispose()


def test_cancellation_does_not_release_real_worker_slot_or_leak_on_close():
    entered, release, second = threading.Event(), threading.Event(), threading.Event()
    async def run():
        manager = BoundedLexicalExecutor(workers=1)
        first = asyncio.create_task(manager.run(lambda: (entered.set(), release.wait(2))))
        assert await asyncio.to_thread(entered.wait, 2)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        queued = asyncio.create_task(manager.run(second.set))
        await asyncio.sleep(.03)
        assert not second.is_set()
        release.set()
        await queued
        await manager.close()
        assert second.is_set() and all(not t.is_alive() for t in manager.pool._threads)
    asyncio.run(run())


def test_unshareable_and_unmanaged_sessions_remain_serial():
    engine = create_engine('sqlite://')
    async def run():
        with Session(engine) as session:
            async with managed_lexical_executor():
                assert retrieval.RetrievalService._parallel_lexical_bind(session) is None
            assert retrieval.RetrievalService._parallel_lexical_bind(session) is None
    asyncio.run(run())
    engine.dispose()


def test_synthetic_overlap_reduces_wall_time(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'source.db'))
    svc = service()
    async def embed(*a, **kw):
        await asyncio.sleep(.15)
        return [.1]
    def lexical(**kw):
        time.sleep(.15)
        return [{'chunk_id': 'b', 'bm25_score': 3}]
    svc._embed_query_for_kb.side_effect = embed
    svc.bm25_index.search.side_effect = lexical
    async def run():
        with Session(engine) as session:
            start = time.perf_counter()
            serial = await svc._hybrid_search(session, 'q', ['kb'], 2, 0, .7, .3)
            serial_ms = (time.perf_counter() - start) * 1000
            async with managed_lexical_executor():
                start = time.perf_counter()
                parallel = await svc._hybrid_search(session, 'q', ['kb'], 2, 0, .7, .3)
                parallel_ms = (time.perf_counter() - start) * 1000
            assert parallel[0] == serial[0]
            assert parallel_ms < serial_ms * .8
            print(f'SYNTHETIC_SERIAL_MS={serial_ms:.2f} PARALLEL_MS={parallel_ms:.2f}')
    asyncio.run(run())
    engine.dispose()


def test_parallel_reads_actual_postings_and_committed_scope(tmp_path):
    from sqlmodel import SQLModel
    from app.models import KnowledgeBase, FileDocument, DocumentChunk
    from app.services.bm25 import BM25Index
    engine = create_engine('sqlite:///' + str(tmp_path / 'postings.db'), connect_args={'check_same_thread': False})
    SQLModel.metadata.create_all(engine)
    svc = service()
    svc.bm25_index = BM25Index()
    with Session(engine) as session:
        session.add_all([KnowledgeBase(id=k, name=k, embedding_model='', vlm_model='') for k in ['kb', 'other']])
        session.add_all([FileDocument(id=f, kb_id=k, name=f, local_path='x', size=1)
                         for f, k in [('f', 'kb'), ('g', 'other')]])
        session.add_all([DocumentChunk(id=c, file_id=f, content='budget 14009')
                         for c, f in [('literal', 'f'), ('outside', 'g')]])
        session.flush()
        svc.bm25_index.index_chunk(session, 'literal', 'budget 14009', 'kb')
        svc.bm25_index.index_chunk(session, 'outside', 'budget 14009', 'other')
        session.commit()
        async def run():
            async with managed_lexical_executor():
                rows, metrics, _ = await svc._hybrid_search(session, 'budget 14009', ['kb'], 2, 0, .7, .3)
                assert {row['id'] for row in rows} == {'v', 'literal'}
                assert metrics['hybrid_initial_parallel']
        asyncio.run(run())
    engine.dispose()


def test_flushed_and_raw_uncommitted_writes_keep_independent_reader(tmp_path):
    from sqlmodel import SQLModel
    from sqlalchemy import text
    from app.models import KnowledgeBase
    engine = create_engine('sqlite:///' + str(tmp_path / 'writes.db'))
    SQLModel.metadata.create_all(engine)
    async def run():
        async with managed_lexical_executor():
            with Session(engine) as session:
                assert retrieval.RetrievalService._parallel_lexical_bind(session).url == engine.url
                session.add(KnowledgeBase(id='new', name='new', embedding_model='', vlm_model=''))
                assert retrieval.RetrievalService._parallel_lexical_bind(session) is not None
                session.flush()
                assert not session.new and not session.dirty
                bind, reason = retrieval.RetrievalService._lexical_execution_scope(session)
                assert bind is not None and reason == 'managed_independent_reader'
                with Session(bind) as reader:
                    assert reader.get(KnowledgeBase, 'new') is None
                session.rollback()
                session.execute(text("insert into knowledge_bases (id,name,description,embedding_model,vlm_model,chunk_count,is_deleted,created_at,updated_at) values ('raw','raw','','','',0,0,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"))
                assert retrieval.RetrievalService._parallel_lexical_bind(session) is not None
    asyncio.run(run())
    engine.dispose()


def test_caller_exhausted_pool_does_not_block_lexical_reader(tmp_path):
    from sqlalchemy import text
    engine = create_engine('sqlite:///' + str(tmp_path / 'one.db'), pool_size=1, max_overflow=0, pool_timeout=.05)
    with engine.begin() as connection:
        connection.execute(text('create table corpus (value text)'))
        connection.execute(text("insert into corpus values ('lexical')"))
    svc = service()
    def lexical(**kw):
        assert kw['session'].execute(text('select value from corpus')).scalar() == 'lexical'
        with pytest.raises(Exception, match='readonly'):
            kw['session'].execute(text("insert into corpus values ('forbidden')"))
        return [{'chunk_id': 'b', 'bm25_score': 3}]
    svc.bm25_index.search.side_effect = lexical
    async def run():
        with Session(engine) as caller:
            caller.execute(text('select value from corpus')).all()
            assert engine.pool.checkedout() == 1
            async with managed_lexical_executor():
                rows, metrics, _ = await asyncio.wait_for(svc._hybrid_search(caller, 'q', ['kb'], 2, 0, .7, .3), 2)
                assert {row['id'] for row in rows} == {'v', 'b'}
                assert metrics['hybrid_initial_parallel']
    asyncio.run(run())
    engine.dispose()


def test_concurrent_requests_bound_readers_without_caller_pool_starvation(tmp_path):
    from sqlalchemy import text
    engine = create_engine('sqlite:///' + str(tmp_path / 'many.db'), pool_size=6, max_overflow=0, pool_timeout=.05)
    with engine.begin() as connection:
        connection.execute(text('create table corpus (value int)'))
        connection.execute(text('insert into corpus values (1)'))
    svc = service()
    lock = threading.Lock()
    active = peak = 0
    def lexical(**kw):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        try:
            assert kw['session'].execute(text('select value from corpus')).scalar() == 1
            time.sleep(.03)
            return [{'chunk_id': 'b', 'bm25_score': 3}]
        finally:
            with lock:
                active -= 1
    svc.bm25_index.search.side_effect = lexical
    async def run():
        async with managed_lexical_executor():
            sessions = [Session(engine) for _ in range(6)]
            try:
                for caller in sessions:
                    caller.execute(text('select value from corpus')).all()
                assert engine.pool.checkedout() == 6
                outcomes = await asyncio.wait_for(asyncio.gather(*[
                    svc._hybrid_search(caller, 'q', ['kb'], 2, 0, .7, .3) for caller in sessions]), 3)
                assert all({row['id'] for row in result[0]} == {'v', 'b'} for result in outcomes)
            finally:
                for caller in sessions:
                    caller.close()
    asyncio.run(run())
    assert peak == 2 and active == 0
    engine.dispose()

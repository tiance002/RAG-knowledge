"""Bounded Posting production and atomic replacement against frozen P1 SQL."""
from collections import Counter
import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import delete, event
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import BM25Posting, ChunkIndex, DocumentChunk, FileDocument, FileStatus, IndexStats
from app.services import bm25


@pytest.fixture
def p1():
    source = (Path(__file__).resolve().parents[2] / '.planning' /
              '2026-11-12-bm25-stats-p0p1/p2/baseline/backend/app/services/bm25.py')
    if not source.is_file():
        pytest.skip('optional historical P1 source is not present')
    spec = importlib.util.spec_from_file_location('frozen_p1_bm25', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.BM25Index()


def prepared(index, rows):
    result = index.prepare_file_index(rows)
    for row in result:
        row['id'] = 'index-' + row['chunk_id']
    return result


def store(path, index):
    engine = create_engine('sqlite:///' + str(path))
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        for file_id, kb in [('target', 'kb'), ('other', 'kb'), ('alien', 'elsewhere')]:
            session.add(FileDocument(id=file_id, kb_id=kb, name=file_id,
                                     local_path='unused', size=1, status=FileStatus.READY))
        session.flush()
        for file_id, kb, text in [('target', 'kb', 'obsolete common'),
                                  ('other', 'kb', 'preserved common common'),
                                  ('alien', 'elsewhere', 'foreign common')]:
            rows = [dict(id=file_id, content=text)]
            session.add(DocumentChunk(id=file_id, file_id=file_id, content=text))
            session.flush()
            index.finish_file_replacement(session, kb, (0, 0), prepared(index, rows))
            session.flush()
        session.commit()
        # P1 explicitly retains this non-authoritative snapshot byte-for-byte.
        session.get(IndexStats, 'kb').term_doc_freq = '{malformed retained snapshot'
        session.commit()
    return engine


def rows_for(generation, children=1200):
    rows = []
    for i in range((children + 9) // 10):
        rows.append(dict(id=f'{generation}-parent-{i}', content='parentonly hidden',
                         chunk_type='parent', parent_id=None))
    for i in range(children):
        rows.append(dict(id=f'{generation}-child-{i}',
                         content=f'common common {generation} token{i} group{i % 7} '
                                 'alpha beta gamma delta epsilon zeta',
                         chunk_type='child', parent_id=f'{generation}-parent-{i // 10}'))
    rows.append(dict(id=generation + '-empty', content='', chunk_type='content', parent_id=None))
    return rows


def replace(session, index, rows, new_prepared):
    removed = index.begin_file_replacement(session, 'target', 'kb')
    session.execute(delete(DocumentChunk).where(DocumentChunk.file_id == 'target'))
    session.add_all([DocumentChunk(id=row['id'], file_id='target', content=row['content'],
                                  chunk_type=row.get('chunk_type', 'content'),
                                  parent_id=row.get('parent_id')) for row in rows])
    session.flush()
    index.finish_file_replacement(session, 'kb', removed, new_prepared)


def state(engine, index):
    with Session(engine) as session:
        indices = sorted((i.id, i.kb_id, i.chunk_id, i.doc_length, i.terms)
                         for i in session.exec(select(ChunkIndex)))
        postings = sorted((p.kb_id, p.index_id, p.term, p.tf)
                          for p in session.exec(select(BM25Posting)))
        stats = sorted((s.kb_id, s.total_docs, s.avg_doc_length, s.term_doc_freq)
                       for s in session.exec(select(IndexStats)))
        chunks = sorted((c.id, c.file_id, c.content, c.chunk_type, c.parent_id)
                        for c in session.exec(select(DocumentChunk)))
        df = sorted(Counter((kb, term) for kb, _, term, _ in postings).items())
        rankings = [index.search(session, query, kb, 30) for kb, query in
                    [('kb', 'common common alpha'), ('kb', 'token1137 group3'),
                     ('kb', 'replacement'), ('kb', 'obsolete parentonly'),
                     ('elsewhere', 'foreign common')]]
        return dict(indices=indices, postings=postings, stats=stats, chunks=chunks,
                    df=df, rankings=rankings)


class CountedFrequencies(dict):
    """Count production, so a whole materialized list with sliced sends fails."""
    def __init__(self, values, progress):
        super().__init__(values)
        self.progress = progress

    def items(self):
        for pair in super().items():
            self.progress['produced'] += 1
            yield pair


def exercise_large_replacements(tmp_path, reference=None):
    index = bm25.BM25Index()
    candidate = store(tmp_path / 'candidate.sqlite', index)
    baseline = store(tmp_path / 'p1.sqlite', reference) if reference else None
    initial = state(candidate, index)
    progress = dict(produced=0, inserted=0)
    batches = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith('insert into bm25_postings'):
            size = len(parameters) if executemany else 1
            # Production itself must remain bounded, not just SQL sends.
            assert progress['produced'] - progress['inserted'] == size
            assert size <= bm25._POSTING_INSERT_BATCH_SIZE
            batches.append(size)
            progress['inserted'] += size

    event.listen(candidate, 'before_cursor_execute', capture)
    try:
        for generation in ['replacement', 'replacement', 'nextversion']:
            rows = rows_for(generation)
            data = prepared(index, rows)
            expected_indices = sorted(
                [(r['id'], 'kb', r['chunk_id'], r['doc_length'], r['terms']) for r in data] +
                [r for r in initial['indices'] if r[0] in {'index-other', 'index-alien'}])
            expected_postings = sorted(
                [('kb', r['id'], term, tf) for r in data
                 for term, tf in json.loads(r['terms']).items()] +
                [r for r in initial['postings'] if r[1] in {'index-other', 'index-alien'}])
            for row in data:
                row['frequencies'] = CountedFrequencies(row['frequencies'], progress)
            with Session(candidate) as session:
                replace(session, index, rows, data)
                session.commit()
            if baseline:
                with Session(baseline) as session:
                    replace(session, reference, rows, prepared(reference, rows))
                    session.commit()
            actual = state(candidate, index)
            if baseline:
                assert actual == state(baseline, reference)
            assert actual['indices'] == expected_indices
            assert actual['postings'] == expected_postings
            assert actual['df'] == sorted(Counter((kb, term) for kb, _, term, _ in expected_postings).items())
            kb_stats = [s for s in actual['stats'] if s[0] == 'kb'][0]
            expected_length = sum(r[3] for r in expected_indices if r[1] == 'kb')
            assert kb_stats == ('kb', 1201, expected_length / 1201, '{malformed retained snapshot')
            assert actual['rankings'][3] == []
            assert actual['rankings'][4] == initial['rankings'][4]
            assert all(hit['chunk_id'].startswith(generation + '-child-') or hit['chunk_id'] == 'other'
                       for ranking in actual['rankings'][:3] for hit in ranking)
            assert [s for s in actual['stats'] if s[0] == 'kb'][0][1] == 1201
            assert len([c for c in actual['chunks'] if c[3] == 'parent']) == 120
            assert all('parent-' not in row[2] and not row[2].endswith('-empty')
                       for row in actual['indices'])
            assert len(actual['postings']) == 1200 * 10 + 4
            for key in ['indices', 'postings', 'stats', 'chunks']:
                position = {'indices': 0, 'postings': 1, 'stats': 0, 'chunks': 1}[key]
                other_values = {'indices': {'index-other', 'index-alien'},
                                'postings': {'index-other', 'index-alien'},
                                'stats': {'elsewhere'}, 'chunks': {'alien', 'other'}}[key]
                assert [r for r in actual[key] if r[position] in other_values] == [
                    r for r in initial[key] if r[position] in other_values]
        assert len(batches) == 36
        assert max(batches) == bm25._POSTING_INSERT_BATCH_SIZE == 1000
        assert progress['produced'] == progress['inserted'] == 36000
    finally:
        event.remove(candidate, 'before_cursor_execute', capture)
        candidate.dispose()
        if baseline:
            baseline.dispose()


def test_large_parent_child_replacements_have_bounded_production(tmp_path):
    exercise_large_replacements(tmp_path)


def test_optional_frozen_p1_full_state_and_ranking_parity(tmp_path, p1):
    exercise_large_replacements(tmp_path, p1)


def test_real_later_batch_sql_failure_rolls_back_entire_replacement(tmp_path):
    index = bm25.BM25Index()
    engine = store(tmp_path / 'rollback.sqlite', index)
    before = state(engine, index)
    rows = rows_for('replacement', children=270)
    data = prepared(index, rows)
    failing_index_id = data[150]['id']
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER fail_late_posting BEFORE INSERT ON bm25_postings "
            f"WHEN NEW.index_id = '{failing_index_id}' BEGIN "
            "SELECT RAISE(ABORT, 'real late posting failure'); END")
    successful_batches = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lower().startswith('insert into bm25_postings'):
            successful_batches.append(len(parameters) if executemany else 1)

    event.listen(engine, 'after_cursor_execute', capture)
    try:
        with pytest.raises(IntegrityError, match='real late posting failure'):
            with Session(engine) as session:
                replace(session, index, rows, data)
                session.commit()
        assert successful_batches == [1000]
        assert state(engine, index) == before
        # No helper commit occurred: SQL changes earlier than the failing batch
        # (old-row deletes, replacement chunks/indices and stats) all disappeared.
    finally:
        event.remove(engine, 'after_cursor_execute', capture)
        engine.dispose()

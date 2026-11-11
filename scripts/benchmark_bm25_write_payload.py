"""SYNTHETIC large parent/child BM25 write comparison; no production data or API."""
import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import importlib.util
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ.update(RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false', REDIS_URL='memory://')
from sqlmodel import Session, SQLModel, create_engine, select
from app.models import BM25Posting, ChunkIndex, DocumentChunk, FileDocument, IndexStats, KnowledgeBase
from app.services.bm25 import BM25Index


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    assert not args.output.exists(), 'use a new output file'
    spec = importlib.util.spec_from_file_location('saved_bm25_baseline', args.baseline)
    baseline = importlib.util.module_from_spec(spec); spec.loader.exec_module(baseline)
    tokenizer = 'jieba' if importlib.util.find_spec('jieba') else 'simple_fallback'
    # Both arms retain the product tokenizer; avoid printing one identical
    # dependency warning per child. The effective fallback is explicit below.
    logging.getLogger('app.services.bm25').setLevel(logging.ERROR)
    logging.getLogger('saved_bm25_baseline').setLevel(logging.ERROR)
    rows = []
    for group in range(120):
        for child in range(10):
            rows.append(dict(id=f'c-{group}-{child}', content=(f'evidence group{group} unit kilograms field{child} ' * 100),
                             chunk_type='child', parent_id=f'p-{group}'))
        rows.append(dict(id=f'p-{group}', content='\n'.join(r['content'] for r in rows[-10:]), chunk_type='parent'))
    vocabulary = {f'background_term_{i:06}': 1 for i in range(50000)}
    runs = []
    logical_snapshots = []
    # Temporary DBs are synthetic and deleted on exit; original baselines are never opened.
    with tempfile.TemporaryDirectory(prefix='bm25-wal-', dir=args.output.parent) as temporary, ExitStack() as resources:
        for name, cls in [('saved', baseline.BM25Index), ('candidate', BM25Index)]:
            path = Path(temporary) / (name + '.db')
            engine = create_engine('sqlite:///' + path.as_posix())
            resources.callback(engine.dispose)
            SQLModel.metadata.create_all(engine)
            with engine.connect() as conn:
                conn.exec_driver_sql('PRAGMA journal_mode=WAL')
                conn.exec_driver_sql('PRAGMA wal_autocheckpoint=0')
            with Session(engine) as session:
                session.add(KnowledgeBase(id='kb', name='synthetic', embedding_model='', vlm_model=''))
                session.add(FileDocument(id='f', kb_id='kb', name='synthetic-large.txt', local_path='synthetic', size=sum(len(r['content'].encode()) for r in rows if r['chunk_type'] != 'parent')))
                session.add_all([DocumentChunk(id=r['id'], file_id='f', content=r['content'], chunk_type=r['chunk_type'], parent_id=r.get('parent_id')) for r in rows])
                session.add(DocumentChunk(id='background', file_id='f', content=' '.join(vocabulary)))
                session.flush()
                session.add(ChunkIndex(id='background', chunk_id='background', kb_id='kb', terms=json.dumps(vocabulary), doc_length=len(vocabulary)))
                session.flush()
                session.execute(BM25Posting.__table__.insert(), [dict(kb_id='kb', term=t, index_id='background', tf=1) for t in vocabulary])
                session.add(IndexStats(kb_id='kb', total_docs=1, avg_doc_length=len(vocabulary), term_doc_freq=json.dumps(vocabulary)))
                session.commit()
            with engine.connect() as conn:
                conn.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)')
            started = time.perf_counter()
            prepared = cls.prepare_file_index(rows)
            for i, row in enumerate(prepared): row['id'] = f'new-index-{i}'
            prepare_ms = (time.perf_counter() - started) * 1000
            with Session(engine) as session:
                started = time.perf_counter()
                cls.finish_file_replacement(session, 'kb', (0, 0, Counter()), prepared)
                session.flush()
                flush_ms = (time.perf_counter() - started) * 1000
                started = time.perf_counter(); session.commit()
                commit_ms = (time.perf_counter() - started) * 1000
                stats = session.get(IndexStats, 'kb')
                logical_snapshots.append((stats.total_docs, stats.avg_doc_length, json.loads(stats.term_doc_freq)))
                runs.append(dict(arm=name, prepare_ms=prepare_ms, bm25_update_flush_ms=flush_ms, commit_ms=commit_ms,
                    stats_utf8_bytes=len(stats.term_doc_freq.encode()), terms_utf8_bytes=sum(len(r['terms'].encode()) for r in prepared),
                    wal_bytes=Path(str(path) + '-wal').stat().st_size, indexed_children=len(prepared),
                    postings=sum(len(r['frequencies']) for r in prepared)))
                assert not session.exec(select(ChunkIndex).where(ChunkIndex.chunk_id.like('p-%'))).all()
            engine.dispose()
    assert logical_snapshots[0] == logical_snapshots[1]
    report = dict(scope='SYNTHETIC BM25 phase only, local SQLite; not ECS or full publication speed',
        tokenizer=tokenizer,
        source_bytes=sum(len(r['content'].encode()) for r in rows if r['chunk_type'] != 'parent'),
        parent_count=120, child_count=1200, background_vocabulary=50000, results=runs,
        logical_statistics_equal=True, parents_not_indexed=True, temporary_databases_removed=True,
        baseline_sha256=hashlib.sha256(args.baseline.read_bytes()).hexdigest())
    with args.output.open('x', encoding='utf-8') as output: json.dump(report, output, indent=2)
    print(json.dumps(report, indent=2))


if __name__ == '__main__': main()

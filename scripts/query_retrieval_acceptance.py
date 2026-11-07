"""Bounded read-only acceptance of real rewrite + hybrid retrieval.

Default is offline preflight. --live requires a new output directory and an
explicit JSON manifest; no provider calls are made by preflight. The manifest
contains public inputs and IDs, never credentials. SQLite is opened mode=ro;
Chroma existing collections are queried through the application's real store.

Manifest fields: db_path, chroma_dir, kb_id, embedding_model_id,
embedding_model_name, source_path, source_sha256, cases (exactly four items:
id, category, query, optional history, gold_chunk_ids), paired_ids (two case
IDs), optional top_k (default 10). Categories must cover english, chinese,
technical, and history. Query/history text is authorized public material.
Gold IDs support hit reporting and are never sent to the model or retriever.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
QWEN = 'Qwen/Qwen3.5-4B'


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def readonly(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)


def load_plan(path):
    manifest = json.loads(Path(path).read_text(encoding='utf-8'))
    required = ('db_path', 'chroma_dir', 'kb_id', 'embedding_model_id',
                'embedding_model_name', 'source_path', 'source_sha256', 'cases', 'paired_ids')
    if any(not manifest.get(k) for k in required):
        raise ValueError('missing required manifest field')
    if not Path(manifest['db_path']).is_file() or not Path(manifest['chroma_dir']).is_dir():
        raise ValueError('existing DB and Chroma directory required')
    if digest(manifest['source_path']) != manifest['source_sha256']:
        raise ValueError('source hash mismatch')
    cases = manifest['cases']
    if len(cases) != 4 or {x.get('category') for x in cases} != {'english', 'chinese', 'technical', 'history'}:
        raise ValueError('four distinct required categories expected')
    ids = [x.get('id') for x in cases]
    if len(set(ids)) != 4 or not all(ids) or len(set(manifest['paired_ids'])) != 2 or not set(manifest['paired_ids']) <= set(ids):
        raise ValueError('four unique IDs and two paired IDs required')
    for case in cases:
        if not isinstance(case.get('query'), str) or not case['query'].strip():
            raise ValueError('empty query')
        if not isinstance(case.get('gold_chunk_ids'), list) or not case['gold_chunk_ids']:
            raise ValueError('each case needs frozen gold chunk IDs')
        for turn in case.get('history', []):
            if turn.get('role') not in ('user', 'assistant') or not isinstance(turn.get('content'), str):
                raise ValueError('invalid history')
    top_k = manifest.get('top_k', 10)
    if not isinstance(top_k, int) or not 1 <= top_k <= 50:
        raise ValueError('top_k outside bounds')
    with readonly(manifest['db_path']) as conn:
        kb = conn.execute('SELECT embedding_model, is_deleted FROM knowledge_bases WHERE id = ?', (manifest['kb_id'],)).fetchone()
        model = conn.execute('SELECT model_name, base_url, is_active FROM custom_models WHERE id = ?', (manifest['embedding_model_id'],)).fetchone()
        if not kb or kb[1] or kb[0] != manifest['embedding_model_id'] or not model or not model[2] or model[0] != manifest['embedding_model_name']:
            raise ValueError('KB / embedding binding mismatch')
        if not model[1] or urlsplit(model[1]).scheme not in ('http', 'https'):
            raise ValueError('invalid model endpoint')
        for case in cases:
            for chunk_id in case['gold_chunk_ids']:
                linked = conn.execute(
                    'SELECT 1 FROM document_chunks c JOIN file_documents f ON f.id = c.file_id '
                    'WHERE c.id = ? AND f.kb_id = ?', (chunk_id, manifest['kb_id'])).fetchone()
                if not linked:
                    raise ValueError('gold chunk is absent from the selected KB')
    return manifest


def record(path, row):
    with open(path, 'a', encoding='utf-8') as out:
        out.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
        out.flush()
        os.fsync(out.fileno())


def close_session_and_engine(session, engine):
    """Finish the active transaction before disposing its pooled connection."""
    session.close()
    engine.dispose()


async def live(plan, output):
    # Disable optional telemetry and route all runtime observation outside shared storage.
    os.environ.update(RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false',
                      ANONYMIZED_TELEMETRY='False', CHROMA_DB_DIR=str(Path(plan['chroma_dir']).resolve()))
    from sqlalchemy import create_engine
    from sqlmodel import Session
    import httpx
    from app.services.llm import LLMService
    from app.services.retrieval import RetrievalService
    from app.services.vector_store import VectorStoreService
    from app.models import KnowledgeBase, CustomModel

    db_path = str(Path(plan['db_path']).resolve())
    engine = create_engine('sqlite://', creator=lambda: readonly(db_path))
    attempts = {'rewrite': 0, 'embedding': 0}
    journal = output / 'attempts.jsonl'
    records = output / 'results.jsonl'
    # The real provider transport is bounded BEFORE each send, including retries.
    old_send = httpx.AsyncClient.send
    allowed_origin = None
    async def guarded_send(client, request, *args, **kwargs):
        try:
            body = json.loads(request.content)
        except Exception:
            raise RuntimeError('unapproved HTTP request') from None
        model = body.get('model')
        category = 'rewrite' if model == QWEN else 'embedding' if model == plan['embedding_model_name'] else None
        parsed = urlsplit(str(request.url))
        if not category or (parsed.scheme, parsed.netloc) != allowed_origin:
            raise RuntimeError('unapproved provider or model')
        if category == 'rewrite' and (body.get('enable_thinking') is not False or body.get('max_tokens') != 200):
            raise RuntimeError('rewrite parameters differ from the approved test configuration')
        expected_path = '/chat/completions' if category == 'rewrite' else '/embeddings'
        if not parsed.path.endswith(expected_path):
            raise RuntimeError('unapproved provider endpoint')
        if attempts[category] >= (4 if category == 'rewrite' else 6):
            raise RuntimeError('provider attempt budget exhausted')
        attempts[category] += 1
        serial = attempts[category]
        details = ({'enable_thinking': False, 'max_tokens': 200} if category == 'rewrite' else {})
        record(journal, {'kind': category, 'attempt': serial, 'status': 'STARTED', 'model': model, **details})
        started = time.monotonic()
        try:
            response = await old_send(client, request, *args, **kwargs)
            try:
                usage = response.json().get('usage')
            except Exception:
                usage = None
            record(journal, {'kind': category, 'attempt': serial, 'status': 'HTTP_RETURNED',
                             'http_status': response.status_code, 'elapsed_seconds': time.monotonic()-started,
                             'usage': usage if isinstance(usage, dict) else None,
                             'usage_status': 'PROVIDER_REPORTED' if isinstance(usage, dict) else 'UNKNOWN'})
            return response
        except BaseException as exc:
            record(journal, {'kind': category, 'attempt': serial, 'status': 'UNKNOWN',
                             'error_type': type(exc).__name__, 'elapsed_seconds': time.monotonic()-started})
            raise

    with Session(engine) as session:
        kb = session.get(KnowledgeBase, plan['kb_id'])
        model = session.get(CustomModel, plan['embedding_model_id'])
        if not kb or not model or kb.embedding_model != model.id or model.model_name != plan['embedding_model_name']:
            raise RuntimeError('KB binding changed since preflight')
        parsed = urlsplit(model.base_url)
        allowed_origin = (parsed.scheme, parsed.netloc)
        llm = LLMService(base_url=model.base_url, api_key=model.api_key, model=QWEN)
        # Evaluation-only request option. Preserve the real client and response;
        # no synthetic output or change to the product service is involved.
        completions = llm._get_client().chat.completions
        create = completions.create
        async def create_without_thinking(*args, **kwargs):
            extra = dict(kwargs.get('extra_body') or {})
            extra['enable_thinking'] = False
            kwargs['extra_body'] = extra
            return await create(*args, **kwargs)
        completions.create = create_without_thinking
        service = RetrievalService()  # existing Chroma collection only; no index mutation
        collection = service.vector_store._existing_collection(plan['kb_id'])
        if collection is None or collection.count() == 0:
            raise RuntimeError('selected existing Chroma collection is absent or empty')
        bm25 = service.bm25_index
        original_bm25 = bm25.search
        original_embedding = service._embed_query_for_kb
        original_vector = service.vector_store.query
        observed = {}
        def tracked_bm25(*args, **kwargs):
            rows = original_bm25(*args, **kwargs)
            observed['bm25'].append({'query': kwargs.get('query'), 'kb_id': kwargs.get('kb_id'),
                                     'candidate_ids': [r.get('chunk_id') for r in rows]})
            return rows
        async def tracked_embedding(session, kb_id, query):
            observed['embedding_queries'].append({'kb_id': kb_id, 'query': query})
            return await original_embedding(session, kb_id, query)
        def tracked_vector(*args, **kwargs):
            rows = original_vector(*args, **kwargs)
            observed['vector'].append({'kb_id': kwargs.get('kb_id'),
                                       'candidate_ids': [r.get('id') for r in rows]})
            return rows
        bm25.search = tracked_bm25
        service._embed_query_for_kb = tracked_embedding
        service.vector_store.query = tracked_vector
        httpx.AsyncClient.send = guarded_send
        try:
            for case in plan['cases']:
                start = time.monotonic()
                before = dict(attempts)
                rewritten = await llm.rewrite_query(case['query'], history=case.get('history', []))
                selected = rewritten or case['query']
                record(records, {'id': case['id'], 'stage': 'rewrite', 'original_query': case['query'],
                                 'rewritten': rewritten, 'selected_query': selected,
                                 'status': 'COMPLETED' if rewritten else 'EMPTY_OR_TRUNCATED_FALLBACK',
                                 'elapsed_seconds': time.monotonic()-start,
                                 'attempts': attempts['rewrite'] - before['rewrite']})
                arms = [('rewritten', selected)]
                if case['id'] in plan['paired_ids']:
                    arms.append(('raw', case['query']))
                for arm, query in arms:
                    observed = {'bm25': [], 'embedding_queries': [], 'vector': []}
                    before = dict(attempts)
                    start = time.monotonic()
                    rows, metrics = await service.search(session=session, query=query, kb_ids=[plan['kb_id']],
                        search_mode='hybrid', top_k=plan.get('top_k', 10), score_threshold=0.0,
                        vector_weight=0.7, bm25_weight=0.3, use_cache=False, log_retrieval=False,
                        original_query=case['query'])
                    ids = [r.get('chunk_id') or r.get('id') for r in rows]
                    gold = set(case['gold_chunk_ids'])
                    actual_calls = attempts['embedding'] - before['embedding']
                    record(records, {'id': case['id'], 'category': case['category'], 'arm': arm,
                        'retrieval_status': metrics.get('retrieval_status'), 'metrics': metrics,
                        'elapsed_seconds': time.monotonic()-start, 'candidate_ids': ids,
                        'gold_chunk_ids': sorted(gold), 'gold_hits': sorted(gold.intersection(ids)),
                        'gold_recall': len(gold.intersection(ids)) / len(gold),
                        'embedding_http_attempts': actual_calls, **observed})
                    if actual_calls != 1 or len(observed['embedding_queries']) != 1 or metrics.get('retrieval_status') != 'SUCCESS' or metrics.get('degraded'):
                        raise RuntimeError('hybrid route incomplete; stop without retry')
        finally:
            httpx.AsyncClient.send = old_send
            bm25.search = original_bm25
            service._embed_query_for_kb = original_embedding
            service.vector_store.query = original_vector
            if llm._client is not None:
                await llm._client.close()
            close_session_and_engine(session, engine)
    return attempts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', help='new directory required only for --live')
    args = parser.parse_args()
    plan = load_plan(args.manifest)
    summary = {'status': 'PREFLIGHT', 'network_calls': 0, 'max_rewrite_calls': 4,
               'max_embedding_calls': 6, 'mode': 'hybrid', 'kb_id': plan['kb_id'],
               'embedding_model': plan['embedding_model_name'], 'rewrite_model': QWEN,
               'source_sha256': plan['source_sha256'], 'manifest_sha256': digest(args.manifest),
               'runner_sha256': digest(__file__),
               'case_ids': [c['id'] for c in plan['cases']], 'paired_ids': plan['paired_ids'],
               'gold_assessment': 'chunk IDs are recall indicators, not semantic correctness judgments'}
    if not args.live:
        print(json.dumps(summary, ensure_ascii=False))
        return
    if not args.output:
        raise ValueError('--live needs --output')
    output = Path(args.output).resolve()
    output.mkdir(parents=False, exist_ok=False)
    record(output / 'run.jsonl', {**summary, 'status': 'STARTED'})
    try:
        counts = asyncio.run(live(plan, output))
        record(output / 'run.jsonl', {'status': 'COMPLETED', 'attempt_counts': counts})
        print(json.dumps({'status': 'COMPLETED', 'output': str(output), 'attempt_counts': counts}))
    except BaseException as exc:
        record(output / 'run.jsonl', {'status': 'STOPPED', 'error_type': type(exc).__name__})
        print(json.dumps({'status': 'STOPPED', 'output': str(output), 'error_type': type(exc).__name__}))
        raise


if __name__ == '__main__':
    main()

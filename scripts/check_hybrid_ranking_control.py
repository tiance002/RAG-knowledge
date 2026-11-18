"""Bounded ranking diagnosis: two live embeddings, then four fixed-vector searches.

Importing this module is inert: no app/probe import, environment mutation, DB or
network access. Run in a fresh process with the existing candidate interpreter.
The output directory must not exist. Vectors are private artifacts, never stdout.
This controls retrieval only; no parent context, Rerank, Agent or answer calls.
"""
import argparse
import asyncio
import copy
from collections import defaultdict
from contextlib import AsyncExitStack
from contextvars import ContextVar
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sys
import threading
import time
from urllib.parse import urlsplit
from unittest.mock import patch


MAX_QUESTIONS = 7
MAX_EMBEDDING_CALLS = 14
MAX_SEARCHES = 28
MEMORY_FLOOR = 800 * 1024**2
DISK_FLOOR = 8 * 1024**3
_capture = ContextVar('ranking_control_capture', default=None)


class ControlStop(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def checked_vector(vector, dimension):
    try:
        valid = (type(vector) is list and len(vector) == dimension
            and all(type(x) in (int, float) and math.isfinite(x) for x in vector))
    except (OverflowError, ValueError, TypeError):
        valid = False
    if not valid:
        raise ControlStop('INVALID_EMBEDDING_VECTOR')
    return tuple(vector)


def balanced_order(index):
    return ('S1', 'P1', 'P2', 'S2') if index % 2 == 0 else ('P1', 'S1', 'S2', 'P2')


def select_plan(cases_path, pairs_dir):
    """Select observed mismatches, never use Gold to select or inject evidence."""
    frozen = json.loads(cases_path.read_text(encoding='utf-8'))
    scope = frozen.get('kb_ids')
    config = frozen.get('fixed_config', {})
    if (not scope or len(scope) != len(set(scope)) or config.get('search_mode') != 'hybrid'
            or config.get('rerank_enabled') is not False):
        raise ControlStop('INVALID_FROZEN_SCOPE_OR_CONFIG')
    records = {}
    for path in sorted(pairs_dir.glob('*.json')):
        row = json.loads(path.read_text(encoding='utf-8'))
        if 'qid' not in row:
            continue
        key = (row['qid'], row['repetition'], row['arm'])
        if key in records:
            raise ControlStop('DUPLICATE_HISTORICAL_PAIR')
        records[key] = row
    mismatches = []
    for qid, repetition, arm in sorted(records):
        if arm != 'parallel':
            continue
        parallel = records[(qid, repetition, 'parallel')]
        serial = records.get((qid, repetition, 'serial'))
        if (serial is None or parallel['request'] != serial['request']
                or parallel['status'] != 'COMPLETED' or serial['status'] != 'COMPLETED'):
            raise ControlStop('INCOMPLETE_HISTORICAL_PAIR')
        fields = lambda row: [(v['chunkId'], v.get('content', ''))
                              for v in row['body']['data']['results']]
        if fields(parallel) != fields(serial):
            mismatches.append({'qid': qid, 'repetition': repetition,
                'parallel_id': parallel['id'], 'serial_id': serial['id']})
    wanted = {row['qid'] for row in mismatches}
    questions = [{'qid': row['qid'], 'query': row['question']}
                 for row in frozen['cases'] if row['qid'] in wanted]
    if (not questions or len(questions) > MAX_QUESTIONS
            or len(questions) != len(wanted) or len(wanted) != len({q['qid'] for q in questions})):
        raise ControlStop('INVALID_QUESTION_SELECTION')
    for question in questions:
        expected = {'data': {**config, 'query': question['query']}, 'kb_ids': scope}
        for key, row in records.items():
            if key[0] == question['qid'] and row['request'] != expected:
                raise ControlStop('HISTORICAL_REQUEST_DIFFERS_FROM_FROZEN_CONFIG')
    return {'scope': scope, 'config': config, 'questions': questions,
            'historical_mismatched_pairs': mismatches,
            'cases_sha256': hashlib.sha256(cases_path.read_bytes()).hexdigest()}


def write_new(path, value):
    with path.open('x', encoding='utf-8') as handle:
        handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


class Resources:
    def __init__(self, output):
        self.output = output
        self.lowest_available = None

    def check(self):
        # The real execution target is Linux ECS; fail closed elsewhere.
        try:
            available = next(int(line.split()[1]) * 1024
                for line in Path('/proc/meminfo').read_text().splitlines()
                if line.startswith('MemAvailable:'))
        except (OSError, StopIteration, ValueError) as exc:
            raise ControlStop('MEMORY_GUARD_UNAVAILABLE') from exc
        free = shutil.disk_usage(self.output).free
        self.lowest_available = (available if self.lowest_available is None
                                 else min(self.lowest_available, available))
        if available < MEMORY_FLOOR or free < DISK_FLOOR:
            raise ControlStop('RESOURCE_FLOOR_REACHED')
        return {'mem_available': available, 'disk_free': free}


def summarize_searches(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[row['qid']].append(row)
    comparisons = []
    for qid, values in groups.items():
        for index, left in enumerate(values):
            for right in values[index + 1:]:
                comparisons.append({'qid': qid, 'left': left['mode'], 'right': right['mode'],
                    **{name: left.get(name) == right.get(name) for name in
                       ('chroma_pages_sha256', 'vector_rrf_input_sha256',
                        'lexical_rrf_input_sha256', 'fused_sha256', 'final_sha256')}})
    return comparisons


def public_rows(rows, lexical=False):
    return [{'id': row.get('id') or row.get('chunk_id'), 'kb_id': row.get('kb_id'),
             'score': row.get('score'),
             **({'bm25_score': row.get('bm25_score')} if lexical else {})}
            for row in rows]


def artifact_hashes(root):
    result = {}
    for path in sorted(root.rglob('*')):
        if path.is_file():
            if not path.resolve().is_relative_to(root):
                raise ControlStop('PRIOR_ARTIFACT_OUTSIDE_RUN')
            result[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def load_replay(root, plan):
    """Validate the completed parent experiment; never rerequest its embeddings."""
    root = root.resolve()
    hashes = artifact_hashes(root)
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    summary = json.loads((root / 'summary.json').read_text(encoding='utf-8'))
    qids = [q['qid'] for q in plan['questions']]
    if (summary.get('status') != 'COMPLETED'
            or manifest.get('schema') != 'hybrid-fixed-vector-control-v1'
            or manifest.get('cases_sha256') != plan['cases_sha256']
            or manifest.get('kb_ids') != plan['scope'] or manifest.get('unique_qids') != qids
            or manifest.get('retrieval_totals') != [500, 500]
            or summary.get('embedding_method_calls') != 2 * len(qids)
            or summary.get('searches') != 4 * len(qids)):
        raise ControlStop('PRIOR_RUN_CONTRACT_MISMATCH')
    counts = {k: summary[k] for k in ('embedding_method_calls', 'embedding_http_sends', 'searches')}
    if (not 0 <= counts['embedding_http_sends'] <= counts['embedding_method_calls'] <= MAX_EMBEDDING_CALLS
            or counts['searches'] > MAX_SEARCHES):
        raise ControlStop('PRIOR_RUN_CALL_CAP_INVALID')
    dimension = manifest['dimension']
    vectors, receipts = {}, {}
    for path in sorted((root / 'embedding').glob('*.json')):
        row = json.loads(path.read_text(encoding='utf-8'))
        key = (row['qid'], row['repetition'])
        if key in receipts or row['status'] != 'RETURNED' or row['dimension'] != dimension:
            raise ControlStop('PRIOR_EMBEDDING_RECEIPT_INVALID')
        private = (root / 'private-vectors' / row['private_vector_file']).resolve()
        if not private.is_relative_to(root / 'private-vectors'):
            raise ControlStop('PRIVATE_VECTOR_PATH_INVALID')
        vector = checked_vector(json.loads(private.read_text(encoding='utf-8')), dimension)
        if fingerprint(vector) != row['vector_sha256']:
            raise ControlStop('PRIOR_VECTOR_HASH_MISMATCH')
        receipts[key], vectors[key] = row, vector
    if set(receipts) != {(q, repetition) for q in qids for repetition in (1, 2)}:
        raise ControlStop('PRIOR_EMBEDDINGS_INCOMPLETE')
    searches = []
    keys = set()
    for path in sorted((root / 'search').glob('*.json')):
        row = json.loads(path.read_text(encoding='utf-8'))
        key = row['qid'], row['mode']
        expected_hash = receipts[(row['qid'], 1)]['vector_sha256']
        if (key in keys or row['status'] != 'COMPLETED' or not row.get('capture_complete')
                or row['frozen_vector_sha256'] != expected_hash
                or set(row['embedding_injections']) != {expected_hash}
                or {v['kb_id'] for v in row['vector_targets']} != set(plan['scope'])
                or {v['kb_id'] for v in row['chroma_pages']} != set(plan['scope'])
                or any(v['query_vector_sha256'] != expected_hash
                       for v in row['vector_targets'] + row['chroma_pages'])):
            raise ControlStop('PRIOR_SEARCH_VECTOR_PROOF_INVALID')
        metrics = row['metrics']
        expected_limits = {'vector_request_depth': 500, 'bm25_initial_request_depth': 500,
            'bm25_supplement_request_depth': 50, 'max_expansion_queries': 5,
            'max_variant_target_pairs': 8, 'target_inflight_limit': 2, 'retrieval_deadline_seconds': 30}
        if (any(metrics.get(name) != value for name, value in expected_limits.items())
                or metrics.get('fallback_used') or metrics.get('retrieval_deadline_exhausted')
                or bool(metrics['hybrid_initial_parallel']) != row['mode'].startswith('P')):
            raise ControlStop('PRIOR_SEARCH_CONFIG_INVALID')
        for field in ('chroma_pages', 'vector_rrf_input', 'lexical_rrf_input', 'fused', 'final'):
            if fingerprint(row[field]) != row[field + '_sha256']:
                raise ControlStop('PRIOR_SEARCH_CAPTURE_HASH_MISMATCH')
        searches.append(row)
        keys.add(key)
    if keys != {(q, mode) for q in qids for mode in ('S1', 'S2', 'P1', 'P2')}:
        raise ControlStop('PRIOR_FIXED_VECTOR_SEARCHES_INCOMPLETE')
    comparisons = summarize_searches(searches)
    if not all(all(row[field] for field in ('chroma_pages_sha256', 'vector_rrf_input_sha256',
        'lexical_rrf_input_sha256', 'fused_sha256', 'final_sha256')) for row in comparisons):
        raise ControlStop('PRIOR_FIXED_VECTOR_SEARCHES_ALREADY_DIFFER')
    changed = [q for q in qids if fingerprint(vectors[q, 1]) != fingerprint(vectors[q, 2])]
    if not changed or counts['searches'] + 2 * len(changed) > MAX_SEARCHES:
        raise ControlStop('REPLAY_SEARCH_CAP_OR_CHANGED_PAIRS_INVALID')
    if artifact_hashes(root) != hashes:
        raise ControlStop('PRIOR_ARTIFACTS_CHANGED_DURING_AUDIT')
    return {'root': root, 'manifest': manifest, 'hashes': hashes, 'counts': counts,
            'changed_qids': changed, 'comparisons': len(comparisons),
            'vectors': {(q, label): vectors[q, repetition] for q in changed
                        for label, repetition in (('A', 1), ('B', 2))}}


class CaptureCollection:
    """Capture true returned KNN distances, without documents or metadata bodies."""
    def __init__(self, collection, kb_id, state, resources):
        self.collection, self.kb_id, self.state = collection, kb_id, state
        self.resources = resources
        self.page = 0

    def __getattr__(self, name):
        return getattr(self.collection, name)

    def query(self, **kwargs):
        self.resources.check()
        vectors = kwargs.get('query_embeddings')
        if len(vectors or []) != 1 or fingerprint(vectors[0]) != self.state['frozen_vector_sha256']:
            raise ControlStop('CHROMA_QUERY_VECTOR_CHANGED')
        result = self.collection.query(**kwargs)
        ids = result.get('ids') or [[]]
        distances = result.get('distances') or [[]]
        page = {'kb_id': self.kb_id, 'page': self.page,
                'query_vector_sha256': fingerprint(vectors[0]),
                'scope_sha256': fingerprint({k: v for k, v in kwargs.items()
                    if k not in ('query_embeddings', 'include')}),
                'ids': ids[0], 'distances': distances[0]}
        if len(page['ids']) != len(page['distances']):
            raise ControlStop('CHROMA_DISTANCE_MAPPING_INVALID')
        page['ids_distances_sha256'] = fingerprint(list(zip(page['ids'], page['distances'])))
        with self.state['_lock']:
            self.state['chroma_pages'].append(page)
        self.page += 1
        return result


async def execute(args, plan, output):
    """The only function importing application modules or permitting live work."""
    sys.path.insert(0, str(args.backend_root))
    if args.env_file:
        from dotenv import dotenv_values
        for key, value in dotenv_values(args.env_file).items():
            if value is not None:
                os.environ.setdefault(key, value)
    # Configure before any app import; no production lifespan/schema startup.
    from sqlalchemy.engine import URL
    db_url = URL.create('sqlite', database='file:' + args.db.as_posix(),
                        query={'mode': 'ro', 'uri': 'true'})
    os.environ.update({'DATABASE_URL': db_url.render_as_string(hide_password=False),
        'ENV': 'test', 'RAG_OBSERVABILITY_ENABLED': 'false', 'LANGFUSE_TRACING_ENABLED': 'false',
        'CHROMA_HOST': args.chroma_host, 'CHROMA_PORT': str(args.chroma_port),
        'RAG_CHROMA_EXECUTOR_ENABLED': 'true', 'RAG_CHROMA_QUERY_WORKERS': str(args.chroma_workers),
        'RAG_RETRIEVAL_VECTOR_DEPTH': '500', 'RAG_RETRIEVAL_BM25_INITIAL_DEPTH': '500',
        'RAG_RETRIEVAL_BM25_SUPPLEMENT_DEPTH': '50', 'RAG_RETRIEVAL_MAX_VARIANTS': '5',
        'RAG_RETRIEVAL_MAX_VARIANT_TARGET_PAIRS': '8', 'RAG_RETRIEVAL_TARGET_INFLIGHT': '2',
        'RAG_RETRIEVAL_DEADLINE_SECONDS': '30', 'RAG_QUERY_EMBEDDING_REUSE_ENABLED': 'true'})
    import httpx
    from sqlalchemy import event
    from sqlalchemy.pool import NullPool
    from sqlmodel import Session, create_engine
    from app.models import KnowledgeBase
    from app.services import retrieval
    from app.services.embedding import EmbeddingService
    from app.services.vector_store import VectorStoreService
    from app.services.bm25 import BM25Index
    from app.services.provider_http import managed_provider_http
    from app.services.bounded_chroma_executor import managed_chroma_executor
    from app.services.bounded_lexical_executor import managed_lexical_executor

    for module in (retrieval, sys.modules[EmbeddingService.__module__],
                   sys.modules[VectorStoreService.__module__]):
        if not Path(module.__file__).resolve().is_relative_to(args.backend_root):
            raise ControlStop('APPLICATION_SOURCE_ROOT_MISMATCH')
    engine = create_engine(db_url, poolclass=NullPool,
        connect_args={'check_same_thread': False, 'timeout': 30})
    @event.listens_for(engine, 'connect')
    def query_only(connection, _):
        connection.execute('PRAGMA query_only=ON')
    resources = Resources(output)
    resource_samples = []
    searches, live_records, frozen_vectors = [], [], {}
    counters = {'embedding_method_calls': 0, 'embedding_http_sends': 0, 'searches': 0}
    prior_counts = dict(counters)
    replay = None
    original_embed = EmbeddingService.embed_query
    original_send = httpx.AsyncClient.send
    phase = ContextVar('ranking_control_phase', default='preflight')
    journal = output / 'events.jsonl'

    def log(row):
        with journal.open('a', encoding='utf-8') as handle:
            handle.write(canonical(row) + '\n')

    async def guarded_send(client, request, **kwargs):
        if (phase.get() != 'live_embedding' or request.method != 'POST'
                or not request.url.path.rstrip('/').endswith('/v1/embeddings')):
            raise ControlStop('UNAUTHORIZED_PROVIDER_REQUEST')
        counters['embedding_http_sends'] += 1
        if counters['embedding_http_sends'] > MAX_EMBEDDING_CALLS:
            raise ControlStop('EMBEDDING_HTTP_CALL_CAP')
        return await original_send(client, request, **kwargs)

    def identity(service, model_id, binding_id, dimension):
        return (binding_id, service.base_url, service.model, service.api_key,
                model_id or service.model, dimension, tuple(sorted(plan['scope'])))

    try:
        if getattr(args, 'replay_from', None):
            replay = load_replay(args.replay_from, plan)
            counters.update(replay['counts'])
            prior_counts = dict(counters)
        # An actual constructor/binding preflight with no provider send.
        captured = []
        async def intercept(service, text, model_id=None):
            captured.append((service, model_id))
            return [0.0] * dimensions[current_kb[0]]
        dimensions, current_kb = {}, [None]
        service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
        with patch.object(EmbeddingService, 'embed_query', intercept), Session(engine) as db:
            bindings = {}
            for kb_id in plan['scope']:
                resources.check()
                kb = db.get(KnowledgeBase, kb_id)
                if kb is None or kb.is_deleted:
                    raise ControlStop('KB_SCOPE_INVALID')
                dimensions[kb_id] = service._known_target_dimension(db, kb_id)
                if dimensions[kb_id] is None:
                    raise ControlStop('TARGET_DIMENSION_UNKNOWN')
                current_kb[0] = kb_id
                bindings[kb_id] = kb.embedding_model
                await service._embed_query_for_kb(db, kb_id, plan['questions'][0]['query'])
        identities = [identity(obj, model, bindings[kb], dimensions[kb])
                      for kb, (obj, model) in zip(plan['scope'], captured)]
        if len(captured) != len(plan['scope']) or any(v != identities[0] for v in identities):
            raise ControlStop('SCOPED_EMBEDDING_IDENTITIES_DIFFER')
        exemplar, model_id = captured[0]
        parsed = urlsplit(exemplar.base_url.rstrip('/'))
        target_model = model_id or exemplar.model
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.scheme not in ('http', 'https') or not parsed.hostname
                or not parsed.path.rstrip('/').endswith(('/v1', '/v1/embeddings'))
                or 'doubao' in target_model.lower() or 'volces.com' in parsed.hostname):
            raise ControlStop('UNSUPPORTED_SINGLE_SEND_EMBEDDING_CONTRACT')
        dimension = dimensions[plan['scope'][0]]
        controlled_questions = ([q for q in plan['questions'] if q['qid'] in replay['changed_qids']]
                                if replay else plan['questions'])
        manifest = {'schema': 'hybrid-fixed-vector-control-v1',
            'status': 'PREFLIGHT_READY', 'source_backend': str(args.backend_root),
            'source_db': str(args.db), 'source_db_bytes': args.db.stat().st_size,
            'cases_sha256': plan['cases_sha256'], 'kb_ids': plan['scope'],
            'historical_mismatched_pairs': plan['historical_mismatched_pairs'],
            'unique_qids': [q['qid'] for q in controlled_questions],
            'planned_embedding_calls': 0 if replay else 2 * len(plan['questions']),
            'planned_searches': (2 if replay else 4) * len(controlled_questions),
            'binding_equal_across_scope': True, 'credentials_exported': False,
            'model': target_model, 'dimension': dimension,
            'chroma_workers': args.chroma_workers, 'retrieval_totals': [500, 500],
            'chroma_endpoint': {'host': args.chroma_host, 'port': args.chroma_port},
            'runner_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'vector_hash_contract': 'canonical-json-numbers-sha256-v1',
            'application_hashes': {str(Path(m.__file__).resolve().relative_to(args.backend_root)):
                hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest()
                for m in (retrieval, sys.modules['app.services.retrieval_budget'],
                          sys.modules[EmbeddingService.__module__], sys.modules[VectorStoreService.__module__])}}
        if replay:
            old = replay['manifest']
            if (old['application_hashes'] != manifest['application_hashes']
                    or old['source_backend'] != str(args.backend_root)
                    or old['source_db'] != str(args.db)
                    or old['source_db_bytes'] != args.db.stat().st_size
                    or old['model'] != target_model or old['dimension'] != dimension
                    or old['chroma_workers'] != args.chroma_workers):
                raise ControlStop('REPLAY_SOURCE_OR_RUNTIME_CONTRACT_CHANGED')
            manifest.update(prior_run=str(replay['root']), prior_counts=prior_counts,
                prior_artifact_sha256=replay['hashes'], replay_mode='serial-A-versus-serial-B',
                cumulative_search_cap=MAX_SEARCHES, provider_sends_allowed=0,
                historical_identity_limit='Prior manifest did not capture endpoint/credential fingerprint; '
                    'prior endpoint is operator-command attested, not retroactively inferred. '
                    'No provider sends or credential exports are permitted in replay.')
        write_new(output / 'manifest.json', manifest)
        if not args.execute:
            return {'status': 'PREFLIGHT_ONLY', **counters}
        if replay:
            # A sibling claim preserves every original receipt while preventing
            # a second invocation from resetting the experiment's 28-search cap.
            claim = replay['root'].parent / (replay['root'].name + '.serial-ab-replay-claim.json')
            try:
                write_new(claim, {'prior_summary_sha256': replay['hashes']['summary.json'],
                    'output': str(output), 'prior_counts': prior_counts,
                    'reserved_incremental_searches': 2 * len(controlled_questions),
                    'automatic_retry': False})
            except FileExistsError as exc:
                raise ControlStop('REPLAY_ALREADY_CLAIMED_NO_AUTOMATIC_RETRY') from exc
            frozen_vectors.update(replay['vectors'])

        # Same connection makes PRAGMA data_version a useful SQL-change guard;
        # this is still not an atomic SQLite/Chroma snapshot.
        with engine.connect() as watcher:
            initial_data_version = watcher.exec_driver_sql('PRAGMA data_version').scalar_one()
            def before_step():
                resource_samples.append(resources.check())
                if watcher.exec_driver_sql('PRAGMA data_version').scalar_one() != initial_data_version:
                    raise ControlStop('SOURCE_SQL_CHANGED')

            async with AsyncExitStack() as stack:
                await stack.enter_async_context(managed_provider_http(preinitialize=True))
                await stack.enter_async_context(managed_chroma_executor())
                await stack.enter_async_context(managed_lexical_executor())
                stack.enter_context(patch.object(httpx.AsyncClient, 'send', guarded_send))
                # Finish both actual embeddings for every question before any
                # fixed-vector search. No UNKNOWN outcome is ever retried.
                for index, question in enumerate([] if replay else plan['questions']):
                    vectors = []
                    for repetition in (1, 2):
                        before_step()
                        counters['embedding_method_calls'] += 1
                        if counters['embedding_method_calls'] > MAX_EMBEDDING_CALLS:
                            raise ControlStop('EMBEDDING_METHOD_CALL_CAP')
                        row = {'qid': question['qid'], 'stage': 'live_embedding',
                            'repetition': repetition, 'status': 'STARTED'}
                        log(row)
                        token = phase.set('live_embedding')
                        began = time.perf_counter()
                        try:
                            value = await original_embed(exemplar, question['query'], model_id=model_id)
                            vector = checked_vector(value, dimension)
                            private_name = f'q{index + 1:02d}-embedding-{repetition}.json'
                            write_new(output / 'private-vectors' / private_name, list(vector))
                            vectors.append(vector)
                            row.update(status='RETURNED', dimension=dimension,
                                vector_sha256=fingerprint(vector), private_vector_file=private_name)
                        except BaseException as exc:
                            row.update(status='UNKNOWN', error_type=type(exc).__name__, automatic_retry=False)
                            raise
                        finally:
                            phase.reset(token)
                            row['seconds'] = time.perf_counter() - began
                            live_records.append(row)
                            log(row)
                            write_new(output / 'embedding' / f'q{index + 1:02d}-{repetition}.json', row)
                    for label, vector in zip(('A', 'B'), vectors):
                        frozen_vectors[question['qid'], label] = vector
                    log({'qid': question['qid'], 'stage': 'embedding_repeat_comparison',
                         'equal_hashes': fingerprint(vectors[0]) == fingerprint(vectors[1]),
                         'max_abs_delta': max(abs(a - b) for a, b in zip(*vectors))})

                async def injected(obj, text, model_id=None):
                    state = _capture.get()
                    if (state is None or text != state['_query']
                            or identity(obj, model_id, bindings[plan['scope'][0]], dimension) != identities[0]):
                        raise ControlStop('FROZEN_EMBEDDING_IDENTITY_MISMATCH')
                    vector = frozen_vectors[state['qid'], state['vector_label']]
                    if fingerprint(vector) != state['frozen_vector_sha256']:
                        raise ControlStop('FROZEN_VECTOR_CHANGED')
                    state['embedding_injections'].append(fingerprint(vector))
                    return list(vector)

                original_collection = VectorStoreService._existing_collection
                def collection(obj, kb_id):
                    actual = original_collection(obj, kb_id)
                    state = _capture.get()
                    return (CaptureCollection(actual, kb_id, state, resources)
                            if actual is not None and state is not None else actual)
                original_query = VectorStoreService.query
                def query(obj, **kwargs):
                    state = _capture.get()
                    resources.check()
                    vector_hash = fingerprint(kwargs['query_vector'])
                    if state is None or vector_hash != state['frozen_vector_sha256']:
                        raise ControlStop('TARGET_QUERY_VECTOR_CHANGED')
                    rows = original_query(obj, **kwargs)
                    with state['_lock']:
                        state['vector_targets'].append({'kb_id': kwargs['kb_id'],
                            'top_k': kwargs['top_k'], 'query_vector_sha256': vector_hash,
                            'rows': public_rows(rows)})
                    return rows
                original_rrf = retrieval.weighted_rrf
                original_lexical = BM25Index.search
                def lexical(obj, *args, **kwargs):
                    resources.check()
                    return original_lexical(obj, *args, **kwargs)
                def rrf(vectors, lexical, *args, **kwargs):
                    state = _capture.get()
                    state['vector_rrf_input'] = public_rows(vectors)
                    state['lexical_rrf_input'] = public_rows(lexical, lexical=True)
                    result = original_rrf(vectors, lexical, *args, **kwargs)
                    state['fused'] = [{k: row.get(k) for k in ('id', 'kb_id', 'score', 'rrf_score',
                        'vector_score', 'bm25_score', 'vector_rank', 'bm25_rank',
                        'vector_contribution', 'bm25_contribution')} for row in result]
                    return result
                stack.enter_context(patch.object(EmbeddingService, 'embed_query', injected))
                stack.enter_context(patch.object(VectorStoreService, '_existing_collection', collection))
                stack.enter_context(patch.object(VectorStoreService, 'query', query))
                stack.enter_context(patch.object(BM25Index, 'search', lexical))
                stack.enter_context(patch.object(retrieval, 'weighted_rrf', rrf))
                service = retrieval.RetrievalService()
                production_scope = service._lexical_execution_scope
                # Serial scheduling exists only in this controlled experiment.
                # Production Hybrid has no serial-mode configuration switch.
                def experiment_scope(db):
                    state = _capture.get()
                    if state is not None and state['mode'].startswith('S'):
                        return None, 'experiment_serial_control'
                    return production_scope(db)
                stack.enter_context(patch.object(service, '_lexical_execution_scope', experiment_scope))
                for index, question in enumerate(controlled_questions):
                    for mode in (('S-A', 'S-B') if replay else balanced_order(index)):
                        before_step()
                        counters['searches'] += 1
                        if counters['searches'] > MAX_SEARCHES:
                            raise ControlStop('SEARCH_CALL_CAP')
                        vector_label = mode[-1] if replay else 'A'
                        state = {'qid': question['qid'], 'mode': mode, 'status': 'STARTED',
                            'vector_label': vector_label,
                            'frozen_vector_sha256': fingerprint(frozen_vectors[question['qid'], vector_label]),
                            'embedding_injections': [], 'vector_targets': [], 'chroma_pages': [],
                            '_lock': threading.Lock(), '_query': question['query']}
                        token = _capture.set(state)
                        began = time.perf_counter()
                        log({k: state[k] for k in ('qid', 'mode', 'status', 'frozen_vector_sha256')})
                        try:
                            with Session(engine) as db:
                                config = plan['config']
                                result, metrics = await service.search(db, question['query'], plan['scope'],
                                    search_mode=config['search_mode'], top_k=config['top_k'],
                                    score_threshold=config['score_threshold'], vector_weight=config['vector_weight'],
                                    bm25_weight=config['bm25_weight'], use_cache=False, log_retrieval=False)
                            state['metrics'] = metrics
                            state['final'] = [{k: row.get(k) for k in
                                ('id', 'kb_id', 'score', 'vector_score', 'bm25_score', 'vector_rank', 'bm25_rank', 'rrf_score')}
                                | {'content_sha256': hashlib.sha256(row.get('content', '').encode()).hexdigest()}
                                for row in result]
                            if (metrics.get('retrieval_status') != 'SUCCESS' or metrics.get('fallback_used')
                                    or metrics.get('retrieval_deadline_exhausted')
                                    or bool(metrics.get('hybrid_initial_parallel')) != mode.startswith('P')):
                                raise ControlStop('RETRIEVAL_INCOMPLETE_OR_WRONG_EXECUTION_MODE')
                            if (not state['embedding_injections'] or
                                    {v['kb_id'] for v in state['vector_targets']} != set(plan['scope'])
                                    or {p['kb_id'] for p in state['chroma_pages']} != set(plan['scope'])):
                                raise ControlStop('MISSING_TARGET_VECTOR_VERIFICATION')
                            state['chroma_pages'].sort(key=lambda v: (v['kb_id'], v['page']))
                            state['vector_targets'].sort(key=lambda v: v['kb_id'])
                            for field in ('chroma_pages', 'vector_rrf_input', 'lexical_rrf_input', 'fused', 'final'):
                                state[field + '_sha256'] = fingerprint(state[field])
                            state['status'] = 'COMPLETED'
                        except BaseException as exc:
                            state.update(status='UNKNOWN' if not isinstance(exc, ControlStop) else 'CONTROL_ABORTED',
                                         error_type=type(exc).__name__, automatic_retry=False)
                            raise
                        finally:
                            _capture.reset(token)
                            state['seconds'] = time.perf_counter() - began
                            with state['_lock']:
                                clean = copy.deepcopy({k: v for k, v in state.items() if not k.startswith('_')})
                            clean['capture_complete'] = state['status'] == 'COMPLETED'
                            searches.append(clean)
                            write_new(output / 'search' / f'q{index + 1:02d}-{mode}.json', clean)
                            log({k: clean[k] for k in ('qid', 'mode', 'status', 'seconds')})
        if replay and artifact_hashes(replay['root']) != replay['hashes']:
            raise ControlStop('PRIOR_ARTIFACTS_CHANGED_DURING_REPLAY')
        return {'status': 'COMPLETED', **counters, 'questions': len(controlled_questions),
            'replay_mode': bool(replay),
            'incremental_embedding_method_calls': counters['embedding_method_calls'] - prior_counts['embedding_method_calls'],
            'incremental_embedding_http_sends': counters['embedding_http_sends'] - prior_counts['embedding_http_sends'],
            'incremental_searches': counters['searches'] - prior_counts['searches'],
            'prior_artifacts_unchanged': True if replay else None,
            'prior_fixed_vector_comparisons': replay['comparisons'] if replay else None,
            'prior_live_embedding_hash_changes': len(replay['changed_qids']) if replay else None,
            'live_embedding_hash_changes': sum(
                live_records[i]['vector_sha256'] != live_records[i + 1]['vector_sha256']
                for i in range(0, len(live_records), 2)),
            'comparisons': summarize_searches(searches),
            'mem_available_min': resources.lowest_available,
            'parent_context_rerank_agent_calls': 0,
            'limits': ['No atomic SQLite/Chroma snapshot; SQL data_version guarded only.',
                'No performance claim: captures/guards add work.',
                'A fixed-vector difference locates variation after embedding, not its internal ANN/numeric cause.']}
    except BaseException as exc:
        return {'status': 'ABORTED', 'error_type': type(exc).__name__,
                'reason_code': str(exc) if isinstance(exc, ControlStop) else None,
                **counters, 'automatic_retry': False, 'completed_searches': len(searches),
                'mem_available_min': resources.lowest_available}
    finally:
        engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cases', type=Path, required=True)
    parser.add_argument('--pairs-dir', type=Path, required=True)
    parser.add_argument('--backend-root', type=Path, required=True)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--chroma-host', required=True)
    parser.add_argument('--chroma-port', type=int, default=8001)
    parser.add_argument('--chroma-workers', type=int, choices=range(1, 17), default=1)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--replay-from', type=Path,
        help='Reuse only changed A/B vectors from a completed run; zero providers, cumulative cap, one-use sibling claim.')
    parser.add_argument('--execute', action='store_true', help='Permit the bounded live model/search stages; otherwise preflight only.')
    args = parser.parse_args(argv)
    args.backend_root, args.db, args.out = (p.resolve() for p in (args.backend_root, args.db, args.out))
    if not args.db.is_file() or not (args.backend_root / 'app/services/retrieval.py').is_file():
        parser.error('Existing DB and candidate backend source are required')
    plan = select_plan(args.cases, args.pairs_dir)
    args.out.mkdir(mode=0o700, parents=False, exist_ok=False)
    previous_umask = os.umask(0o077)
    try:
        for directory in ('private-vectors', 'embedding', 'search'):
            (args.out / directory).mkdir(mode=0o700)
        summary = asyncio.run(execute(args, plan, args.out))
        write_new(args.out / 'summary.json', summary)
        print(canonical({k: summary.get(k) for k in
            ('status', 'replay_mode', 'embedding_method_calls', 'embedding_http_sends', 'searches',
             'incremental_embedding_http_sends', 'incremental_searches', 'error_type')}))
        return 0 if summary['status'] in ('COMPLETED', 'PREFLIGHT_ONLY') else 2
    finally:
        os.umask(previous_umask)


if __name__ == '__main__':
    raise SystemExit(main())

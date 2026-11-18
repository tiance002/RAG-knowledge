"""Support for benchmark_bm25_write_payload --publication; no separate CLI.

All storage is synthetic and local. Source modules are imported in full, with
their own BM25 and vector implementations; shared dependencies remain current.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
import gc
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import statistics
import sys
import tempfile
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

os.environ.update(DATABASE_URL='sqlite://', REDIS_URL='memory://',
                  RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false',
                  ANONYMIZED_TELEMETRY='False', CHROMA_HOST='')
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine, select
from app.models import (KnowledgeBase, FileDocument, FileIndexBuild, DocumentChunk,
                        ChunkIndex, BM25Posting, IndexStats)
from app.tasks import dispatch
import chromadb
from chromadb.config import Settings

SERVICE_NAMES = ('bm25', 'vector_store', 'index_builds')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def freeze_sources(reference_dir, candidate_dir=None):
    """Read all six source files once before loading either arm."""
    return {arm: {name: (directory / (name + '.py')).read_bytes() for name in SERVICE_NAMES}
            for arm, directory in [('reference', Path(reference_dir)),
                                   ('candidate', Path(candidate_dir) if candidate_dir is not None else ROOT / 'backend/app/services')]}


def shared_hashes(paths=None):
    # Fingerprint loaded application dependencies, not unrelated workstreams.
    if paths is None:
        paths = set()
        for name,module in list(sys.modules.items()):
            location=getattr(module,'__file__',None)
            if name.startswith('app.') and location:
                path=Path(location).resolve()
                if path.is_relative_to(ROOT/'backend/app') and path.suffix=='.py':
                    if path.name not in {n+'.py' for n in SERVICE_NAMES}: paths.add(str(path.relative_to(ROOT)))
        paths.update('backend/app/services/'+name+'.py' for name in ('pdf_provenance','observability'))
    return {str(path):digest((ROOT/path).read_bytes()) for path in sorted(paths)}


def load_arm(name, sources, directory):
    """Load full frozen modules, temporarily binding their ordinary import names."""
    modules = {}
    saved = {key: sys.modules.get('app.services.' + key) for key in SERVICE_NAMES}
    try:
        for key in SERVICE_NAMES:
            path = Path(directory) / f'{name}-{key}.py'
            path.write_bytes(sources[key])
            spec = importlib.util.spec_from_file_location('app.services.' + key, path)
            module = importlib.util.module_from_spec(spec)
            sys.modules['app.services.' + key] = module
            spec.loader.exec_module(module)
            modules[key] = module
        assert modules['index_builds'].VectorStoreService is modules['vector_store'].VectorStoreService
        assert modules['index_builds'].get_bm25_index is modules['bm25'].get_bm25_index
        return modules
    finally:
        for key, previous in saved.items():
            if previous is None: sys.modules.pop('app.services.' + key, None)
            else: sys.modules['app.services.' + key] = previous


def publication_fixture(parents=120, children_per_parent=10, vocabulary=50000):
    """Reuse the established deterministic source fixture, with valid typed UUIDs."""
    from benchmark_bm25_write_payload import fixture
    values = fixture(parents, children_per_parent, vocabulary)
    result = []
    for rows in values:
        mapping = {row['id']: str(uuid5(NAMESPACE_URL, 'publication/' + row['id'])) for row in rows}
        result.append([{**row, 'id': mapping[row['id']],
                        **({'parent_id': mapping[row['parent_id']]} if row.get('parent_id') else {})}
                       for row in rows])
    return tuple(result)


def embeddings_for(rows):
    return [[((i * 17 + j * 13) % 251 - 125) / 125.0 for j in range(256)]
            for i, row in enumerate(rows) if row.get('chunk_type') != 'parent']


@contextmanager
def local_storage_guard():
    """Fail closed on accidental network or default application DB access."""
    from app.core.database import engine as default_engine
    def deny_network(*args, **kwargs):
        raise RuntimeError('PUBLICATION_BENCHMARK_NETWORK_FORBIDDEN')
    def deny_database(*args, **kwargs):
        raise RuntimeError('PUBLICATION_BENCHMARK_DEFAULT_DATABASE_FORBIDDEN')
    originals = (socket.create_connection, socket.socket.connect, socket.socket.connect_ex)
    socket.create_connection = socket.socket.connect = socket.socket.connect_ex = deny_network
    event.listen(default_engine, 'connect', deny_database)
    event.listen(default_engine, 'checkout', deny_database)
    try:
        yield
    finally:
        event.remove(default_engine, 'connect', deny_database)
        event.remove(default_engine, 'checkout', deny_database)
        socket.create_connection, socket.socket.connect, socket.socket.connect_ex = originals


def run_one(name, modules, rows, directory, run_id):
    with local_storage_guard():
        return _run_one(name, modules, rows, directory, run_id)


def _run_one(name, modules, rows, directory, run_id):
    """Run one independent sample. Seed real ACTIVE builds outside timing.

    Returns raw evidence plus a full canonical logical snapshot. It intentionally
    performs no cross-arm acceptance; the caller journals first, then validates.
    """
    index, bm25, vector = (modules[k] for k in ('index_builds', 'bm25', 'vector_store'))
    old, new, background = rows
    path = Path(directory) / f'{run_id}-{name}.db'
    if path.exists(): raise FileExistsError(path)
    wal = Path(str(path) + '-wal')
    engine = create_engine('sqlite:///' + path.as_posix())
    @event.listens_for(engine, 'connect')
    def configure(connection, record):
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA wal_autocheckpoint=0')
    client = chromadb.EphemeralClient(settings=Settings(anonymized_telemetry=False))
    kb_id = 'p3-' + str(uuid4())
    original_engine, original_index_engine = dispatch.engine, index.engine
    original_client = vector.VectorStoreService._client
    original_phase = index._publication_phase
    original_commit = index._commit_index_transaction
    original_guard, original_complete = dispatch.guard_claim_in_session, dispatch.complete_in_session
    stages, sql, chroma = [], defaultdict(int), defaultdict(lambda: defaultdict(float))
    state = {'active': False, 'stack': []}
    ledger_calls = defaultdict(int)
    committed_build_states = []
    restores = []
    keeper = None
    result = None
    cleanup = {'collection_deleted': False, 'engine_disposed': False, 'database_removed': False}
    def commit(session):
        pending = [(obj.build_id, obj.state) for obj in list(session.identity_map.values())+list(session.new)
                   if isinstance(obj, FileIndexBuild)]
        output = original_commit(session)
        if state['active']: committed_build_states.append([value for _,value in pending])
        return output
    def guard(session, claim):
        output = original_guard(session, claim)
        if state['active']: ledger_calls['guard_calls'] += 1; ledger_calls['guard_successes'] += int(output)
        return output
    def complete(session, claim, value):
        output = original_complete(session, claim, value)
        if state['active']: ledger_calls['complete_calls'] += 1; ledger_calls['complete_successes'] += int(output)
        return output
    def sql_event(connection, cursor, statement, parameters, context, executemany):
        if not state['active']: return
        kind = statement.lower().lstrip().split(None, 1)[0]
        sql['calls'] += 1; sql[kind + '_calls'] += 1
        sql['parameter_sets'] += len(parameters) if executemany else 1
        if statement.lower().lstrip().startswith('insert into bm25_postings'):
            sql['posting_insert_max_rows'] = max(sql['posting_insert_max_rows'], len(parameters) if executemany else 1)
    @contextmanager
    def phase(stage, **counts):
        started = time.perf_counter(); before = wal.stat().st_size if wal.exists() else 0
        with original_phase(stage, **counts) as real_counts:
            try: yield real_counts
            finally:
                if state['active']:
                    stages.append({'stage':stage, 'wall_ms':(time.perf_counter()-started)*1000,
                                   'wal_before_bytes':before, 'wal_after_bytes':wal.stat().st_size if wal.exists() else 0,
                                   'counts':dict(real_counts)})
    # Instrument actual service methods, always delegate to their complete bodies.
    for method in ('_ingest_existing_collection', '_ingest_get_or_create_collection',
                   '_ingest_count', '_ingest_dimension_query', '_ingest_add', '_ingest_get'):
        original = getattr(vector.VectorStoreService, method)
        def wrapper(self, *args, _original=original, _method=method, **kwargs):
            start = time.perf_counter()
            output = _original(self, *args, **kwargs)
            if state['active']:
                entry = chroma[_method]; entry['calls'] += 1
                entry['wall_ms'] += (time.perf_counter()-start)*1000
                if 'ids' in kwargs: entry['requested_ids'] += len(kwargs['ids'])
                if isinstance(output, dict) and 'ids' in output: entry['returned_ids'] += len(output['ids'])
            return output
        setattr(vector.VectorStoreService, method, wrapper); restores.append((method, original))
    dispatch.engine = index.engine = engine
    dispatch.guard_claim_in_session, dispatch.complete_in_session = guard, complete
    vector.VectorStoreService._client = client
    index._publication_phase = phase
    index._commit_index_transaction = commit
    try:
        SQLModel.metadata.create_all(engine); keeper = engine.raw_connection()
        with Session(engine) as session:
            session.add(KnowledgeBase(id=kb_id, name='SYNTHETIC', embedding_model='synthetic-256', vlm_model=''))
            for file_id in ('target', 'background'):
                session.add(FileDocument(id=file_id, kb_id=kb_id, name=file_id+'.txt',
                    local_path='SYNTHETIC_ONLY', size=0, desired_generation=1))
            session.commit()
        def publish(file_id, values, task_id, generation):
            with Session(engine) as session:
                doc = session.get(FileDocument, file_id); doc.desired_generation = generation; session.add(doc)
                session.add(dispatch.FileTaskReceipt(task_id=task_id, file_id=file_id, active_file=file_id,
                    operation='index', stage='index', payload='{}', target_generation=generation))
                session.commit()
            claim = dispatch.claim_task(task_id, 'index', 'synthetic-worker')
            if claim is None: raise AssertionError('REAL_CLAIM_FAILED')
            return claim, index.publish_index_build(claim, values, embeddings_for(values), 'synthetic-256')
        _, old_result = publish('target', old, 'initial-target', 1)
        _, background_result = publish('background', background, 'initial-background', 1)
        # Freeze complete background SQL/vector evidence before replacement.
        before = semantic_snapshot(engine, bm25, client, vector, kb_id, rows)
        with Session(engine) as session:
            doc = session.get(FileDocument, 'target'); doc.desired_generation = 2; session.add(doc)
            session.add(dispatch.FileTaskReceipt(task_id='replacement',file_id='target',active_file='target',
                operation='index',stage='index',payload='{}',target_generation=2));session.commit()
        claim = dispatch.claim_task('replacement','index','synthetic-worker')
        if claim is None: raise AssertionError('REAL_CLAIM_FAILED')
        with engine.connect() as connection:
            checkpoint = list(connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)').one())
            policy = {key:connection.exec_driver_sql('PRAGMA '+key).scalar() for key in
                      ('journal_mode','wal_autocheckpoint','synchronous','page_size')}
        if checkpoint[0] != 0: raise AssertionError('CHECKPOINT_BUSY')
        vectors = embeddings_for(new)
        gc.collect(); event.listen(engine, 'before_cursor_execute', sql_event)
        wal_before = wal.stat().st_size if wal.exists() else 0
        state['active'] = True; started = time.perf_counter()
        try:
            try: published = index.publish_index_build(claim, new, vectors, 'synthetic-256')
            finally:
                total = (time.perf_counter()-started)*1000; state['active'] = False
                event.remove(engine, 'before_cursor_execute', sql_event)
        except BaseException as exc:
            with Session(engine) as session:
                terminal = {'builds':[(b.task_id,b.state) for b in session.exec(select(FileIndexBuild)).all()],
                            'receipt_status':session.get(dispatch.FileTaskReceipt,claim.task_id).status,
                            'active_build_unchanged':session.get(FileDocument,'target').active_build_id==old_result['build_id']}
                terminal['target_content_sha256'] = sorted(digest(c.content.encode()) for c in session.exec(
                    select(DocumentChunk).where(DocumentChunk.file_id=='target')).all())
            exc.publication_evidence = {'terminal':terminal,'cleanup':cleanup,'ledger_calls':dict(ledger_calls),
                                        'total_ms':total,'phases':stages,'sql':dict(sql),
                                        'chroma':{k:dict(v) for k,v in chroma.items()},
                                        'background_before_sha256':before['background_sha256']}
            raise
        wal_after = wal.stat().st_size if wal.exists() else 0
        snapshot = semantic_snapshot(engine, bm25, client, vector, kb_id, rows)
        old_vectors_before=[r for r in before['logical']['vectors'] if r[0].startswith('old:')]
        old_vectors_after=[r for r in snapshot['logical']['vectors'] if r[0].startswith('old:')]
        lifecycle = read_lifecycle(engine, published, old_result, background_result, claim)
        result = {'arm':name,'total_ms':total,'phases':stages,'sql':dict(sql),
                  'chroma':{key:dict(value) for key,value in chroma.items()},
                  'wal_before_bytes':wal_before,'wal_after_bytes':wal_after,'wal_growth_bytes':wal_after-wal_before,
                  'checkpoint_before':checkpoint,'sqlite_policy':policy,'semantic':snapshot,
                  'input_sha256':digest(canonical({'rows':rows,'embeddings':[embeddings_for(v) for v in rows]}).encode()),
                  'background_before_sha256':before['background_sha256'],
                  'retired_vectors_before_sha256':digest(canonical(old_vectors_before).encode()),
                  'retired_vectors_after_sha256':digest(canonical(old_vectors_after).encode()),
                  'lifecycle':lifecycle,'ledger_calls':dict(ledger_calls),
                  'committed_build_states':committed_build_states,'cleanup':cleanup,
                  'timing_scope':'actual publish_index_build entry through return including natural Python cleanup; seed/claim/readback excluded',
                  'rss':'NOT MEASURED: native/server allocation peak not established'}
        return result
    finally:
        try:
            collection_name = vector.VectorStoreService._get_collection_name(kb_id)
            if collection_name in [c.name for c in client.list_collections()]:
                client.delete_collection(collection_name)
            cleanup['collection_deleted'] = collection_name not in [c.name for c in client.list_collections()]
        finally:
            for method, original in restores: setattr(vector.VectorStoreService, method, original)
            dispatch.engine, index.engine = original_engine, original_index_engine
            dispatch.guard_claim_in_session, dispatch.complete_in_session = original_guard, original_complete
            index._publication_phase = original_phase
            index._commit_index_transaction = original_commit
            vector.VectorStoreService._client = original_client
            if keeper is not None: keeper.close()
            engine.dispose(); cleanup['engine_disposed'] = True
            removed_bytes = 0
            for suffix in ('', '-wal', '-shm'):
                item = Path(str(path)+suffix)
                if item.exists(): removed_bytes += item.stat().st_size; item.unlink()
            cleanup['removed_bytes'] = removed_bytes
            cleanup['database_removed'] = all(not Path(str(path)+s).exists() for s in ('','-wal','-shm'))


def semantic_snapshot(engine, bm25, client, vector, kb_id, fixture_rows):
    """Canonicalize fresh UUIDs through known source/content positions, never counts."""
    known = {}
    for family, rows in zip(('old','new','background'), fixture_rows):
        for position,row in enumerate(rows):
            key=(row['chunk_type'],row['content'])
            if key in known: raise ValueError('DUPLICATE_FIXTURE_CONTENT_IDENTITY')
            known[key] = f'{family}:{position}'
    with Session(engine) as session:
        chunks = session.exec(select(DocumentChunk)).all()
        builds = session.exec(select(FileIndexBuild)).all()
        build_alias = {b.build_id:b.task_id for b in builds}
        collection = client.get_collection(vector.VectorStoreService._get_collection_name(kb_id))
        stored = collection.get(include=['documents','metadatas','embeddings'])
        id_alias = {row.id:known[(row.chunk_type,row.content)] for row in chunks}
        for i,ident in enumerate(stored['ids']):
            id_alias[ident] = known[(stored['metadatas'][i]['chunk_type'],stored['documents'][i])]
        # Retired parents have no vectors: recover positions from the immutable manifest.
        for build in builds:
            family = {'initial-target':'old','replacement':'new','initial-background':'background'}[build.task_id]
            manifest = json.loads(build.manifest)
            for position,ident in enumerate(manifest['chunk_ids']): id_alias[ident] = f'{family}:{position}'
        def norm(value):
            if isinstance(value,str): return id_alias.get(value,build_alias.get(value,'KB' if value==kb_id else value))
            if isinstance(value,list): return [norm(v) for v in value]
            if isinstance(value,dict): return {k:norm(v) for k,v in value.items()}
            if hasattr(value,'value'): return value.value
            return value
        indices = session.exec(select(ChunkIndex)).all(); ci_alias = {r.id:id_alias[r.chunk_id] for r in indices}
        postings = session.exec(select(BM25Posting)).all()
        stats = session.get(IndexStats,kb_id)
        logical = {'chunks':sorted((id_alias[r.id],r.file_id,r.chunk_type,norm(r.parent_id),
                        digest(r.content.encode()),r.page_number,norm(r.content_type),r.original_index,
                        r.heading_level,r.heading_text,r.token_count,r.start_offset,r.end_offset,r.source_metadata_json) for r in chunks),
                   'indices':sorted((ci_alias[r.id],norm(r.kb_id),json.loads(r.terms),r.doc_length) for r in indices),
                   'postings':sorted((norm(r.kb_id),r.term,ci_alias[r.index_id],r.tf) for r in postings),
                   'posting_df':dict(Counter(r.term for r in postings)),
                   'stats':{'total_docs':stats.total_docs,'avg_doc_length':stats.avg_doc_length,
                            'legacy_df':json.loads(stats.term_doc_freq)},
                   'builds':sorted((b.task_id,b.file_id,b.generation,b.attempt_no,b.state,b.chunk_count,
                                     norm(json.loads(b.manifest))) for b in builds),
                   'files':sorted((r.id,norm(r.status),r.desired_generation,norm(r.active_build_id),r.progress)
                                  for r in session.exec(select(FileDocument)).all()),
                   'kb':[(r.name,r.embedding_model,r.chunk_count,r.is_deleted) for r in session.exec(select(KnowledgeBase)).all()],
                   'receipts':sorted((r.task_id,r.file_id,r.status,r.stage,r.attempt_no,r.target_generation,
                                      norm(json.loads(r.result)) if r.result else None,r.active_file,r.owner_token,r.lease_until)
                                     for r in session.exec(select(dispatch.FileTaskReceipt)).all()),
                   'attempts':sorted((r.task_id,r.attempt_no,r.stage,r.worker_id,r.outcome,r.error_code)
                                     for r in session.exec(select(dispatch.FileTaskAttempt)).all())}
        vectors = []
        for i,ident in enumerate(stored['ids']):
            vectors.append((id_alias[ident],digest(stored['documents'][i].encode()),norm(stored['metadatas'][i]),
                            [float(v) for v in stored['embeddings'][i]]))
        logical['vectors'] = sorted(vectors)
        queries = {}
        for query in ('evidence','fresh','prior','background_term_000001','group42 field3','中文 mixed 研究'):
            hits = bm25.get_bm25_index().search(session,query,kb_id,top_k=len(indices)+1)
            queries[query] = {'all':[(id_alias[h['chunk_id']],h['bm25_score']) for h in hits],
                              'top20':[(id_alias[h['chunk_id']],h['bm25_score']) for h in
                                       bm25.get_bm25_index().search(session,query,kb_id,top_k=20)]}
        logical['queries'] = queries
        q = collection.query(query_embeddings=[embeddings_for(fixture_rows[1])[0]],n_results=collection.count(),
                             include=['distances'])
        raw_vector_order=[(id_alias[i],float(d)) for i,d in zip(q['ids'][0],q['distances'][0])]
        logical['vector_query_membership_distances'] = sorted(raw_vector_order)
        background = {'chunks':[r for r in logical['chunks'] if r[1]=='background'],
                      'indices':[r for r in logical['indices'] if r[0].startswith('background:')],
                      'postings':[r for r in logical['postings'] if r[2].startswith('background:')],
                      'vectors':[r for r in logical['vectors'] if r[0].startswith('background:')],
                      'builds':[r for r in logical['builds'] if r[1]=='background'],
                      'files':[r for r in logical['files'] if r[0]=='background']}
        return {'full_logical_sha256':digest(canonical(logical).encode()),'logical':logical,
                'background_sha256':digest(canonical(background).encode()),
                'raw_vector_query_order_diagnostic':raw_vector_order,
                'rank_comparison':'BM25 exact actual full and top20 sequence; vector full membership/distances only, raw HNSW order diagnostic',
                'parent_count':sum(r.chunk_type=='parent' for r in chunks),
                'child_count':sum(r.chunk_type=='child' for r in chunks),'vector_count':len(vectors)}


def read_lifecycle(engine, published, old, background, claim):
    with Session(engine) as session:
        active = session.get(FileIndexBuild,published['build_id'])
        previous = session.get(FileIndexBuild,old['build_id'])
        other = session.get(FileIndexBuild,background['build_id'])
        receipt = session.get(dispatch.FileTaskReceipt,claim.task_id)
        attempt = session.get(dispatch.FileTaskAttempt,(claim.task_id,claim.attempt_no))
        return {'active_state':active.state,'previous_state':previous.state,'background_state':other.state,
                'receipt_status':receipt.status,'attempt_outcome':attempt.outcome,
                'receipt_result_matches':json.loads(receipt.result)==published,
                'active_task_matches':active.task_id==claim.task_id and active.attempt_no==claim.attempt_no,
                'attempt_owner_matches':attempt.owner_token==claim.owner_token,
                'active_file_matches':session.get(FileDocument,claim.file_id).active_build_id==published['build_id'],
                'manifest_hash_matches':digest(active.manifest.encode())==active.manifest_hash}


def validate_run(run):
    assert run['semantic']['background_sha256']==run['background_before_sha256'],'BACKGROUND_CHANGED'
    assert run['retired_vectors_before_sha256']==run['retired_vectors_after_sha256'],'RETIRED_VECTORS_CHANGED'
    life = run['lifecycle']
    assert (life['active_state'],life['previous_state'],life['background_state'])==('ACTIVE','RETIRED','ACTIVE')
    assert life['receipt_status']==life['attempt_outcome']=='COMPLETED'
    assert all(value for key,value in life.items() if key.endswith('matches'))
    assert all(run['cleanup'][key] for key in ('collection_deleted','engine_disposed','database_removed'))
    assert run['ledger_calls']=={'guard_calls':1,'guard_successes':1,'complete_calls':1,'complete_successes':1}
    assert 'STAGING' in run['committed_build_states'][0]
    assert 'ACTIVE' in run['committed_build_states'][-1]
    logical=run['semantic']['logical']
    assert not any(r[0].startswith('old:') for r in logical['chunks'])
    parent_ids = {r[0] for r in logical['chunks'] if r[2]=='parent'}
    assert not any(r[0] in parent_ids for r in logical['indices'])
    assert not any(r[2] in parent_ids for r in logical['postings'])
    assert not any(r[0] in parent_ids for r in logical['vectors'])


def assert_equal(left, right):
    assert left['full_logical_sha256']==right['full_logical_sha256'],'FULL_PUBLICATION_SEMANTIC_MISMATCH'


def archive_record(run, snapshotdir):
    """Persist each full logical payload once, retaining even unaccepted samples.

    Return a compact copy without changing the full in-memory run used for
    validation. Address storage by the actual bytes rather than a supplied hash,
    so a bad declared identity is retained without overwriting another payload.
    """
    payload = canonical(run['semantic']['logical']).encode('utf8')
    actual_hash = digest(payload)
    snapshotdir = Path(snapshotdir)
    snapshotdir.mkdir(parents=True, exist_ok=True)
    path = snapshotdir / (actual_hash + '.json')
    try:
        with path.open('xb') as handle: handle.write(payload)
    except FileExistsError:
        if path.read_bytes()!=payload: raise ValueError('SNAPSHOT_ARCHIVE_IDENTITY_CONFLICT')
    semantic = {key:value for key,value in run['semantic'].items() if key!='logical'}
    semantic.update(snapshot_ref=str(path.resolve()), snapshot_sha256=actual_hash)
    return {**run, 'semantic':semantic}


def main_publication(args):
    if args.candidate or args.reference or args.baseline or args.arms:
        raise ValueError('--publication uses frozen reference-services and current candidate; other source/arms flags are not applicable')
    candidate_dir=getattr(args,'candidate_services',None)
    sources=freeze_sources(args.reference_services,candidate_dir)
    source_directories={'reference':str(Path(args.reference_services).resolve()),
                        'candidate':str((Path(candidate_dir) if candidate_dir is not None else ROOT/'backend/app/services').resolve())}
    journal=args.output.with_suffix(args.output.suffix+'.runs.jsonl')
    identity=args.output.with_suffix(args.output.suffix+'.identity.json')
    snapshotdir=args.output.with_suffix(args.output.suffix+'.snapshots')
    if any(path.exists() for path in (args.output,journal,identity,snapshotdir)):
        raise FileExistsError('use new output/journal/identity/snapshot paths')
    snapshotdir.mkdir(parents=True,exist_ok=False)
    journal.touch(exist_ok=False)
    rows=publication_fixture(); runs=[]; accepted=None
    with tempfile.TemporaryDirectory(prefix='publication-',dir=args.output.parent) as temporary:
        directory=Path(temporary)
        modules={name:load_arm(name,raw,directory) for name,raw in sources.items()}
        from app.services import pdf_provenance, observability  # freeze lazy publication dependencies before samples
        shared_before=shared_hashes()
        with identity.open('x',encoding='utf8') as handle:
            json.dump({'sources':{name:{key:digest(raw) for key,raw in arm.items()} for name,arm in sources.items()},
                       'source_directories':source_directories,
                       'shared_sources_before':shared_before,
                       'input_sha256':digest(canonical({'rows':rows,'embeddings':[embeddings_for(v) for v in rows]}).encode()),
                       'runner_sha256':digest(Path(__file__).read_bytes()),
                       'entrypoint_sha256':digest((ROOT/'scripts/benchmark_bm25_write_payload.py').read_bytes())},handle,indent=2)
        for kind,count in [('warmup',args.warmups),('formal',args.repetitions)]:
            for repetition in range(count):
                order=['reference','candidate'] if repetition%2==0 else ['candidate','reference']
                for position,name in enumerate(order):
                    try: run=run_one(name,modules[name],rows,directory,f'{kind}-{repetition}')
                    except BaseException as exc:
                        with journal.open('a',encoding='utf8') as handle:
                            handle.write(canonical({'status':'FAILED','arm':name,'sample_kind':kind,'repetition':repetition,
                                                    'error_type':type(exc).__name__,'error':str(exc),
                                                    'evidence':getattr(exc,'publication_evidence',None)})+'\n')
                        raise
                    run.update(sample_kind=kind,repetition=repetition,order_position=position,arm_order=order)
                    archived=archive_record(run,snapshotdir)
                    with journal.open('a',encoding='utf8') as handle:handle.write(canonical(archived)+'\n')
                    assert archived['semantic']['snapshot_sha256']==run['semantic']['full_logical_sha256'],'DECLARED_SNAPSHOT_IDENTITY_MISMATCH'
                    validate_run(run)
                    if accepted is None:accepted=run['semantic']
                    else:assert_equal(accepted,run['semantic'])
                    runs.append(archived)
                    print(canonical({'arm':name,'sample_kind':kind,'repetition':repetition,'total_ms':run['total_ms'],'semantics':'MATCH'}),flush=True)
    shared_after=shared_hashes(shared_before)
    source_after=freeze_sources(args.reference_services,candidate_dir)
    assert shared_before==shared_after,'SHARED_SOURCE_CHANGED'
    assert sources==source_after,'ARM_SOURCE_CHANGED'
    summary={name:{field:{'median':statistics.median(values),'min':min(values),'max':max(values)}
                  for field in ('total_ms','wal_growth_bytes')
                  for values in [[r[field] for r in runs if r['arm']==name and r['sample_kind']=='formal']]}
             for name in sources}
    report={'schema':'synthetic-full-publication/v1','scope':'local real SQLite WAL + real Chroma EphemeralClient; no models/ECS',
            'sources':{name:{key:digest(raw) for key,raw in arm.items()} for name,arm in sources.items()},
            'source_directories':source_directories,
            'shared_sources_before':shared_before,'shared_sources_after':shared_after,'sources_unchanged':True,
            'runner_sha256':digest(Path(__file__).read_bytes()),'runs':runs,'summary':summary,
            'new_source_bytes':sum(len(r['content'].encode()) for r in rows[1] if r['chunk_type']!='parent'),
            'parents':120,'children':1200,'vector_dimensions':256,'warmups_per_arm':args.warmups,
            'formal_samples_per_arm':args.repetitions,'run_journal':str(journal),
            'snapshot_directory':str(snapshotdir),'snapshot_storage':'one canonical logical payload per actual SHA256; sample records reference it',
            'limits':'Synthetic local timing; nested stages must not be summed; no full RSS/IOPS/parse/embedding/network claims'}
    with args.output.open('x',encoding='utf8') as handle:json.dump(report,handle,indent=2)
    print(canonical({'status':'PASS','output':str(args.output),'summary':summary}),flush=True)

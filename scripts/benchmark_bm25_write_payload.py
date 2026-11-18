"""SYNTHETIC real BM25 file replacements; local SQLite, no models or user data."""
import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager
import gc
import hashlib
import importlib.util
import importlib.metadata
import json
import logging
import math
import os
from pathlib import Path
import platform
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
os.environ.update(DATABASE_URL='sqlite://', RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false', REDIS_URL='memory://', ANONYMIZED_TELEMETRY='False')
from sqlalchemy import delete, event
from sqlmodel import Session, SQLModel, create_engine, select
from app.models import BM25Posting, ChunkIndex, DocumentChunk, FileDocument, FileStatus, IndexStats, KnowledgeBase

OLD = '2041bfb5355bab5c0a4e123d413dabcbd619125a'
CURRENT = '7c7415f85a063e3f7645f676bd22f7c8f766a732'


def sha(value): return hashlib.sha256(value).hexdigest()
def canonical(value): return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
def size(path): return path.stat().st_size if path.exists() else 0


def git_source(commit):
    result = subprocess.run(['git', 'show', commit + ':backend/app/services/bm25.py'], cwd=ROOT, capture_output=True, timeout=15)
    if result.returncode: raise RuntimeError('Pinned public Git source unavailable: ' + commit)
    return result.stdout


def load_source(name, raw, directory):
    path = directory / (name + '.py'); path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location('benchmark_bm25_' + name, path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    logging.getLogger(module.__name__).setLevel(logging.ERROR)
    return module


def fixture(parents=120, children_per_parent=10, vocabulary=50000):
    def rows(version):
        result = []
        for group in range(parents):
            children = [dict(id=f'{version}-c-{group:04}-{child:03}', chunk_type='child', parent_id=f'{version}-p-{group:04}',
                content=((f'{"prior" if version == "old" else "fresh"} evidence group{group} unit kilograms field{child} ' * 100) + '中文研究'))
                for child in range(children_per_parent)]
            result.extend(children)
            result.append(dict(id=f'{version}-p-{group:04}', content='\n'.join(row['content'] for row in children), chunk_type='parent'))
        return result
    background = [dict(id='background-chunk', content=' '.join(f'background_term_{i:06}' for i in range(vocabulary)), chunk_type='content')]
    return rows('old'), rows('replacement'), background


class JsonMeter:
    """Proxy just one product module, never mutate global json functions."""
    def __init__(self, measurement): self.measurement = measurement
    def __getattr__(self, key): return getattr(json, key)
    def loads(self, value, *args, **kwargs):
        start = time.perf_counter(); result = json.loads(value, *args, **kwargs)
        self.record('decode', time.perf_counter() - start, len(value.encode()) if isinstance(value, str) else len(value))
        return result
    def dumps(self, value, *args, **kwargs):
        start = time.perf_counter(); result = json.dumps(value, *args, **kwargs)
        self.record('encode', time.perf_counter() - start, len(result.encode()))
        return result
    def record(self, operation, seconds, byte_count):
        if self.measurement.active is None: return
        row = self.measurement.phases[self.measurement.active]
        row['json_' + operation + '_calls'] += 1
        row['json_' + operation + '_ms'] += seconds * 1000
        row['json_' + operation + '_utf8_bytes'] += byte_count
        if byte_count >= 100000: row['large_json_' + operation + '_calls'] += 1


def rss_bytes():
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        class Memory(ctypes.Structure):
            _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD)] + [(name, ctypes.c_size_t) for name in
                ['PeakWorkingSetSize','WorkingSetSize','QuotaPeakPagedPoolUsage','QuotaPagedPoolUsage',
                 'QuotaPeakNonPagedPoolUsage','QuotaNonPagedPoolUsage','PagefileUsage','PeakPagefileUsage']]
        data = Memory(); data.cb = ctypes.sizeof(data)
        kernel = ctypes.WinDLL('kernel32'); kernel.GetCurrentProcess.restype = wintypes.HANDLE
        function = ctypes.WinDLL('psapi').GetProcessMemoryInfo
        function.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD]; function.restype = wintypes.BOOL
        return data.WorkingSetSize if function(kernel.GetCurrentProcess(), ctypes.byref(data), data.cb) else None
    status = Path('/proc/self/statm')
    return int(status.read_text().split()[1]) * os.sysconf('SC_PAGE_SIZE') if status.exists() else None


class Measurement:
    def __init__(self): self.active = None; self.phases = defaultdict(lambda: defaultdict(float)); self.plan_inputs = {}
    @contextmanager
    def phase(self, name, wal):
        self.active = name; started = time.perf_counter()
        try: yield
        finally:
            self.phases[name]['wall_ms'] = (time.perf_counter() - started) * 1000
            self.phases[name]['wal_bytes_after'] = size(wal); self.active = None
    def sql(self, connection, cursor, statement, parameters, context, executemany):
        if self.active is None: return
        row = self.phases[self.active]; normalized = statement.lower().lstrip(); kind = normalized.split(None, 1)[0]
        row['sql_calls'] += 1; row['sql_' + kind + '_calls'] += 1
        row['sql_executemany_calls'] += int(executemany)
        row['sql_parameter_sets'] += len(parameters) if executemany else 1
        if normalized.startswith('insert into bm25_postings'):
            row['posting_insert_max_rows'] = max(row['posting_insert_max_rows'], len(parameters) if executemany else 1)
        if self.active == 'begin' and not executemany:
            self.plan_inputs.setdefault(statement, parameters)
        if normalized.startswith('select') and 'index_stats.term_doc_freq' in normalized: row['selects_including_global_df_json'] += 1
        if normalized.startswith('update index_stats') and 'term_doc_freq' in normalized.split(' where ')[0]: row['updates_including_global_df_json'] += 1


def prepare(cls, rows, prefix):
    values = cls.prepare_file_index(rows)
    for row in values: row['id'] = prefix + row['chunk_id']
    return values


def insert_chunks(session, file_id, rows):
    session.execute(DocumentChunk.__table__.insert(), [dict(id=row['id'], file_id=file_id, content=row['content'],
        chunk_type=row['chunk_type'], parent_id=row.get('parent_id')) for row in rows])


def background_snapshot(session):
    indices = session.exec(select(ChunkIndex).where(ChunkIndex.chunk_id == 'background-chunk')).all()
    postings = session.exec(select(BM25Posting).where(BM25Posting.index_id.in_([row.id for row in indices]))).all()
    return sha(canonical({'indices':[(row.id,row.chunk_id,json.loads(row.terms),row.doc_length) for row in indices],
        'postings':sorted((row.kb_id,row.term,row.index_id,row.tf) for row in postings)}).encode())


def semantic_snapshot(session, cls):
    stats = session.get(IndexStats, 'kb', populate_existing=True)
    indices = session.exec(select(ChunkIndex).order_by(ChunkIndex.id)).all()
    postings = session.exec(select(BM25Posting).order_by(BM25Posting.kb_id,BM25Posting.term,BM25Posting.index_id)).all()
    chunks = session.exec(select(DocumentChunk).order_by(DocumentChunk.id)).all()
    assert not any(row.chunk_id.startswith(('old-p-', 'replacement-p-')) for row in indices)
    assert not any(row.id.startswith('old-') for row in chunks if row.file_id == 'target')
    by_id = {row.id:row for row in chunks}
    assert all(row.parent_id in by_id and by_id[row.parent_id].chunk_type == 'parent' and row.content in by_id[row.parent_id].content
               for row in chunks if row.chunk_type == 'child')
    df = Counter(row.term for row in postings)
    logical = {'chunk_index':[(row.id,row.chunk_id,row.kb_id,json.loads(row.terms),row.doc_length) for row in indices],
        'postings':[(row.kb_id,row.term,row.index_id,row.tf) for row in postings], 'posting_df':dict(df),
        'chunks':[(row.id,row.file_id,row.chunk_type,row.parent_id,sha(row.content.encode())) for row in chunks]}
    queries = ['evidence','fresh','prior','background_term_000001','group42 field3','中文 mixed 研究']
    results = {query:[(row['chunk_id'],row['bm25_score']) for row in cls().search(session,query,'kb',top_k=20)] for query in queries}
    return {'full_logical_sha256':sha(canonical(logical).encode()),'total_docs':stats.total_docs,'avg_doc_length':stats.avg_doc_length,
        'chunk_index_count':len(indices),'posting_count':len(postings),'parent_count':sum(row.chunk_type=='parent' for row in chunks),
        'child_count':sum(row.chunk_type=='child' for row in chunks),'queries':results,'background_sha256':background_snapshot(session),
        'legacy_df_matches_postings':json.loads(stats.term_doc_freq)==dict(df),'legacy_df_utf8_bytes':len(stats.term_doc_freq.encode()),
        'terms_utf8_bytes':sum(len(row.terms.encode()) for row in indices)}


def run_one(name, module, seed, rows, directory, run_id):
    old, new, background = rows
    path = directory / (run_id + '-' + name + '.db'); wal = Path(str(path) + '-wal')
    engine = create_engine('sqlite:///' + path.as_posix())
    @event.listens_for(engine, 'connect')
    def configure(connection, record):
        connection.execute('PRAGMA journal_mode=WAL'); connection.execute('PRAGMA wal_autocheckpoint=0')
    keeper = None; sampler = None; stop = threading.Event(); rss = []
    measurement = Measurement(); module.json = JsonMeter(measurement)
    try:
        SQLModel.metadata.create_all(engine); keeper = engine.raw_connection()
        with Session(engine) as session:
            session.add(KnowledgeBase(id='kb',name='synthetic',embedding_model='',vlm_model=''))
            session.add_all([FileDocument(id=file_id,kb_id='kb',name=file_id+'.txt',local_path='SYNTHETIC_ONLY',size=0,status=FileStatus.PARSED)
                             for file_id in ['target','background-file']]); session.commit()
        # Same valid bulk seed for every arm, always outside measured replacement.
        for file_id, values in [('target',old),('background-file',background)]:
            with Session(engine) as session:
                insert_chunks(session,file_id,values)
                initial=prepare(seed,values,'seed-')
                removed=seed.begin_file_replacement(session,file_id,'kb')
                seed.finish_file_replacement(session,'kb',removed,initial);session.commit()
        with Session(engine) as session:
            prior_background=background_snapshot(session);stats=session.get(IndexStats,'kb')
            old_json_bytes=len(stats.term_doc_freq.encode()); assert stats.total_docs==len(old)-sum(row['chunk_type']=='parent' for row in old)+1
        with engine.connect() as connection:
            checkpoint=tuple(connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)').one()); assert checkpoint[0]==0
            policy={key:connection.exec_driver_sql('PRAGMA '+key).scalar() for key in ['journal_mode','wal_autocheckpoint','synchronous','page_size']}
        event.listen(engine,'before_cursor_execute',measurement.sql);gc.collect()
        def sample():
            while not stop.is_set():
                value=rss_bytes()
                if value is not None:rss.append(value)
                stop.wait(.01)
        sampler=threading.Thread(target=sample,daemon=True);sampler.start()
        started=time.perf_counter();db_before=size(path);wal_before=size(wal)
        with measurement.phase('prepare',wal):new_prepared=prepare(module.BM25Index,new,'new-index-')
        with Session(engine) as session:
            with measurement.phase('begin',wal):removed=module.BM25Index.begin_file_replacement(session,'target','kb')
            with measurement.phase('sql_replace',wal):
                session.execute(delete(DocumentChunk).where(DocumentChunk.file_id=='target').execution_options(synchronize_session=False));insert_chunks(session,'target',new)
            with measurement.phase('finish',wal):module.BM25Index.finish_file_replacement(session,'kb',removed,new_prepared)
            with measurement.phase('flush',wal):session.flush()
            with measurement.phase('commit',wal):session.commit()
        total=(time.perf_counter()-started)*1000;stop.set();sampler.join();event.remove(engine,'before_cursor_execute',measurement.sql)
        db_after=size(path);wal_after=size(wal)
        plans=[]
        with engine.connect() as connection:
            for statement,parameters in measurement.plan_inputs.items():
                plan=connection.exec_driver_sql('EXPLAIN QUERY PLAN '+statement,parameters).all()
                plans.append({'sql':statement,'parameter_count':len(parameters),'plan':[list(row) for row in plan],
                              'scope':'Actual measured begin statement; EXPLAIN only after timing, no DML replay'})
        with Session(engine) as session:snapshot=semantic_snapshot(session,module.BM25Index)
        assert snapshot['background_sha256']==prior_background
        return {'arm':name,'total_ms':total,'phases':{key:dict(value) for key,value in measurement.phases.items()},
            'old_global_df_json_bytes':old_json_bytes,'new_global_df_json_bytes':snapshot['legacy_df_utf8_bytes'],
            'db_before_bytes':db_before,'db_after_bytes':db_after,'wal_before_bytes':wal_before,'wal_after_bytes':wal_after,
            'wal_growth_bytes':wal_after-wal_before,'checkpoint_before':checkpoint,'sqlite_policy':policy,
            'rss':{'method':'10ms sampled whole-process resident/working-set bytes; not absolute peak or isolated allocation',
                   'samples':len(rss),'start_bytes':rss[0] if rss else None,'max_sampled_bytes':max(rss) if rss else None},
            'semantic':snapshot,'background_unchanged':True,'parents_sql_only':True,'begin_query_plans':plans}
    finally:
        stop.set()
        if sampler is not None:sampler.join(timeout=2)
        if keeper is not None:keeper.close()
        engine.dispose()


def assert_equal(left, right):
    for key in ['full_logical_sha256','total_docs','chunk_index_count','posting_count','parent_count','child_count','background_sha256']:
        assert left[key]==right[key],('SEMANTIC_MISMATCH',key)
    assert math.isclose(left['avg_doc_length'],right['avg_doc_length'],rel_tol=1e-12,abs_tol=1e-12)
    for query, rows in left['queries'].items():
        other=right['queries'][query];assert [row[0] for row in rows]==[row[0] for row in other],query
        assert all(math.isclose(a[1],b[1],rel_tol=1e-12,abs_tol=1e-12) for a,b in zip(rows,other)),query


def bounded_begin(session, file_id, kb_id):
    """Benchmark-only safe comparator: narrow rows, bounded IDs, both outer KB guards."""
    rows=session.exec(select(ChunkIndex.id,ChunkIndex.doc_length).join(DocumentChunk,DocumentChunk.id==ChunkIndex.chunk_id)
        .where(DocumentChunk.file_id==file_id,ChunkIndex.kb_id==kb_id)).all()
    ids=[row[0] for row in rows]
    for offset in range(0,len(ids),500):
        batch=ids[offset:offset+500]
        session.execute(delete(BM25Posting).where(BM25Posting.kb_id==kb_id,BM25Posting.index_id.in_(batch))
            .execution_options(synchronize_session=False))
        session.execute(delete(ChunkIndex).where(ChunkIndex.kb_id==kb_id,ChunkIndex.id.in_(batch))
            .execution_options(synchronize_session=False))
    return len(rows),sum(row[1] for row in rows)


def compare_begin(args, raw):
    """Isolate removal only; every sample rolls back, no publication or model I/O."""
    results=[]
    with tempfile.TemporaryDirectory(prefix='bm25-begin-',dir=args.output.parent) as temporary:
        directory=Path(temporary);seed=load_source('begin_seed',git_source(CURRENT),directory)
        candidate=load_source('begin_candidate',raw,directory);candidate.tokenize=seed.tokenize
        old,_,background=fixture()
        for kind,count in [('warmup',args.warmups),('formal',args.repetitions)]:
            for repetition in range(count):
                order=['subquery','bounded_ids'] if repetition%2==0 else ['bounded_ids','subquery']
                for name in order:
                    path=directory/f'{kind}-{repetition}-{name}.db';engine=create_engine('sqlite:///'+path.as_posix())
                    @event.listens_for(engine,'connect')
                    def configure(connection,record):
                        connection.execute('PRAGMA journal_mode=WAL');connection.execute('PRAGMA wal_autocheckpoint=0')
                    meter=Measurement();candidate.json=JsonMeter(meter)
                    try:
                        SQLModel.metadata.create_all(engine)
                        with Session(engine) as session:
                            session.add(KnowledgeBase(id='kb',name='synthetic',embedding_model='',vlm_model=''))
                            session.add_all([FileDocument(id=f,kb_id='kb',name=f,local_path='SYNTHETIC_ONLY',size=0,status=FileStatus.PARSED)
                                             for f in ['target','background-file']]);session.commit()
                        for file_id,values in [('target',old),('background-file',background)]:
                            with Session(engine) as session:
                                insert_chunks(session,file_id,values);prepared=prepare(seed.BM25Index,values,'seed-')
                                removed=seed.BM25Index.begin_file_replacement(session,file_id,'kb')
                                seed.BM25Index.finish_file_replacement(session,'kb',removed,prepared);session.commit()
                        with Session(engine) as session:before_background=background_snapshot(session)
                        with engine.connect() as connection:checkpoint=tuple(connection.exec_driver_sql('PRAGMA wal_checkpoint(TRUNCATE)').one())
                        event.listen(engine,'before_cursor_execute',meter.sql);gc.collect()
                        with Session(engine) as session:
                            with meter.phase('begin',Path(str(path)+'-wal')):
                                removed=(candidate.BM25Index.begin_file_replacement if name=='subquery' else bounded_begin)(session,'target','kb')
                            assert removed[0]==1200
                            assert len(session.exec(select(ChunkIndex.id)).all())==1
                            assert background_snapshot(session)==before_background
                            session.rollback()
                        event.remove(engine,'before_cursor_execute',meter.sql)
                        plans=[]
                        with engine.connect() as connection:
                            for sql,parameters in meter.plan_inputs.items():
                                plans.append({'sql':sql,'parameter_count':len(parameters),'plan':[list(row) for row in connection.exec_driver_sql('EXPLAIN QUERY PLAN '+sql,parameters)]})
                        with Session(engine) as session:
                            assert len(session.exec(select(ChunkIndex.id)).all())==1201
                            assert background_snapshot(session)==before_background
                        result={'arm':name,'sample_kind':kind,'repetition':repetition,'order':order,'phase':dict(meter.phases['begin']),
                            'removed_count':removed[0],'removed_length':removed[1],'plans':plans,'rollback_restored':True,'checkpoint_before':checkpoint}
                        results.append(result)
                        print(canonical({k:result[k] for k in ['arm','sample_kind','repetition','phase']}),flush=True)
                    finally:engine.dispose()
    report={'schema':'synthetic-bm25-begin-comparison/v1','candidate_sha256':sha(raw),'scope':'begin only, rollback each independent seeded WAL database',
        'source_bytes':sum(len(row['content'].encode()) for row in old if row['chunk_type']!='parent'),'background_vocabulary':50000,
        'bounded_variant':'narrow id,length SELECT;500-ID batches;both outer KB guards;no forcedindex/schema/ANALYZE',
        'runs':results,'summary':{name:{'median_ms':statistics.median(values),'min_ms':min(values),'max_ms':max(values)} for name in ['subquery','bounded_ids']
            for values in [[row['phase']['wall_ms'] for row in results if row['sample_kind']=='formal' and row['arm']==name]]},'temporary_databases_removed':True}
    with args.output.open('x',encoding='utf8') as output:json.dump(report,output,indent=2)
    print(canonical({'status':'PASS','summary':report['summary']}),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,help='Optional explicitly identified oldsource; pinned public2041 is default')
    parser.add_argument('--candidate',type=Path,help='Only after candidate owner READY; frozen at start')
    parser.add_argument('--reference',type=Path,help='Frozen local reference, e.g. reviewed P1 source for an isolated P2 comparison')
    parser.add_argument('--arms',nargs='+',choices=['old2041','current7c','reference','candidate'],help='Explicit measured arm subset; same pinned seed/tokenizer')
    parser.add_argument('--begin-only-comparison',action='store_true',help='Candidate subquery vs bounded narrow-ID deletion; rollback each sample')
    parser.add_argument('--publication',action='store_true',help='Real isolated SQLite/Chroma full publication comparison')
    parser.add_argument('--reference-services',type=Path,help='Frozen P1 services directory containing bm25/index_builds/vector_store.py')
    parser.add_argument('--candidate-services',type=Path,help='Explicit frozen candidate services directory for --publication only')
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--warmups',type=int,default=1);parser.add_argument('--repetitions',type=int,default=5)
    args=parser.parse_args();assert args.warmups>=1 and args.repetitions>=5
    journal=args.output.with_suffix(args.output.suffix+'.runs.jsonl')
    assert not args.output.exists() and not journal.exists(),'use a new outputfile';args.output.parent.mkdir(parents=True,exist_ok=True)
    if args.publication:
        if not args.reference_services:parser.error('--publication requires --reference-services')
        if args.begin_only_comparison:parser.error('publication and begin-only are mutually exclusive')
        from benchmark_publication_helpers import main_publication
        return main_publication(args)
    if args.candidate_services:parser.error('--candidate-services requires --publication')
    if args.begin_only_comparison:
        if not args.candidate:parser.error('--begin-only-comparison requires --candidate')
        return compare_begin(args,args.candidate.read_bytes())
    raw={'old2041':args.baseline.read_bytes() if args.baseline else git_source(OLD),'current7c':git_source(CURRENT)}
    if args.candidate:raw['candidate']=args.candidate.read_bytes()
    if args.reference:raw['reference']=args.reference.read_bytes()
    if args.arms:
        if len(set(args.arms))!=len(args.arms) or any(name not in raw for name in args.arms):
            parser.error('--arms requires distinct available arms; reference/candidate require their source path')
        raw={name:raw[name] for name in args.arms}
    journal.touch(exist_ok=False)
    rows=fixture();runs=[];reference=None
    with tempfile.TemporaryDirectory(prefix='bm25-replacement-',dir=args.output.parent) as temporary:
        directory=Path(temporary);modules={name:load_source(name,value,directory) for name,value in raw.items()}
        seed=modules.get('current7c') or load_source('common_seed',git_source(CURRENT),directory)
        tokenizer=seed.tokenize
        for module in modules.values():module.tokenize=tokenizer
        for kind,count in [('warmup',args.warmups),('formal',args.repetitions)]:
            for repetition in range(count):
                base=list(modules);shift=repetition%len(base)
                if len(base)==2: order=base if repetition%2==0 else list(reversed(base))
                else:
                    order=base[shift:]+base[:shift]
                    if (repetition//len(base))%2:order.reverse()
                for position,name in enumerate(order):
                    try:
                        run=run_one(name,modules[name],seed.BM25Index,rows,directory,f'{kind}-{repetition}')
                    except Exception as error:
                        with journal.open('a',encoding='utf-8') as output:
                            output.write(canonical({'status':'FAILED','sample_kind':kind,'repetition':repetition,'arm':name,
                                'error_type':type(error).__name__,'error':str(error)})+'\n')
                        raise
                    run.update(sample_kind=kind,repetition=repetition,order_position=position,arm_order=order)
                    # Preserve each completed measurement before semantic acceptance; no writes inside timing.
                    with journal.open('a',encoding='utf-8') as output:output.write(canonical(run)+'\n')
                    if reference is None:reference=run['semantic']
                    else:assert_equal(reference,run['semantic'])
                    runs.append(run)
                    print(json.dumps({'sample_kind':kind,'repetition':repetition,'arm':name,'total_ms':run['total_ms'],'semantics':'MATCH'}),flush=True)
        summary={}
        for name in modules:
            samples=[row for row in runs if row['arm']==name and row['sample_kind']=='formal']
            fields={'total_ms':[row['total_ms'] for row in samples],'wal_growth_bytes':[row['wal_growth_bytes'] for row in samples]}
            for phase in ['prepare','begin','sql_replace','finish','flush','commit']:fields[phase+'_ms']=[row['phases'][phase]['wall_ms'] for row in samples]
            summary[name]={field:{'median':statistics.median(values),'min':min(values),'max':max(values)} for field,values in fields.items()}
    report={'schema':'synthetic-bm25-file-replacement/v2','scope':'LOCALsyntheticSQLite; not ECS/fullpublication/physicaldiskIOPS',
        'runtime':{'python':sys.version,'executable':sys.executable,'platform':platform.platform(),'sqlite':sqlite3.sqlite_version,
            'packages':{name:importlib.metadata.version(name) for name in ['sqlmodel','sqlalchemy']}},
        'run_journal':str(journal),'script_sha256':sha(Path(__file__).read_bytes()),
        'common_seed_sha256':sha(git_source(CURRENT)),
        'sources':{name:{'git_commit':OLD if name=='old2041' and not args.baseline else CURRENT if name=='current7c' else None,'sha256':sha(value)} for name,value in raw.items()},
        'tokenizer':'jieba' if importlib.util.find_spec('jieba') else 'simple_fallback','tokenizer_shared_from':CURRENT,
        'new_source_bytes':sum(len(row['content'].encode()) for row in rows[1] if row['chunk_type']!='parent'),
        'parent_count':120,'child_count':1200,'background_vocabulary':50000,'background_in_separate_file':True,
        'warmups_per_arm':args.warmups,'formal_samples_per_arm':args.repetitions,'runs':runs,'summary':summary,
        'full_semantic_tables_and_query_results_equal':True,'legacy_df_json_is_not_semantic_authority':True,
        'temporary_databases_removed':True,'rss_limit':'SampledwholeprocessRSS includesallocator/tokenizerhistory, not per-arm peak',
        'timing_limit':'Do not sum nested producttimings;5samples give no meaningfulP95'}
    with args.output.open('x',encoding='utf-8') as output:json.dump(report,output,indent=2)
    print(json.dumps({'status':'PASS','output':str(args.output),'summary':summary},indent=2))


if __name__=='__main__':main()

"""P1 immutable-corpus preflight and bounded isolated HTTP/SSE regression."""
import argparse
from array import array
import hashlib
import json
import sqlite3
from pathlib import Path

BASE = Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
PDF = ('cn-fiscal2024/v1:cn-audit-2024:answer:1',
       'cn-fiscal2024/v1:cn-audit-2024:answer:4',
       'mmlongbench-doc:2ff6aa9237fc777b6627dc57a486e9225ac5fb86:row-0685',
       'ecs-table-probe/v2:121')
OFFICE = ('office50:001', 'office50:026')

def sha(path):
    h = hashlib.sha256()
    with path.open('rb') as src:
        for block in iter(lambda: src.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()

def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]

def final_citations(events):
    """SSE rag_result replaces the prior list; it is not an append event."""
    return next((data.get('citations', []) for kind, data in reversed(events)
                 if kind == 'rag_result'), [])


def sanitized_metrics(text):
    """Keep bounded metric labels, including braces in normalized API routes."""
    import re
    names = ('rag_http_', 'rag_sse_', 'rag_provider_', 'rag_background_', 'rag_agent_retry_')
    labels_allowed = {'method', 'route', 'status_code', 'mode', 'outcome', 'event_type',
                      'response_delivery', 'stage', 'token_type', 'task', 'action', 'attempt', 'le'}
    routes = {'/health', '/metrics', '/api/v1/chat/completions',
              '/api/v1/files/{file_id}/source', 'unmatched'}
    safe = []
    for line in text.splitlines():
        if not line.startswith(names):
            continue
        match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? ([+-]?(?:[0-9.eE+-]+|Inf|NaN))', line)
        if not match:
            raise RuntimeError('unexpected metric format')
        if match.group(2):
            seen = set()
            for item in match.group(2).split(','):
                label = re.fullmatch(r'([a-z_]+)="([a-zA-Z0-9_./:{}+-]{1,100})"', item)
                if not label or label.group(1) not in labels_allowed or label.group(1) in seen:
                    raise RuntimeError('unexpected metric label')
                seen.add(label.group(1))
                if label.group(1) == 'route' and label.group(2) not in routes:
                    raise RuntimeError('unexpected metric route')
        safe.append(line)
    return '\n'.join(safe) + ('\n' if safe else '')


def citation_check(db, citation, kb_id):
    if citation.get('source_type') == 'source_native_table_expansion':
        parent = citation.get('parent_chunk_id')
        row = db.execute('select f.kb_id,f.local_path from document_chunks d join file_documents f on f.id=d.file_id where d.id=? and f.id=?',
                         (parent, citation.get('fileId'))).fetchone()
        proof = citation.get('source_metadata') or {}
        expected = proof.get('source_sha256') or citation.get('source_sha256')
        try:
            valid = bool(row and row[0] == kb_id and expected and sha(Path(row[1])) == expected)
        except OSError:
            valid = False
        return {'source_type': 'source_native_table_expansion', 'parent_chunk_id': parent,
                'valid_scope_and_quote': None, 'valid_scope_and_source_hash': valid}
    row = db.execute('select d.content,f.kb_id from document_chunks d join file_documents f on f.id=d.file_id where d.id=? and f.id=?',
                     (citation.get('chunk_id'), citation.get('fileId'))).fetchone()
    return {'chunk_id': citation.get('chunk_id'), 'valid_scope_and_quote': bool(
        row and row[1] == kb_id and citation.get('content') and citation['content'] in row[0])}

def source_hash(root):
    digest = hashlib.sha256()
    paths = sorted((root / 'app').rglob('*.py'))
    if not paths:
        raise ValueError('missing staged app')
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(b'\0')
        digest.update(bytes.fromhex(sha(path)))
    return digest.hexdigest()

def cache_check(path, question):
    db = sqlite3.connect('file:' + str(path) + '?mode=ro', uri=True)
    try:
        row = db.execute('select dimensions,data from vectors where key=?', (hashlib.sha256(question.encode()).hexdigest(),)).fetchone()
        if not row:
            return None
        vector = array('d'); vector.frombytes(row[1])
        if len(vector) != row[0] or not vector:
            raise ValueError('corrupt vector cache entry')
        return len(vector)
    finally:
        db.close()

def read_model_key(db_path, model_id, model_name, base_url):
    """Read the verified model's key without writing it to an artifact."""
    db = sqlite3.connect('file:' + str(db_path) + '?mode=ro', uri=True)
    try:
        row = db.execute('select model_name,base_url,api_key from custom_models where id=?', (model_id,)).fetchone()
        if not row or row[0] != model_name or row[1] != base_url or not row[2]:
            raise ValueError('verified provider configuration unavailable')
        return row[2]
    finally:
        db.close()

def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--code-sha256', required=True)
    parser.add_argument('--llm-model', required=True)
    parser.add_argument('--embedding-model', required=True)
    parser.add_argument('--provider-role', choices=('llm', 'vlm'), default='llm', help='verified credential configuration role')
    parser.add_argument('--provider-model', help='exact temporary wire model; defaults to credential model')
    parser.add_argument('--reuse-empty-kb', help='existing empty KB ID in immutable PDF runtime')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--grounded-generation', choices=('on', 'off'), default='on')
    parser.add_argument('--reuse-runtime', type=Path, help='reuse an inactive prior private runtime read-only')
    parser.add_argument('--case', action='append', dest='selected_cases', help='exact qid to run, repeatable')
    parser.add_argument('--compare-grounded', action='store_true', help='run the same six cases off then on, sharing the six-send global cap')
    parser.add_argument('--rewrite-only', action='store_true', help='one rewrite send for one reused fulltext case, without answer generation')
    parser.add_argument('--scratch-fulltext', action='store_true', help='use empty private Chroma directories with fulltext-only guards')
    parser.add_argument('--capture-metrics', action='store_true', help='capture sanitized metrics from the isolated API after each case')
    parser.add_argument('--private-candidate-snapshots', action='store_true', help='store local-only snapshots in each private case directory')
    parser.add_argument('--empty-only', action='store_true', help='run only empty-KB cases with all provider traffic disabled')
    parser.add_argument('--port', type=int, default=0, help='loopback port; 0 chooses an ephemeral port')
    parser.add_argument('--metrics-hold-seconds', type=float, default=0, help='keep the server running after each result for a metrics scrape (0–60 seconds)')
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error('--port must be between 0 and 65535')
    if not 0 <= args.metrics_hold_seconds <= 60:
        parser.error('--metrics-hold-seconds must be between 0 and 60')
    if args.compare_grounded and args.empty_only:
        parser.error('--compare-grounded cannot be combined with --empty-only')
    if args.reuse_empty_kb and (not args.reuse_runtime or not args.scratch_fulltext):
        parser.error('--reuse-empty-kb requires immutable fulltext runtime reuse')
    if args.provider_model and not args.reuse_runtime:
        parser.error('--provider-model requires immutable runtime reuse')
    if args.reuse_runtime and (not args.scratch_fulltext or not args.selected_cases or args.compare_grounded or args.empty_only):
        parser.error('--reuse-runtime requires --scratch-fulltext, --case, and no comparison or empty-only')
    if args.selected_cases and (not args.reuse_runtime or len(set(args.selected_cases)) != len(args.selected_cases) or len(args.selected_cases) > 6):
        parser.error('--case requires reuse and at most six distinct qids')
    if args.rewrite_only and (not args.reuse_runtime or not args.scratch_fulltext
            or args.grounded_generation != 'off' or args.compare_grounded or args.empty_only
            or not args.selected_cases or len(args.selected_cases) != 1):
        parser.error('--rewrite-only requires one --case, --reuse-runtime, --scratch-fulltext, --grounded-generation off, no comparison or empty-only')
    return args


def planned_bounds(args):
    calls = 1 if args.rewrite_only else (0 if args.empty_only or (args.grounded_generation == 'off' and not args.compare_grounded) else 6)
    return {'chat': calls, 'per_question': int(calls > 0), 'embedding': 0, 'rerank': 0}


def send_is_permitted(case, *, rewrite_only, in_rewrite, used):
    if case['qid'].startswith('empty-') or used:
        return False
    if rewrite_only:
        return case['grounded_generation'] == 'off' and in_rewrite
    return case['grounded_generation'] == 'on'


def request_config(case, rewrite_only):
    return {'mode': case['mode'], 'search_mode': 'fulltext', 'use_rewrite': rewrite_only,
            'grounded_generation': case['grounded_generation']}


def build_http_request(case, model_id, rewrite_only):
    return {'messages': [{'role': 'user', 'content': case['question']}],
            'kb_ids': [case['kb_id']], 'mode': case['mode'], 'model_id': model_id,
            'search_mode': 'fulltext', 'top_k': 20, 'rerank_enabled': False,
            'context_window': 0, 'use_rewrite': rewrite_only}


def main():
    args = parse_args()
    backend = args.backend.resolve()
    code = source_hash(backend)
    if code != args.code_sha256:
        raise ValueError('staged source hash mismatch')
    data, prior = BASE / 'dataset200', BASE / 'runtime200-r4'
    office, office_runtime = BASE / 'office50-v1/dataset250', BASE / 'office50-v1/evaluation'
    freeze = json.loads((data / 'freeze.json').read_text(encoding='utf-8'))
    for name, expected in freeze['files'].items():
        path = (data / name).resolve()
        if not path.is_relative_to(data.resolve()) or sha(path) != expected:
            raise ValueError('PDF frozen file mismatch')
    office_freeze = json.loads((office / 'freeze250.json').read_text(encoding='utf-8'))
    if sha(office / 'office50.jsonl') != office_freeze['office50_sha256'] or sha(data / 'freeze.json') != office_freeze['old200_freeze_sha256']:
        raise ValueError('office freeze mismatch')
    pdf_gold = {r['qid']: r for r in rows(data / 'gold100.jsonl')}
    registry = {r['qid']: r for r in rows(data / 'registry200.jsonl')}
    office_gold = {r['qid']: r for r in rows(office / 'office50.jsonl')}
    # Additional cases are accepted only when explicitly selected from the
    # frozen Office dataset; the default regression set remains unchanged.
    office_qids = tuple(dict.fromkeys(OFFICE + tuple(
        qid for qid in (args.selected_cases or ()) if qid in office_gold
    )))
    controls = rows(prior / 'control-sql.jsonl')
    known = {r['value']['name'].removeprefix('prod-'): r['value']['id'] for r in controls if r['kind'] == 'kb'}
    ids = json.loads(Path('/opt/amrag/ids.json').read_text(encoding='utf-8'))['models']
    ids['llm'] = ids[args.provider_role]
    model_db = sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro', uri=True)
    try:
        llm_url = None
        for role, expected in [('llm', args.llm_model), ('embedding', args.embedding_model)]:
            actual = model_db.execute('select model_name from custom_models where id=?', (ids[role],)).fetchone()
            if not actual or actual[0] != expected:
                raise ValueError('model identity mismatch: ' + role)
            if role == 'llm':
                llm_url = model_db.execute('select base_url from custom_models where id=?', (ids[role],)).fetchone()[0]
    finally:
        model_db.close()
    cases = []
    for corpus, qids, gold, runtime in [('pdf', PDF, pdf_gold, prior), ('office', office_qids, office_gold, office_runtime)]:
        for required in (('isolated.db',) if args.scratch_fulltext else ('isolated.db', 'chroma')):
            if not (runtime / required).exists():
                raise ValueError('missing corpus runtime')
        db = sqlite3.connect('file:' + str(runtime / 'isolated.db') + '?mode=ro', uri=True)
        try:
            for qid in qids:
                case = gold[qid]
                if corpus == 'pdf':
                    profile = registry[qid]['profile']
                    kb = known.get(profile, 'v200-' + profile + ('-after' if profile.startswith('pdf-') else ''))
                    cache = prior / 'vector-cache.db'
                else:
                    if case['format'] not in ('docx', 'xlsx') or case['kb_scope'] != ['office50-v1']:
                        raise ValueError('unexpected office case')
                    if sha(Path(case['source_path'])) != case['source_sha256']:
                        raise ValueError('office source hash mismatch')
                    kb, cache = 'office50-v1', office_runtime / 'vectors.db'
                if not db.execute('select 1 from knowledge_bases where id=? and is_deleted=0', (kb,)).fetchone():
                    raise ValueError('scope missing: ' + kb)
                files = db.execute('select count(*) from file_documents where kb_id=?', (kb,)).fetchone()[0]
                chunks = db.execute('select count(*) from document_chunks d join file_documents f on d.file_id=f.id where f.kb_id=?', (kb,)).fetchone()[0]
                index = db.execute('select count(*) from chunk_index where kb_id=?', (kb,)).fetchone()[0]
                if not (files and chunks and index):
                    raise ValueError('no scoped indexed corpus: ' + kb)
                cases.append({'qid': qid, 'corpus': corpus, 'kb_id': kb, 'question_sha256': hashlib.sha256(case['question'].encode()).hexdigest(), 'query_cache_dimensions': None if args.scratch_fulltext else cache_check(cache, case['question']), 'files': files, 'chunks': chunks, 'bm25_rows': index})
        finally:
            db.close()
    if args.selected_cases:
        selected = set(args.selected_cases)
        if not selected.issubset({case['qid'] for case in cases}):
            raise ValueError('unknown case filter')
        cases = [case for case in cases if case['qid'] in selected]
    print(json.dumps({'status': 'PREFLIGHT_PASS', 'provider_calls': 0, 'code_sha256': code, 'runner_sha256': sha(Path(__file__)), 'pdf_freeze_sha256': sha(data / 'freeze.json'), 'office_freeze_sha256': sha(office / 'freeze250.json'), 'models': {'credential_model': args.llm_model, 'provider_role': args.provider_role, 'wire_model': args.provider_model or args.llm_model, 'embedding': args.embedding_model}, 'cases': cases, 'planned_bounds': planned_bounds(args), 'grounded_generation': args.grounded_generation, 'rewrite_only': args.rewrite_only, 'compare_grounded': args.compare_grounded, 'scratch_fulltext': args.scratch_fulltext, 'capture_metrics': args.capture_metrics, 'empty_only': args.empty_only, 'port': args.port, 'execution': 'READY' if args.execute else 'NOT_RUN'}, ensure_ascii=False), flush=True)
    if args.execute:
        import sys
        credentials = {} if args.empty_only else json.load(sys.stdin)
        execute(backend, cases, pdf_gold, office_gold, ids, args.llm_model, llm_url, credentials, args.grounded_generation, args.empty_only, args.port, args.metrics_hold_seconds, args.compare_grounded, args.scratch_fulltext, args.capture_metrics, args.reuse_runtime, args.private_candidate_snapshots, args.rewrite_only, args.provider_model or args.llm_model, args.reuse_empty_kb)



def execute(backend, cases, pdf_gold, office_gold, ids, llm_model, llm_url, credentials, grounded_generation='on', empty_only=False, port=0, metrics_hold_seconds=0, compare_grounded=False, scratch_fulltext=False, capture_metrics=False, reuse_runtime=None, private_candidate_snapshots=False, rewrite_only=False, provider_model=None, reuse_empty_kb=None):
    """Run serial isolated requests; unresolved dispatch blocks subsequent runs."""
    import multiprocessing as mp
    import os
    import shutil
    import socket
    import time
    import uuid
    from urllib.parse import urlsplit
    if rewrite_only and (not reuse_runtime or not scratch_fulltext or grounded_generation != 'off'
                         or compare_grounded or empty_only or len(cases) != 1):
        raise ValueError('invalid rewrite-only execution scope')
    os.umask(0o077)
    if not llm_url or urlsplit(llm_url).scheme != 'https':
        raise ValueError('verified HTTPS provider URL required')
    langfuse = bool(credentials.get('LANGFUSE_PUBLIC_KEY') or credentials.get('LANGFUSE_SECRET_KEY'))
    if langfuse and (not credentials.get('LANGFUSE_PUBLIC_KEY') or not credentials.get('LANGFUSE_SECRET_KEY') or credentials.get('LANGFUSE_BASE_URL', '').rstrip('/') != 'https://us.cloud.langfuse.com'):
        raise ValueError('invalid Langfuse credentials')
    provider_model = provider_model or llm_model
    marker = BASE / '.p1-provider-unknown-or-inflight.json'
    if marker.exists():
        raise RuntimeError('Prior P1 provider reservation unresolved; stop and reconcile')
    out = BASE / ('p1-http-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    out.mkdir(mode=0o700)
    journal = out / 'provider-journal.jsonl'
    results = []
    counts = mp.Value('i', 0)
    unknown = mp.Value('i', 0)
    def record(path, payload):
        with path.open('a', encoding='utf-8') as dst:
            dst.write(json.dumps(payload, ensure_ascii=False) + '\n')
            dst.flush(); os.fsync(dst.fileno())
    def scrub(path):
        if path.exists():
            conn = sqlite3.connect(path)
            try:
                conn.execute("update custom_models set api_key=''")
                conn.commit(); conn.execute('vacuum')
            finally:
                conn.close()
    runtimes = {}
    if reuse_runtime:
        reuse = reuse_runtime.resolve(strict=True)
        if reuse.parent != BASE.resolve() or not reuse.name.startswith('p1-http-') or not reuse.is_dir():
            raise ValueError('reuse runtime must be a direct p1-http child of BASE')
        if reuse_runtime.is_symlink():
            raise ValueError('reuse runtime must not be a symlink')
        for name in ('pdf', 'office'):
            target = reuse / name
            if target.is_symlink() or not target.is_dir() or (target / 'isolated.db').is_symlink() or not (target / 'isolated.db').is_file() or (target / 'chroma').is_symlink() or not (target / 'chroma').is_dir():
                raise ValueError('reuse runtime is incomplete or contains symlinks')
            if any((target / ('isolated.db' + ext)).exists() and (target / ('isolated.db' + ext)).stat().st_size > 0 for ext in ('-wal', '-journal')):
                raise ValueError('reuse runtime has uncheckpointed SQLite changes; reconcile first')
            db = sqlite3.connect('file:' + str(target / 'isolated.db') + '?mode=ro&immutable=1', uri=True)
            try:
                if db.execute("select count(*) from custom_models where coalesce(api_key, '') <> ''").fetchone()[0]:
                    raise ValueError('reuse runtime contains credentials')
                for case in (x for x in cases if x['corpus'] == name):
                    kb = case['kb_id']
                    actual = (db.execute('select count(*) from file_documents where kb_id=?', (kb,)).fetchone()[0],
                              db.execute('select count(*) from document_chunks d join file_documents f on d.file_id=f.id where f.kb_id=?', (kb,)).fetchone()[0],
                              db.execute('select count(*) from chunk_index where kb_id=?', (kb,)).fetchone()[0])
                    if not db.execute('select 1 from knowledge_bases where id=? and is_deleted=0', (kb,)).fetchone() or actual != (case['files'], case['chunks'], case['bm25_rows']):
                        raise ValueError('reuse scope/count mismatch')
            finally:
                db.close()
            runtimes[name] = target
        # Fail closed if a running process holds a runtime file or advertises it in its environment.
        proc = Path('/proc')
        if not proc.is_dir():
            raise RuntimeError('process inspection unavailable')
        for process in proc.iterdir():
            if not process.name.isdigit() or int(process.name) == os.getpid():
                continue
            try:
                environment = (process / 'environ').read_bytes()
                fds = list((process / 'fd').iterdir())
                if str(reuse).encode() in environment or any(str(reuse) in os.readlink(fd) for fd in fds if fd.exists()):
                    raise RuntimeError('reuse runtime in use')
            except (FileNotFoundError, ProcessLookupError):
                continue
            except PermissionError as exc:
                raise RuntimeError('process inspection denied') from exc
        # Never retry a selected case with a recorded unresolved outcome.
        selected = {x['qid'] for x in cases}
        for journal_file in BASE.glob('p1-http-*/provider-journal.jsonl'):
            pending = set()
            for entry in rows(journal_file):
                qid = entry.get('qid', entry.get('case', '').removesuffix(':off').removesuffix(':on'))
                name = entry.get('case')
                if qid in selected and entry.get('state') == 'RESERVED': pending.add(name)
                elif qid in selected and entry.get('state') == 'RETURNED': pending.discard(name)
            if pending: raise RuntimeError('selected case has unresolved historical reservation')
        for result_file in BASE.glob('p1-http-*/result.json'):
            previous = json.loads(result_file.read_text(encoding='utf-8'))
            for item in previous.get('cases', []):
                qid = item.get('qid', item.get('case', '').removesuffix(':off').removesuffix(':on'))
                if qid in selected and (item.get('http_terminal_state') != 'SSE_DONE' and item.get('provider_calls', 0)):
                    raise RuntimeError('selected case has unresolved historical outcome')
                if qid in selected and ('UNKNOWN' in str(item.get('answer_mode', '')) or 'UNKNOWN' in str(item.get('http_terminal_state', ''))):
                    raise RuntimeError('selected case has UNKNOWN historical outcome')
    try:
        for name, source in ([] if reuse_runtime else [('pdf', BASE / 'runtime200-r4'), ('office', BASE / 'office50-v1/evaluation')]):
            target = out / name
            target.mkdir(mode=0o700)
            dbpath = target / 'isolated.db'
            src = sqlite3.connect('file:' + str(source / 'isolated.db') + '?mode=ro', uri=True)
            dst = sqlite3.connect(dbpath)
            try:
                src.backup(dst)
                dst.execute("update custom_models set api_key=''")
                # The selected model identity and URL are verified in preflight;
                # the copied model row never contains a credential.
                prod = sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro', uri=True)
                try:
                    row = list(prod.execute("select id,name,model_type,base_url,'' as api_key,model_name,context_length,is_active,created_at,updated_at from custom_models where id=?", (ids['llm'],)).fetchone())
                finally:
                    prod.close()
                dst.execute('insert or replace into custom_models values (' + ','.join('?' * len(row)) + ')', row)
                row[4] = ''
                dst.commit()
            finally:
                src.close(); dst.close()
            if scratch_fulltext:
                (target / 'chroma').mkdir(mode=0o700)
            else:
                shutil.copytree(source / 'chroma', target / 'chroma')
            runtimes[name] = target
        sequence = []
        if reuse_runtime and reuse_empty_kb:
            db = sqlite3.connect('file:' + str(runtimes['pdf'] / 'isolated.db') + '?mode=ro&immutable=1', uri=True)
            try:
                kb = db.execute('select 1 from knowledge_bases where id=? and is_deleted=0', (reuse_empty_kb,)).fetchone()
                files = db.execute('select count(*) from file_documents where kb_id=?', (reuse_empty_kb,)).fetchone()[0]
                chunks = db.execute('select count(*) from document_chunks d join file_documents f on d.file_id=f.id where f.kb_id=?', (reuse_empty_kb,)).fetchone()[0]
                index = db.execute('select count(*) from chunk_index where kb_id=?', (reuse_empty_kb,)).fetchone()[0]
                if not kb or files or chunks or index:
                    raise ValueError('reused empty KB missing or nonempty')
            finally:
                db.close()
            sequence = [{'qid': 'empty-chat', 'corpus': 'pdf', 'kb_id': reuse_empty_kb, 'question': '请根据知识库回答资料内容', 'mode': 'chat'},
                        {'qid': 'empty-agent', 'corpus': 'pdf', 'kb_id': reuse_empty_kb, 'question': '请根据知识库回答资料内容', 'mode': 'agent'}]
        if not reuse_runtime:
            # The empty KB exists only in the private copied database.
            empty_kb = 'p1-empty-' + uuid.uuid4().hex
            empty_db = sqlite3.connect(runtimes['pdf'] / 'isolated.db')
            try:
                row = list(empty_db.execute('select * from knowledge_bases where id=?', (cases[0]['kb_id'],)).fetchone())
                row[0], row[1], row[5] = empty_kb, 'p1-empty', 0
                empty_db.execute('insert into knowledge_bases values (' + ','.join('?' * len(row)) + ')', row)
                empty_db.commit()
            finally:
                empty_db.close()
            sequence = [{'qid': 'empty-chat', 'corpus': 'pdf', 'kb_id': empty_kb, 'question': '请根据知识库回答资料内容', 'mode': 'chat'},
                        {'qid': 'empty-agent', 'corpus': 'pdf', 'kb_id': empty_kb, 'question': '请根据知识库回答资料内容', 'mode': 'agent'}]
        for case in sequence:
            case.update(case_name=case['qid'], grounded_generation=grounded_generation)
        for setting in (('off', 'on') if compare_grounded else (grounded_generation,)):
            for case in ([] if empty_only else cases):
                source = pdf_gold if case['corpus'] == 'pdf' else office_gold
                sequence.append({**case, 'question': source[case['qid']]['question'], 'mode': 'chat', 'case_name': case['qid'] + ':' + setting, 'grounded_generation': setting})
        for ordinal, case in enumerate(sequence):
            if marker.exists() or unknown.value:
                raise RuntimeError('Unresolved prior provider outcome; remaining requests stopped')
            run = out / ('case-%02d' % ordinal)
            run.mkdir(mode=0o700)
            runtime = runtimes[case['corpus']]
            sock = socket.socket(); sock.bind(('127.0.0.1', port)); case_port = sock.getsockname()[1]; sock.close()
            before = counts.value
            started = time.monotonic()
            terminal_state = 'STARTUP_ERROR'
            result_recorded = False
            def serve():
                os.environ['LLM_API_KEY'] = '' if empty_only or (case['grounded_generation'] == 'off' and not rewrite_only) else read_model_key(Path('/opt/amrag/backend/local.db'), ids['llm'], llm_model, llm_url)
                database_url = ('sqlite:///file:' + str(runtime / 'isolated.db') + '?mode=ro&immutable=1&uri=true') if reuse_runtime else ('sqlite:///' + str(runtime / 'isolated.db'))
                local_storage = run if reuse_runtime else runtime
                os.environ.update(ENV='test', DATABASE_URL=database_url, CHROMA_DB_DIR=str(local_storage / 'chroma'), IMAGE_DIR=str(local_storage / 'images'), UPLOAD_DIR=str(local_storage / 'uploads'), ANONYMIZED_TELEMETRY='False', RAG_OBSERVABILITY_ENABLED='true', RAG_SNAPSHOTS_ENABLED='false', RAG_GROUNDED_GENERATION='true' if case['grounded_generation'] == 'on' else 'false', RAG_PROMETHEUS_ENABLED='true' if capture_metrics else 'false', RAG_OBSERVATION_LOG=str(run / 'events.jsonl'), LANGFUSE_ENABLED='true' if langfuse else 'false')
                if private_candidate_snapshots:
                    os.environ['RAG_SNAPSHOTS_ENABLED'] = 'true'
                    os.environ['RAG_SNAPSHOT_DIR'] = str(run / 'private-snapshots')
                if langfuse:
                    os.environ.update({k: credentials[k] for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')})
                os.chdir(run)
                import sys
                sys.path.insert(0, str(backend))
                import httpx, requests
                from app.services.rerank import RerankService
                from app.services.retrieval import RetrievalService
                from app.services.observability import _sdk
                if reuse_runtime:
                    # Override only ChatService's model lookup, never write the immutable DB.
                    import app.services.chat as chat_module
                    from app.models.models import CustomModel, ModelType
                    original_session = chat_module.Session
                    class SelectedModelSession(original_session):
                        def get(self, entity, ident, *args, **kwargs):
                            if entity is CustomModel and ident == ids['llm']:
                                return CustomModel(id=ident, name='temporary-regression', model_type=ModelType.LLM,
                                                   base_url=llm_url, api_key=os.environ['LLM_API_KEY'],
                                                   model_name=provider_model, is_active=True)
                            return super().get(entity, ident, *args, **kwargs)
                    chat_module.Session = SelectedModelSession
                original_send = httpx.AsyncClient.send
                from contextvars import ContextVar
                from app.services.llm import LLMService
                in_rewrite = ContextVar('p1_in_rewrite', default=False)
                if rewrite_only:
                    original_rewrite = LLMService.rewrite_query
                    async def guarded_rewrite(self, *args, **kwargs):
                        token = in_rewrite.set(True)
                        try:
                            return await original_rewrite(self, *args, **kwargs)
                        finally:
                            in_rewrite.reset(token)
                    LLMService.rewrite_query = guarded_rewrite
                allowed_host = urlsplit(llm_url).hostname
                used = False
                async def guarded_send(client, request, *args, **kwargs):
                    nonlocal used
                    if langfuse and request.url.scheme == 'https' and request.url.host == 'us.cloud.langfuse.com':
                        return await original_send(client, request, *args, **kwargs)
                    if request.url.scheme != 'https' or request.url.host != allowed_host or not request.url.path.endswith('/chat/completions'):
                        raise RuntimeError('unapproved provider endpoint')
                    try:
                        wire_model = json.loads(request.content).get('model')
                    except (ValueError, TypeError, AttributeError) as exc:
                        raise RuntimeError('cannot verify outgoing model') from exc
                    if wire_model != provider_model:
                        raise RuntimeError('outgoing model identity mismatch')
                    if not send_is_permitted(case, rewrite_only=rewrite_only, in_rewrite=in_rewrite.get(), used=used):
                        raise RuntimeError('provider send not authorized for this case and stage')
                    with counts.get_lock():
                        if counts.value >= (1 if rewrite_only else 6):
                            raise RuntimeError('global generation cap reached')
                        # Marker creation is atomic and happens before any send.
                        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        with os.fdopen(fd, 'w', encoding='utf-8') as f:
                            json.dump({'run': out.name, 'case': case['case_name'], 'qid': case['qid'], 'state': 'RESERVED'}, f)
                            f.flush(); os.fsync(f.fileno())
                        record(journal, {'case': case['case_name'], 'qid': case['qid'], 'state': 'RESERVED', 'time': time.time()})
                        counts.value += 1; used = True
                    try:
                        response = await original_send(client, request, *args, **kwargs)
                        if response.status_code >= 400:
                            unknown.value = 1
                        return response
                    except BaseException:
                        unknown.value = 1
                        raise
                httpx.AsyncClient.send = guarded_send
                sync_send = httpx.Client.send
                def guarded_httpx_sync(client, request, *args, **kwargs):
                    if langfuse and request.url.scheme == 'https' and request.url.host == 'us.cloud.langfuse.com':
                        return sync_send(client, request, *args, **kwargs)
                    raise RuntimeError('unapproved synchronous HTTP request')
                httpx.Client.send = guarded_httpx_sync
                original_request = requests.sessions.Session.request
                def guarded_sync(self, method, url, *args, **kwargs):
                    parsed = urlsplit(str(url))
                    if langfuse and parsed.scheme == 'https' and parsed.hostname == 'us.cloud.langfuse.com':
                        return original_request(self, method, url, *args, **kwargs)
                    raise RuntimeError('unapproved synchronous network request')
                requests.sessions.Session.request = guarded_sync
                def deny(*args, **kwargs):
                    raise RuntimeError('embedding and rerank disabled')
                if reuse_runtime:
                    RetrievalService._log_retrieval = lambda *args, **kwargs: None
                RetrievalService._embed_query_for_kb = deny
                if scratch_fulltext:
                    RetrievalService._vector_search = deny
                RerankService.rerank = deny
                from app.main import app
                import uvicorn
                with (run / 'server.log').open('w', encoding='utf-8') as log:
                    os.dup2(log.fileno(), 1); os.dup2(log.fileno(), 2)
                    try:
                        uvicorn.run(app, host='127.0.0.1', port=case_port, log_level='warning')
                    finally:
                        sdk = _sdk()
                        if sdk: sdk.flush()
            process = mp.Process(target=serve); process.start()
            try:
                import httpx
                for _ in range(120):
                    if not process.is_alive(): raise RuntimeError('isolated server exited')
                    try:
                        if httpx.get('http://127.0.0.1:%d/health' % case_port, timeout=1, trust_env=False).status_code == 200: break
                    except Exception: time.sleep(.1)
                else: raise RuntimeError('isolated server startup failed')
                request = build_http_request(case, ids['llm'], rewrite_only)
                terminal_state = 'TRANSPORT_ERROR'
                with httpx.Client(timeout=180, trust_env=False) as client:
                    with client.stream('POST', 'http://127.0.0.1:%d/api/v1/chat/completions' % case_port, json=request) as response:
                        status, content_type = response.status_code, response.headers.get('content-type', '')
                        raw = ''.join(response.iter_text())
                terminal_state = 'HTTP_ERROR' if status != 200 else 'SSE_INCOMPLETE'
                (run / 'response.sse').write_text(raw, encoding='utf-8')
                events = []
                for block in raw.split('\n\n'):
                    lines = block.splitlines()
                    kind = next((x[7:] for x in lines if x.startswith('event: ')), None)
                    payload = next((x[6:] for x in lines if x.startswith('data: ')), None)
                    if kind and payload: events.append((kind, json.loads(payload)))
                ok = status == 200 and 'text/event-stream' in content_type and raw.endswith('\n\n') and sum(k == 'done' for k, _ in events) == 1 and not any(k == 'error' for k, _ in events)
                if any(d.get('status') == 'UNKNOWN' or d.get('retrieval_status') == 'UNKNOWN' or d.get('answer_mode') == 'UNKNOWN' for _, d in events):
                    unknown.value = 1
                    ok = False
                if case['qid'].startswith('empty-'):
                    ok = ok and counts.value == before and any(d.get('retrieval_status') == 'NO_RESULTS' for k, d in events if k == 'done')
                if ok:
                    terminal_state = 'SSE_DONE'
                # Completion of a model request is independent of later
                # citation/metrics checks. Never leave a returned request
                # marked UNKNOWN just because an observation export failed.
                if ok and not unknown.value and marker.exists():
                    record(journal, {'case': case['case_name'], 'qid': case['qid'], 'state': 'RETURNED', 'time': time.time()})
                    marker.unlink()
                checks = []
                db = sqlite3.connect('file:' + str(runtime / 'isolated.db') + '?mode=ro', uri=True)
                try:
                    for citation in final_citations(events):
                        checks.append(citation_check(db, citation, case['kb_id']))
                finally: db.close()
                obs = rows(run / 'events.jsonl') if (run / 'events.jsonl').exists() else []
                trace_ids = sorted({r['trace_id'] for r in obs if r.get('stage') == 'qa.request' and r.get('event') == 'start'})
                result = {'case': case['case_name'], 'qid': case['qid'], 'kb_id': case['kb_id'], 'mode': case['mode'], 'grounded_generation': case['grounded_generation'], 'request_config': request_config(case, rewrite_only), 'elapsed_seconds': round(time.monotonic() - started, 3), 'http_terminal_state': terminal_state, 'http_status': status, 'sse_complete': ok, 'events': [k for k, _ in events], 'citations': checks, 'trace_ids': trace_ids, 'provider_calls': counts.value - before, 'answer_mode': next((d.get('answer_mode') for k, d in events if k == 'done'), None)}
                results.append(result)
                result_recorded = True
                source_reads = []
                for citation in final_citations(events):
                    if citation.get('source_type') != 'source_native_table_expansion':
                        continue
                    url = citation.get('sourceUrl', '')
                    parsed = urlsplit(url)
                    if parsed.scheme or parsed.netloc or not parsed.path.startswith('/api/v1/files/') or len(source_reads) >= 5:
                        raise ValueError('unapproved source-read URL')
                    source_response = httpx.get('http://127.0.0.1:%d%s' % (case_port, url), timeout=10, trust_env=False)
                    expected = (citation.get('source_metadata') or {}).get('source_sha256')
                    source_reads.append({'status': source_response.status_code,
                                         'valid_source_bytes': bool(source_response.status_code == 200 and expected and
                                             hashlib.sha256(source_response.content).hexdigest() == expected)})
                if source_reads:
                    result['source_reads'] = source_reads
                if capture_metrics:
                    # Copy only known metric families and bounded labels, never arbitrary response text.
                    metric_response = httpx.get('http://127.0.0.1:%d/metrics' % case_port, timeout=5, trust_env=False)
                    if metric_response.status_code != 200:
                        raise RuntimeError('isolated metrics endpoint unavailable')
                    safe = sanitized_metrics(metric_response.text)
                    (run / 'metrics.prom').write_text(safe, encoding='utf-8')
                    result['metrics_capture'] = 'SAVED' if safe else 'EMPTY'
                if metrics_hold_seconds:
                    time.sleep(metrics_hold_seconds)
                if not ok or unknown.value:
                    raise RuntimeError('request failed or outcome unknown; no replay')
                if marker.exists():
                    record(journal, {'case': case['case_name'], 'qid': case['qid'], 'state': 'RETURNED', 'time': time.time()})
                    marker.unlink()
            except BaseException:
                if not result_recorded:
                    results.append({'case': case['case_name'], 'qid': case['qid'], 'mode': case['mode'], 'grounded_generation': case['grounded_generation'], 'request_config': request_config(case, rewrite_only), 'elapsed_seconds': round(time.monotonic() - started, 3), 'http_terminal_state': terminal_state, 'provider_calls': counts.value - before})
                raise
            finally:
                if process.is_alive(): process.terminate()
                process.join(20)
    finally:
        if not reuse_runtime:
            for runtime in runtimes.values(): scrub(runtime / 'isolated.db')
        (out / 'result.json').write_text(json.dumps({'cases': results, 'provider_calls': counts.value, 'unresolved_marker': marker.exists(), 'rewrite_only': rewrite_only}, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'output': str(out), 'cases_completed': len(results), 'provider_calls': counts.value, 'unresolved_marker': marker.exists()}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()

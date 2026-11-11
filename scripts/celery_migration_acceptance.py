"""Bounded isolated Celery reliability receipt, using the real application pipeline.

Import is side-effect free. Default is capability preflight. Execution requires
Linux/native existing binaries, private loopback Redis+Chroma, a new output
root, and enough available memory. Only owned process groups may be signalled.
No natural Redis visibility-timeout redelivery or real-model quality is claimed.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from uuid import uuid4

REPO = Path(__file__).resolve().parents[1]


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2)


def available_memory_mib():
    if platform.system() == 'Linux':
        values = dict(line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())
        return int(values['MemAvailable'].split()[0]) / 1024
    if os.name == 'nt':
        import ctypes
        class Memory(ctypes.Structure):
            _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [(key, ctypes.c_ulonglong) for key in ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
        memory = Memory(); memory.length = ctypes.sizeof(memory)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(memory)):
            return memory.available / (1024 ** 2)
    return None


def find_binary(name):
    candidate = Path(sys.executable).parent / (name + ('.exe' if os.name == 'nt' else ''))
    return str(candidate.resolve()) if candidate.is_file() else shutil.which(name)


def proc_executable_identity(path):
    """Identify a borrowed ELF without resolving its container-only link target."""
    parts = Path(path).parts
    if len(parts) != 4 or parts[1] != 'proc' or not parts[2].isdigit() or parts[3] != 'exe':
        return None
    process = Path('/proc') / parts[2]
    stat = (process / 'stat').read_text()
    start_ticks = stat[stat.rfind(')') + 2:].split()[19]
    with Path(path).open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    return {'pid': int(parts[2]), 'start_ticks': start_ticks, 'executable_sha256': digest}


def audit(args):
    binaries = {name: find_binary(name) for name in ('redis-server', 'chroma', 'unshare', 'ip')}
    explicit_binaries = {'redis-server': args.redis_server, 'chroma': args.chroma_bin}
    invalid_binaries = []
    binary_identities = {}
    for name, configured in explicit_binaries.items():
        if configured is None:
            continue
        try:
            literal = Path(os.path.abspath(configured))
            identity = proc_executable_identity(literal)
            candidate = literal if identity else literal.resolve()
            if not candidate.is_file() or not os.access(candidate, os.X_OK):
                raise ValueError('EXECUTABLE_UNAVAILABLE')
            if name == 'redis-server' and args.redis_start_ticks is not None:
                if identity is None or identity['start_ticks'] != args.redis_start_ticks or identity['executable_sha256'] != args.redis_exe_sha256:
                    raise ValueError('BORROWED_EXECUTABLE_IDENTITY_CHANGED')
            binaries[name] = str(candidate)
            if identity:
                binary_identities[name] = identity
        except (OSError, ValueError, IndexError):
            binaries[name] = None
            invalid_binaries.append('INVALID_' + name.upper().replace('-', '_') + '_EXECUTABLE')
    memory = available_memory_mib()
    reasons = list(invalid_binaries)
    if platform.system() != 'Linux' or not hasattr(os, 'fork'):
        reasons.append('LINUX_PREFORK_REQUIRED')
    reasons += ['MISSING_' + name.upper().replace('-', '_') for name, value in binaries.items() if not value]
    if memory is None or memory < args.min_free_mib:
        reasons.append('INSUFFICIENT_OR_UNKNOWN_AVAILABLE_MEMORY')
    if (args.backend / '.env').exists() or (args.backend.parent / 'config/runtime-secrets.env').exists():
        reasons.append('SOURCE_SNAPSHOT_CONTAINS_DOTENV_OR_RUNTIME_SECRETS')
    if not (args.backend / 'app/main.py').is_file():
        reasons.append('BACKEND_SOURCE_NOT_FOUND')
    versions = {}
    for package in ('celery', 'redis', 'chromadb', 'uvicorn'):
        try: versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError: reasons.append('MISSING_PACKAGE_' + package.upper())
    namespace_probe = None
    if not reasons and binaries['unshare']:
        probe = subprocess.run([binaries['unshare'],'--net','--',sys.executable,'-I','-S','-c','pass'],capture_output=True,text=True,timeout=3)
        namespace_probe = probe.returncode
        if probe.returncode: reasons.append('PRIVATE_NETWORK_NAMESPACE_UNAVAILABLE')
    return {'namespace_probe_exit_code':namespace_probe, 'platform': platform.platform(), 'python': sys.version.split()[0], 'binaries': binaries, 'binary_identities': binary_identities,
            'package_versions': versions, 'cpu_count': os.cpu_count(),
            'load_average': list(os.getloadavg()) if hasattr(os, 'getloadavg') else None, 'available_memory_mib': memory, 'minimum_memory_mib': args.min_free_mib,
            'capability': 'BLOCKED' if reasons else 'AVAILABLE', 'reasons': reasons,
            'installs_or_image_pulls': 0}


def free_port():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        return listener.getsockname()[1]


def isolated_environment(root, ports, backend, run_id):
    """Remove inherited secrets/endpoints; children run outside any project .env."""
    env = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if (any(part in upper for part in ('API_KEY', 'SECRET', 'PASSWORD', 'TOKEN', 'PROXY', 'CREDENTIAL', 'ACCESS_KEY', 'PRIVATE_KEY', 'AUTHORIZATION'))
                or upper.startswith(('RAG_', 'CELERY_', 'FILE_TASK_', 'LANGFUSE_', 'OTEL_', 'CHROMA_', 'OPENAI_', 'ANTHROPIC_'))
                or (upper.startswith('DEFAULT_') and upper.endswith('_MODEL_ID'))
                or upper in ('DATABASE_URL', 'REDIS_URL', 'MEMORY_REDIS_URL', 'UPLOAD_DIR', 'IMAGE_DIR', 'LLM_BASE_URL', 'EMBEDDING_BASE_URL', 'VLM_BASE_URL', 'OLLAMA_BASE_URL', 'PROVIDER_CA_BUNDLE')):
            continue
        env[key] = value
    endpoint = f"http://127.0.0.1:{ports['embedding']}/v1"
    env.update(DATABASE_URL='sqlite:///' + str(root / 'tasks.db'),
        REDIS_URL=f"redis://127.0.0.1:{ports['redis']}/0",
        MEMORY_REDIS_URL=f"redis://127.0.0.1:{ports['redis']}/2",
        CELERY_BROKER_URL=f"redis://127.0.0.1:{ports['redis']}/0",
        CELERY_RESULT_BACKEND=f"redis://127.0.0.1:{ports['redis']}/1",
        CHROMA_HOST='127.0.0.1', CHROMA_PORT=str(ports['chroma']), CHROMA_DB_DIR=str(root / 'chroma'),
        FILE_TASK_ARTIFACT_DIR=str(root / 'artifacts'), UPLOAD_DIR=str(root / 'uploads'), IMAGE_DIR=str(root / 'images'),
        CELERY_WORKER_CONCURRENCY='1', CELERY_TASK_TIME_LIMIT='60', FILE_TASK_RENOTIFY_MS='500',
        FILE_TASK_SCAN_SECONDS='.2', ANONYMIZED_TELEMETRY='false',
        RAG_OBSERVABILITY_ENABLED='false', LANGFUSE_ENABLED='false', RAG_PROMETHEUS_ENABLED='false',
        RAG_MODEL_ROUTER_MODE='OFF', RAG_AGENT_EVIDENCE_PROJECTION='OFF', RAG_CHROMA_EXECUTOR_ENABLED='false',
        LLM_BASE_URL=endpoint, EMBEDDING_BASE_URL=endpoint, VLM_BASE_URL=endpoint, OLLAMA_BASE_URL=endpoint,
        LLM_API_KEY='synthetic-only', EMBEDDING_API_KEY='synthetic-only', VLM_API_KEY='synthetic-only',
        LLM_MODEL='synthetic-unused', EMBEDDING_MODEL='synthetic-embedding', PROVIDER_CA_BUNDLE='',
        RAYON_NUM_THREADS='1', TOKIO_WORKER_THREADS='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
        PYTHONPATH=os.pathsep.join((str(root / 'guard'), str(backend))), PYTHONIOENCODING='utf-8',
        RAG_RUNTIME_SECRETS_FILE=str(root/'empty-runtime-secrets.env'),
        RAG_TEST_RUN_ID=run_id, RAG_TEST_ROOT=str(root), RAG_TEST_PORTS=','.join(map(str, ports.values())))
    return env


def write_network_guard(root):
    guard = root / 'guard'; guard.mkdir()
    # This support artifact is not another executor: it denies all non-owned TCP endpoints.
    (guard / 'sitecustomize.py').write_text('''import os,socket,json,time
from pathlib import Path
root=Path(os.environ['RAG_TEST_ROOT'])
ports={int(v) for v in os.environ['RAG_TEST_PORTS'].split(',')}
(root/('guard-ready-'+str(os.getpid())+'.json')).write_text(json.dumps({'pid':os.getpid(),'time':time.time()}))
def deny(kind):
 with (root/'network-denied.jsonl').open('a') as out:out.write(json.dumps({'pid':os.getpid(),'operation':kind})+'\\n')
 raise OSError('ISOLATED_NETWORK_GUARD')
for name in ('connect','connect_ex'):
 original=getattr(socket.socket,name)
 def wrap(method):
  def call(sock,address):
   if sock.family==socket.AF_UNIX:return method(sock,address)
   if not isinstance(address,tuple) or address[0] not in ('127.0.0.1','::1') or address[1] not in ports:deny('connect')
   return method(sock,address)
  return call
 setattr(socket.socket,name,wrap(original))
original_dns=socket.getaddrinfo
def dns(host,*args,**kwargs):
 if host not in ('127.0.0.1',b'127.0.0.1','::1',b'::1','localhost',b'localhost'):deny('dns')
 return original_dns(host,*args,**kwargs)
socket.getaddrinfo=dns
''', encoding='utf-8')


def start_identity(pid):
    try: return Path(f'/proc/{pid}/stat').read_text().rpartition(')')[2].split()[19]
    except (FileNotFoundError, IndexError, ProcessLookupError): return None


@dataclass
class OwnedProcess:
    name: str
    process: subprocess.Popen
    start_ticks: str | None
    command: list
    role: str | None = None
    native_identity: dict | None = None


class Experiment:
    def __init__(self, args, root, env, ports):
        self.args, self.root, self.env, self.ports = args, root, env, ports
        self.deadline = time.monotonic() + args.timeout_seconds
        self.processes, self.events, self.checks, self.probes = [], [], [], []
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.embedding_calls = []
        self.hold_embedding = threading.Event()
        self.embedding_entered = threading.Event()
        self.embedding_release = threading.Event()
        self.embedding_server = None
        self.resource_samples = []
        self.last_sample = 0.0

    def sample_resources(self):
        if time.monotonic() - self.last_sample < .5:
            return
        self.last_sample = time.monotonic()
        pids = {os.getpid()}
        def descendants(pid):
            try:
                children = Path(f'/proc/{pid}/task/{pid}/children').read_text().split()
            except FileNotFoundError:
                return
            for child in children:
                child = int(child)
                if child not in pids:
                    pids.add(child); descendants(child)
        for owned in self.processes:
            if owned.process.poll() is None:
                pids.add(owned.process.pid); descendants(owned.process.pid)
        rows = []
        for pid in sorted(pids):
            try:
                fields = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/status').read_text().splitlines() if ':' in line)
                rss = int(fields.get('VmRSS', '0 kB').split()[0]) / 1024
                try:
                    pss_fields = dict(line.split(':', 1) for line in Path(f'/proc/{pid}/smaps_rollup').read_text().splitlines() if ':' in line)
                    pss = int(pss_fields['Pss'].split()[0]) / 1024
                except (OSError, KeyError):
                    pss = None
                rows.append({'pid': pid, 'rss_mib': rss, 'pss_mib': pss})
            except FileNotFoundError:
                continue
        self.resource_samples.append({'time': time.time(), 'available_memory_mib': available_memory_mib(),
            'load_average': list(os.getloadavg()), 'owned_processes': rows,
            'owned_rss_sum_mib': sum(row['rss_mib'] for row in rows),
            'owned_pss_sum_mib': sum(row['pss_mib'] for row in rows) if all(row['pss_mib'] is not None for row in rows) else None})

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0: raise TimeoutError('EXPERIMENT_DEADLINE')
        return value

    def spawn(self, name, command, role=None, native_identity=None):
        env = dict(self.env)
        if role: env['FILE_TASK_WORKER_ROLE'] = role
        log = (self.root / (name + '.log')).open('ab', buffering=0)
        working_directory = Path(native_identity['cwd']) if native_identity else self.root
        try: process = subprocess.Popen(command, cwd=working_directory, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        finally: log.close()
        owned = OwnedProcess(name, process, start_identity(process.pid), command, role, native_identity)
        self.processes.append(owned)  # Register before any verification can raise.
        if owned.start_ticks is None:
            raise RuntimeError('OWNED_PROCESS_START_NOT_VERIFIED')
        self.events.append({'action': 'start', 'name': name, 'pid': process.pid, 'start_ticks': owned.start_ticks, 'command': command, 'native_identity': native_identity, 'time': time.time()})
        return owned

    def group_members(self, owned):
        members = []
        for path in Path('/proc').glob('[0-9]*/stat'):
            try:
                values = path.read_text().rpartition(')')[2].split()
                if values[0] == 'Z': continue  # Already-dead processes cannot retain sockets/work.
                if int(values[2]) != owned.process.pid or int(values[3]) != owned.process.pid:
                    continue
                pid = int(path.parent.name)
                if owned.native_identity:
                    # Redis rewrites its process title/environment; its recorded
                    # native ownership tuple remains independently verifiable.
                    expected = owned.native_identity
                    with (path.parent/'exe').open('rb') as stream:
                        executable_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if (pid != owned.process.pid or values[19] != owned.start_ticks
                            or os.readlink(path.parent/'ns/net') != expected['netns']
                            or os.readlink(path.parent/'cwd') != expected['cwd']
                            or executable_sha != expected['executable_sha256']):
                        raise RuntimeError('OWNED_NATIVE_IDENTITY_MISMATCH')
                else:
                    # Only inspect environment of members of our recorded new session.
                    environment = (path.parent/'environ').read_bytes().split(b'\0')
                    marker = ('RAG_TEST_RUN_ID='+self.env['RAG_TEST_RUN_ID']).encode()
                    if marker not in environment:
                        raise RuntimeError('OWNED_GROUP_MARKER_MISMATCH')
                members.append({'pid':pid,'start_ticks':values[19]})
            except (FileNotFoundError, ProcessLookupError):
                continue
        return members

    def stop(self, owned, fault=False):
        process = owned.process
        if process.poll() is None and start_identity(process.pid) != owned.start_ticks:
            raise RuntimeError('OWNED_PROCESS_IDENTITY_CHANGED')
        members = self.group_members(owned)
        if not members:
            if process.poll() is None: process.wait(timeout=1)
            return
        sig = signal.SIGKILL if fault else signal.SIGTERM
        os.killpg(process.pid, sig)
        self.events.append({'action':'owned_fault' if fault else 'owned_stop','name':owned.name,
            'pid':process.pid,'start_ticks':owned.start_ticks,'members':members,'signal':sig.name,'time':time.time()})
        end = time.monotonic()+4
        while self.group_members(owned) and time.monotonic()<end:
            process.poll(); time.sleep(.05)
        remaining = self.group_members(owned)
        if remaining:
            os.killpg(process.pid, signal.SIGKILL)
            self.events.append({'action':'owned_cleanup_kill','name':owned.name,'members':remaining})
        try: process.wait(timeout=2)
        except subprocess.TimeoutExpired: raise RuntimeError('OWNED_PROCESS_DID_NOT_EXIT')

    def request(self, path, payload=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.ports['api']}" + path,
            data=json.dumps(payload).encode() if payload is not None else None, headers={'Content-Type': 'application/json'})
        with self.opener.open(request, timeout=min(1, self.remaining())) as response: return json.load(response)

    def wait(self, predicate, *, scan=False):
        while self.remaining():
            self.sample_resources()
            if scan: self.scan_once()
            value = predicate()
            if value: return value
            time.sleep(min(.1, self.remaining()))

    def http_ready(self, port, path):
        try:
            with self.opener.open(f'http://127.0.0.1:{port}' + path, timeout=min(.3, self.remaining())) as response:
                return response.status == 200
        except OSError: return False

    def embedding_start(self):
        experiment = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                if self.path != '/v1/embeddings':
                    self.send_error(400); return
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                inputs = body['input']; inputs = [inputs] if isinstance(inputs, str) else inputs
                experiment.embedding_calls.append({'request': len(experiment.embedding_calls) + 1, 'input_count': len(inputs), 'time': time.time()})
                if experiment.hold_embedding.is_set():
                    experiment.embedding_entered.set()
                    experiment.embedding_release.wait(min(20, max(.1, experiment.remaining())))
                data = json.dumps({'model': 'synthetic-embedding', 'data': [{'index': i, 'embedding': [1., 0., 0.]} for i in range(len(inputs))]}).encode()
                try:
                    self.send_response(200); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
                except OSError: pass
        self.embedding_server = ThreadingHTTPServer(('127.0.0.1', self.ports['embedding']), Handler)
        threading.Thread(target=self.embedding_server.serve_forever, daemon=True).start()

    def worker(self, name, role):
        return self.spawn(name, [sys.executable, '-m', 'celery', '-A', 'app.tasks.celery_app:celery_app', 'worker',
            '-Q', 'document.' + role, '-n', name + '@%h', '--pool=prefork', '--concurrency=1',
            '--without-gossip', '--without-mingle', '--without-heartbeat', '--loglevel=INFO'], role)

    def check(self, name, passed, **detail):
        self.checks.append({'check': name, 'status': 'PASS' if passed else 'FAIL', **detail})
        if not passed: raise AssertionError(name)

    def run(self, capabilities):
        from redis import Redis
        self.redis = Redis(host='127.0.0.1', port=self.ports['redis'], socket_connect_timeout=.3, socket_timeout=.3)
        redis_dir = self.root / 'redis'; redis_dir.mkdir()
        for name, expected in capabilities['binary_identities'].items():
            if proc_executable_identity(capabilities['binaries'][name]) != expected:
                raise RuntimeError('BORROWED_EXECUTABLE_IDENTITY_CHANGED')
        with Path(capabilities['binaries']['redis-server']).open('rb') as stream:
            redis_executable_sha = hashlib.file_digest(stream, 'sha256').hexdigest()
        redis_identity = {'netns': os.readlink('/proc/self/ns/net'), 'cwd': str(redis_dir),
                          'executable_sha256': redis_executable_sha}
        self.spawn('redis', [capabilities['binaries']['redis-server'], '--bind', '127.0.0.1', '--port', str(self.ports['redis']),
            '--dir', str(redis_dir), '--save', '', '--appendonly', 'no'], native_identity=redis_identity)
        def redis_ready():
            try: return self.redis.ping()
            except OSError: return False
            except Exception as exc:
                if type(exc).__name__ == 'ConnectionError': return False
                raise
        self.wait(redis_ready)
        self.spawn('chroma', [capabilities['binaries']['chroma'], 'run', '--host', '127.0.0.1', '--port', str(self.ports['chroma']), '--path', str(self.root / 'chroma')])
        self.wait(lambda: self.http_ready(self.ports['chroma'], '/api/v2/heartbeat'))
        from app.core.database import engine, init_db
        from app.models import CustomModel, ModelType, KnowledgeBase, FileDocument
        from app.services.runtime_settings import get_runtime_settings, update_runtime_settings
        from app.tasks import dispatch
        from app.tasks.file_task_recovery import scan_once
        from sqlmodel import Session, select
        self.scan_once = scan_once
        init_db()
        source = self.root / 'source.txt'
        source.write_text('Public synthetic reliability fixture. Water freezes at zero degrees Celsius. Revenue is 42 units.', encoding='utf-8')
        with Session(engine) as session:
            settings = get_runtime_settings(session)
            update_runtime_settings(session, {'max_concurrency': 1}, settings.revision)
            session.add(CustomModel(id='fixture-model', name='Synthetic local embedding', model_type=ModelType.EMBEDDING,
                base_url=f"http://127.0.0.1:{self.ports['embedding']}/v1", api_key='synthetic-only', model_name='synthetic-embedding', context_length=8192))
            session.add(KnowledgeBase(id='queue-kb', name='Public synthetic', embedding_model='fixture-model', vlm_model=''))
            for name in ('complete', 'lease', 'fault'):
                session.add(FileDocument(id=name, kb_id='queue-kb', name=name+'.txt', local_path=str(source), size=source.stat().st_size))
            session.commit()
        self.embedding_start()
        self.spawn('api', [sys.executable, '-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1', '--port', str(self.ports['api']), '--workers', '1'])
        self.wait(lambda: self.http_ready(self.ports['api'], '/health'))
        parse_worker = self.worker('parse-worker', 'parse')
        payload = {'chunk_mode': 'custom', 'chunk_size': 500, 'chunk_overlap': 0, 'auto_vectorize': True}
        task = self.request('/api/v1/files/complete/parse', payload)['data']['task_id']
        def status(task_id):
            with Session(engine) as session: return session.get(dispatch.FileTaskReceipt, task_id).status
        def completed():
            started = time.perf_counter(); response = self.request('/health')
            self.probes.append({'operation': 'health_during_ingestion', 'elapsed_ms': (time.perf_counter()-started)*1000})
            current = status(task)
            if current in ('FAILED', 'UNKNOWN'): raise RuntimeError('FIRST_TASK_' + current)
            return current == 'COMPLETED'
        def ready_for_index():
            with Session(engine) as session:
                row = session.get(dispatch.FileTaskReceipt, task)
                if row.status in ('FAILED', 'UNKNOWN'): raise RuntimeError('FIRST_TASK_' + row.status)
                return row.stage == 'index' and row.status == 'QUEUED'
        self.wait(ready_for_index, scan=True)
        self.stop(parse_worker)
        index_worker = self.worker('index-worker', 'index')
        self.wait(completed, scan=True)
        from app.models import DocumentChunk
        with Session(engine) as session: chunks = [row.id for row in session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'complete')).all()]
        self.check('real_worker_pipeline_completed', bool(chunks), chunk_count=len(chunks), synthetic_embedding_requests=len(self.embedding_calls))
        started = time.perf_counter()
        recall = self.request('/api/v1/knowledge-bases/queue-kb/recall', {'query':'Water freezes', 'search_mode':'fulltext', 'top_k':5, 'context_window':0})
        self.probes.append({'operation':'fulltext_after_ingestion', 'elapsed_ms':(time.perf_counter()-started)*1000})
        self.check('published_evidence_readable', any('Water freezes' in row['content'] for row in recall['data']['results']))
        before_calls = len(self.embedding_calls)
        delivery = dispatch.celery_app.send_task('app.tasks.dispatch.execute_file_operation', args=[task, 'index'], task_id=str(uuid4()), queue='document.index')
        duplicate = delivery.get(timeout=min(5, self.remaining()))
        with Session(engine) as session: after = [row.id for row in session.exec(select(DocumentChunk).where(DocumentChunk.file_id == 'complete')).all()]
        self.check('real_duplicate_delivery_no_mutation_or_embedding', duplicate.get('duplicate_delivery') is True and chunks == after and before_calls == len(self.embedding_calls))
        self.stop(index_worker)  # Both role workers are quiescent during controlled manual claims.
        lease_task = dispatch.dispatch_file_task('lease', 'parse', {**payload, 'auto_vectorize':False})['task_id']
        old = dispatch.claim_task(lease_task, 'parse', 'controlled-old-owner')
        self.check('controlled_lease_claim_created', old is not None)
        with Session(engine) as session:
            row = session.get(dispatch.FileTaskReceipt, lease_task); row.lease_until = dispatch.now_ms()-1; session.add(row); session.commit()
        recovered = scan_once()
        with Session(engine) as session:
            row = session.get(dispatch.FileTaskReceipt, lease_task); row.next_run_at=0; session.add(row); session.commit()
        new = dispatch.claim_task(lease_task, 'parse', 'controlled-new-owner')
        with Session(engine) as session:
            old_rejected = not dispatch.complete_in_session(session, old, {'synthetic':'old'})
            new_allowed = dispatch.complete_in_session(session, new, {'synthetic':'lease-fencing-only'}) if new else False
            session.commit()
        self.check('controlled_lease_recovery_fences_old_owner', recovered['retried']==1 and old_rejected and new_allowed,
                   mechanism='private lease timestamp moved into past; not a natural-duration test')
        parse_worker = self.worker('parse-worker-fault', 'parse')
        self.hold_embedding.set(); self.embedding_entered.clear()
        fault = self.request('/api/v1/files/fault/parse', payload)['data']['task_id']
        def pending():
            with Session(engine) as session:
                effect = session.exec(select(dispatch.FileTaskEffect).where(dispatch.FileTaskEffect.task_id==fault, dispatch.FileTaskEffect.state=='PENDING')).first()
                if not self.embedding_entered.is_set() or effect is None: return None
                row = session.get(dispatch.FileTaskReceipt, fault)
                attempt = session.get(dispatch.FileTaskAttempt, (fault, row.attempt_no))
                if not attempt or attempt.owner_token != row.owner_token: return None
                return {'owner_token': row.owner_token, 'stage': row.stage, 'worker_id': attempt.worker_id,
                        'attempt_no': row.attempt_no}
        effect_owner = self.wait(pending, scan=True)
        actual_owner = next((owned for owned in self.processes if owned.role and owned.process.poll() is None
            and effect_owner['worker_id'].partition('@')[0] == owned.name), None)
        self.check('pending_effect_owner_matches_owned_process', actual_owner is not None,
            stage=effect_owner['stage'], worker_id=effect_owner['worker_id'],
            owner_token_sha256=hashlib.sha256(effect_owner['owner_token'].encode()).hexdigest())
        self.stop(actual_owner, fault=True)
        with Session(engine) as session:
            row = session.get(dispatch.FileTaskReceipt, fault); row.lease_until=dispatch.now_ms()-1; session.add(row); session.commit()
        recovered = scan_once()
        self.check('owned_worker_loss_pending_effect_becomes_unknown', recovered['unknown']==1 and status(fault)=='UNKNOWN',
                   mechanism='real dedicated process-group SIGKILL; controlled private lease expiry')
        self.embedding_release.set()
        calls_at_unknown = len(self.embedding_calls)
        self.worker(actual_owner.name+'-restarted', actual_owner.role)
        duplicate = dispatch.celery_app.send_task('app.tasks.dispatch.execute_file_operation', args=[fault,effect_owner['stage']], task_id=str(uuid4()), queue='document.'+actual_owner.role).get(timeout=min(8,self.remaining()))
        self.check('unknown_effect_not_replayed_by_explicit_duplicate', duplicate.get('duplicate_delivery') is True and status(fault)=='UNKNOWN' and len(self.embedding_calls)==calls_at_unknown)
        self.redis.close(); engine.dispose()

    def cleanup(self):
        self.embedding_release.set()
        for owned in reversed(self.processes):
            try: self.stop(owned)
            except Exception as exc: self.events.append({'action':'cleanup_error','name':owned.name,'error_type':type(exc).__name__})
        if self.embedding_server:
            self.embedding_server.shutdown(); self.embedding_server.server_close()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--backend', type=Path, default=REPO/'backend')
    parser.add_argument('--redis-server', type=Path, help='Existing native executable; no installation or container reuse')
    parser.add_argument('--chroma-bin', type=Path, help='Existing native Chroma executable outside the interpreter bin')
    parser.add_argument('--redis-start-ticks', help='Expected /proc Redis executable owner start_ticks')
    parser.add_argument('--redis-exe-sha256', help='Expected /proc Redis executable SHA-256')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--timeout-seconds', type=int, default=120)
    parser.add_argument('--min-free-mib', type=int, default=1024)
    parser.add_argument('--namespace-child', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--parent-netns', help=argparse.SUPPRESS)
    args=parser.parse_args(argv)
    if (args.redis_start_ticks is None) != (args.redis_exe_sha256 is None):
        parser.error('Expected Redis start_ticks and executable SHA-256 must be supplied together')
    if args.redis_start_ticks is not None and args.redis_server is None:
        parser.error('Expected Redis identity requires --redis-server')
    if not 30 <= args.timeout_seconds <= 120 or args.min_free_mib < 512:
        parser.error('Require timeout30..120 and memory floor>=512MiB')
    args.backend=args.backend.resolve(); root=args.output_dir.resolve()
    capabilities=audit(args)
    if args.execute and not args.namespace_child and capabilities['capability']=='AVAILABLE':
        parent_ns=os.readlink('/proc/self/ns/net')
        command=[capabilities['binaries']['unshare'],'--net','--',sys.executable,str(Path(__file__).resolve()),
            '--namespace-child','--parent-netns',parent_ns,'--backend',str(args.backend),'--output-dir',str(root),
            '--timeout-seconds',str(args.timeout_seconds),'--min-free-mib',str(args.min_free_mib),'--execute']
        for option, name in (('--redis-server', 'redis-server'), ('--chroma-bin', 'chroma')):
            command.extend([option, capabilities['binaries'][name]])
        redis_identity = capabilities['binary_identities'].get('redis-server')
        if redis_identity:
            command.extend(['--redis-start-ticks', redis_identity['start_ticks'], '--redis-exe-sha256', redis_identity['executable_sha256']])
        result=subprocess.run(command,timeout=args.timeout_seconds+40)
        if root.exists():write_json(root/'namespace-launch.json',{'command':command,'parent_network_namespace':parent_ns,'exit_code':result.returncode})
        return result.returncode
    root.mkdir(parents=True,exist_ok=False)
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'run_id':uuid4().hex,'capabilities':capabilities,
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'backend':str(args.backend),
        'max_experiment_seconds':args.timeout_seconds,'execute':args.execute,'paid_provider_calls':0 if not args.execute else None,
        'real_model_quality':'NOT_RUN','cost':None,'tokens':None,'natural_visibility_redelivery':'NOT_RUN',
        'fixture':'one public tiny text; no original corpus/index copies','queues':['document.parse','document.index'],
        'native_telemetry':'ANONYMIZED_TELEMETRY=false requested; actual external egress denied by the private network namespace, not Python monkeypatch',
        'source_secrets_check':'reject backend/.env and project/config/runtime-secrets.env; runtime secrets path overridden to empty file',
        'scope':'source snapshot without dotenv/secrets; fresh private loopback-only network namespace with Redis/Chroma/SQLite/artifacts; no production signals'}
    if not args.execute or capabilities['capability']=='BLOCKED':
        manifest['status']='SKIPPED_CAPABILITY' if capabilities['capability']=='BLOCKED' else 'PREFLIGHT_READY_NOT_EXECUTED'
        write_json(root/'report.json',manifest)
        print(json.dumps({'status':manifest['status'],'capability':capabilities['capability'],'reasons':capabilities['reasons'],'report':str(root/'report.json')}))
        return 2 if capabilities['capability']=='BLOCKED' else 0
    try:
        current_ns=os.readlink('/proc/self/ns/net')
        if not args.namespace_child or not args.parent_netns or current_ns==args.parent_netns:
            raise RuntimeError('PRIVATE_NETWORK_NAMESPACE_REQUIRED')
        subprocess.run([capabilities['binaries']['ip'],'link','set','lo','up'],check=True)
        links=json.loads(subprocess.check_output([capabilities['binaries']['ip'],'-j','link','show']))
        routes=json.loads(subprocess.check_output([capabilities['binaries']['ip'],'-j','route','show']))
        if {link['ifname'] for link in links}!={'lo'} or any(route.get('dst')=='default' for route in routes):
            raise RuntimeError('PRIVATE_LOOPBACK_ONLY_NAMESPACE_REQUIRED')
        manifest['resource_policy']='one CPU affinity; sequential parse/embed then index; configured memory floor is a precheck, not a measured full-stack capacity guarantee'
        manifest['network_isolation']={'parent':args.parent_netns,'child':current_ns,'interfaces':[link['ifname'] for link in links],'routes':routes,'native_and_python_egress':'LOOPBACK_ONLY_NETWORK_NAMESPACE'}
    except Exception as exc:
        write_json(root/'report.json',{**manifest,'status':'SKIPPED_CAPABILITY','reason':'NETWORK_NAMESPACE_PREPARATION_FAILED','error_type':type(exc).__name__,'paid_provider_calls':0,'stack_started':False})
        return 2
    ports={}
    for name in ('redis','chroma','api','embedding'):
        value=free_port()
        while value in ports.values():value=free_port()
        ports[name]=value
    write_network_guard(root)
    (root/'empty-runtime-secrets.env').write_text('',encoding='utf-8')
    env=isolated_environment(root,ports,args.backend,manifest['run_id'])
    manifest['ports']=ports
    manifest['paths']={name:str(root/name) for name in ('tasks.db','redis','chroma','uploads','images','artifacts')}
    write_json(root/'manifest.json',manifest)
    cpu = min(os.sched_getaffinity(0))
    os.sched_setaffinity(0, {cpu})  # All owned children inherit a single allowed CPU.
    manifest['owned_cpu_affinity'] = [cpu]
    os.environ.clear(); os.environ.update(env); os.chdir(root); sys.path[:0]=[str(root/'guard'),str(args.backend)]
    import importlib.util
    guard_spec=importlib.util.spec_from_file_location('owned_isolation_guard',root/'guard/sitecustomize.py')
    guard_module=importlib.util.module_from_spec(guard_spec);guard_spec.loader.exec_module(guard_module)
    if not (root/('guard-ready-'+str(os.getpid())+'.json')).is_file():raise RuntimeError('ORCHESTRATOR_GUARD_NOT_INSTALLED')
    experiment=Experiment(args,root,env,ports)
    code=1
    try:
        experiment.run(capabilities)
        code=0
    except BaseException as exc:
        experiment.checks.append({'check':'experiment','status':'FAIL','error_type':type(exc).__name__,'detail':str(exc)})
    finally:
        experiment.cleanup()
        denied=(root/'network-denied.jsonl').exists()
        alive=[]
        for owned in experiment.processes:
            try:
                if owned.process.poll() is None or experiment.group_members(owned):alive.append(owned.name)
            except Exception as exc:
                alive.append(owned.name)
                experiment.events.append({'action':'cleanup_unverified','name':owned.name,'error_type':type(exc).__name__})
        if denied or alive:code=1
        report={**manifest,'paid_provider_calls':0 if not denied and manifest.get('network_isolation') else None,'status':'PASS_ISOLATED_RELIABILITY' if code==0 else 'FAIL','exit_code':code,
            'checks':experiment.checks,'owned_process_events':experiment.events,'owned_processes_still_alive':alive,
            'network_guard_denied_attempts':denied,'synthetic_embedding_http_requests':experiment.embedding_calls,
            'contention_probes':experiment.probes,'resource_samples':experiment.resource_samples,'contention_claim':'sequential role workers, raw loopback health/fulltext timings only; not full parallel QA, quality/P95 or production contention',
            'cleanup':'owned process groups stopped; synthetic SQLite/UNKNOWN/artifacts/logs retained for inspection'}
        write_json(root/'report.json',report)
    print(json.dumps({'status':report['status'],'exit_code':code,'report':str(root/'report.json')}))
    return code


if __name__=='__main__':
    raise SystemExit(main())

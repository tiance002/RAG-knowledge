"""Credential-free, networkless Linux capsule; reuse an existing image and venv.

Only frozen backend code, public worker/helper code and a single public case are
mounted. No registry, evaluation, holdout store, host home or production data is
mounted. The protected executor still owns all pairing and fact checks.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import re
import queue
import shlex
import subprocess
import tarfile
import threading
import time
import uuid


ROOT = Path(__file__).resolve().parents[1]
BASE_COMMIT = '90d86ce87e8c76c8c45f5f296160ddf53de71d54'
REMOTE_BASE = '/opt/amrag/experiments/rsi-v22-20261115'
IMAGE = '7d79b6e09c48'
MAX_OUTPUT = 4 * 1024 * 1024


REMOTE_PROGRAM = r'''
import base64,hashlib,io,json,os,selectors,shutil,subprocess,tarfile,time
from pathlib import Path
base=Path('/opt/amrag/experiments/rsi-v22-20261115').resolve()
job=(base/REQ['job']).resolve()
if not job.is_relative_to(base) or len(REQ['job'])!=32 or any(c not in '0123456789abcdef' for c in REQ['job']):
    raise ValueError('invalid capsule directory')
def reply(value):
    print(json.dumps(value))
if REQ['op']=='setup':
    if job.exists():raise FileExistsError('capsule already exists')
    data=base64.b64decode(REQ['tar'],validate=True)
    if hashlib.sha256(data).hexdigest()!=REQ['tar_sha256']:raise ValueError('code archive digest mismatch')
    job.mkdir(parents=True)
    (job/'code').mkdir()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            target=(job/'code'/member.name).resolve()
            if not target.is_relative_to(job/'code') or not (member.isfile() or member.isdir()):
                raise ValueError('unsafe code archive member')
        archive.extractall(job/'code',filter='data')
    for path in (job/'code').rglob('*'):
        path.chmod(0o755 if path.is_dir() else 0o444)
    (job/'inputs').mkdir()
    (job/'owner.json').write_text(json.dumps({'job':REQ['job'],'tar_sha256':REQ['tar_sha256']}))
    reply({'status':'READY','code_archive_sha256':REQ['tar_sha256']})
elif REQ['op']=='cleanup':
    names=subprocess.check_output(['docker','ps','-aq','--filter','label=codex.rsi-job='+REQ['job']],text=True).strip()
    if names:raise ValueError('capsule has live containers; cleanup refused')
    owner=json.loads((job/'owner.json').read_text())
    if owner['job']!=REQ['job']:raise ValueError('capsule owner mismatch')
    # Code files are read-only, directory ownership remains with this helper.
    shutil.rmtree(job)
    reply({'status':'REMOVED','path':str(job)})
elif REQ['op']=='run':
    if json.loads((job/'owner.json').read_text())['job']!=REQ['job']:raise ValueError('unknown capsule owner')
    step=REQ['step']
    if not step.isdigit():raise ValueError('invalid execution step')
    directory=job/'inputs'/step
    directory.mkdir()
    for name,encoded in REQ['files'].items():
        if name not in ('worker.py','input.json','config.json'):raise ValueError('unapproved mounted input')
        data=base64.b64decode(encoded,validate=True)
        if len(data)>2000000:raise ValueError('input exceeds capsule limit')
        (directory/name).write_bytes(data)
        (directory/name).chmod(0o444)
    name='rsi-v22-'+REQ['job'][:10]+'-'+step
    launcher="""import base64,contextlib,runpy,sys
sys.argv=['/input/worker.py',STAGE,*ARGS,'/out/result.json']
with contextlib.redirect_stdout(sys.stderr):
    runpy.run_path('/input/worker.py',run_name='__main__')
from pathlib import Path
data=Path('/out/result.json').read_bytes()
if len(data)>3000000:raise ValueError('capture output exceeds limit')
print('RSI_CAPTURE_BASE64='+base64.b64encode(data).decode())
import importlib.metadata,json,os
runtime={'python':sys.version.split()[0],'uid':os.getuid(),'packages':{name:importlib.metadata.version(name) for name in ('sqlmodel','sqlalchemy','pydantic','openai','pandas','langgraph','httpx')}}
print('RSI_RUNTIME_BASE64='+base64.b64encode(json.dumps(runtime,sort_keys=True).encode()).decode())
""".replace('STAGE',repr(REQ['stage'])).replace('ARGS',repr(['/input/input.json']+(['/input/config.json'] if REQ['stage']=='deliver' else [])))
    args=['docker','run','--rm','--name',name,'--label','codex.task=rsi-v22-20261115',
          '--label','codex.rsi-job='+REQ['job'],'--network','none','--read-only',
          '--cap-drop','ALL','--security-opt','no-new-privileges','--user','65534:65534',
          '--memory','768m','--cpus','1','--pids-limit','64','--log-driver','none',
          '--tmpfs','/tmp:rw,noexec,nosuid,size=32m','--tmpfs','/out:rw,noexec,nosuid,size=32m',
          '--tmpfs','/app:ro,noexec,nosuid,size=1m',
          '--mount','type=bind,src=/usr/bin/python3.12,dst=/usr/bin/python3.12,readonly',
          '--mount','type=bind,src=/usr/lib,dst=/usr/lib,readonly',
          '--mount','type=bind,src=/usr/lib64,dst=/usr/lib64,readonly',
          '--mount','type=bind,src=/opt/amrag/.venv/lib/python3.12/site-packages,dst=/runtime-deps,readonly',
          '--mount','type=bind,src='+str(job/'code')+',dst=/code,readonly',
          '--mount','type=bind,src='+str(directory)+',dst=/input,readonly',
          '--entrypoint','/usr/bin/env',REQ['image'],'-i','PATH=/usr/bin:/bin',
          'PYTHONPATH=/code/backend:/code/scripts:/runtime-deps','PYTHONDONTWRITEBYTECODE=1',
          'PYTHONNOUSERSITE=1','DATABASE_URL=sqlite://','LANGFUSE_ENABLED=false',
          'RAG_OBSERVABILITY_ENABLED=false','RAG_MODEL_ROUTER_MODE=OFF','RAG_SNAPSHOTS_ENABLED=false',
          'python3.12','-B','-c',launcher]
    started=time.monotonic()
    proc=subprocess.Popen(args,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    selector=selectors.DefaultSelector();selector.register(proc.stdout,selectors.EVENT_READ)
    output=bytearray();reason=None
    while selector.get_map():
        if time.monotonic()-started>REQ['timeout']:
            reason='TIMEOUT';break
        for key,_ in selector.select(0.2):
            chunk=os.read(key.fileobj.fileno(),65536)
            if not chunk:selector.unregister(key.fileobj)
            else:output.extend(chunk)
        if len(output)>4194304:reason='OUTPUT_LIMIT';break
    if reason:
        subprocess.run(['docker','kill',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
        proc.kill()
    proc.wait(timeout=10)
    # --rm removes the whole isolated process tree, including any descendants.
    # Nonzero `docker inspect` can mean daemon failure, not just missing object.
    # A successful authoritative inventory with no matching ID proves removal.
    inventory=subprocess.run(['docker','ps','-aq','--filter','name=^/'+name+'$'],capture_output=True,text=True,timeout=10)
    if inventory.returncode:raise ValueError('container state unknown; Docker inventory unavailable')
    exists=bool(inventory.stdout.strip())
    if exists:raise ValueError('capsule process tree was not removed')
    reply({'exit_code':proc.returncode,'reason':reason,'output_base64':base64.b64encode(output[:4194304]).decode(),
           'elapsed_seconds':time.monotonic()-started,'container_removed':not exists,'container_name':name})
else:raise ValueError('unsupported capsule operation')
'''


def source_archive(business_root):
    archive = subprocess.check_output(['git', '-C', str(business_root), 'archive', BASE_COMMIT, 'backend/app'])
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as target:
        with tarfile.open(fileobj=io.BytesIO(archive)) as original:
            for member in original.getmembers():
                target.addfile(member, original.extractfile(member) if member.isfile() else None)
        for name in ('rsi_delivery_worker.py', 'replay_source_preparation.py'):
            data = (ROOT / 'scripts' / name).read_bytes()
            info = tarfile.TarInfo('scripts/' + name)
            info.size, info.mode = len(data), 0o444
            target.addfile(info, io.BytesIO(data))
    data = stream.getvalue()
    return data


class Capsule:
    def __init__(self, host, identity, business_root, evidence_root, *, image=IMAGE):
        if not re.fullmatch(r'[A-Za-z0-9_.@:-]+', host):
            raise ValueError('invalid SSH host')
        if image != IMAGE:
            raise ValueError('runtime image must be explicitly audited')
        self.host, self.identity = host, Path(identity)
        self.business_root, self.evidence_root = Path(business_root), Path(evidence_root)
        self.image, self.job, self.sequence = image, uuid.uuid4().hex, 0
        self.receipts, self.ready = [], False
        self.deadline = None
        self.transport = None
        self.responses = queue.Queue()
        self.transport_lock = threading.Lock()

    def start_transport(self):
        self.evidence_root.mkdir(parents=True, exist_ok=True)
        self.transport_log = (self.evidence_root / ('ssh-' + self.job + '.log')).open('xb')
        server = '''import json,sys,traceback
for line in sys.stdin:
    try:
        command=json.loads(line)
        exec(compile(command['program'],'<trusted-capsule>','exec'),{'REQ':command['request']})
    except Exception as error:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({'rpc_error':type(error).__name__,'message':str(error)}),flush=True)
    sys.stdout.flush()
'''
        self.transport = subprocess.Popen(
            ['ssh', '-i', str(self.identity), '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
             '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', self.host,
             'python3 -u -c ' + shlex.quote(server)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=self.transport_log, text=True, encoding='utf-8', bufsize=1)
        def read_responses():
            try:
                while True:
                    line = self.transport.stdout.readline(8 * 1024 * 1024)
                    if not line:
                        self.responses.put({'transport_error': 'EOF'})
                        return
                    self.responses.put(json.loads(line))
            except Exception as error:
                self.responses.put({'transport_error': type(error).__name__})
        self.reader = threading.Thread(target=read_responses, daemon=True)
        self.reader.start()

    def rpc(self, request, *, timeout=115):
        payload = {'job': self.job, **request}
        with self.transport_lock:
            if self.transport is None:
                self.start_transport()
            if self.transport.poll() is not None:
                raise RuntimeError('INFRA_FAILURE: SSH transport is terminal; no blind replay')
            self.transport.stdin.write(json.dumps({'program': REMOTE_PROGRAM, 'request': payload}) + '\n')
            self.transport.stdin.flush()
            try:
                response = self.responses.get(timeout=timeout)
            except queue.Empty:
                self.transport.kill()
                self.transport.wait(timeout=5)
                raise RuntimeError('RECOVERY_ERROR: capsule response unknown; inspect live container before retry')
            if 'transport_error' in response or 'rpc_error' in response:
                raise RuntimeError('INFRA_FAILURE: capsule RPC failed; restricted transport log retained')
            return response

    def setup(self):
        subprocess.run(['git', '-C', str(self.business_root), 'diff', '--quiet', BASE_COMMIT,
                        '--', 'backend/app'], check=True)
        data = source_archive(self.business_root)
        receipt = self.rpc({'op': 'setup', 'tar': base64.b64encode(data).decode(),
                            'tar_sha256': hashlib.sha256(data).hexdigest()})
        self.ready = receipt.get('status') == 'READY'
        if receipt.get('code_archive_sha256') != hashlib.sha256(data).hexdigest():
            raise ValueError('EXECUTION_ARCHIVE_MISMATCH')
        self.code_archive_sha256 = receipt['code_archive_sha256']
        self.receipts.append({'operation': 'setup', 'job': self.job, 'code_bytes': len(data), **receipt})
        return receipt

    @property
    def evidence_policy(self):
        from .execution_evidence import ExecutionEvidence
        return ExecutionEvidence.real(self.business_root)

    def child(self, stage, paths, cwd, *, worker=None, helper_dir=None, business_root=None):
        if not self.ready or stage not in ('prepare', 'deliver'):
            raise ValueError('capsule not ready or unsupported worker stage')
        if business_root and Path(business_root).resolve() != self.business_root.resolve():
            raise ValueError('business source mismatch')
        self.sequence += 1
        worker = Path(worker) if worker else ROOT / 'scripts/rsi_delivery_worker.py'
        files = {'worker.py': worker.read_bytes(), 'input.json': Path(paths[0]).read_bytes()}
        if stage == 'deliver':
            files['config.json'] = Path(paths[1]).read_bytes()
        remaining = 90 if self.deadline is None else min(90, self.deadline - time.monotonic())
        if remaining <= 0:
            raise ValueError('INFRA_FAILURE: bundle deadline expired before starting another worker')
        response = self.rpc({'op': 'run', 'image': self.image, 'step': str(self.sequence), 'stage': stage,
                             'files': {name: base64.b64encode(data).decode() for name, data in files.items()},
                             'timeout': remaining}, timeout=remaining + 20)
        output = base64.b64decode(response.pop('output_base64'), validate=True)
        lines = output.splitlines()
        captures = [line.partition(b'=')[2] for line in lines if line.startswith(b'RSI_CAPTURE_BASE64=')]
        runtimes = [line.partition(b'=')[2] for line in lines if line.startswith(b'RSI_RUNTIME_BASE64=')]
        if len(runtimes) == 1:
            response['worker_runtime'] = json.loads(base64.b64decode(runtimes[0], validate=True))
        with (Path(cwd) / 'worker.log').open('xb') as log:
            log.write(b'\n'.join(line for line in lines if not line.startswith((b'RSI_CAPTURE_BASE64=', b'RSI_RUNTIME_BASE64='))))
        self.receipts.append({'operation': stage, 'sequence': self.sequence, **response})
        data = base64.b64decode(captures[0], validate=True) if len(captures) == 1 else None
        with (Path(cwd) / 'execution-receipt.json').open('x', encoding='utf-8') as stream:
            json.dump({'schema': 'rsi-execution-evidence/v2.3', 'stage': stage,
                       'execution_adapter_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       'source_base_commit': BASE_COMMIT,
                       'capture_sha256': hashlib.sha256(data).hexdigest() if data is not None else None, 'worker_sha256': hashlib.sha256(files['worker.py']).hexdigest(),
                       'input_sha256': hashlib.sha256(files['input.json']).hexdigest(),
                       'config_sha256': hashlib.sha256(files['config.json']).hexdigest() if 'config.json' in files else None,
                       'code_archive_sha256': self.code_archive_sha256, 'image': self.image,
                       **response}, stream, sort_keys=True)
        if response['exit_code'] or response['reason'] or len(captures) != 1 or len(runtimes) != 1:
            raise ValueError('INFRA_FAILURE: isolated worker incomplete; see worker.log')
        data = base64.b64decode(captures[0], validate=True)
        value = json.loads(data.decode('utf-8-sig'))
        with Path(paths[-1]).open('xb') as destination:
            destination.write(data)
        return value

    @contextmanager
    def bind(self, executor):
        original = executor.child
        executor.child = self.child
        try:
            yield self
        finally:
            executor.child = original

    def close(self):
        try:
            if self.ready:
                self.receipts.append({'operation': 'cleanup', **self.rpc({'op': 'cleanup'})})
                self.ready = False
        finally:
            if self.transport is not None:
                if self.transport.poll() is None:
                    self.transport.stdin.close()
                    try:
                        self.transport.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.transport.kill()
                        self.transport.wait(timeout=5)
                self.reader.join(timeout=1)
                self.transport_log.close()
            self.evidence_root.mkdir(parents=True, exist_ok=True)
            path = self.evidence_root / ('capsule-' + self.job + '.json')
            with path.open('x', encoding='utf-8') as stream:
                json.dump({'image': self.image, 'job': self.job, 'receipts': self.receipts,
                           'production_mounts': [], 'network': 'none', 'user': '65534:65534',
                           'note': 'Reused existing image, system libraries and read-only venv; no dependency copy.'}, stream, indent=2)

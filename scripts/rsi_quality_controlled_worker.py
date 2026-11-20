"""F2 offline boundary around the unchanged, approved product-quality worker.

Derived from backend/tests/test_rsi_quality_worker.py::run_offline_product.
Only provider responses are synthetic; product SQLite, BM25, HTTP/SSE and prompt
serialization execute normally. This is wiring evidence, never quality evidence.
"""
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading

APPROVED_COMMIT = 'e242e91a082fe0e28242f172c81bfb25a540dfda'
EVIDENCE_MODE = 'OFFLINE_PROVIDER_STUB'
ENDPOINT = 'https://api.siliconflow.cn/v1/chat/completions'


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value).hexdigest()


def verify_approved_source(root):
    """Compare actual inventory and contents to exact approved Git blobs, not HEAD."""
    root = Path(root).resolve(strict=True)
    listing = subprocess.check_output(['git', '-C', str(root), 'ls-tree', '-r',
                                      '--name-only', APPROVED_COMMIT], text=True)
    expected = sorted(name for name in listing.splitlines()
                      if (name.startswith('backend/app/') and name.endswith('.py'))
                      or name == 'scripts/rsi_quality_worker.py')
    actual = sorted([p.relative_to(root).as_posix()
                     for p in (root / 'backend/app').rglob('*.py')]
                    + ['scripts/rsi_quality_worker.py'])
    if not expected or actual != expected:
        raise ValueError('Approved product Python inventory mismatch')
    queries = ''.join(APPROVED_COMMIT + ':' + name + '\n' for name in expected).encode()
    batch = subprocess.run(['git', '-C', str(root), 'cat-file', '--batch'],
                           input=queries, capture_output=True, check=True).stdout
    cursor, hashes = 0, {}
    for name in expected:
        path = root / name
        if not path.resolve(strict=True).is_relative_to(root) or path.is_symlink():
            raise ValueError('Product source escapes approved root')
        header_end = batch.index(b'\n', cursor)
        header = batch[cursor:header_end].split()
        if len(header) != 3 or header[1] != b'blob':
            raise ValueError('Approved Git blob missing: ' + name)
        size = int(header[2])
        blob = batch[header_end + 1:header_end + 1 + size]
        cursor = header_end + 1 + size + 1
        content = path.read_bytes().replace(b'\r\n', b'\n')
        if content != blob.replace(b'\r\n', b'\n'):
            raise ValueError('Approved product source mismatch: ' + name)
        hashes[name] = digest(content)
    return {'approved_commit': APPROVED_COMMIT, 'files': hashes,
            'inventory_sha256': digest(canonical(hashes))}


def install_guards(root, output):
    """Deny external sockets and formal-checkout writes before importing product."""
    root, output = Path(root).resolve(), Path(output).resolve()
    allowed_ports = set()
    internal = threading.local()
    original_socketpair = socket.socketpair

    def socketpair(*args, **kwargs):
        # Windows implements asyncio's self-pipe with an ephemeral loopback pair.
        internal.socketpair = True
        try:
            return original_socketpair(*args, **kwargs)
        finally:
            internal.socketpair = False

    socket.socketpair = socketpair

    def audit(event, args):
        if event in ('socket.sendto', 'socket.sendmsg'):
            raise RuntimeError('F2 denied datagram network operation')
        if event in ('socket.gethostbyname', 'socket.gethostbyaddr'):
            if args[0] not in ('127.0.0.1', '::1', 'localhost'):
                raise RuntimeError('F2 denied external name resolution')
        if event in ('socket.connect', 'socket.getaddrinfo'):
            address = args[1] if event == 'socket.connect' else args
            host, port = address[:2]
            if host not in ('127.0.0.1', '::1', 'localhost') or (port not in allowed_ports
                    and not getattr(internal, 'socketpair', False)):
                raise RuntimeError('F2 denied network destination')
        if event == 'open' and isinstance(args[0], (str, bytes, os.PathLike)):
            path = Path(os.fsdecode(args[0])).resolve()
            flags = args[2] or 0
            writing = flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
            if writing and not path.is_relative_to(output):
                raise RuntimeError('F2 denied write outside isolated output')
            if path.is_relative_to(root) and not path.is_relative_to(root / '.venv'):
                relative = path.relative_to(root).as_posix()
                if not (relative.startswith('backend/app/') and path.suffix in ('.py', '.pyc')) and relative != 'scripts/rsi_quality_worker.py':
                    raise RuntimeError('F2 denied non-code formal checkout read')
        if event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.rename'):
            for value in args[:2] if event == 'os.rename' else args[:1]:
                if isinstance(value, (str, bytes, os.PathLike)):
                    path = Path(os.fsdecode(value)).resolve()
                    if not path.is_relative_to(output):
                        raise RuntimeError('F2 denied mutation outside isolated output')

    sys.addaudithook(audit)
    return allowed_ports


def run(root, job_path, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    approved = verify_approved_source(root)
    # Install guards before even loading the unchanged worker or product app.
    output.mkdir(parents=True, exist_ok=False)
    os.environ['RAG_MODEL_ROUTER_ENV_FILE'] = str(output / 'no-router-environment.env')
    ports = install_guards(root, output)
    import httpx

    original_send = httpx.Client.send
    intercepted = []

    def send(client, request, *args, **kwargs):
        if str(request.url) == ENDPOINT and request.method == 'POST':
            payload = json.loads(request.content)
            intercepted.append({'actual_request_sha256': digest(request.content),
                                'messages_sha256': digest(canonical(payload['messages']))})
            # No usage: provider simulation must never invent measured token cost.
            return httpx.Response(200, request=request, json={
                'id': 'f2-offline-stub', 'object': 'chat.completion',
                'model': payload['model'], 'choices': [{'index': 0,
                    'message': {'role': 'assistant', 'content': 'Offline provider stub [1].'},
                    'finish_reason': 'stop'}]})
        if request.url.host not in ('127.0.0.1', '::1', 'localhost') or request.url.port not in ports:
            raise RuntimeError('F2 denied HTTP destination')
        return original_send(client, request, *args, **kwargs)

    httpx.Client.send = send

    class VerifiedSourceLoader(importlib.machinery.SourceFileLoader):
        def get_code(self, fullname):
            # Ignore existing bytecode: only exact verified Python is executable.
            path = Path(self.path).resolve(strict=True)
            content = path.read_bytes().replace(b'\r\n', b'\n')
            name = path.relative_to(root).as_posix()
            if approved['files'].get(name) != digest(content):
                raise ValueError('Source changed before import: ' + name)
            return compile(content, self.path, 'exec')

    class ApplicationFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname != 'app' and not fullname.startswith('app.'):
                return None
            source = root / 'backend' / Path(*fullname.split('.'))
            package, module = source / '__init__.py', source.with_suffix('.py')
            if package.is_file():
                return importlib.util.spec_from_file_location(fullname, package,
                    loader=VerifiedSourceLoader(fullname, str(package)),
                    submodule_search_locations=[str(package.parent)])
            if module.is_file():
                return importlib.util.spec_from_file_location(fullname, module,
                    loader=VerifiedSourceLoader(fullname, str(module)))
            raise ModuleNotFoundError('Missing approved application source: ' + fullname)

    sys.meta_path.insert(0, ApplicationFinder())
    worker_path = root / 'scripts/rsi_quality_worker.py'
    spec = importlib.util.spec_from_file_location('f2_approved_product_worker', worker_path,
        loader=VerifiedSourceLoader('f2_approved_product_worker', str(worker_path)))
    worker = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(worker)
    original_start = worker.RecordingProxy.start

    def start(proxy):
        port = original_start(proxy)
        ports.add(port)
        return port

    worker.RecordingProxy.start = start
    observations = []
    original_build = worker.build_product

    def build(job, path, context):
        # Installed on the actual consumer class BEFORE build_product creates its
        # instance-level recording wrapper. Preserve its signature and result.
        from app.services.retrieval import RetrievalService
        original_search = RetrievalService.search
        signature = inspect.signature(original_search)

        async def search(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            hits, metrics = await original_search(*args, **kwargs)
            config = {key: bound.arguments[key] for key in (
                'search_mode', 'top_k', 'score_threshold', 'vector_weight', 'bm25_weight')}
            observations.append({'case_id': context['case']['id'],
                'observed_top_k': config['top_k'], 'effective_config': config,
                'effective_config_sha256': digest(canonical(config)),
                'returned_ids': [hit['id'] for hit in hits]})
            return hits, metrics

        RetrievalService.search = search
        return original_build(job, path, context)

    worker.build_product = build
    code = worker.main(['--input', str(job_path), '--output', str(output / 'product'), '--live'])
    receipt = {'evidence_mode': EVIDENCE_MODE, 'actual_external_calls': 0,
               'provider_stub_calls': intercepted, 'retrieval_observations': observations,
               'approved_source': approved,
               'runtime_identity': {'python': sys.version.split()[0],
                   'httpx': httpx.__version__,
                   'fastapi': getattr(sys.modules.get('fastapi'), '__version__', None),
                   'sqlmodel': getattr(sys.modules.get('sqlmodel'), '__version__', None)}}
    (output / 'controlled-receipt.json').write_bytes(canonical(receipt))
    return code


if __name__ == '__main__':
    raise SystemExit(run(*sys.argv[1:]))

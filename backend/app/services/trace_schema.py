"""E1.1 content permissions and immutable snapshots; no model or SDK dependency."""
import contextvars
import hashlib
import hmac
import json
import os
import re
import stat
import time
from contextlib import contextmanager

TRACE_SCHEMA_VERSION = 'rag-trace-v1'
CONTENT_ROLES = frozenset({'original_chunk', 'rerank_sent', 'context_sent',
                           'provider_request', 'provider_response', 'final_answer'})
_context = contextvars.ContextVar('rag_capture_context', default=None)
_export_key = os.urandom(32)
_HASH = re.compile(r'^[0-9a-f]{64}$')
_SECRET = re.compile(r'(?i)(authorization\s*[:=]|bearer\s+|(?:api[_-]?key|secret[_-]?key|password)\s*[:=]|sk-(?:lf-|proj-)?[a-z0-9]{12,}|://[^/\s:@]+:[^/\s@]+@|<(?:think|thinking|analysis|reasoning)>|(?:^|\n)\s*(?:thinking|analysis|reasoning)\s*:)')
_FORBIDDEN_KEYS = frozenset({'reasoning_content', 'thinking', 'reasoning', 'api_key',
                            'authorization', 'secret_key', 'password'})


def _forbidden(value):
    if isinstance(value, str):
        return bool(_SECRET.search(value))
    if isinstance(value, dict):
        return any(str(k).lower() in _FORBIDDEN_KEYS or _forbidden(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return any(_forbidden(v) for v in value)
    return False


def content_bytes(value):
    return (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False,
            sort_keys=True, separators=(',', ':'))).encode('utf-8')


def capture_identity():
    context = _context.get() or {}
    return {k: context.get(k) for k in ('run_id', 'attempt_id', 'qid', 'dataset_version')}


def capture_settings():
    context = _context.get() or {}
    return {'capture_mode': context.get('capture_mode', 'BASIC'),
            'debug_details': bool(context.get('debug_details', False))}


@contextmanager
def capture_scope(*, run_id=None, attempt_id=None, qid=None, dataset_version=None,
                  source_sha256s=(), manifest=None, capture_mode='BASIC', debug_details=False):
    """Trusted runner entry; not populated from arbitrary HTTP request fields.

    Authorization requires exact dataset/qid and every contributing source hash.
    Empty source sets do not authorize an entire prompt by accident.
    """
    sources = frozenset(source_sha256s)
    allowed = False
    if capture_mode == 'EVAL' and sources and all(isinstance(s, str) and _HASH.fullmatch(s) for s in sources):
        try:
            entries = manifest['entries']
            approved = {e['source_sha256'] for e in entries
                        if e['dataset_version'] == dataset_version and e['qid'] == qid
                        and e.get('cloud_allowed') is True}
            allowed = sources.issubset(approved)
        except (KeyError, TypeError):
            allowed = False
    context = dict(run_id=run_id, attempt_id=attempt_id, qid=qid,
                   dataset_version=dataset_version, source_sha256s=sorted(sources),
                   capture_mode='EVAL' if allowed else 'BASIC',
                   debug_details=bool(debug_details and allowed), inline_hashes=set())
    token = _context.set(context)
    try:
        yield context
    finally:
        _context.reset(token)


@contextmanager
def _private_root_fd(root, *, create=False):
    """Walk each component from / with no-follow descriptors; verify the final directory."""
    if os.name != 'posix' or not all(hasattr(os, name) for name in ('O_DIRECTORY', 'O_NOFOLLOW')):
        raise PermissionError('POSIX private snapshot storage required; Windows ACL not verified')
    path = os.path.abspath(os.fspath(root))
    parts = [part for part in path.split(os.sep) if part]
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(os.sep, flags)
    try:
        for part in parts:
            try:
                next_fd = os.open(part, flags, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise PermissionError('snapshot directory must be owned and mode 0700')
        yield fd
    finally:
        os.close(fd)


def _read_snapshot_fd(ref, root_fd, *, max_bytes, max_age_seconds):
    fd = os.open(ref + '.txt', os.O_RDONLY | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0), dir_fd=root_fd)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
            raise PermissionError('snapshot must be owned and mode 0600')
        if time.time() - info.st_mtime > max_age_seconds:
            raise ValueError('snapshot retention expired')
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes or hashlib.sha256(raw).hexdigest() != ref:
        raise ValueError('snapshot corrupt or too large')
    return raw


def save_snapshot(raw, *, root, max_bytes=1_000_000, capacity_bytes=50_000_000):
    """Store exact bytes under a verified directory descriptor, serialized for capacity."""
    if len(raw) > max_bytes:
        raise ValueError('snapshot payload exceeds capacity')
    digest = hashlib.sha256(raw).hexdigest()
    with _private_root_fd(root, create=True) as root_fd:
        import fcntl
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        try:
            try:
                existing = _read_snapshot_fd(digest, root_fd, max_bytes=max_bytes, max_age_seconds=7 * 86400)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing != raw:
                    raise ValueError('snapshot hash collision or corruption')
                return digest
            with os.scandir(root_fd) as entries:
                used = sum(entry.stat(follow_symlinks=False).st_size for entry in entries
                           if entry.is_file(follow_symlinks=False))
            if used + len(raw) > capacity_bytes:
                raise ValueError('snapshot store exceeds capacity')
            fd = os.open(digest + '.txt', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=root_fd)
            with os.fdopen(fd, 'wb') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                    raise PermissionError('snapshot must be owned and mode 0600')
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            return digest
        finally:
            fcntl.flock(root_fd, fcntl.LOCK_UN)


def read_snapshot(ref, *, root, max_bytes=1_000_000, max_age_seconds=7 * 86400):
    if not isinstance(ref, str) or not _HASH.fullmatch(ref):
        raise ValueError('snapshot reference must be a SHA256')
    with _private_root_fd(root) as root_fd:
        return _read_snapshot_fd(ref, root_fd, max_bytes=max_bytes, max_age_seconds=max_age_seconds)


def capture_payload(value, *, content_role, root=None, save_local=False,
                    source_sha256=None, index_fingerprint=None, observation_id=None):
    if content_role not in CONTENT_ROLES:
        raise ValueError('unsupported content role')
    raw = content_bytes(value)
    text = raw.decode('utf-8')
    digest = hashlib.sha256(raw).hexdigest()
    context = _context.get() or {}
    result = {**capture_identity(), 'trace_schema_version': ('rag-trace-v1.2' if os.getenv('RAG_OBSERVABILITY_ENABLED', 'false').lower() == 'true' and os.getenv('RAG_TRACE_SCHEMA', 'e11').lower() == 'e12' else TRACE_SCHEMA_VERSION),
              'capture_mode': context.get('capture_mode', 'BASIC'),
              'content_role': content_role, 'captured_at': time.time(),
              'content_sha256': digest, 'sha256': digest, 'chars': len(text),
              'source_sha256': source_sha256, 'index_fingerprint': index_fingerprint,
              'observation_id': observation_id, 'snapshot_ref': None,
              'content_visibility': 'NOT_CAPTURED'}
    # Secrets never enter a snapshot, including LOCAL_ONLY snapshots.
    if _forbidden(value) or _SECRET.search(text):
        result['content_visibility'] = 'REDACTED'
        return result
    if save_local and root:
        try:
            result['snapshot_ref'] = save_snapshot(raw, root=root)
            result['content_visibility'] = 'LOCAL_ONLY'
        except (OSError, ValueError):
            pass
    if context.get('capture_mode') == 'EVAL':
        result['content_visibility'] = 'CLOUD_INLINE'
        result['text'] = text
        result['_capture_authorization'] = _authorization(result)
    return result


def _authorization(payload):
    fields = {k: payload.get(k) for k in ('content_sha256', 'content_role', 'run_id',
              'attempt_id', 'qid', 'dataset_version', 'captured_at', 'content_visibility')}
    return hmac.new(_export_key, content_bytes(fields), hashlib.sha256).hexdigest()


def authorized_inline(payload):
    """Validate before background SDK export even after request scope has ended."""
    if not isinstance(payload, dict) or payload.get('content_visibility') != 'CLOUD_INLINE':
        return False
    text = payload.get('text')
    if not isinstance(text, str) or _SECRET.search(text):
        return False
    digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
    token = payload.get('_capture_authorization')
    return (digest == payload.get('content_sha256') and isinstance(token, str)
            and hmac.compare_digest(token, _authorization(payload)))

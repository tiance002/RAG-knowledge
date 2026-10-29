"""Opt-in, fail-open observations; never export document or prompt contents."""
import contextvars
import functools
import hashlib
import inspect
import json
import os
import time
import uuid
from app.services.metrics import record_provider_usage
from contextlib import contextmanager
from pathlib import Path
from threading import Lock

_current = contextvars.ContextVar('rag_observation', default=None)
_lock = Lock()
_client = None
_attempted = False


def enabled():
    return os.getenv('RAG_OBSERVABILITY_ENABLED', 'false').lower() == 'true'


def fingerprint(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def sanitize(value, key=''):
    """Fail closed on arbitrary strings; explicit numeric/ID metadata only."""
    if any(s in key.lower() for s in ('secret', 'api_key', 'password', 'authorization')):
        return '[REDACTED]'
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): sanitize(v, str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, key) for v in value]
    if key == 'search_mode' and value in ('vector', 'fulltext', 'hybrid'):
        return value
    if key == 'answer_mode' and value in ('GROUNDED_GENERATION', 'EXTRACTIVE_FALLBACK', 'INSUFFICIENT_EVIDENCE', 'RETRIEVAL_ERROR', 'UNKNOWN'):
        return value
    if key == 'validation_status' and value in ('VERIFIED', 'UNKNOWN', 'UNSUPPORTED', 'INVALID', 'NOT_RUN'):
        return value
    if key == 'generation_status' and value in ('NOT_RUN', 'TRUNCATED', 'EMPTY', 'COMPLETED', 'UNKNOWN'):
        return value
    safe = ('id', 'qid', 'sha256', 'hash', 'version', 'status', 'outcome', 'mode', 'type', 'reason', 'stage', 'name', 'model', 'finish_reason', 'verdict', 'method', 'format', 'role')
    if key in safe or key.endswith(('_id', '_ids', '_sha256', '_hash', '_version')):
        # Names supplied by instrumentation are controlled, identifiers are opaque.
        return str(value)[:256]
    return {'sha256': fingerprint(value), 'chars': len(str(value))}


def snapshot(value):
    """Raw snapshots stay local and require explicit opt-in; never put raw text in spans."""
    text = str(value)
    ref = {'sha256': fingerprint(text), 'chars': len(text), 'snapshot_id': None}
    if enabled() and os.getenv('RAG_SNAPSHOTS_ENABLED', 'false').lower() == 'true':
        try:
            root = Path(os.getenv('RAG_SNAPSHOT_DIR', './storage/observation_snapshots'))
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            path = root / (ref['sha256'] + '.txt')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(text)
            ref['snapshot_id'] = ref['sha256']
        except FileExistsError:
            ref['snapshot_id'] = ref['sha256']
        except Exception:
            pass
    return ref


def _sdk():
    global _client, _attempted
    if _attempted:
        return _client
    with _lock:
        if _attempted:
            return _client
        _attempted = True
        if os.getenv('LANGFUSE_ENABLED', 'false').lower() != 'true':
            return None
        if not all(os.getenv(k) for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY')):
            return None
        try:
            from langfuse import Langfuse
            from opentelemetry.sdk.trace import TracerProvider
            _client = Langfuse(base_url=os.getenv('LANGFUSE_BASE_URL', 'https://cloud.langfuse.com'),
                               tracer_provider=TracerProvider(), timeout=2,
                               mask=lambda **kw: sanitize(kw.get('data')))
        except Exception:
            _client = None
    return _client


def _record(row):
    try:
        path = Path(os.getenv('RAG_OBSERVATION_LOG', './storage/observations/events.jsonl'))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _lock, path.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    except Exception:
        pass


def flush_observations():
    """Flush an already initialized client without affecting application shutdown."""
    try:
        if _client is not None:
            _client.flush()
    except Exception:
        pass


@contextmanager
def span(name, **metadata):
    if not enabled():
        yield None
        return
    parent = _current.get()
    state = {'trace_id': parent['trace_id'] if parent else uuid.uuid4().hex,
             'span_id': uuid.uuid4().hex[:16], 'parent_id': parent['span_id'] if parent else None}
    event_id = uuid.uuid4().hex
    try:
        data = {**sanitize(metadata), 'event_id': event_id}
    except Exception:
        data = {'event_id': event_id}
    token = _current.set(state)
    start = time.monotonic()
    manager = None
    observation = None
    try:
        client = _sdk()
        if client:
            try:
                kind = 'generation' if name.startswith('llm.') else 'agent' if name.startswith('agent.') else 'span'
                manager = client.start_as_current_observation(name=name, as_type=kind, metadata=data)
                observation = manager.__enter__()
                state['trace_id'] = client.get_current_trace_id() or state['trace_id']
                state['span_id'] = client.get_current_observation_id() or state['span_id']
            except Exception:
                manager = None
        _record({**state, 'event_id': event_id, 'stage': name, 'event': 'start', 'time': time.time(), 'metadata': data})
        yield state
    except BaseException as exc:
        if observation:
            try:
                observation.update(level='ERROR', status_message=type(exc).__name__)
            except Exception:
                pass
        event('failure', status='CANCELLED' if isinstance(exc, GeneratorExit) else 'FAILED',
              error_type=type(exc).__name__)
        raise
    finally:
        _record({**state, 'event_id': event_id, 'stage': name, 'event': 'end', 'latency_ms': (time.monotonic()-start)*1000})
        if manager:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        _current.reset(token)


def event(name, **metadata):
    if not enabled():
        return
    try:
        state = _current.get()
        if state is None:
            with span(name, **metadata):
                pass
            return
        event_id = uuid.uuid4().hex
        data = {**sanitize(metadata), 'event_id': event_id}
        _record({**state, 'event_id': event_id, 'stage': name, 'event': 'stage', 'time': time.time(), 'metadata': data})
        client = _sdk()
        if client:
            extra = {}
            if name == 'llm.response':
                usage = metadata.get('usage') or {}
                extra = {'as_type': 'generation', 'model': metadata.get('model'),
                         'usage_details': {k: v for k, v in usage.items() if isinstance(v, int)}}
            with client.start_as_current_observation(name=name, metadata=data, **extra):
                pass
    except Exception:
        pass


def candidates(name, rows, **metadata):
    if not enabled():
        return
    try:
        evidence = []
        for rank, row in enumerate(rows, 1):
            evidence.append({k: v for k, v in {
                'rank': rank, 'id': row.get('id', row.get('chunk_id')),
                'file_id': row.get('file_id'), 'page_number': row.get('page_number'),
                'score': row.get('score'), 'vector_score': row.get('vector_score'),
                'bm25_score': row.get('bm25_score'), 'rerank_score': row.get('rerank_score'),
                'parent_id': row.get('parent_id'), 'source_metadata': row.get('source_metadata'),
                'content_sha256': fingerprint(row.get('content', '')),
            }.items()})
        event(name, candidates=evidence, **metadata)
    except Exception:
        pass


def observe(name):
    def decorator(fn):
        if inspect.isasyncgenfunction(fn):
            @functools.wraps(fn)
            async def stream(*args, **kwargs):
                with span(name):
                    async for item in fn(*args, **kwargs):
                        yield item
            return stream
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_call(*args, **kwargs):
                with span(name):
                    return await fn(*args, **kwargs)
            return async_call
        @functools.wraps(fn)
        def sync_call(*args, **kwargs):
            with span(name):
                return fn(*args, **kwargs)
        return sync_call
    return decorator


def model_response(response, *, role='generation', model=None):
    try:
        usage = getattr(response, 'usage', None)
        usage = usage.model_dump() if hasattr(usage, 'model_dump') else usage
        usage = usage or getattr(response, 'usage_metadata', None)
        record_provider_usage(role, usage)
        if not enabled():
            return
        metadata = getattr(response, 'response_metadata', {}) or {}
        choices = getattr(response, 'choices', None)
        finish = choices[0].finish_reason if choices else metadata.get('finish_reason')
        event('llm.response', role=role, model=getattr(response, 'model', None) or model,
              finish_reason=finish, usage=usage, cost=None,
              status='TRUNCATED' if finish == 'length' else 'RETURNED')
    except Exception:
        pass

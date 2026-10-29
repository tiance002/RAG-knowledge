"""Opt-in, fail-open observations; never export document or prompt contents."""
import contextvars
import asyncio
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
from functools import lru_cache
from app.services.trace_schema import capture_payload, authorized_inline, capture_identity, capture_settings

_current = contextvars.ContextVar('rag_observation', default=None)
_active_observation = contextvars.ContextVar('rag_sdk_observation', default=None)
_raw_answer_hash = contextvars.ContextVar('rag_raw_answer_hash', default=None)
_qa_context = contextvars.ContextVar('rag_qa_metadata', default=None)
retrieval_attempt = contextvars.ContextVar('rag_retrieval_attempt', default=None)
_lock = Lock()
_client = None
_attempted = False
_sdk_init_error = None


def enabled():
    return os.getenv('RAG_OBSERVABILITY_ENABLED', 'false').lower() == 'true'


def fingerprint(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def runtime_identity():
    context, state = capture_identity(), _current.get() or {}
    return {k: context.get(k) or state.get(k) for k in ('run_id', 'attempt_id', 'qid', 'dataset_version')}


def _reset_context(variable, token, fallback):
    try:
        variable.reset(token)
    except ValueError:
        # Async generators can be closed/resumed by a task with a copied context.
        variable.set(fallback)


@lru_cache(maxsize=1)
def code_fingerprint():
    """Hash the immutable application source snapshot, not a guessed Git revision."""
    try:
        root = Path(__file__).resolve().parents[1]
        h = hashlib.sha256()
        for path in sorted(root.rglob('*.py')):
            h.update(path.relative_to(root).as_posix().encode() + b'\0' + hashlib.sha256(path.read_bytes()).digest())
        return h.hexdigest()
    except OSError:
        return None


def _root_metadata_update(fields=None, config=None):
    root = _qa_context.get()
    if root is None:
        return None
    with _lock:
        root['metadata'].update(fields or {})
        root['metadata'].setdefault('effective_config', {}).update(config or {})
        result = dict(root['metadata'], effective_config=dict(root['metadata']['effective_config']))
    return result


def _observe_root_resolution(name, metadata, io):
    """Attach observed resolution facts to the same QA root, without changing behavior."""
    root = _qa_context.get()
    if root is None:
        return
    fields, config = {}, {}
    if name == 'qa.config':
        config = {k: metadata[k] for k in ('search_mode', 'top_k', 'score_threshold', 'vector_weight', 'bm25_weight',
            'rerank_enabled', 'rerank_model_id', 'rerank_top_k', 'rerank_score_threshold', 'context_window', 'use_rewrite') if k in metadata}
        fields = {'mode': metadata.get('mode'), 'kb_scope': metadata.get('kb_ids', [])}
        if metadata.get('search_mode') == 'fulltext':
            config.update(embedding_model='NOT_APPLICABLE', score_threshold='NOT_APPLICABLE',
                          vector_weight='NOT_APPLICABLE', bm25_weight='NOT_APPLICABLE')
        if not metadata.get('rerank_enabled'):
            config.update(rerank_model_id='NOT_APPLICABLE', rerank_top_k='NOT_APPLICABLE', rerank_score_threshold='NOT_APPLICABLE')
    elif name == 'retrieval.metrics':
        config = {k: metadata[k] for k in ('search_mode', 'top_k') if k in metadata}
        fields['retrieval_metrics'] = metadata.get('metrics')
    elif name in ('grounded.request.sent', 'llm.request'):
        config = {dest: metadata[key] for key, dest in [('model', 'chat_model'), ('max_tokens', 'max_tokens'),
                                                      ('temperature', 'temperature')] if key in metadata}
        descriptor = io.get('input')
        if isinstance(descriptor, dict):
            config['prompt_hash'] = descriptor.get('content_sha256')
    elif name == 'retrieval.file_lineage':
        if metadata.get('file_id') and metadata.get('index_version'):
            files = root.setdefault('observed_files', {})
            files[metadata['file_id']] = metadata['index_version']
            fields.update(index_fingerprint=fingerprint(json.dumps(files, sort_keys=True, separators=(',', ':'))),
                          index_fingerprint_scope='observed_file_chunks_only', observed_file_fingerprints=[
                              {'file_id': k, 'index_fingerprint': v} for k, v in sorted(files.items())])
    if fields or config:
        merged = _root_metadata_update(fields, config)
        if root.get('observation') is not None:
            root['observation'].update(metadata=sanitize(merged))


def sanitize(value, key='', *, final_export=False):
    """Fail closed on arbitrary strings; explicit numeric/ID metadata only."""
    if any(s in key.lower() for s in ('secret', 'api_key', 'password', 'authorization')):
        return '[REDACTED]'
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, dict):
        if authorized_inline(value):
            return {str(k): (v if k in ('text', '_capture_authorization') else sanitize(v, str(k), final_export=final_export))
                    for k, v in value.items() if k != '_capture_authorization' or not final_export}
        return {str(k): sanitize(v, str(k), final_export=final_export) for k, v in value.items()
                if k != '_capture_authorization'}
    if isinstance(value, (list, tuple)):
        return [sanitize(v, key, final_export=final_export) for v in value]
    if key == 'search_mode' and value in ('vector', 'fulltext', 'hybrid'):
        return value
    if key == 'answer_mode' and value in ('GROUNDED_GENERATION', 'EXTRACTIVE_FALLBACK', 'INSUFFICIENT_EVIDENCE', 'RETRIEVAL_ERROR', 'UNKNOWN'):
        return value
    if key == 'validation_status' and value in ('VERIFIED', 'UNKNOWN', 'UNSUPPORTED', 'INVALID', 'NOT_RUN'):
        return value
    if key == 'generation_status' and value in ('NOT_RUN', 'TRUNCATED', 'EMPTY', 'COMPLETED', 'UNKNOWN'):
        return value
    if key == 'retrieval_status' and value in ('SUCCESS', 'NO_RESULTS', 'LOW_RELEVANCE', 'RETRIEVAL_ERROR', 'UNKNOWN'):
        return value
    safe = ('id', 'qid', 'sha256', 'hash', 'version', 'status', 'outcome', 'mode', 'type', 'reason', 'stage', 'name', 'model', 'finish_reason', 'verdict', 'method', 'format', 'role',
            'operation', 'reason_code', 'rule_version', 'score_kind', 'sort_key', 'input_kind',
            'output_kind', 'content_role', 'capture_mode', 'content_visibility', 'snapshot_ref',
            'delivery_target', 'fallback_score_origin', 'stage_execution', 'trace_schema_version')
    if key == 'execution_status' and value in ('RETURNED', 'FAILED', 'UNKNOWN', 'TRUNCATED', 'CANCELLED', 'NOT_RUN'):
        return value
    if key == 'sse_terminal' and value in ('done', 'error'):
        return value
    if key == 'cloud_export' and value in ('FAIL', 'NOT_CONFIRMED', 'CONFIRMED'):
        return value
    if key == 'transform_reason' and value in ('sse_assembly', 'extractive_fallback', 'image_disclosure'):
        return value
    if key == 'source_type' and value in ('indexed_chunk', 'context_expansion', 'native_source_expansion', 'image_ocr', 'image_caption', 'other'):
        return value
    if value == 'NOT_APPLICABLE':
        return value
    if key in ('kb_scope', 'chat_model', 'embedding_model', 'index_fingerprint', 'index_fingerprint_scope', 'code_hash_scope',
               'effective_search_mode', 'content_field'):
        return str(value)[:256]
    if key == 'projection_excludes' and value in ('reasoning', 'provider_specific_fields', 'unsupported_content_blocks'):
        return value
    if key == 'history_source' and value == 'actual_provider_messages':
        return value
    if key == 'truncation_rule' and value == 'none_at_this_boundary':
        return value
    if key in safe or key.endswith(('_id', '_ids', '_sha256', '_hash', '_version')):
        # Names supplied by instrumentation are controlled, identifiers are opaque.
        return str(value)[:256]
    return {'sha256': fingerprint(value), 'chars': len(str(value))}


def capture(value, *, content_role, allow_cloud=False, **metadata):
    """Capture authorized content, otherwise a private immutable snapshot/reference."""
    if not enabled():
        return {'content_visibility': 'NOT_CAPTURED', 'capture_mode': 'BASIC', 'snapshot_ref': None}
    state = _current.get() or {}
    try:
        result = capture_payload(value, content_role=content_role,
            root=os.getenv('RAG_SNAPSHOT_DIR', './storage/observation_snapshots'),
            save_local=os.getenv('RAG_SNAPSHOTS_ENABLED', 'false').lower() == 'true',
            observation_id=state.get('observation_id'), **metadata)
    except Exception:
        return {'content_visibility': 'NOT_CAPTURED', 'capture_mode': 'BASIC', 'snapshot_ref': None}
    result['parent_observation_id'] = state.get('parent_observation_id')
    if not authorized_inline(result):
        result.update(runtime_identity())
    if content_role == 'provider_response':
        _raw_answer_hash.set(result.get('content_sha256'))
    # Caller must additionally prove the complete mixed payload's source scope.
    if not allow_cloud and result.get('content_visibility') == 'CLOUD_INLINE':
        result.pop('text', None)
        result.pop('_capture_authorization', None)
        result['content_visibility'] = 'LOCAL_ONLY' if result.get('snapshot_ref') else 'NOT_CAPTURED'
    return result


def observation_io(*, input=None, output=None, **metadata):
    """Update the existing observation; never create an unrelated root."""
    if not enabled():
        return
    try:
        payload = {k: v for k, v in {'input': input, 'output': output}.items() if v is not None}
        observation = _active_observation.get()
        root = _qa_context.get()
        if root is not None and observation is root.get('observation'):
            metadata = _root_metadata_update(metadata) or metadata
        _record({**(_current.get() or {}), 'stage': 'observation.io',
                 'time': time.time(), **sanitize(payload), 'metadata': sanitize(metadata)})
        if observation is not None:
            observation.update(**payload, metadata=sanitize(metadata))
    except Exception:
        pass


def observe_model_history(messages):
    """Record the conversation actually reaching this boundary, not an earlier draft."""
    if not enabled():
        return
    try:
        conversation = [m for m in messages if isinstance(m, dict) and m.get('role') != 'system']
        if len(conversation) <= 1:
            return
        payload = capture(conversation, content_role='provider_request')
        event('qa.history', input=payload,
              output={'retained_message_indices': list(range(len(conversation))), 'retained_ids': None, 'removed_ids': None},
              history_source='actual_provider_messages', history_message_count_in=len(conversation),
              history_message_count_out=len(conversation), truncation_rule='none_at_this_boundary',
              effective_messages_sha256=payload.get('content_sha256'), message_ids_status='NOT_CAPTURED')
    except Exception:
        pass


def snapshot(value):
    """Legacy reference shape backed by the same private, verified snapshot policy."""
    if not enabled():
        return {'sha256': fingerprint(value), 'chars': len(str(value)), 'snapshot_id': None,
                'snapshot_ref': None, 'content_visibility': 'NOT_CAPTURED'}
    result = capture(value, content_role='original_chunk')
    result['snapshot_id'] = result.get('snapshot_ref')
    return result


def _sdk():
    global _client, _attempted, _sdk_init_error
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
                               mask=lambda **kw: sanitize(kw.get('data'), final_export=True))
        except Exception as exc:
            _client = None
            _sdk_init_error = type(exc).__name__
            _record({'stage': 'sdk.initialization_failed', 'time': time.time(),
                     'cloud_export': 'FAIL', 'error_type': _sdk_init_error})
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
    identity = capture_identity()
    state = {'trace_id': parent['trace_id'] if parent else uuid.uuid4().hex,
             'span_id': uuid.uuid4().hex[:16], 'parent_id': parent['span_id'] if parent else None,
             'observation_id': None, 'parent_observation_id': parent.get('observation_id') if parent else None,
             'run_id': identity.get('run_id') or (parent or {}).get('run_id') or uuid.uuid4().hex,
             'attempt_id': identity.get('attempt_id') or (parent or {}).get('attempt_id') or uuid.uuid4().hex,
             'qid': identity.get('qid'), 'dataset_version': identity.get('dataset_version'),
             'stage_attempt_id': retrieval_attempt.get() or uuid.uuid4().hex,
             'trace_schema_version': 'rag-trace-v1'}
    event_id = uuid.uuid4().hex
    try:
        data = {**sanitize(metadata), **{k: v for k, v in state.items() if k not in ('span_id', 'trace_id', 'parent_id')}, 'event_id': event_id}
    except Exception:
        data = {'event_id': event_id}
    token = _current.set(state)
    start = time.monotonic()
    manager = None
    observation = None
    sdk_token = None
    previous_observation = _active_observation.get()
    try:
        client = _sdk()
        if client:
            try:
                kind = 'generation' if name.startswith('llm.') else 'agent' if name.startswith('agent.') else 'span'
                manager = client.start_as_current_observation(name=name, as_type=kind, metadata=data)
                observation = manager.__enter__()
                state['trace_id'] = client.get_current_trace_id() or state['trace_id']
                state['span_id'] = client.get_current_observation_id() or state['span_id']
                state['observation_id'] = client.get_current_observation_id()
            except Exception:
                manager = None
        state['cloud_export'] = 'NOT_CONFIRMED'
        if client is None and os.getenv('LANGFUSE_ENABLED', 'false').lower() == 'true':
            state['cloud_export'] = 'FAIL'
            data['cloud_export'] = 'FAIL'
            data['sdk_error_type'] = _sdk_init_error
        sdk_token = _active_observation.set(observation)
        _record({**state, 'event_id': event_id, 'stage': name, 'event': 'start', 'time': time.time(), 'metadata': data})
        yield state
    except BaseException as exc:
        if observation:
            try:
                observation.update(level='ERROR', status_message=type(exc).__name__)
            except Exception:
                pass
        event('failure', status='CANCELLED' if isinstance(exc, (GeneratorExit, asyncio.CancelledError)) else 'FAILED',
              error_type=type(exc).__name__)
        raise
    finally:
        _record({**state, 'event_id': event_id, 'stage': name, 'event': 'end', 'latency_ms': (time.monotonic()-start)*1000})
        if manager:
            try:
                manager.__exit__(None, None, None)
            except Exception:
                pass
        _reset_context(_current, token, parent)
        if sdk_token is not None:
            _reset_context(_active_observation, sdk_token, previous_observation)


def event(name, **metadata):
    if not enabled():
        return
    try:
        if name == 'rag.candidate_transition':
            metadata.setdefault('input', {'operation': metadata.get('operation'), 'input_kind': metadata.get('input_kind'),
                                          'candidate_ids': metadata.get('input_candidate_ids', metadata.get('input_ids'))})
            metadata.setdefault('output', {'output_kind': metadata.get('output_kind'),
                                           'candidate_ids': metadata.get('output_candidate_ids', metadata.get('output_ids'))})
        io = {k: sanitize(metadata.pop(k)) for k in ('input', 'output') if k in metadata}
        try:
            _observe_root_resolution(name, metadata, io)
        except Exception:
            pass
        state = _current.get()
        if state is None:
            with span(name, **metadata):
                observation_io(**io)
                pass
            return
        event_id = uuid.uuid4().hex
        data = {**sanitize(metadata), **runtime_identity(), 'trace_schema_version': 'rag-trace-v1',
                'stage_attempt_id': metadata.get('retrieval_attempt_id') or retrieval_attempt.get() or state.get('stage_attempt_id'),
                'event_id': event_id}
        record = {**state, 'observation_id': None, 'parent_observation_id': state.get('observation_id'),
                  'stage_attempt_id': metadata.get('retrieval_attempt_id') or retrieval_attempt.get() or state.get('stage_attempt_id'),
                  'event_id': event_id, 'stage': name, 'event': 'stage', 'time': time.time(), 'metadata': data, **io}
        client = _sdk()
        try:
            if client:
                extra = {}
                if name == 'llm.response':
                    usage = metadata.get('usage') or {}
                    extra = {'as_type': 'generation', 'model': metadata.get('model'),
                             'usage_details': {k: v for k, v in usage.items() if isinstance(v, int)}}
                with client.start_as_current_observation(name=name, metadata=data, **io, **extra) as child:
                    record['trace_id'] = client.get_current_trace_id() or state['trace_id']
                    record['observation_id'] = client.get_current_observation_id()
                    for descriptor in io.values():
                        if isinstance(descriptor, dict) and 'content_role' in descriptor:
                            descriptor['parent_observation_id'] = descriptor.get('observation_id')
                            descriptor['observation_id'] = record['observation_id']
                    if io:
                        child.update(**io)
        except Exception:
            record['cloud_export'] = 'FAIL'
        finally:
            _record(record)
    except Exception:
        pass


def candidates(name, rows, **metadata):
    if not enabled():
        return
    try:
        evidence = []
        for rank, row in enumerate(rows, 1):
            content = capture(row['content'], content_role='original_chunk') if 'content' in row else {
                'content_visibility': 'NOT_CAPTURED', 'snapshot_ref': None, 'content_sha256': None}
            kinds = {'retrieval.vector': 'vector_raw', 'retrieval.bm25': 'bm25_raw',
                     'retrieval.normalized_vector': 'vector_normalized', 'retrieval.normalized_bm25': 'bm25_normalized',
                     'retrieval.fusion': 'fusion', 'rerank.batch_output': 'rerank', 'rerank.filtered': 'rerank', 'rerank.selected': 'rerank'}
            identifier = row.get('id', row.get('chunk_id'))
            evidence.append({k: v for k, v in {
                'rank': rank, 'id': identifier, 'evidence_id': identifier, 'chunk_id': identifier,
                'source_type': row.get('source_type') or ('indexed_chunk' if identifier else 'other'),
                'score_kind': kinds.get(name, metadata.get('score_kind')), 'score_value': row.get('score'),
                'file_id': row.get('file_id'), 'page_number': row.get('page_number'),
                'score': row.get('score'), 'vector_score': row.get('vector_score'),
                'bm25_score': row.get('bm25_score'), 'rerank_score': row.get('rerank_score'),
                'parent_id': row.get('parent_id'), 'source_metadata': row.get('source_metadata'),
                'content_sha256': content.get('content_sha256'), 'content_capture': content,
                'content_visibility': content.get('content_visibility'), 'snapshot_ref': content.get('snapshot_ref'),
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
                    parts, terminal, chunk_count, terminal_data, cancelled = [], None, 0, {}, False
                    previous_raw_hash = _raw_answer_hash.get()
                    raw_token = _raw_answer_hash.set(None) if name == 'qa.request' else None
                    previous_qa_context = _qa_context.get()
                    qa_token = None
                    if name == 'qa.request' and enabled():
                        qa_token = _qa_context.set({'observation': _active_observation.get(), 'metadata': {
                            **runtime_identity(), **capture_settings(), 'mode': None, 'kb_scope': [],
                            'effective_config': {k: None for k in ('search_mode', 'top_k', 'score_threshold', 'vector_weight',
                               'bm25_weight', 'rerank_enabled', 'rerank_model_id', 'rerank_top_k', 'rerank_score_threshold',
                               'context_window', 'use_rewrite', 'chat_model', 'embedding_model', 'prompt_hash', 'temperature', 'max_tokens')},
                            'code_hash': code_fingerprint(), 'code_hash_scope': 'backend_app_py_source',
                            'index_fingerprint': None, 'index_fingerprint_scope': 'NOT_CAPTURED',
                            'trace_schema_version': 'rag-trace-v1'}})
                    if name == 'qa.request' and enabled():
                        try:
                            bound = inspect.signature(fn).bind(*args, **kwargs)
                            messages = bound.arguments.get('messages') or []
                            users = []
                            for message in messages:
                                role = message.get('role') if isinstance(message, dict) else getattr(message, 'role', '')
                                if str(getattr(role, 'value', role)).lower() == 'user':
                                    users.append(message.get('content') if isinstance(message, dict) else getattr(message, 'content', None))
                            observation_io(input=capture(users[-1] if users else '', content_role='provider_request'),
                                           **runtime_identity())
                        except Exception:
                            pass
                    try:
                        async for item in fn(*args, **kwargs):
                            if name == 'qa.request' and enabled() and isinstance(item, tuple) and len(item) == 2:
                                kind, data = item
                                if kind == 'answer_chunk':
                                    parts.append(data.get('content', ''))
                                    chunk_count += 1
                                elif kind in ('done', 'error'):
                                    terminal = kind
                                    terminal_data = data
                            yield item
                    except (GeneratorExit, asyncio.CancelledError):
                        cancelled = True
                        raise
                    finally:
                        if name == 'qa.request' and enabled():
                            try:
                                answer = ''.join(parts)
                                status = ('CANCELLED' if cancelled else 'TRUNCATED' if terminal_data.get('generation_status') == 'TRUNCATED'
                                          else 'RETURNED' if terminal == 'done' else 'FAILED' if terminal == 'error' else 'UNKNOWN')
                                original_hash = _raw_answer_hash.get()
                                answer_observed = bool(parts) or terminal == 'done'
                                observation_io(output=capture(answer, content_role='final_answer') if answer_observed else {
                                                   'content_visibility': 'NOT_CAPTURED', 'snapshot_ref': None},
                                               execution_status=status, sse_terminal=terminal,
                                               final_answer_sha256=fingerprint(answer) if answer_observed else None, chunk_count=chunk_count,
                                               original_answer_sha256=original_hash,
                                               generation_status=terminal_data.get('generation_status', 'NOT_RUN'),
                                               answer_mode=terminal_data.get('answer_mode'),
                                               transform_reason='sse_assembly' if original_hash and original_hash != fingerprint(answer) else None,
                                               answer_empty=not bool(answer) if answer_observed else None, **runtime_identity())
                            except Exception:
                                pass
                            finally:
                                flush_observations()
                        if raw_token is not None:
                            _reset_context(_raw_answer_hash, raw_token, previous_raw_hash)
                        if qa_token is not None:
                            _reset_context(_qa_context, qa_token, previous_qa_context)
            return stream
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_call(*args, **kwargs):
                attempt_token = None
                previous_attempt = retrieval_attempt.get()
                if name == 'retrieval.search' and enabled():
                    attempt_token = retrieval_attempt.set(previous_attempt or uuid.uuid4().hex)
                try:
                    with span(name):
                        search_metadata = None
                        if name == 'retrieval.search' and enabled():
                            try:
                                bound = inspect.signature(fn).bind(*args, **kwargs)
                                bound.apply_defaults()
                                requested = {k: v for k, v in bound.arguments.items() if k not in ('self', 'session', 'query')}
                                search_metadata = {'retrieval_attempt_id': retrieval_attempt.get(),
                                                   'requested_parameters': requested,
                                                   'kb_scope': bound.arguments.get('kb_ids'),
                                                   'effective_search_mode': bound.arguments.get('search_mode')}
                                observation_io(input=capture(bound.arguments.get('query'), content_role='provider_request'),
                                               **search_metadata)
                            except Exception:
                                pass
                        result = await fn(*args, **kwargs)
                        if search_metadata is not None:
                            try:
                                rows, metrics = result
                                observation_io(output={'candidate_ids': [r.get('id', r.get('chunk_id')) for r in rows],
                                                       'candidate_count_out': len(rows), 'retrieval_status': metrics.get('retrieval_status', 'UNKNOWN')},
                                               **search_metadata, candidate_count_in=metrics.get('merged_count'),
                                               candidate_count_out=len(rows), retrieval_status=metrics.get('retrieval_status', 'UNKNOWN'))
                            except Exception:
                                pass
                        return result
                finally:
                    if attempt_token is not None:
                        _reset_context(retrieval_attempt, attempt_token, previous_attempt)
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

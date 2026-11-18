"""Opt-in monotonic stage receipts; no query, document or provider credentials."""
from functools import wraps
from inspect import iscoroutinefunction
import json
import os
import time


def benchmark_timed(stage):
    def decorate(function):
        def emit(started, status, args, kwargs, result):
            ended = time.monotonic_ns()
            receipt = {
                'stage': stage, 'status': status,
                'started_monotonic_ns': started, 'ended_monotonic_ns': ended,
                'wall_ms': (ended-started)/1e6,
            }
            # IDs and policy outcomes permit delivery auditing without source text.
            try:
                if stage == 'parent_expansion':
                    rows = kwargs.get('top_chunks', args[0] if args else [])
                    receipt['input_seed_ids'] = [r.get('id') for r in rows]
                    receipt['groups'] = [{
                        'parent_chunk_id': r.get('parent_chunk_id'),
                        'seed_child_ids': r.get('seed_child_ids', []),
                        'truncation_status': (r.get('context_group') or {}).get('truncation_status'),
                        'degradation_reason': (r.get('context_group') or {}).get('degradation_reason'),
                        'omitted_seed_ids': (r.get('context_group') or {}).get('omitted_seed_ids', []),
                    } for r in result or []]
                elif stage == 'context_build' and result is not None:
                    summary = result[1]
                    receipt.update({k: summary.get(k) for k in (
                        'context_group_count', 'context_char_count', 'truncation_status',
                        'expansion_delivery_status', 'source_mapping_status')})
            except Exception as exc:
                receipt['diagnostic_error_type'] = type(exc).__name__
            print('REQUEST_STAGE_TIMING ' + json.dumps(receipt), flush=True)

        if iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapped(*args, **kwargs):
                if os.getenv('RAG_BENCHMARK_STAGE_TIMING', '').lower() != 'true':
                    return await function(*args, **kwargs)
                started = time.monotonic_ns()
                status = 'FAILED'
                result = None
                try:
                    result = await function(*args, **kwargs)
                    status = 'RETURNED'
                    return result
                finally:
                    emit(started, status, args, kwargs, result)
            return async_wrapped

        @wraps(function)
        def wrapped(*args, **kwargs):
            if os.getenv('RAG_BENCHMARK_STAGE_TIMING', '').lower() != 'true':
                return function(*args, **kwargs)
            started = time.monotonic_ns()
            status = 'FAILED'
            result = None
            try:
                result = function(*args, **kwargs)
                status = 'RETURNED'
                return result
            finally:
                emit(started, status, args, kwargs, result)
        return wrapped
    return decorate

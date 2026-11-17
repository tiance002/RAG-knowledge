"""Optional experiment-only tracing around the runner's actual provider call.

Uses the application's existing capture/export policy. Configure observability in
the isolated runner process before calling; importing this module loads no app or
SDK and reads no credentials. BASIC capture is intentional for frozen Gold data.
"""
import hashlib
import importlib
import json
import time


def _load_observability():
    return importlib.import_module('app.services.observability')


def _load_capture_scope():
    return importlib.import_module('app.services.trace_schema').capture_scope


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _usage(usage):
    """Only map reported integer counts; absent counts stay absent."""
    if not isinstance(usage, dict):
        return None
    mapped = {}
    for destination, keys in {'input': ('input', 'input_tokens', 'prompt_tokens'),
                              'output': ('output', 'output_tokens', 'completion_tokens'),
                              'total': ('total', 'total_tokens')}.items():
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                mapped[destination] = value
                break
    return mapped or None


class _TraceCall:
    def __init__(self, case, role, model, metadata):
        self.case, self.role, self.model = case, role, model
        self.metadata = metadata
        self._managers = []
        self._obs = self._generation = None
        self._finished = False
        self._started = time.monotonic()
        self.trace_id = None
        self._request_hash = None

    def _enter(self, manager):
        value = manager.__enter__()
        self._managers.append(manager)
        return value

    def __enter__(self):
        try:
            self._obs = _load_observability()
            if not self._obs.native_enabled():
                self._obs = None
                return self
            case, meta = self.case, self.metadata or {}
            qid = case.get('qid', case.get('id'))
            identity = {key: meta.get(key, case.get(key))
                        for key in ('run_id', 'attempt_id', 'dataset_version')}
            self._enter(_load_capture_scope()(qid=qid, capture_mode='BASIC', **identity))
            # Do not forward arbitrary caller metadata (which may contain secrets
            # or raw prompts) through metadata's identifier allowlist.
            fields = {'qid': qid, 'role': self.role, 'model': self.model,
                      'router_mode': meta.get('router_mode', meta.get('arm')),
                      'cost': None, 'cost_status': 'UNKNOWN', **identity}
            request = meta.get('request_payload', meta.get('messages'))
            if isinstance(request, dict):
                fields.update({k: request[k] for k in ('max_tokens', 'temperature')
                               if isinstance(request.get(k), (int, float)) and not isinstance(request[k], bool)})
            fields['request_hash'] = meta.get('request_hash') or (_hash(request) if request is not None else None)
            self._request_hash = fields['request_hash']
            state = self._enter(self._obs.span('routing.provider_call', **fields))
            if isinstance(state, dict):
                self.trace_id = state.get('trace_id')
            question = self._obs.capture(case.get('question'), content_role='provider_request')
            self._obs.observation_io(input={'question': question}, **fields)
            features = meta.get('features')
            if isinstance(features, dict):
                # Numeric/boolean features only; free-form strings go through
                # capture rather than fields with privileged identifier names.
                features = {k: v for k, v in features.items()
                            if isinstance(v, (int, float, bool)) or v is None}
                self._obs.event('routing.decision', features=features, **fields)
            payload = self._obs.capture(request, content_role='provider_request') if request is not None else None
            self._generation = self._enter(self._obs.generation(
                'routing.provider_generation', model=self.model, input=payload,
                **{k: v for k, v in fields.items() if k != 'model'}))
        except Exception:
            # Preserve any successfully entered parent until normal exit. A
            # metadata/capture/export failure must never skip the provider call.
            self._obs = self._generation = None
        return self

    def finish(self, result):
        """Record a result dict once, without changing it or raising to caller."""
        if self._finished:
            return
        self._finished = True
        try:
            if self._obs is None:
                return
            usage = result.get('usage')
            response = result.get('response_payload', result.get('response', result.get('text', result.get('content'))))
            status = result.get('state', result.get('status'))
            failed = bool(result.get('error') or result.get('error_type'))
            execution = ('FAILED' if failed else status if status in
                         ('FAILED', 'UNKNOWN', 'TRUNCATED', 'CANCELLED', 'NOT_RUN') else 'RETURNED')
            generation = (status if status in ('UNKNOWN', 'TRUNCATED', 'EMPTY', 'COMPLETED', 'NOT_RUN')
                          else 'TRUNCATED' if result.get('finish_reason') == 'length' else 'UNKNOWN')
            fields = {
                'finish_reason': result.get('finish_reason'),
                'provider_request_id': result.get('provider_request_id', result.get('request_id')),
                'request_hash': result.get('request_hash') or self._request_hash,
                'latency_ms': result.get('latency_ms', (time.monotonic() - self._started) * 1000),
                'execution_status': execution, 'generation_status': generation,
                'provider_status': status,
                'models': {'expected': {'model': self.model}, 'actual': {'model': result.get('model')}},
                'error_type': result.get('error_type'),
                'cost': None, 'cost_status': 'UNKNOWN',
            }
            output = self._obs.capture(response, content_role='provider_response')
            if self._generation is not None:
                self._generation.update(output=output, usage=_usage(usage), **fields)
            self._obs.observation_io(output=output, usage=usage, **fields)
        except Exception:
            pass

    def __exit__(self, exc_type, exc, tb):
        if exc is not None and not self._finished:
            self.finish({'error': True, 'error_type': exc_type.__name__})
        for manager in reversed(self._managers):
            try:
                manager.__exit__(exc_type, exc, tb)
            except Exception:
                pass
        self._managers.clear()
        return False  # Business errors always retain their original behavior.


def trace_call(case, role, model, metadata=None):
    """Yield a fail-open collector with ``finish(result)`` for one model call."""
    return _TraceCall(case, role, model, metadata)


def flush_observations():
    """Best-effort flush of the existing SDK client before script shutdown."""
    try:
        _load_observability().flush_observations()
    except Exception:
        pass


flush = flush_observations

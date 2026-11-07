"""Isolated, gold-blind product-quality worker. No paid calls without --live.

One fresh process handles one arm, seeds actual BM25 once into in-memory SQLite,
and calls the actual /api/v1/chat/completions HTTP/SSE route using ASGITransport.
The worker never imports prior experiment drivers or reads provider credentials
from files. The controller owns authorization, monetary reservation and scoring.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = 'rsi-quality-job-v1'
MODEL = 'Qwen/Qwen3.5-4B'
PROVIDER = 'https://api.siliconflow.cn/v1'
DEADLINE = 180.0
FIXED_SEARCH = dict(search_mode='fulltext', top_k=10, context_window=0,
                    rerank_enabled=False, score_threshold=0)
EMBEDDING_ENV = ('RSI_QUALITY_EMBEDDING_BASE_URL', 'RSI_QUALITY_EMBEDDING_MODEL',
                 'RSI_QUALITY_EMBEDDING_API_KEY')
# Only explicitly reviewed free-provider identities may be added here.
APPROVED_EMBEDDINGS = {(PROVIDER, 'BAAI/bge-m3')}
EMBEDDING_BATCH_SIZE = 32


def search_config(job):
    config = job.get('retrieval', {})
    allowed_keys(config, {*FIXED_SEARCH, 'vector_weight', 'bm25_weight'}, 'retrieval')
    result = {**FIXED_SEARCH, **config}
    if result['search_mode'] not in ('fulltext', 'hybrid'):
        raise ValueError('Only fulltext or real hybrid retrieval is supported')
    for name, low, high in (('top_k', 1, 100), ('context_window', 0, 2)):
        if type(result[name]) is not int or not low <= result[name] <= high:
            raise ValueError('Invalid retrieval ' + name)
    if result['rerank_enabled'] is not False:
        raise ValueError('Rerank is outside the approved worker scope')
    for name in ('score_threshold', 'vector_weight', 'bm25_weight'):
        if name in result and (type(result[name]) not in (int, float)
                or not math.isfinite(result[name]) or not 0 <= result[name] <= 1):
            raise ValueError('Invalid retrieval ' + name)
    if result['search_mode'] == 'hybrid':
        result.setdefault('vector_weight', 0.7)
        result.setdefault('bm25_weight', 0.3)
        if result['vector_weight'] <= 0 or result['bm25_weight'] <= 0:
            raise ValueError('Hybrid requires both vector and BM25 weights to be positive')
        cap = job.get('max_embedding_requests')
        if type(cap) is not int or not 1 <= cap <= 10000:
            raise ValueError('Hybrid requires bounded max_embedding_requests')
    return result


def embedding_environment(job):
    # Never read deployment files or infer identities from production settings.
    if search_config(job)['search_mode'] != 'hybrid':
        return None
    values = [os.environ.get(name, '').strip() for name in EMBEDDING_ENV]
    if not all(values):
        raise ValueError('Hybrid requires process-only ' + ', '.join(EMBEDDING_ENV))
    base_url, model, key = values
    if (base_url, model) not in APPROVED_EMBEDDINGS:
        raise ValueError('Embedding endpoint/model is not explicitly allowlisted')
    return {'base_url': base_url, 'model': model, 'api_key': key}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def allowed_keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f'{label}: unexpected fields (Gold/private fields are forbidden)')


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}: nonempty string required')
    return value


def validate(job):
    """Strict public-only schema. No imports, network, settings or writes."""
    allowed_keys(job, {'schema_version', 'model', 'provider_base_url', 'max_model_requests',
        'cases', 'sources', 'chunks', 'artifact', 'repeat', 'records', 'dataset_version',
        'arm_id', 'authorization_id', 'retrieval', 'max_embedding_requests', 'retry_once'}, 'job')
    search_config(job)
    if type(job.get('retry_once', False)) is not bool:
        raise ValueError('retry_once must be boolean')
    if job.get('schema_version') != SCHEMA or job.get('model', MODEL) != MODEL:
        raise ValueError('Unsupported schema/model')
    if job.get('provider_base_url', PROVIDER) != PROVIDER:
        raise ValueError('Only the approved SiliconFlow endpoint is allowed')
    if type(job.get('max_model_requests')) is not int or job['max_model_requests'] < 1:
        raise ValueError('Positive hard request cap required')
    artifact = job.get('artifact', {})
    allowed_keys(artifact, {'prompt_addendum', 'memory_addendum', 'workflow'}, 'artifact')
    for key in ('prompt_addendum', 'memory_addendum'):
        if not isinstance(artifact.get(key, ''), str):
            raise ValueError(f'{key}: string required')
    workflow = artifact.get('workflow', {})
    allowed_keys(workflow, {'mode', 'use_rewrite'}, 'workflow')
    if workflow.get('mode', 'chat') not in ('chat', 'agent') or type(workflow.get('use_rewrite', False)) is not bool:
        raise ValueError('Unsupported workflow')
    if workflow.get('mode') == 'agent' and artifact.get('prompt_addendum'):
        raise ValueError('Normal-generation prompt addendum has no Agent hook')
    sources, chunks, cases = job.get('sources'), job.get('chunks'), job.get('cases')
    if not all(isinstance(items, list) and items for items in (sources, chunks, cases)):
        raise ValueError('Nonempty public sources/chunks/cases required')
    source_ids, chunk_ids, case_ids, kb_ids = set(), set(), set(), set()
    for source in sources:
        allowed_keys(source, {'id', 'path', 'name', 'kb_id', 'sha256'}, 'source')
        sid = nonempty(source.get('id'), 'source.id')
        if sid in source_ids:
            raise ValueError('Duplicate source identity')
        source_ids.add(sid)
        kb_ids.add(nonempty(source.get('kb_id', 'rsi-quality-kb'), 'source.kb_id'))
        if 'name' in source:
            nonempty(source['name'], 'source.name')
        if source.get('path'):
            path = Path(source['path'])
            if not path.is_absolute() or not path.is_file():
                raise ValueError('Source path must be an existing absolute file')
            if source.get('sha256') and digest(path.read_bytes()) != source['sha256']:
                raise ValueError('Frozen source hash mismatch')
        elif source.get('sha256'):
            raise ValueError('Source hash requires source path')
    for chunk in chunks:
        allowed_keys(chunk, {'id', 'source_id', 'content', 'page_number'}, 'chunk')
        cid = nonempty(chunk.get('id'), 'chunk.id')
        if cid in chunk_ids or chunk.get('source_id') not in source_ids:
            raise ValueError('Duplicate or unlinked chunk')
        chunk_ids.add(cid)
        nonempty(chunk.get('content'), 'chunk.content')
        if chunk.get('page_number') is not None and (type(chunk['page_number']) is not int or chunk['page_number'] < 1):
            raise ValueError('Invalid source page')
    styles = {'concise', 'normal', 'detailed'}
    levels = {'beginner', 'intermediate', 'expert'}
    outputs = {'step_by_step', 'table', 'code_example', 'principle_first'}
    for case in cases:
        allowed_keys(case, {'id', 'question', 'history', 'preferences', 'kb_ids'}, 'case')
        cid = nonempty(case.get('id'), 'case.id')
        if cid in case_ids:
            raise ValueError('Duplicate case identity')
        case_ids.add(cid)
        nonempty(case.get('question'), 'question')
        history = case.get('history', [])
        if not isinstance(history, list) or len(history) % 2:
            raise ValueError('History must contain complete user/assistant turns')
        for i, message in enumerate(history):
            allowed_keys(message, {'role', 'content'}, 'history message')
            if message.get('role') != ('user' if i % 2 == 0 else 'assistant'):
                raise ValueError('History has incorrect active-turn scope')
            nonempty(message.get('content'), 'history content')
        scope = case.get('kb_ids', sorted(kb_ids))
        if not isinstance(scope, list) or not scope or len(scope) != len(set(scope)) or not set(scope) <= kb_ids:
            raise ValueError('Case scope must select known seeded KBs')
        prefs = case.get('preferences', {})
        allowed_keys(prefs, {'answer_style', 'knowledge_level', 'output_preferences'}, 'preferences')
        if ('answer_style' in prefs and prefs['answer_style'] not in styles) or ('knowledge_level' in prefs and prefs['knowledge_level'] not in levels):
            raise ValueError('Unsupported preference enum')
        op = prefs.get('output_preferences', [])
        if not isinstance(op, list) or any(x not in outputs for x in op) or len(op) != len(set(op)):
            raise ValueError('Invalid output preferences')
    if 'records' in job and 'repeat' in job:
        raise ValueError('Use records or repeat, not both')
    if 'records' in job:
        records, identities = job['records'], set()
        if not isinstance(records, list) or not records:
            raise ValueError('Nonempty records required')
        for record in records:
            allowed_keys(record, {'case_id', 'repeat_id'}, 'record')
            if record.get('case_id') not in case_ids or type(record.get('repeat_id')) is not int or record['repeat_id'] < 1:
                raise ValueError('Invalid planned record')
            identity = (record['case_id'], record['repeat_id'])
            if identity in identities:
                raise ValueError('Duplicate record; retries are forbidden')
            identities.add(identity)
    else:
        if type(job.get('repeat', 1)) is not int or job.get('repeat', 1) < 1:
            raise ValueError('Positive repeat required')
        records = [{'case_id': case['id'], 'repeat_id': repeat}
                   for repeat in range(1, job.get('repeat', 1) + 1) for case in cases]
    return records


def preflight(job):
    records = validate(job)
    return {'status': 'PASS', 'execution': 'NOT RUN', 'model_calls': 0,
        'planned_records': len(records), 'max_model_requests': job['max_model_requests'],
        'job_sha256': digest(canonical(job).encode()), 'model': MODEL,
        'provider_base_url': PROVIDER, 'fixed_search': search_config(job),
        'embedding': {'execution': 'NOT RUN', 'model_calls': 0,
            'required_process_env': list(EMBEDDING_ENV),
            'optional_process_env': ['RSI_QUALITY_EMBEDDING_CACHE_DIR'],
            'allowed_identities': [{'base_url': base, 'model': model} for base, model in sorted(APPROVED_EMBEDDINGS)],
            'max_requests': job.get('max_embedding_requests'), 'batch_size': EMBEDDING_BATCH_SIZE,
            'seeded_chunks': len(job['chunks'])} if search_config(job)['search_mode'] == 'hybrid' else None,
        'observability': 'Langfuse disabled; local request, SSE and retrieval artifacts only',
        'max_tokens': 4096, 'enable_thinking': False, 'question_deadline_seconds': DEADLINE,
        'retry_policy': {'retry_once': job.get('retry_once', False),
            'boundary': 'record; failed embedding seed batch separately', 'max_attempts': 2 if job.get('retry_once') else 1,
            'attempt_deadline_seconds': DEADLINE, 'provider_hidden_retries': 0},
        'gold_access': False, 'database': 'in-memory SQLite; BM25 seeded once per process',
        'resources': {'chroma': 'NOT CREATED', 'corpus_copy': 'NOT CREATED',
                      'production_database': 'NOT OPENED', 'credential_file': 'NOT READ'},
        'workflow_notes': ('Agent changes intent/tool selection/grading/final composition together; '
                           'use_rewrite is not consumed by the existing Agent HTTP path'
                           if job.get('artifact', {}).get('workflow', {}).get('mode') == 'agent'
                           else 'Normal HTTP path; rewrite uses the existing product control')}


class RecordingProxy:
    """Bounded exact provider request boundary, with UNKNOWN reserved before send."""
    def __init__(self, output, key, cap):
        self.output, self.key, self.cap = Path(output), key, cap
        self.calls, self.lock = [], threading.Lock()
        self.context, self.deadline, self.stopped = None, 0.0, False
        self.product_context = {}
        self.server = None
        (self.output / 'calls').mkdir()

    def write(self, path, value):
        # Exact credential strings never enter artifacts, including error bodies.
        text = json.dumps(value, ensure_ascii=False, indent=2).replace(self.key, '[REDACTED]')
        (self.output / path).write_text(text, encoding='utf-8')

    @staticmethod
    def headers(headers):
        # Allowlist avoids credentials, cookies and unknown provider secret headers.
        allowed = {'content-type', 'content-length', 'x-request-id', 'request-id', 'date'}
        return {k.lower(): v for k, v in headers.items() if k.lower() in allowed}

    @staticmethod
    def response_metadata(text, content_type):
        objects = []
        if 'text/event-stream' in content_type:
            for line in text.splitlines():
                if line.startswith('data:') and line[5:].strip() != '[DONE]':
                    try:
                        objects.append(json.loads(line[5:].strip()))
                    except (ValueError, TypeError):
                        pass
        else:
            try:
                objects.append(json.loads(text))
            except (ValueError, TypeError):
                pass
        usages, finishes, texts, models = [], [], [], []
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            if obj.get('usage') is not None:
                usages.append(obj['usage'])
            if obj.get('model'):
                models.append(obj['model'])
            for choice in obj.get('choices') or []:
                if choice.get('finish_reason') is not None:
                    finishes.append(choice['finish_reason'])
                message = choice.get('message') or choice.get('delta') or {}
                if isinstance(message.get('content'), str):
                    texts.append(message['content'])
        return {'usage': usages[-1] if usages else None,
                'finish_reason': finishes[-1] if finishes else None,
                'provider_model': models[-1] if models else None,
                'generated_content': ''.join(texts)}

    def start(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def reply(self, status, body, content_type='application/json'):
                self.send_response(status)
                self.send_header('content-type', content_type)
                self.send_header('content-length', str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def do_POST(self):
                if self.path != '/v1/chat/completions':
                    self.reply(403, b'{"error":"unapproved endpoint"}')
                    return
                raw = self.rfile.read(int(self.headers.get('content-length') or 0))
                try:
                    payload = json.loads(raw)
                except ValueError:
                    self.reply(400, b'{"error":"invalid request"}')
                    return
                with proxy.lock:
                    remaining = proxy.deadline - time.monotonic()
                    if proxy.stopped or remaining <= 0 or len(proxy.calls) >= proxy.cap or proxy.context is None:
                        proxy.stopped = True
                        self.reply(429, b'{"error":"worker hard request cap or deadline reached"}')
                        return
                    seq = len(proxy.calls) + 1
                    meta = {'seq': seq, **proxy.context, 'request_status': 'UNKNOWN',
                            'usage': None, 'finish_reason': None, 'request_file': f'calls/{seq:04d}-request.json',
                            'response_file': f'calls/{seq:04d}-response.json'}
                    proxy.calls.append(meta)
                # Keep original factory request and effective actual wire request separately.
                original = dict(payload)
                payload.update(model=MODEL, enable_thinking=False)
                # Preserve the actual per-call limit, e.g. rewrite's 200 tokens.
                # Only supply the answer default when the product omitted it.
                if payload.get('max_tokens') is None:
                    payload['max_tokens'] = 4096
                elif isinstance(payload['max_tokens'], int) and payload['max_tokens'] > 4096:
                    payload['max_tokens'] = 4096
                wire = canonical(payload).encode('utf-8')
                messages = payload.get('messages', [])
                messages_text = canonical(messages)
                meta.update(request_chars=len(wire.decode()), request_bytes=len(wire),
                    prompt_messages_chars=len(messages_text), prompt_messages_bytes=len(messages_text.encode()),
                    is_grounded=any('请根据以下参考资料' in m.get('content', '')
                        for m in messages if isinstance(m.get('content'), str)),
                    has_tool_role=any(m.get('role') == 'tool' for m in messages))
                evidence = proxy.product_context.get('grounded_evidence')
                verified = (meta['is_grounded'] and isinstance(evidence, str) and bool(evidence)
                    and any(evidence in m.get('content', '') for m in messages
                            if m.get('role') == 'system' and isinstance(m.get('content'), str)))
                meta.update(context_status='VERIFIED_IN_ACTUAL_REQUEST' if verified else 'UNKNOWN',
                    context_chars=len(evidence) if verified else None,
                    context_bytes=len(evidence.encode()) if verified else None,
                    context_measurement_scope='normal_source_numbered_evidence' if verified else None)
                proxy.write(meta['request_file'], {'factory_request': original, 'actual_request': payload,
                    'actual_request_utf8': wire.decode(), 'headers': proxy.headers(self.headers)})
                proxy.write(f'calls/{seq:04d}-meta.json', meta)
                started = time.monotonic()
                try:
                    import httpx
                    with httpx.Client(timeout=httpx.Timeout(min(DEADLINE, remaining)), trust_env=False,
                                      follow_redirects=False) as client:
                        response = client.post(PROVIDER + '/chat/completions', content=wire,
                            headers={'Authorization': 'Bearer ' + proxy.key, 'Content-Type': 'application/json'})
                    response_text = response.content.decode('utf-8', 'replace')
                    content_type = response.headers.get('content-type', 'application/json')
                    data = proxy.response_metadata(response_text, content_type)
                    meta.update(data, http_status=response.status_code,
                        request_status=('RETURNED' if response.status_code < 400 and data['finish_reason'] is not None
                                        else 'FAILED' if response.status_code >= 400 else 'UNKNOWN'))
                    proxy.write(meta['response_file'], {'http_status': response.status_code,
                        'headers': proxy.headers(response.headers), 'raw_body': response_text})
                    if meta['request_status'] == 'UNKNOWN':
                        proxy.stopped = True
                    reply = (response.status_code, response.content, content_type)
                except Exception as exc:
                    proxy.stopped = True
                    meta.update(request_status='UNKNOWN', error_type=type(exc).__name__)
                    proxy.write(meta['response_file'], {'status': 'UNKNOWN', 'error_type': type(exc).__name__})
                    reply = (502, b'{"error":"provider outcome UNKNOWN; worker stopped"}')
                finally:
                    meta['elapsed_seconds'] = time.monotonic() - started
                    proxy.write(f'calls/{seq:04d}-meta.json', meta)
                # Product completion may immediately snapshot proxy.calls. Finalize
                # timing and the durable receipt before exposing either response.
                # elapsed_seconds covers provider/receipt processing, not forwarding.
                self.reply(*reply)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self.server.server_address[1]

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


class EmbeddingLedger:
    """Observe real embedding HTTP, reusing only verified provider vectors."""
    def __init__(self, proxy, binding, cap):
        self.proxy, self.binding, self.cap = proxy, binding, cap
        self.calls, self.original, self.provider_requests = [], None, 0
        directory = os.environ.get('RSI_QUALITY_EMBEDDING_CACHE_DIR') if binding else None
        self.cache = Path(directory).resolve() if directory else None
        if self.cache:
            self.cache.mkdir(parents=True, exist_ok=True)

    def cache_key(self, text):
        return digest(canonical({'endpoint': self.binding['base_url'],
            'model': self.binding['model'], 'text_sha256': digest(text.encode())}).encode())

    @staticmethod
    def valid_vector(vector):
        return isinstance(vector, list) and bool(vector) and all(
            type(x) in (int, float) and math.isfinite(x) for x in vector)

    def read_cache(self, text):
        if not self.cache:
            return None
        path = self.cache / (self.cache_key(text) + '.json')
        if not path.exists():
            return None
        row = json.loads(path.read_text(encoding='utf-8'))
        if (row.get('schema_version') != 'rsi-provider-embedding-cache-v1'
                or row.get('cache_key') != self.cache_key(text)
                or row.get('endpoint') != self.binding['base_url']
                or row.get('model') != self.binding['model']
                or row.get('text_sha256') != digest(text.encode())
                or row.get('status') != 'RETURNED' or not row.get('source_response_sha256')
                or not self.valid_vector(row.get('embedding'))):
            raise ValueError('Untrusted or corrupt embedding cache record')
        return row

    def write_cache(self, text, vector, response_hash, provider_model):
        if not self.cache:
            return
        key = self.cache_key(text)
        row = {'schema_version': 'rsi-provider-embedding-cache-v1', 'cache_key': key,
            'endpoint': self.binding['base_url'], 'model': self.binding['model'],
            'text_sha256': digest(text.encode()), 'status': 'RETURNED',
            'embedding': vector, 'source_response_sha256': response_hash,
            'provider_model': provider_model}
        # The trusted controller owns this per-experiment directory. Artifacts
        # cannot choose it. Atomic publication never caches an UNKNOWN response.
        path = self.cache / (key + '.json')
        temporary = self.cache / (key + f'.{os.getpid()}.tmp')
        temporary.write_text(canonical(row), encoding='utf-8')
        os.replace(temporary, path)

    def install(self):
        import httpx
        self.original = httpx.AsyncClient.post
        ledger = self
        async def post(client, url, *args, **kwargs):
            if str(url) != ledger.binding['base_url'] + '/embeddings':
                return await ledger.original(client, url, *args, **kwargs)
            if ledger.proxy.stopped:
                raise RuntimeError('Prior UNKNOWN; no retry')
            payload = kwargs.get('json')
            if (not isinstance(payload, dict) or payload.get('model') != ledger.binding['model']
                    or not isinstance(payload.get('input'), list)
                    or not payload['input'] or not all(isinstance(x, str) for x in payload['input'])):
                raise ValueError('Actual embedding request has unexpected model/input')
            texts = payload['input']
            cached = [ledger.read_cache(text) for text in texts]
            missing = [index for index, row in enumerate(cached) if row is None]
            if missing and ledger.provider_requests >= ledger.cap:
                ledger.proxy.stopped = True
                raise RuntimeError('Embedding hard provider request cap; no retry')
            seq = len(ledger.calls) + 1
            meta = {'seq': seq, 'model_role': 'embedding',
                'configured_model': ledger.binding['model'], 'base_url': ledger.binding['base_url'],
                'record': dict(ledger.proxy.context) if ledger.proxy.context else None,
                'request_status': 'UNKNOWN' if missing else 'CACHE_HIT',
                'request_file': f'calls/embedding-{seq:04d}-request.json',
                'cache_hit_count': len(texts) - len(missing), 'cache_miss_count': len(missing),
                'actual_provider_requests': 1 if missing else 0,
                'provider_model': None, 'usage': None,
                'cache_keys': [ledger.cache_key(text) for text in texts],
                'cache_source_response_sha256': [row['source_response_sha256'] if row else None for row in cached]}
            ledger.calls.append(meta)
            actual_payload = {**payload, 'input': [texts[i] for i in missing]} if missing else None
            ledger.proxy.write(meta['request_file'], {'product_request': payload,
                'actual_request': actual_payload, 'transport': 'provider_http' if missing else 'verified_provider_vector_cache'})
            ledger.proxy.write('embedding-calls.json', ledger.calls)
            started = time.monotonic()
            try:
                if missing:
                    ledger.provider_requests += 1
                    response = await ledger.original(client, url, *args, **{**kwargs, 'json': actual_payload})
                    data = response.json()
                    rows = sorted(data.get('data', []), key=lambda row: row.get('index', -1))
                    vectors = [row.get('embedding') for row in rows]
                    valid = (len(vectors) == len(missing) and all(ledger.valid_vector(v) for v in vectors)
                        and [row.get('index') for row in rows] == list(range(len(missing))))
                    valid = valid and len({len(v) for v in vectors}) == 1
                    response_hash = digest(response.content)
                    meta.update(http_status=response.status_code, provider_model=data.get('model'),
                        usage=data.get('usage'), response_sha256=response_hash,
                        request_status='RETURNED' if response.status_code == 200 and valid else 'UNKNOWN')
                    if meta['request_status'] == 'UNKNOWN':
                        raise RuntimeError('Embedding outcome UNKNOWN; worker stopped without retry')
                    for index, vector in zip(missing, vectors):
                        ledger.write_cache(texts[index], vector, response_hash, data.get('model'))
                        cached[index] = {'embedding': vector, 'provider_model': data.get('model')}
                vectors = [row['embedding'] for row in cached]
                if len({len(v) for v in vectors}) != 1:
                    raise RuntimeError('Cached/provider embedding dimension mismatch')
                meta['dimensions'] = [len(vector) for vector in vectors]
                # Cached responses are explicitly local reuse, never represented
                # as a new provider call or new provider-reported token usage.
                return httpx.Response(200, json={'data': [{'index': i, 'embedding': vector}
                    for i, vector in enumerate(vectors)], 'model': ledger.binding['model']},
                    request=httpx.Request('POST', url))
            except Exception as exc:
                ledger.proxy.stopped = True
                meta.update(request_status='UNKNOWN', error_type=type(exc).__name__)
                raise
            finally:
                meta['elapsed_seconds'] = time.monotonic() - started
                ledger.proxy.write(f'calls/embedding-{seq:04d}-meta.json', meta)
                ledger.proxy.write('embedding-calls.json', ledger.calls)
        httpx.AsyncClient.post = post

    def summary(self):
        return {'actual_provider_requests': self.provider_requests,
            'cache_hit_texts': sum(call['cache_hit_count'] for call in self.calls),
            'cache_miss_texts': sum(call['cache_miss_count'] for call in self.calls),
            'cache_directory': str(self.cache) if self.cache else None,
            'usage_authority': 'actual provider responses only; cache hits have no new usage'}

    def result_fields(self):
        sent = [call for call in self.calls if call['actual_provider_requests']]
        known = all(isinstance(call.get('usage'), dict)
            and type(call['usage'].get('total_tokens')) is int
            and call['usage']['total_tokens'] >= 0 for call in sent)
        return {'embedding_requests': self.provider_requests,
            'embedding_cache_hits': sum(call['cache_hit_count'] for call in self.calls),
            'embedding_usage_tokens': sum(call['usage']['total_tokens'] for call in sent) if known else None,
            'embedding_known_usage_tokens': sum(call['usage']['total_tokens'] for call in sent
                if isinstance(call.get('usage'), dict) and type(call['usage'].get('total_tokens')) is int
                and call['usage']['total_tokens'] >= 0),
            'embedding_unknown_usage_call_count': sum(not (isinstance(call.get('usage'), dict)
                and type(call['usage'].get('total_tokens')) is int and call['usage']['total_tokens'] >= 0) for call in sent),
            'embedding_status': ('DISABLED' if self.binding is None else 'UNKNOWN' if any(
                call['request_status'] == 'UNKNOWN' for call in self.calls) else 'RETURNED'),
            'embedding_receipt_path': 'embedding-calls.json' if self.binding else None,
            'embedding_summary': self.summary()}

    def restore(self):
        if self.original:
            import httpx
            httpx.AsyncClient.post = self.original


async def seed_vectors(job, embedding, proxy):
    from app.services.embedding import EmbeddingService
    from app.services.retrieval import get_retrieval_service
    service = get_retrieval_service()
    embedder = EmbeddingService(**{'base_url': embedding['base_url'],
        'api_key': embedding['api_key'], 'model': embedding['model']})
    sources = {source['id']: source for source in job['sources']}
    for kb_id in sorted({source.get('kb_id', 'rsi-quality-kb') for source in sources.values()}):
        chunks = [c for c in job['chunks'] if sources[c['source_id']].get('kb_id', 'rsi-quality-kb') == kb_id]
        for offset in range(0, len(chunks), EMBEDDING_BATCH_SIZE):
            batch = chunks[offset:offset + EMBEDDING_BATCH_SIZE]
            for attempt in range(1, 3 if job.get('retry_once') else 2):
                proxy.context = {'phase': 'embedding_seed', 'kb_id': kb_id, 'batch_offset': offset,
                    'attempt_number': attempt, 'attempt_id': f'seed-{kb_id}-{offset}-attempt-{attempt}'}
                try:
                    vectors = await embedder.embed_documents([c['content'] for c in batch])
                    break
                except Exception:
                    if not job.get('retry_once') or attempt == 2:
                        raise
                    proxy.stopped = False
                finally:
                    proxy.context = None
            if len(vectors) != len(batch):
                raise RuntimeError('Incomplete embedding seed; worker stopped')
            service.vector_store.add_documents(kb_id, [{'id': c['id'], 'content': c['content'],
                'metadata': {'file_id': c['source_id']}} for c in batch], vectors)
        if service.vector_store.get_count(kb_id) != len(chunks):
            raise RuntimeError('Incomplete isolated Chroma seed')


def configure_product(output, proxy_port, embedding=None):
    """No app lifespan: no production DB/bootstrap/custom-model configuration."""
    if 'app.core.database' in sys.modules or 'app.core.config' in sys.modules:
        raise RuntimeError('Worker requires a fresh process')
    output = Path(output).resolve()
    os.environ.update(ENV='test', DATABASE_URL='sqlite:///:memory:',
        RAG_RUNTIME_SECRETS_FILE=str(output / 'never-read-secrets.env'),
        CHROMA_DB_DIR=str(output / ('chroma' if embedding else 'chroma-not-created')), UPLOAD_DIR=str(output / 'uploads'),
        IMAGE_DIR=str(output / 'images'), PDF_AUDIT_DIR=str(output / 'pdf-audit'),
        REDIS_URL='redis://127.0.0.1:1/0', MEMORY_REDIS_URL='redis://127.0.0.1:1/0',
        LLM_BASE_URL=f'http://127.0.0.1:{proxy_port}/v1', LLM_API_KEY='worker-loopback-only',
        LLM_MODEL=MODEL, LLM_ANSWER_MAX_TOKENS='4096', RAG_GROUNDED_GENERATION='true',
        RAG_MODEL_ROUTER_MODE='OFF', RAG_ROUTER_ALLOW_MODEL_CALLS='false',
        LANGFUSE_ENABLED='false', RAG_OBSERVABILITY_ENABLED='false', RAG_METRICS_ENABLED='false',
        RAG_SNAPSHOTS_ENABLED='false', RAG_PDF_TABLE_GROUNDING='false',
        EMBEDDING_BASE_URL='http://127.0.0.1:1', EMBEDDING_API_KEY='disabled', EMBEDDING_MODEL='disabled',
        VLM_BASE_URL='http://127.0.0.1:1', VLM_API_KEY='disabled', VLM_MODEL='disabled',
        TAVILY_API_KEY='', TAVILY_API_KEY_FILE='', DEFAULT_LLM_MODEL_ID='',
        DEFAULT_EMBEDDING_MODEL_ID='', DEFAULT_VLM_MODEL_ID='', AGENT_GRADING_TIMEOUT_SECONDS='60',
        CELERY_WORKER_CONCURRENCY='1', CHUNK_SIZE='500', CHUNK_OVERLAP='50',
        MAX_CHAT_HISTORY_ROUNDS='20', BM25_K1='1.5', BM25_B='0.75', BM25_TOP_K='50',
        ANONYMIZED_TELEMETRY='False',
        PYTHONDONTWRITEBYTECODE='1', JIEBA_TMP_DIR=str(output), NO_PROXY='127.0.0.1,localhost')
    if embedding:
        os.environ.update(EMBEDDING_BASE_URL=embedding['base_url'],
            EMBEDDING_MODEL=embedding['model'], EMBEDDING_API_KEY=embedding['api_key'])
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / 'backend'))
    from app.core import config
    # Disable both .env sources before the first Settings instance exists.
    config.Settings.model_config['env_file'] = None
    config.get_settings.cache_clear()


def build_product(job, output, preference_context):
    from fastapi import FastAPI
    from sqlmodel import SQLModel, Session
    from app.core.database import engine
    from app.models import KnowledgeBase, FileDocument, FileStatus, DocumentChunk
    from app.services.bm25 import BM25Index
    from app.services import retrieval, memory, memory_commands, llm, grounded_generation
    from app.services.vector_store import VectorStoreService
    from app.services.runtime_settings import PersistedSystemSettings, RuntimeValues, _encode, get_runtime_settings
    try:
        import jieba
    except ImportError:
        jieba = None  # Preserve the product tokenizer's existing fallback.
    if jieba is not None:
        # Existing tokenizer, with any generated cache scoped to this output.
        jieba.dt.tmp_dir = str(output)
    SQLModel.metadata.create_all(engine)
    sources = {source['id']: source for source in job['sources']}
    kbs = sorted({source.get('kb_id', 'rsi-quality-kb') for source in sources.values()})
    index = BM25Index()
    hybrid = search_config(job)['search_mode'] == 'hybrid'
    with Session(engine) as session:
        # Register the table before create_all, and explicitly seed isolated values.
        values = RuntimeValues(default_llm_model='sys_llm',
            default_embedding_model='sys_embedding' if hybrid else None).model_dump()
        session.add(PersistedSystemSettings(data=_encode(values, 1)))
        session.commit()
        snapshot = get_runtime_settings(session)
        if snapshot.degraded or snapshot.config_error:
            raise RuntimeError('Isolated runtime settings are not healthy')
        for kb in kbs:
            count = sum(sources[c['source_id']].get('kb_id', 'rsi-quality-kb') == kb for c in job['chunks'])
            session.add(KnowledgeBase(id=kb, name=kb, embedding_model='sys_embedding' if hybrid else 'disabled', vlm_model='disabled', chunk_count=count))
        session.commit()
        for source in sources.values():
            path = Path(source['path']) if source.get('path') else Path(output) / 'unmaterialized-sources' / (source.get('name') or source['id'] + '.txt')
            session.add(FileDocument(id=source['id'], kb_id=source.get('kb_id', 'rsi-quality-kb'),
                name=source.get('name') or path.name, local_path=str(path), size=path.stat().st_size if path.is_file() else 0,
                status=FileStatus.READY, progress=100))
        session.commit()
        for ordinal, chunk in enumerate(job['chunks']):
            session.add(DocumentChunk(id=chunk['id'], file_id=chunk['source_id'], content=chunk['content'],
                page_number=chunk.get('page_number'), original_index=ordinal))
        session.commit()
        for chunk in job['chunks']:
            index.index_chunk(session, chunk['id'], chunk['content'], sources[chunk['source_id']].get('kb_id', 'rsi-quality-kb'))
        session.commit()

    service = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    if hybrid:
        # This is the actual product Chroma adapter; no synthetic vectors or
        # replacement _embed_query_for_kb are installed in live execution.
        service.vector_store = VectorStoreService()
    else:
        def forbid_vector(*args, **kwargs):
            raise RuntimeError('Vector/Chroma access forbidden in frozen fulltext worker')
        VectorStoreService.get_client = classmethod(forbid_vector)
        class ForbiddenVector:
            def __getattr__(self, name):
                raise RuntimeError('Unexpected vector operation: ' + name)
        service.vector_store = ForbiddenVector()
    service.bm25_index = index
    original_search = service.search
    async def recorded_search(*args, **kwargs):
        hits, metrics = await original_search(*args, **kwargs)
        preference_context.setdefault('retrieval_calls', []).append({
            'search_mode': kwargs.get('search_mode', 'hybrid'),
            'kb_ids': kwargs.get('kb_ids', args[2] if len(args) > 2 else None),
            'candidate_ids': [hit['id'] for hit in hits], 'metrics': metrics})
        return hits, metrics
    service.search = recorded_search
    retrieval._retrieval_service = service

    class FrozenMemory:
        async def get_preferences(self, scope=None):
            prefs = preference_context['case'].get('preferences', {})
            return memory.PreferencesSnapshot(values=memory.PreferenceValues(**prefs),
                sources={field: 'explicit' if field in prefs else 'default' for field in memory.FIELDS})

        def __getattr__(self, name):
            raise RuntimeError('Preference writes forbidden in fixed-case worker: ' + name)
    memory.get_memory_service = lambda: FrozenMemory()
    resolver = memory_commands.resolve_display_preferences
    def resolve(snapshot, question):
        if question != preference_context['case']['question']:
            raise RuntimeError('Incorrect current-turn question scope')
        policy, values, origins = resolver(snapshot, question)
        addendum = job.get('artifact', {}).get('memory_addendum', '')
        applied = bool(policy and addendum and preference_context['case'].get('preferences'))
        if applied:
            policy += '\n\n' + addendum
        preference_context['resolution'] = {'question': question, 'policy': policy, 'values': values,
            'sources': origins, 'memory_addendum_applied': applied,
            'snapshot': snapshot.model_dump()}
        return policy, values, origins
    memory_commands.resolve_display_preferences = resolve
    original_prompt_builder = grounded_generation.build_grounded_prompt
    def prompt_builder(evidence, *, addendum=''):
        # Capture the real rendering input, then confirm it at the outgoing wire
        # boundary. Selected citations are never treated as proof of delivery.
        preference_context['grounded_evidence'] = evidence
        return original_prompt_builder(evidence, addendum=addendum)
    grounded_generation.build_grounded_prompt = prompt_builder
    original_init = llm.LLMService.__init__
    def factory_init(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._grounded_prompt_addendum = job.get('artifact', {}).get('prompt_addendum', '')
    llm.LLMService.__init__ = factory_init

    from app.api.chat import router
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')
    return app, engine, kbs


def parse_sse(text):
    events = []
    for block in text.replace('\r\n', '\n').split('\n\n'):
        names = [line[6:].strip() for line in block.splitlines() if line.startswith('event:')]
        values = [line[5:].strip() for line in block.splitlines() if line.startswith('data:')]
        if names and values:
            events.append({'event': names[0], 'data': json.loads('\n'.join(values))})
    return events


async def execute_record(job, client, proxy, embedding_ledger, embedding, context, cases, kb_ids, workflow, ordinal, record, attempt):
    case = cases[record['case_id']]
    context.clear()
    context['case'] = case
    result = {**record, 'ordinal': ordinal, 'attempt_number': attempt,
        'attempt_id': f'record-{ordinal:04d}-attempt-{attempt}', 'status': 'NOT RUN', 'answer': '', 'citations': [],
        'elapsed_seconds': None, 'provider_calls': [], 'provider_call_count': 0,
        'usage': None, 'usage_status': 'UNKNOWN', 'final_context_chars': None,
        'final_context_bytes': None, 'final_request': None}
    if proxy.stopped or len(proxy.calls) >= proxy.cap:
        result['reason'] = 'Prior UNKNOWN/deadline or hard request cap; no retry'
        return result
    payload = {'messages': [*case.get('history', []), {'role': 'user', 'content': case['question']}],
        'kb_ids': case.get('kb_ids', kb_ids), 'stream': True, 'mode': workflow.get('mode', 'chat'),
        'use_rewrite': workflow.get('use_rewrite', False), 'web_search_enabled': False, **search_config(job)}
    proxy.context = {**record, 'attempt_number': attempt,
        'attempt_id': f'record-{ordinal:04d}-attempt-{attempt}'}
    started = time.monotonic()
    proxy.deadline = started + DEADLINE
    before = len(proxy.calls)
    embedding_before = len(embedding_ledger.calls)
    try:
        response = await asyncio.wait_for(client.post('/api/v1/chat/completions', json=payload), DEADLINE)
        result.update(http_status=response.status_code, raw_sse=response.text, events=parse_sse(response.text))
        result['status'] = 'RETURNED' if response.status_code == 200 else 'FAILED'
        for event in result['events']:
            if event['event'] == 'answer_chunk':
                result['answer'] += event['data'].get('content') or ''
            elif event['event'] == 'rag_result':
                result['citations'] = event['data'].get('citations') or []
            elif event['event'] == 'done':
                result['done'] = event['data']
            elif event['event'] == 'error':
                result['status'] = event['data'].get('status', 'FAILED')
                result['error'] = event['data']
        if not any(event['event'] in ('done', 'error') for event in result['events']):
            result['status'] = 'UNKNOWN'
            proxy.stopped = True
    except (TimeoutError, asyncio.TimeoutError):
        result.update(status='UNKNOWN', reason='Question deadline exceeded; no retry')
        proxy.stopped = True
    except Exception as exc:
        result.update(status='UNKNOWN' if len(proxy.calls) > before else 'FAILED', error_type=type(exc).__name__)
        proxy.stopped = True
    result['elapsed_seconds'] = time.monotonic() - started
    calls = proxy.calls[before:]
    result.update(request_payload=payload, preference_resolution=context.get('resolution'),
        provider_calls=[dict(call) for call in calls], provider_call_count=len(calls),
        embedding_calls=[dict(call) for call in embedding_ledger.calls[embedding_before:]],
        retrieval_config=search_config(job), retrieval_calls=context.get('retrieval_calls', []))
    usage_keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    if calls and all(isinstance(call.get('usage'), dict) and all(
            type(call['usage'].get(key)) is int and call['usage'][key] >= 0
            for key in usage_keys) for call in calls):
        result['usage'] = {key: sum(call['usage'][key] for call in calls) for key in usage_keys}
        result['usage_status'] = 'PROVIDER_REPORTED'
    final = next((call for call in reversed(calls) if call.get('is_grounded') or call.get('has_tool_role')), None)
    result['final_request'] = ({key: final.get(key) for key in ('seq', 'request_file', 'context_chars',
        'context_bytes', 'context_status', 'context_measurement_scope', 'prompt_messages_chars',
        'prompt_messages_bytes', 'request_chars', 'request_bytes', 'finish_reason', 'usage', 'provider_model',
        'generated_content', 'request_status')} if final else None)
    if final:
        result['final_context_chars'] = final.get('context_chars')
        result['final_context_bytes'] = final.get('context_bytes')
        result['final_context_status'] = final.get('context_status')
        result['final_prompt_messages_chars'] = final.get('prompt_messages_chars')
        result['final_prompt_messages_bytes'] = final.get('prompt_messages_bytes')
        if final.get('context_status') == 'VERIFIED_IN_ACTUAL_REQUEST':
            result['actual_grounded_evidence'] = context['grounded_evidence']
    if embedding:
        searches = result['retrieval_calls']
        result['hybrid_execution_status'] = ('VERIFIED_REAL_PRODUCT_HYBRID' if searches and
            all(not call['metrics'].get('fallback_used') and call['search_mode'] == 'hybrid'
                for call in searches) and result['embedding_calls'] else 'INCOMPLETE_OR_DEGRADED')
        if result['hybrid_execution_status'] != 'VERIFIED_REAL_PRODUCT_HYBRID' and result['status'] == 'RETURNED':
            result['status'] = 'FAILED'
    if any(call['request_status'] == 'UNKNOWN' for call in calls + result['embedding_calls']):
        result['status'] = 'UNKNOWN'
        proxy.stopped = True
    if any(call.get('request_status') == 'FAILED' for call in calls) and result['status'] == 'RETURNED':
        result['status'] = 'FAILED'
    return result


def merge_attempts(attempts):
    result = dict(attempts[-1])
    result['attempts'] = attempts
    result['attempt_count'] = len(attempts)
    result['retried'] = len(attempts) > 1
    result['initial_status'] = attempts[0]['status']
    result['retry_status'] = attempts[1]['status'] if len(attempts) > 1 else 'NOT RUN'
    calls = [call for attempt in attempts for call in attempt.get('provider_calls', [])]
    result['provider_calls'] = calls
    result['provider_call_count'] = len(calls)
    result['embedding_calls'] = [call for attempt in attempts for call in attempt.get('embedding_calls', [])]
    result['elapsed_seconds'] = sum(attempt.get('elapsed_seconds') or 0 for attempt in attempts)
    keys = ('prompt_tokens', 'completion_tokens', 'total_tokens')
    known = [call for call in calls if isinstance(call.get('usage'), dict) and all(
        type(call['usage'].get(key)) is int and call['usage'][key] >= 0 for key in keys)]
    result['known_usage'] = {key: sum(call['usage'][key] for call in known) for key in keys}
    result['usage'] = result['known_usage'] if calls and len(known) == len(calls) else None
    result['usage_status'] = ('PROVIDER_REPORTED' if result['usage'] is not None else
        'PARTIAL_PROVIDER_REPORTED' if known else 'UNKNOWN')
    result['unknown_usage_call_count'] = len(calls) - len(known)
    return result


async def execute(job, output, key):
    records = validate(job)
    proxy = RecordingProxy(output, key, job['max_model_requests'])
    embedding = embedding_environment(job)
    configure_product(output, proxy.start(), embedding)
    context = {}
    proxy.product_context = context
    engine = None
    results = []
    embedding_ledger = None
    try:
        app, engine, kb_ids = build_product(job, output, context)
        embedding_ledger = EmbeddingLedger(proxy, embedding, job.get('max_embedding_requests', 0))
        if embedding:
            embedding_ledger.install()
            try:
                await seed_vectors(job, embedding, proxy)
            except Exception as exc:
                return {'status': 'INCOMPLETE', 'schema_version': SCHEMA,
                    'results': [{**record, 'status': 'NOT RUN', 'reason': 'Embedding seed failed; authorized attempts exhausted',
                        'error_type': type(exc).__name__} for record in records],
                    'model_requests': len(proxy.calls), 'max_model_requests': proxy.cap,
                    'retry_once': job.get('retry_once', False), 'retrieval_config': search_config(job), **embedding_ledger.result_fields()}
        import httpx
        from app.services.memory_commands import parse_preference_commands
        # Recognized active commands would mutate or split the intended case.
        if any(parse_preference_commands(case['question'])[0] for case in job['cases']):
            raise ValueError('Active preference commands are outside fixed-case scope')
        cases = {case['id']: case for case in job['cases']}
        workflow = job.get('artifact', {}).get('workflow', {})
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://rsi-worker') as client:
            for ordinal, record in enumerate(records, 1):
                attempts = []
                for attempt in range(1, 3 if job.get('retry_once') else 2):
                    if job.get('retry_once'):
                        # This is the only retry owner. HTTP clients retain zero
                        # hidden retries and completed records never re-enter.
                        proxy.stopped = False
                    result = await execute_record(job, client, proxy, embedding_ledger, embedding,
                        context, cases, kb_ids, workflow, ordinal, record, attempt)
                    attempts.append(result)
                    proxy.write(f'record-{ordinal:04d}-attempt-{attempt}.json', result)
                    if result['status'] == 'RETURNED':
                        break
                result = merge_attempts(attempts)
                results.append(result)
                proxy.write(f'record-{ordinal:04d}.json', result)
                # Write an incremental ledger: interruption never erases earlier UNKNOWNs.
                proxy.write('results.json', {'schema_version': SCHEMA, 'results': results,
                    'model_requests': len(proxy.calls), 'max_model_requests': proxy.cap})
                proxy.context = None
        return {'status': 'RETURNED' if len(results) == len(records) and all(r['status'] == 'RETURNED' for r in results) else 'INCOMPLETE',
                'schema_version': SCHEMA, 'results': results, 'model_requests': len(proxy.calls),
                'model_known_usage': {key: sum(r.get('known_usage', {}).get(key, 0) for r in results)
                    for key in ('prompt_tokens', 'completion_tokens', 'total_tokens')},
                'model_unknown_usage_call_count': sum(r.get('unknown_usage_call_count', 0) for r in results),
                'max_model_requests': proxy.cap, 'retry_once': job.get('retry_once', False),
                'retry_summary': {'retried_records': sum(r.get('retried', False) for r in results),
                    'record_attempts': sum(r.get('attempt_count', 1) for r in results),
                    'exhausted_records': sum(r['status'] != 'RETURNED' for r in results)},
                'retrieval_config': search_config(job), **embedding_ledger.result_fields(),
                'model_roles': {'answer_and_rewrite': {'binding': 'sys_llm', 'model': MODEL,
                    'transport': 'recorded loopback to ' + PROVIDER},
                    'embedding': {'binding': 'sys_embedding', 'model': embedding['model'],
                        'base_url': embedding['base_url']} if embedding else None,
                    'rerank': 'DISABLED', 'vlm': 'DISABLED'},
                'observability': 'Langfuse disabled; local request, SSE and retrieval artifacts only',
                'usage_authority': 'raw provider response only; missing usage remains UNKNOWN',
                'quality_verdict': 'NOT RUN; external gold evaluator only',
                'resources': {'database': 'disposed in-memory SQLite', 'bm25_seed_count': 1,
                              'chroma': str(Path(output) / 'chroma') if embedding else 'NOT CREATED',
                              'vector_seed_count': len(job['chunks']) if embedding else 0,
                              'corpus_copy': 'NOT CREATED'}}
    finally:
        if embedding_ledger:
            embedding_ledger.restore()
        proxy.stop()
        proxy.write('provider-calls.json', proxy.calls)
        if engine is not None:
            engine.dispose()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live', action='store_true')
    args = parser.parse_args(argv)
    job = json.loads(args.input.read_text(encoding='utf-8-sig'))
    check = preflight(job)
    # Exclusive arm output prevents record overwrite, accidental resume or rerun.
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    (output / 'preflight.json').write_text(json.dumps(check, ensure_ascii=False, indent=2), encoding='utf-8')
    if not args.live:
        print(canonical(check))
        return 0
    embedding_environment(job)  # Validate all real-provider identities before any sends.
    key = os.environ.get('SILICONFLOW_API_KEY') or os.environ.get('M2_AB_API_KEY')
    if not key:
        raise ValueError('SILICONFLOW_API_KEY or M2_AB_API_KEY required; zero sends')
    with (output / 'product.log').open('w', encoding='utf-8') as log, redirect_stdout(log), redirect_stderr(log):
        report = asyncio.run(execute(job, output, key))
    (output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2).replace(key, '[REDACTED]'), encoding='utf-8')
    print(canonical({'status': report['status'], 'model_requests': report['model_requests'], 'output': str(output)}))
    return 0 if report['status'] == 'RETURNED' else 2


if __name__ == '__main__':
    raise SystemExit(main())

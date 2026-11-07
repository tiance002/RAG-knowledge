"""Read-only frozen corpus preflight. No provider or model calls are implemented."""
import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

EXPECTED_MANIFEST_SHA256 = '7b5524c8bfd1e8bbbef6298f6950fa4a6cc2462fe336789ad66639417e5e75d3'


def inspect(manifest_path, db_path):
    raw = Path(manifest_path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_MANIFEST_SHA256:
        raise ValueError('Frozen manifest hash mismatch')
    manifest = json.loads(raw)
    chunks = manifest['chunks']
    expected = {x['id']: (x['source_id'], x['content']) for x in chunks}
    if len(chunks) != 80 or len(expected) != 80 or manifest.get('authorized') is not True:
        raise ValueError('Frozen authorized 80-chunk corpus required')
    db = Path(db_path).resolve(strict=True)
    with sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True) as con:
        rows = list(con.execute('SELECT id, file_id, content FROM document_chunks'))
    actual = {row[0]: (row[1], row[2]) for row in rows}
    if len(rows) != 80 or actual != expected:
        raise ValueError('Database differs from frozen corpus')
    return {'corpus': 'PASS', 'manifest_sha256': digest, 'chunk_count': len(rows),
            'database': str(db), 'model_calls': 0, 'execution': 'BLOCKED',
            'blockers': ['provider account model availability and pricing not checked',
                         'actual Hybrid uses weighted normalized sum, not RRF',
                         'rerank error can fall back to chat scoring',
                         'isolated vector index and safe one-shot provider guard not implemented',
                         'global historical budget and UNKNOWN ledger must be reconciled']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--source-db', required=True)
    args = parser.parse_args()
    print(json.dumps(inspect(args.manifest, args.source_db), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

# This guard is deliberately not wired to an execute command yet. A process that
# loses its response retains the pre-send marker, so the identity cannot replay.
ALLOWED_MODELS = {
    '/v1/embeddings': 'BAAI/bge-m3',
    '/v1/rerank': 'BAAI/bge-reranker-v2-m3',
    '/v1/chat/completions': 'Qwen/Qwen3.5-4B',
}


class GuardStop(BaseException):
    pass


class DurableCalls:
    def __init__(self, directory):
        import threading
        self.directory = Path(directory)
        self.directory.mkdir(exist_ok=False)
        self.lock = threading.Lock()
        self.stopped = False
        self.count = 0

    def begin(self, identity, request):
        from urllib.parse import urlsplit
        import os
        parsed = urlsplit(str(request.url))
        try:
            payload = json.loads(request.content)
        except Exception:
            self.stopped = True
            raise GuardStop('Malformed provider request') from None
        if (self.stopped or request.method != 'POST' or parsed.scheme != 'https'
                or parsed.netloc != 'api.siliconflow.cn' or parsed.query or parsed.fragment
                or parsed.path not in ALLOWED_MODELS
                or payload.get('model') != ALLOWED_MODELS[parsed.path]):
            self.stopped = True
            raise GuardStop('Unapproved provider endpoint or model')
        if not isinstance(identity, str) or not identity:
            self.stopped = True
            raise GuardStop('Missing identity')
        key = hashlib.sha256(identity.encode()).hexdigest()
        body = request.content
        record = {'identity': identity, 'state': 'UNKNOWN_PENDING', 'endpoint': parsed.path,
                  'model': payload['model'], 'request_sha256': hashlib.sha256(body).hexdigest()}
        with self.lock:
            marker = self.directory / (key + '.started.json')
            try:
                with marker.open('xb') as stream:
                    stream.write(json.dumps(record, sort_keys=True).encode())
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                self.stopped = True
                raise GuardStop('Request identity already started; no replay') from None
            with (self.directory / (key + '.request.json')).open('xb') as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            self.count += 1
        return key

    def finish(self, key, response=None, error=None, invalid=None):
        import os
        if error is not None or response is None or response.status_code != 200 or invalid is not None:
            self.stopped = True
            state = 'UNKNOWN' if error is not None else ('FAILED_RESPONSE' if invalid is not None else 'FAILED_HTTP')
        else:
            state = 'COMPLETED'
        record = {'state': state, 'status_code': response.status_code if response is not None else None,
                  'error_type': type(error).__name__ if error is not None else None, 'invalid_reason': invalid}
        if response is not None:
            with (self.directory / (key + '.response.body')).open('xb') as stream:
                stream.write(response.content)
                stream.flush()
                os.fsync(stream.fileno())
        with (self.directory / (key + '.finished.json')).open('xb') as stream:
            stream.write(json.dumps(record, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        if state != 'COMPLETED':
            raise GuardStop('Provider failed or outcome unknown; no retry')


def validate_provider_response(request, response):
    """Reject invalid provider payloads before application fallback can run."""
    import math
    path = request.url.path
    if response.status_code != 200 or path == '/v1/chat/completions':
        return
    try:
        data, sent = response.json(), json.loads(request.content)
        if path == '/v1/rerank':
            rows, docs = data['results'], sent['documents']
            if not isinstance(rows, list) or not rows:
                raise ValueError('empty rerank')
            indexes = [row['index'] for row in rows]
            if len(set(indexes)) != len(indexes) or any(type(i) is not int or not 0 <= i < len(docs) for i in indexes):
                raise ValueError('invalid rerank indices')
            for row in rows:
                score = row.get('relevance_score', row.get('score'))
                if type(score) not in (int, float) or not math.isfinite(score):
                    raise ValueError('invalid rerank score')
        elif path == '/v1/embeddings':
            rows, inputs = data['data'], sent['input']
            count = len(inputs) if isinstance(inputs, list) else 1
            if not isinstance(rows, list) or len(rows) != count:
                raise ValueError('invalid embedding count')
            indexes = [row['index'] for row in rows]
            if sorted(indexes) != list(range(count)):
                raise ValueError('invalid embedding indices')
            vectors = [row['embedding'] for row in rows]
            if not all(isinstance(v, list) and v for v in vectors) or len({len(v) for v in vectors}) != 1:
                raise ValueError('invalid embedding dimensions')
            if any(type(n) not in (int, float) or not math.isfinite(n) for v in vectors for n in v):
                raise ValueError('invalid embedding values')
    except Exception:
        raise GuardStop('Invalid provider response; fallback blocked') from None


class ProviderAdapter:
    """Transport-boundary adapter; inner transports may be mock or real HTTPX transports."""
    def __init__(self, guard, async_inner, sync_inner):
        import itertools
        self.guard, self.async_inner, self.sync_inner = guard, async_inner, sync_inner
        self.serial = itertools.count(1)
        self.context = 'unassigned'

    def _begin(self, request):
        identity = f'{self.context}:call-{next(self.serial):06d}'
        return self.guard.begin(identity, request)

    async def async_send(self, request):
        key = self._begin(request)
        try:
            response = await self.async_inner.handle_async_request(request)
            await response.aread()
            response.request = request
        except BaseException as exc:
            self.guard.finish(key, error=exc)
            raise
        try:
            validate_provider_response(request, response)
        except GuardStop:
            self.guard.finish(key, response=response, invalid='invalid response payload')
        self.guard.finish(key, response=response)
        return response

    def sync_send(self, request):
        key = self._begin(request)
        try:
            response = self.sync_inner.handle_request(request)
            response.read()
            response.request = request
        except BaseException as exc:
            self.guard.finish(key, error=exc)
            raise
        try:
            validate_provider_response(request, response)
        except GuardStop:
            self.guard.finish(key, response=response, invalid='invalid response payload')
        self.guard.finish(key, response=response)
        return response

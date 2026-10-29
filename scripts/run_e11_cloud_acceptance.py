"""Isolated synthetic SSE trace harness. Never uses a real model or production database."""
import argparse
import asyncio
import contextvars
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
import uuid

ROOT = Path(__file__).resolve().parents[1]
QUESTION = 'Synthetic test: what is the sample value?'
SOURCE = 'Synthetic source: the sample value is 42.'
ANSWER = 'The sample value is 42. [1]'
SENTINEL = 'UNAUTHORIZED_E11_SENTINEL_DO_NOT_EXPORT_7c184e'
SOURCE_SHA = hashlib.sha256(SOURCE.encode()).hexdigest()
DATASET = 'e11-synthetic-v1'
QID = 'synthetic-1'


def module_available(name):
    try:
        return importlib.util.find_spec(name) is not None
    except ValueError:
        return name in sys.modules  # Explicit test doubles may not have a spec.


def preflight():
    required = ('fastapi', 'sqlmodel', 'httpx', 'langchain_core', 'openai')
    present = {name: module_available(name) for name in required}
    return {'mode': 'PREFLIGHT', 'network_requests': 0, 'provider_requests': 0,
            'required_import_specs': present, 'chromadb_stub': not module_available('chromadb'),
            'langfuse_installed': module_available('langfuse'),
            'snapshot_supported': os.name == 'posix', 'source_sha256': SOURCE_SHA,
            'status': 'READY' if all(present.values()) else 'BLOCKED'}


class NetworkGuard:
    """Allow only resolved Langfuse host IPs; block model hosts and other sockets."""
    def __init__(self, cloud):
        self.cloud = cloud
        self.allowed_ips = set()
        self.denied = 0
    def __enter__(self):
        self.original_dns, self.original_connect, self.original_connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
        guard = self
        def dns(host, port, *args, **kwargs):
            if not guard.cloud or host != 'us.cloud.langfuse.com' or int(port) != 443:
                guard.denied += 1
                raise OSError('network denied by synthetic acceptance guard')
            rows = guard.original_dns(host, port, *args, **kwargs)
            guard.allowed_ips.update(row[4][0] for row in rows)
            return rows
        def connect(sock, address):
            if not guard.cloud or not isinstance(address, tuple) or address[0] not in guard.allowed_ips or address[1] != 443:
                guard.denied += 1
                raise OSError('network denied by synthetic acceptance guard')
            return guard.original_connect(sock, address)
        def connect_ex(sock, address):
            connect(sock, address)
            return 0
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = dns, connect, connect_ex
        return self
    def __exit__(self, *args):
        socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = self.original_dns, self.original_connect, self.original_connect_ex


def execute(output, cloud=False):
    if cloud and (os.getenv('LANGFUSE_BASE_URL') != 'https://us.cloud.langfuse.com' or
                  not all(os.getenv(k) for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'))):
        raise RuntimeError('Cloud requires exact US endpoint and credentials in environment')
    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    if os.name == 'posix':
        os.chmod(output, 0o700)
    os.environ.update(RAG_OBSERVABILITY_ENABLED='true', RAG_SNAPSHOTS_ENABLED='true',
                      RAG_SNAPSHOT_DIR=str(output / 'snapshots'), RAG_OBSERVATION_LOG=str(output / 'events.jsonl'),
                      RAG_GROUNDED_GENERATION='true', LANGFUSE_ENABLED='true' if cloud else 'false')
    sys.path.insert(0, str(ROOT / 'backend'))
    if not module_available('chromadb'):
        sys.modules['chromadb'] = MagicMock()
        sys.modules['chromadb.config'] = MagicMock()
    import httpx
    from fastapi import FastAPI
    from sqlalchemy.pool import StaticPool
    from sqlmodel import Session, SQLModel, create_engine
    spec = importlib.util.spec_from_file_location('e11_chat_route', ROOT / 'backend' / 'app' / 'api' / 'chat.py')
    route = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = route
    spec.loader.exec_module(route)
    router = route.router
    from app.core.database import get_session
    from app.models import KnowledgeBase, FileDocument, DocumentChunk, FileStatus
    from app.services import chat, retrieval, observability as obs
    from app.services.trace_schema import capture_scope

    source_path = output / 'synthetic-source.txt'
    source_path.write_text(SOURCE, encoding='utf-8')
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != SOURCE_SHA:
        raise RuntimeError('synthetic source hash mismatch')
    engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(KnowledgeBase(id='synthetic-kb', name='Synthetic', embedding_model='none', vlm_model='none', chunk_count=1))
        session.add(FileDocument(id='synthetic-file', kb_id='synthetic-kb', name='synthetic.txt', local_path=str(source_path), size=len(SOURCE), status=FileStatus.READY))
        session.add(DocumentChunk(id='synthetic-chunk', file_id='synthetic-file', content=SOURCE))
        session.commit()
    chat.engine = engine
    calls = {'fake_provider': 0, 'bm25': 0}
    class BM25:
        def search(self, **kwargs):
            calls['bm25'] += 1
            assert kwargs['query'] == QUESTION and kwargs['kb_id'] == 'synthetic-kb'
            return [{'chunk_id': 'synthetic-chunk', 'bm25_score': 2.0}]
    class FakeCompletion:
        async def create(self, **kwargs):
            calls['fake_provider'] += 1
            assert SOURCE in json.dumps(kwargs['messages']) and QUESTION in json.dumps(kwargs['messages'])
            # Explicitly unauthorized payload must remain locally referenced/redacted in exported metadata.
            with capture_scope(run_id='unauthorized', qid='not-allowlisted', dataset_version=DATASET,
                               source_sha256s=[SOURCE_SHA], manifest=manifest, capture_mode='EVAL'):
                obs.event('synthetic.unauthorized', output=obs.capture(SENTINEL, content_role='provider_response', allow_cloud=True))
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=ANSWER), finish_reason='stop')], usage=None, model='synthetic-fake')
    fake_llm = SimpleNamespace(model='synthetic-fake', _get_client=lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletion())))
    rservice = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    rservice.bm25_index = BM25()
    def init(self):
        self.llm_service = fake_llm
        self.retrieval_service = rservice
    chat.ChatService.__init__ = init
    app = FastAPI()
    app.include_router(router)
    def session_override():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_session] = session_override
    manifest = {'entries': [{'dataset_version': DATASET, 'qid': QID, 'source_sha256': SOURCE_SHA, 'cloud_allowed': True}]}
    run_id = 'synthetic-' + uuid.uuid4().hex
    async def request():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url='http://synthetic.local') as client:
            return await client.post('/chat/completions', json={'messages': [{'role': 'user', 'content': QUESTION}],
                'kb_ids': ['synthetic-kb'], 'mode': 'chat', 'search_mode': 'fulltext', 'top_k': 5,
                'use_rewrite': False, 'rerank_enabled': False, 'context_window': 0})
    with asyncio.Runner() as runner, NetworkGuard(cloud) as guard:
        with capture_scope(run_id=run_id, attempt_id='attempt-1', qid=QID, dataset_version=DATASET,
                           source_sha256s=[SOURCE_SHA], manifest=manifest, capture_mode='EVAL', debug_details=True):
            response = runner.run(request(), context=contextvars.copy_context())
        obs.flush_observations()
        # Stop the SDK worker while the network guard is still installed.
        if obs._client is not None:
            obs._client.shutdown()
    (output / 'sse.txt').write_text(response.text, encoding='utf-8')
    rows = [json.loads(line) for line in (output / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    stages = {r.get('stage') for r in rows}
    trace_ids = sorted({r.get('trace_id') for r in rows if r.get('stage') == 'qa.request' and r.get('event') == 'start'})
    roots = [r for r in rows if r.get('stage') == 'qa.request' and r.get('event') == 'start']
    identity_verified = (len(roots) == 1 and roots[0].get('run_id') == run_id
                         and roots[0].get('qid') == QID and roots[0].get('dataset_version') == DATASET)
    final_records = [r for r in rows if r.get('stage') == 'observation.io'
                     and isinstance(r.get('output'), dict) and r['output'].get('content_role') == 'final_answer']
    root_metadata = final_records[-1].get('metadata', {}) if final_records else {}
    contract_fields = {'mode', 'kb_scope', 'effective_config', 'code_hash', 'index_fingerprint', 'capture_mode'}
    root_contract_verified = (contract_fields <= root_metadata.keys()
                              and root_metadata.get('mode') == 'chat'
                              and root_metadata.get('capture_mode') == 'EVAL'
                              and root_metadata.get('effective_config', {}).get('chat_model') == 'synthetic-fake')
    payload_binding_verified = all(
        r.get(direction, {}).get('observation_id') == r.get('observation_id')
        for r in rows if r.get('stage') in ('grounded.request.sent', 'grounded.response')
        for direction in ('input', 'output') if isinstance(r.get(direction), dict)) if cloud else True
    valid = (response.status_code == 200 and 'event: done' in response.text and 'event: error' not in response.text
             and ANSWER in response.text and guard.denied == 0 and calls == {'fake_provider': 1, 'bm25': 1}
             and {'retrieval.bm25', 'rag.candidate_transition', 'grounded.request.sent', 'grounded.response', 'observation.io'} <= stages
             and SENTINEL not in (output / 'events.jsonl').read_text(encoding='utf-8') and identity_verified
             and root_contract_verified and payload_binding_verified)
    result = {'status': 'PASS' if valid else 'FAIL', 'run_id': run_id, 'trace_ids': trace_ids,
              'http_status': response.status_code, 'synthetic_fake_provider_calls': calls['fake_provider'],
              'external_model_requests': 0, 'bm25_calls': calls['bm25'], 'network_denials': guard.denied,
              'cloud_export': 'NOT_CONFIRMED' if cloud else 'NOT_RUN', 'source_sha256': SOURCE_SHA,
              'snapshot_supported': os.name == 'posix', 'identity_verified': identity_verified,
              'root_contract_verified': root_contract_verified, 'payload_binding_verified': payload_binding_verified,
              'stages': sorted(str(x) for x in stages)}
    (output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    engine.dispose()
    return result


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight', action='store_true')
    mode.add_argument('--execute', action='store_true')
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    if args.preflight:
        if args.cloud or args.output_dir:
            parser.error('--preflight is network-free and takes no execution options')
        result = preflight()
    else:
        if not args.output_dir:
            parser.error('--execute requires a new --output-dir')
        result = execute(args.output_dir, args.cloud)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('READY', 'PASS') else 1

if __name__ == '__main__':
    raise SystemExit(main())

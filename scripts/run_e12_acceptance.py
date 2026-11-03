"""E1.2 isolated actual HTTP/SSE acceptance using frozen synthetic test doubles."""
import argparse
import csv
import subprocess
import re
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


def execute(output, cloud=False, schema="e12", scenario="fulltext", content_policy="LOCAL_ONLY"):
    if content_policy not in ("LOCAL_ONLY", "INLINE") or (content_policy == "INLINE" and schema != "e12"):
        raise ValueError("INLINE requires the E1.2 schema")
    if cloud and (os.getenv('LANGFUSE_BASE_URL') != 'https://us.cloud.langfuse.com' or
                  not all(os.getenv(k) for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'))):
        raise RuntimeError('Cloud requires exact US endpoint and credentials in environment')
    output = Path(output).absolute()
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    if os.name == 'posix':
        os.chmod(output, 0o700)
    elif os.name == 'nt':
        who = subprocess.run(['whoami','/user','/fo','csv','/nh'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
        sid = next(csv.reader(who.stdout.splitlines()))[1]
        if not re.fullmatch(r'S-1-[0-9-]+', sid): raise ValueError('cannot verify current user SID')
        subprocess.run(['icacls',str(output),'/inheritance:r','/grant:r','*'+sid+':(OI)(CI)F','/grant:r','*S-1-5-18:(OI)(CI)F'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,check=True)

    os.environ.update(RAG_OBSERVABILITY_ENABLED='false' if schema == 'off' else 'true', RAG_TRACE_SCHEMA=schema, RAG_E12_CONTENT_POLICY=content_policy, RAG_SNAPSHOTS_ENABLED='true' if os.name == 'posix' else 'false',
                      RAG_SNAPSHOT_DIR=str(output / 'snapshots'), RAG_OBSERVATION_LOG=str(output / 'events.jsonl'),
                      RAG_GROUNDED_GENERATION='true', LANGFUSE_ENABLED='true' if schema != 'off' else 'false')
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
    memory_exporter = None
    if not cloud and schema != 'off':
        from langfuse import Langfuse
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        memory_exporter = InMemorySpanExporter()
        options = {}
        if schema == 'e12':
            from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
            provider = TracerProvider(sampler=TraceIdRatioBased(1.0))
            options.update(should_export_span=obs._native_should_export, mask_otel_spans=obs._native_mask_spans)
        else:
            provider = TracerProvider()
        obs._client = Langfuse(public_key='pk-lf-synthetic-e12', secret_key='sk-lf-synthetic-e12',
                               base_url='https://us.cloud.langfuse.com', tracer_provider=provider,
                               span_exporter=memory_exporter, flush_at=1,
                               mask=lambda **kw: obs.sanitize(kw.get('data'), final_export=True), **options)
        obs._attempted = True

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
    calls = {'fake_provider': 0, 'fake_agent_model': 0, 'fake_grade_model': 0, 'tool': 0, 'bm25': 0, 'vector': 0, 'embedding': 0, 'rerank': 0}
    provider_requests = []
    model_requests = []
    provider_entered = asyncio.Event()
    class BM25:
        def search(self, **kwargs):
            calls['bm25'] += 1
            assert kwargs['kb_id'] == 'synthetic-kb'
            if scenario == 'agent_two_rounds':
                return [] if kwargs['query'] == 'synthetic initial query' else [{'chunk_id': 'synthetic-chunk', 'bm25_score': 2.0}]
            assert kwargs['query'] == QUESTION
            return [] if scenario == 'no_results' else [{'chunk_id': 'synthetic-chunk', 'bm25_score': 2.0}]
    class FakeCompletion:
        async def create(self, **kwargs):
            calls['fake_provider'] += 1
            provider_requests.append(kwargs)
            if scenario == 'cancel':
                provider_entered.set()
                await asyncio.Event().wait()
            if scenario == 'unknown':
                from app.services.llm import ProviderStreamError
                raise ProviderStreamError('UNKNOWN')
            assert SOURCE in json.dumps(kwargs['messages']) and QUESTION in json.dumps(kwargs['messages'])
            # Explicitly unauthorized payload must remain locally referenced/redacted in exported metadata.
            with capture_scope(run_id='unauthorized', qid='not-allowlisted', dataset_version=DATASET,
                               source_sha256s=[SOURCE_SHA], manifest=manifest, capture_mode='EVAL'):
                obs.event('synthetic.unauthorized', output=obs.capture(SENTINEL, content_role='provider_response', allow_cloud=True))
            content = '' if scenario == 'empty' else ANSWER
            reason = 'length' if scenario == 'truncated' else 'stop'
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=reason)], usage=None, model='synthetic-fake')
    fake_llm = SimpleNamespace(model='synthetic-fake', _get_client=lambda: SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletion())))
    rservice = retrieval.RetrievalService.__new__(retrieval.RetrievalService)
    rservice.bm25_index = BM25()
    class Vector:
        def query(self, **kwargs):
            calls['vector'] += 1
            return [{'id':'synthetic-chunk','score':0.8}]
    rservice.vector_store = Vector()
    async def fake_embedding(session, kb_id, query):
        calls['embedding'] += 1
        return [0.1, 0.2]
    rservice._embed_query_for_kb = fake_embedding
    if scenario == 'hybrid_rerank':
        def fake_rerank(self, query, candidates, model=None):
            calls['rerank'] += 1
            return [{'index':i, 'score':0.9 - i * 0.1} for i in range(len(candidates))]
        chat.RerankService.rerank = fake_rerank
    def init(self):
        self.llm_service = fake_llm
        self.retrieval_service = rservice
    chat.ChatService.__init__ = init
    if scenario == 'agent_two_rounds':
        # The real AgentWorkflow StateGraph, its tool node and retrieval service
        # run unchanged; only the LangChain model and backend search inputs are synthetic.
        from app.services import agent_workflow
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, messages_to_dict
        from langchain_core.outputs import ChatGeneration, ChatResult
        from langchain_core.tools import tool
        class ScriptedModel(BaseChatModel):
            purpose: str
            @property
            def _llm_type(self): return 'synthetic-e12'
            def bind_tools(self, tools, **kwargs): return self
            def _generate(self, messages, stop=None, run_manager=None, **kwargs):
                model_requests.append({'purpose':self.purpose, 'messages':messages_to_dict(messages)})
                if self.purpose == 'grade':
                    calls['fake_grade_model'] += 1
                    response=AIMessage(content='0.9')
                else:
                    calls['fake_agent_model'] += 1
                    n=calls['fake_agent_model']
                    if n <= 2:
                        query='synthetic initial query' if n == 1 else 'synthetic corrected query'
                        response=AIMessage(content='',tool_calls=[{'name':'search_knowledge_base','id':f'synthetic-call-{n}','args':{'query':query}}])
                    else:
                        response=AIMessage(content=ANSWER, response_metadata={'finish_reason':'stop'})
                return ChatResult(generations=[ChatGeneration(message=response)])
        class AgentLLM:
            model='synthetic-fake-agent'
            def get_chat_model(self, temperature=None, **kwargs):
                return ScriptedModel(purpose='grade' if temperature == 0.1 else 'agent')
        @tool
        async def search_knowledge_base(query: str, kb_ids: list[str], search_mode: str='fulltext', top_k: int=5,
            score_threshold: float=0.0, vector_weight: float=0.7, bm25_weight: float=0.3,
            rerank_top_k: int=20, rerank_score_threshold: float=0.0, rerank_model_id: str | None=None,
            context_window: int=0, use_rerank: bool=False) -> str:
            """Search the scoped synthetic test knowledge base."""
            calls['tool'] += 1
            with Session(engine) as session:
                found, stats = await rservice.search(session, query, kb_ids, search_mode=search_mode,
                    top_k=top_k, score_threshold=score_threshold, vector_weight=vector_weight,
                    bm25_weight=bm25_weight, log_retrieval=False)
            citations=[{'chunk_id':r.get('chunk_id') or r.get('id'), 'content':r.get('content',''),
                        'fileId':'synthetic-file','fileName':'synthetic.txt','kb_id':'synthetic-kb'} for r in found]
            return json.dumps({'context': '\n'.join(c['content'] for c in citations), 'citations': citations,
                               'retrieval_status': stats['retrieval_status']},ensure_ascii=False)
        agent_workflow.search_knowledge_base = search_knowledge_base
        agent_workflow.LLMService = lambda *args,**kwargs: AgentLLM()
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
            payload={'messages': [{'role': 'user', 'content': 'Use the knowledge base to answer the sample value.' if scenario == 'agent_two_rounds' else QUESTION}],
                'kb_ids': ['synthetic-kb'], 'mode': 'agent' if scenario == 'agent_two_rounds' else 'chat', 'search_mode': 'hybrid' if scenario == 'hybrid_rerank' else 'fulltext', 'top_k': 5,
                'use_rewrite': False, 'rerank_enabled': scenario == 'hybrid_rerank', 'context_window': 0}
            if scenario != 'cancel': return await client.post('/chat/completions', json=payload)
            # ASGITransport buffers response bodies, so drive the real ASGI app
            # directly and deliver an actual http.disconnect after provider entry.
            import urllib.parse
            body = json.dumps(payload,ensure_ascii=False).encode('utf8')
            scope={'type':'http','asgi':{'version':'3.0','spec_version':'2.3'}, 'http_version':'1.1',
                   'method':'POST','scheme':'http','path':'/chat/completions','raw_path':b'/chat/completions',
                   'query_string':b'', 'root_path':'', 'headers':[(b'content-type',b'application/json'),
                   (b'content-length',str(len(body)).encode())], 'client':('synthetic',1), 'server':('synthetic',80)}
            request_delivered = False
            disconnect_delivered = False
            emitted = []
            async def receive():
                nonlocal request_delivered, disconnect_delivered
                if not request_delivered:
                    request_delivered=True
                    return {'type':'http.request','body':body,'more_body':False}
                if not disconnect_delivered:
                    await asyncio.wait_for(provider_entered.wait(), timeout=10)
                    disconnect_delivered=True
                    return {'type':'http.disconnect'}
                await asyncio.Event().wait()
            async def send(message):
                emitted.append(message)
            await asyncio.wait_for(app(scope,receive,send),timeout=20)
            status=next((m['status'] for m in emitted if m['type']=='http.response.start'),None)
            text=b''.join(m.get('body',b'') for m in emitted if m['type']=='http.response.body').decode('utf8')
            return SimpleNamespace(status_code=status,text=text,transport_state='CANCELLED' if disconnect_delivered else 'UNKNOWN',
                                   asgi_disconnect=disconnect_delivered)
    import time
    started = time.perf_counter()
    with asyncio.Runner() as runner, NetworkGuard(cloud) as guard:
        with capture_scope(run_id=run_id, attempt_id='attempt-1', qid=QID, dataset_version=DATASET,
                           source_sha256s=[SOURCE_SHA], manifest=manifest, capture_mode='EVAL', debug_details=True):
            response = runner.run(request(), context=contextvars.copy_context())
        obs.flush_observations()
        # Stop the SDK worker while the network guard is still installed.
        if obs._client is not None:
            obs._client.flush()
            if cloud: obs._client.shutdown()
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    (output / 'sse.txt').write_text(response.text, encoding='utf-8')
    event_path=output / 'events.jsonl'
    rows = [json.loads(line) for line in event_path.read_text(encoding='utf-8').splitlines()] if event_path.exists() else []
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
    payload_binding_verified = 'NOT_RUN'  # Requires actual Cloud/local SDK observation-to-descriptor audit.
    parsed = []
    for block in response.text.replace('\r\n', '\n').split('\n\n'):
        event = next((line[6:].strip() for line in block.splitlines() if line.startswith('event:')), None)
        data = next((line[5:].strip() for line in block.splitlines() if line.startswith('data:')), None)
        if event and data:
            try: parsed.append({'event': event, 'data': json.loads(data)})
            except ValueError: parsed.append({'event': event, 'data': None})
    sent = [e['data'].get('content','') for e in parsed if e['event']=='answer_chunk' and isinstance(e['data'],dict)]
    citations = [e['data'] for e in parsed if e['event']=='rag_result']
    done = [e['data'] for e in parsed if e['event']=='done']
    error = [e['data'] for e in parsed if e['event']=='error']
    spans = list(memory_exporter.get_finished_spans()) if memory_exporter is not None else []
    def serialize(value):
        if isinstance(value, (str,int,float,bool)) or value is None: return value
        if isinstance(value, bytes): return value.hex()
        if isinstance(value, (list,tuple)): return [serialize(v) for v in value]
        return str(value)
    observations = [{'name': span.name, 'trace_id':format(span.context.trace_id,'032x'),
                     'id':format(span.context.span_id,'016x'),
                     'parent_id':format(span.parent.span_id,'016x') if span.parent else None,
                     'start_time_ns':span.start_time, 'end_time_ns':span.end_time,
                     'attributes':{k:serialize(v) for k,v in span.attributes.items()}}
                    for span in spans]
    encoded = json.dumps(observations, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    projections = []
    refs = []
    def scan_refs(value):
        if isinstance(value, dict):
            if isinstance(value.get('snapshot_ref'), str): refs.append(value['snapshot_ref'])
            for v in value.values(): scan_refs(v)
        elif isinstance(value, (list, tuple)):
            for v in value: scan_refs(v)
        elif isinstance(value, str):
            try: scan_refs(json.loads(value))
            except (ValueError, RecursionError): pass
    for item in observations:
        fields = {k: v for k,v in item['attributes'].items() if k in ('langfuse.observation.input','langfuse.observation.output') or k.startswith('langfuse.observation.metadata.')}
        projections.append(fields)
        scan_refs(fields)
    projected_bytes = sum(len(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()) for x in projections)
    from collections import Counter
    repeated_ref_bytes = sum((count-1)*len(ref.encode('utf8')) for ref,count in Counter(refs).items() if count > 1)
    generation_count = sum(x['attributes'].get('langfuse.observation.type') == 'generation' for x in observations)
    def attribute_value(attrs, key):
        value = attrs.get(key)
        if isinstance(value, str):
            try: return json.loads(value)
            except ValueError: return value
        return value
    observation_statuses = [{'name':x['name'], 'execution_status':attribute_value(x['attributes'], 'langfuse.observation.metadata.execution_status')} for x in observations]
    observation_ids = {x['id'] for x in observations}
    missing_parents = [x['parent_id'] for x in observations if x['parent_id'] and x['parent_id'] not in observation_ids]

    hashes = [hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest() for x in provider_requests]
    events_path = output / 'events.jsonl'
    if not events_path.exists(): events_path.write_text('',encoding='utf8')
    (output/'observations.json').write_bytes(encoded)
    # Classify test execution separately from HTTP: HTTP 200 may carry SSE error.
    if scenario == 'cancel':
        expected = bool(getattr(response, 'asgi_disconnect', False) and getattr(response, 'transport_state', None) == 'CANCELLED' and calls['fake_provider'] == 1 and not done)
        if schema != 'off':
            expected = expected and any(x['name']=='qa.request' and x['execution_status']=='CANCELLED' for x in observation_statuses)
        if schema == 'e12':
            expected = expected and any(x['name']=='answer.generate' and x['execution_status']=='CANCELLED' for x in observation_statuses)

    elif scenario == 'unknown':
        expected = bool(error and error[-1].get('status') == 'UNKNOWN' and calls['fake_provider'] == 1 and not done)
    elif scenario == 'no_results':
        expected = bool(done and done[-1].get('retrieval_status') == 'NO_RESULTS')
    elif scenario == 'agent_two_rounds':
        status_sequence = [e['data'].get('status') for e in parsed if e['event'] == 'retrieval_status' and isinstance(e['data'],dict)]
        attempt_ids = [r.get('metadata',{}).get('retrieval_attempt_id') for r in rows if r.get('stage') == 'agent.actual_query']
        expected = bool(done and not error and calls['tool'] == 2 and calls['fake_agent_model'] == 3 and calls['fake_grade_model'] == 1 and calls['bm25'] == 2 and status_sequence == ['NO_RESULTS','SUCCESS'])
        if schema != 'off': expected = expected and len(attempt_ids) == 2 and all(attempt_ids) and len(set(attempt_ids)) == 2

    elif scenario in ('empty', 'truncated'):
        expected = bool(done and done[-1].get('generation_status') == scenario.upper() and calls['fake_provider'] == 1)
    else:
        expected = bool(done and not error and ''.join(sent) == ANSWER and calls['fake_provider'] == 1)
    if scenario == 'hybrid_rerank': expected = expected and all(calls[k] > 0 for k in ('vector','embedding','bm25','rerank'))
    expected = expected and response.status_code == 200 and guard.denied == 0
    telemetry_errors = []
    if memory_exporter is not None and schema == 'e12':
        root_spans = [x for x in observations if x['name'] == 'qa.request' and x['parent_id'] is None]
        if len(root_spans) != 1: telemetry_errors.append('root_not_unique')
        if root_spans:
            attrs=root_spans[0]['attributes']
            for key,want in (('run_id',run_id),('qid',QID),('dataset_version',DATASET)):
                if attribute_value(attrs, 'langfuse.observation.metadata.'+key) != want: telemetry_errors.append('root_identity:'+key)
        if missing_parents: telemetry_errors.append('missing_parents')
        actual_models = calls['fake_provider'] + calls['fake_agent_model'] + calls['fake_grade_model']
        if generation_count != actual_models: telemetry_errors.append('generation_count_vs_fake_calls')
        if SENTINEL.encode() in encoded: telemetry_errors.append('unauthorized_sentinel_exported')
        if scenario == 'cancel':
            for span_name in ('qa.request','answer.generate'):
                if not any(x['name']==span_name and x['execution_status']=='CANCELLED' for x in observation_statuses):
                    telemetry_errors.append('cancel_status_missing:'+span_name)
        expected = expected and not telemetry_errors
    result = {'status':'PASS' if expected else 'FAIL', 'telemetry_errors':telemetry_errors, 'schema':schema, 'scenario':scenario,
              'content_policy':content_policy, 'run_id':run_id, 'trace_ids':trace_ids, 'http_status':response.status_code, 'transport_state':getattr(response,'transport_state','RETURNED'),
              'external_model_requests':0, 'fake_calls':calls, 'provider_request_sha256':hashes,
              'agent_model_request_sha256':[hashlib.sha256(json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest() for x in model_requests],
              'agent_model_comparable_sha256':[hashlib.sha256(json.dumps({'purpose':x['purpose'], 'messages':[{'type':m.get('type'), 'content':m.get('data',{}).get('content'), 'tool_calls':m.get('data',{}).get('tool_calls'), 'tool_call_id':m.get('data',{}).get('tool_call_id')} for m in x['messages']]}, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest() for x in model_requests],
              'agent_comparable_hash_method':'message type/content/tool calls and tool_call_id; excludes framework-generated message IDs and metadata',
              'provider_messages_sha256':[hashlib.sha256(json.dumps(x.get('messages'), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest() for x in provider_requests],
              'answer_sha256':hashlib.sha256(''.join(sent).encode()).hexdigest(),
              'answer':''.join(sent), 'citations':citations, 'done':done, 'errors':error,
              'events':[e['event'] for e in parsed], 'serialized_observation_bytes':len(encoded),
              'observations_per_trace':len(observations), 'generation_count':generation_count, 'observation_statuses':observation_statuses, 'units_formula':'1 trace + observations + S scores; S=UNKNOWN', 'missing_parents':missing_parents, 'serialized_io_metadata_bytes':projected_bytes, 'repeated_snapshot_ref_bytes_lower_bound':repeated_ref_bytes, 'elapsed_ms':elapsed_ms,
              'cloud_export':'NOT_CONFIRMED' if cloud else 'NOT_RUN', 'network_denials':guard.denied,
              'repeated_payload_method':'sum UTF-8 lengths of repeated snapshot_ref values after first within exported input/output/metadata; excludes business content and transport compression',
              'snapshot_supported':os.name == 'posix', 'identity_verified':identity_verified,
              'root_contract_verified':root_contract_verified, 'payload_binding_verified':payload_binding_verified,
              'stages':sorted(str(x) for x in stages), 'sampling':'one; not a performance baseline'}
    (output/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    engine.dispose()
    return result


def main():
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--preflight', action='store_true')
    mode.add_argument('--execute', action='store_true')
    parser.add_argument('--cloud', action='store_true')
    parser.add_argument('--output-dir')
    parser.add_argument('--schema', choices=('e11','e12','off'), default='e12')
    parser.add_argument('--content-policy', choices=('LOCAL_ONLY','INLINE'), default='LOCAL_ONLY')
    parser.add_argument('--case', choices=('fulltext','hybrid_rerank','no_results','unknown','empty','truncated','agent_two_rounds','cancel'), default='fulltext')
    args = parser.parse_args()
    if args.preflight:
        if args.cloud or args.output_dir:
            parser.error('--preflight is network-free and takes no execution options')
        result = preflight()
    else:
        if not args.output_dir:
            parser.error('--execute requires a new --output-dir')
        result = execute(args.output_dir, args.cloud, args.schema, args.case, args.content_policy)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in ('READY', 'PASS') else 1

if __name__ == '__main__':
    raise SystemExit(main())

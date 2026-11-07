"""Offline only: actual app indexing and Hybrid/rerank against synthetic HTTP."""
import subprocess
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_isolated_real_services_with_forbidden_sockets(tmp_path):
    run = subprocess.run([sys.executable, str(__file__), str(tmp_path)], cwd=ROOT,
                         capture_output=True, text=True, encoding='utf-8', timeout=180, env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert run.returncode == 0, run.stdout + run.stderr
    assert 'OFFLINE_HYBRID_PASS' in run.stdout


def child(path):
    import asyncio
    import importlib.util
    import json
    import socket
    import httpx
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    def deny(*args, **kwargs):
        raise AssertionError('All socket access forbidden in offline test')
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.getaddrinfo = deny
    spec = importlib.util.spec_from_file_location('isolated_hybrid', ROOT / 'scripts/m2_hybrid_free_app.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.configure_runtime(path, 'redis://127.0.0.1:1/0', 'synthetic-key')
    calls = []
    class MockAdapter:
        context = 'offline'
        async def async_send(self, request):
            return respond(request)
        def sync_send(self, request):
            return respond(request)
    def respond(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        if request.url.path == '/v1/embeddings':
            assert payload['model'] == mod.EMBEDDING_MODEL
            vectors = [[1.0, float(i + 1) / 100] for i, _ in enumerate(payload['input'])]
            return httpx.Response(200, request=request, json={'data': [
                {'index': i, 'embedding': v} for i, v in enumerate(vectors)]})
        if request.url.path == '/v1/rerank':
            assert payload['model'] == mod.RERANK_MODEL
            return httpx.Response(200, request=request, json={'results': [
                {'index': i, 'relevance_score': 1.0 - i / 100} for i in range(len(payload['documents']))]})
        raise AssertionError('Unexpected endpoint')
    source = path / 'synthetic.txt'
    source.write_text('synthetic', encoding='utf-8')
    manifest = {'sources': [{'id': 's', 'title': 'synthetic.txt', 'path': str(source)}],
                'chunks': [{'id': f'chunk-{i}', 'source_id': 's',
                            'content': f'Project task progress report synthetic number {i}'} for i in range(80)]}
    source_before_holder = []
    async def run():
        from sqlmodel import Session, SQLModel, create_engine
        from app.models import KnowledgeBase, FileDocument, DocumentChunk, FileStatus
        from app.services.bm25 import BM25Index
        from app.core.database import engine
        from app.services.chat import ChatService
        from app.services.rerank import RerankService
        source_db = path / 'source.db'
        source_engine = create_engine('sqlite:///' + str(source_db))
        SQLModel.metadata.create_all(source_engine)
        with Session(source_engine) as session:
            session.add(KnowledgeBase(id=mod.KB_ID, name='synthetic', embedding_model='disabled', vlm_model='disabled'))
            session.add(FileDocument(id='s', kb_id=mod.KB_ID, name='synthetic.txt', local_path=str(source), size=9, status=FileStatus.READY))
            session.commit()
            for i, c in enumerate(manifest['chunks']):
                session.add(DocumentChunk(id=c['id'], file_id='s', content=c['content'], original_index=i))
                session.flush()
                BM25Index().index_chunk(session, c['id'], c['content'], mod.KB_ID)
            session.commit()
        source_before_holder.append(source_db.read_bytes())
        import hashlib
        source_sha = hashlib.sha256(source_before_holder[0]).hexdigest()
        async with mod.bound_provider(MockAdapter(), 'synthetic-key'):
            try:
                await mod.build_index(manifest, source_db, MockAdapter(), expected_source_sha256='0' * 64)
            except ValueError as exc:
                assert 'SHA256' in str(exc)
            else:
                raise AssertionError('Mismatched source hash accepted')
            assert not (path / 'isolated.db').exists()
            wal = Path(str(source_db) + '-wal')
            wal.write_bytes(b'synthetic pending changes')
            try:
                await mod.build_index(manifest, source_db, MockAdapter(), expected_source_sha256=source_sha)
            except ValueError as exc:
                assert 'WAL' in str(exc)
            else:
                raise AssertionError('Nonempty source WAL accepted')
            wal.unlink()
            assert not (path / 'isolated.db').exists()
            built = await mod.build_index(manifest, source_db, MockAdapter(), expected_source_sha256=source_sha)
            assert built['chunks'] == 80
            service = ChatService()
            with Session(engine) as session:
                results, metrics = await service._retrieve_with_new_service(session, query='project task progress',
                    kb_ids=[mod.KB_ID], **{k: mod.FIXED_SEARCH[k] for k in (
                    'top_k','score_threshold','search_mode','vector_weight','bm25_weight',
                    'rerank_enabled','rerank_score_threshold','rerank_top_k','context_window')},
                    rerank_model_id=None)
            assert results and all(r.get('rerank_status') == 'SCORED' for r in results)
            assert metrics['retrieval_status'] == 'SUCCESS'
            try:
                RerankService(None)._rerank_via_chat('q', [], None)
            except mod.ForbiddenFallback:
                pass
            else:
                raise AssertionError('fallback was not blocked')
    loop.run_until_complete(run())
    loop.close()
    assert [x[0] for x in calls] == ['/v1/embeddings'] * 6 + ['/v1/rerank']
    assert (path / 'source.db').read_bytes() == source_before_holder[0]
    assert b'synthetic-key' not in (path / 'isolated.db').read_bytes()
    print('OFFLINE_HYBRID_PASS')





def test_chat_and_agent_full_path_synthetic(tmp_path):
    run = subprocess.run([sys.executable, str(__file__), '--full', str(tmp_path)], cwd=ROOT,
                         capture_output=True, text=True, encoding='utf-8', timeout=180,
                         env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert run.returncode == 0, run.stdout[-12000:] + run.stderr[-12000:]
    assert 'synthetic-only-key' not in run.stdout
    assert 'synthetic-only-key' not in run.stderr
    assert 'OFFLINE_FULL_CHAT_AGENT_PASS' in run.stdout


def full_child(path):
    import asyncio
    import importlib.util
    import json
    import socket
    import httpx

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    spec = importlib.util.spec_from_file_location('hybrid_run_full', ROOT / 'scripts/m2_hybrid_free_run.py')
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    redis_script = (ROOT / 'var/preferences-acceptance/redis-test-runtime.txt').read_text().strip()
    os.environ['RAG_TEST_REDIS_CMD'] = json.dumps(['wsl.exe', '--exec', redis_script])
    redis_context = runner.base.isolated_redis(path)
    redis_url = redis_context.__enter__()
    from urllib.parse import urlsplit
    redis_port = urlsplit(redis_url).port
    original_connect, original_connect_ex = socket.socket.connect, socket.socket.connect_ex
    original_dns, original_create = socket.getaddrinfo, loop.create_connection
    def check(address):
        if not (isinstance(address, tuple) and address[0] in ('127.0.0.1', 'localhost') and address[1] == redis_port):
            raise AssertionError('All egress except owned Redis forbidden')
    def connect(sock, address, *args, **kwargs):
        check(address); return original_connect(sock, address, *args, **kwargs)
    def connect_ex(sock, address, *args, **kwargs):
        check(address); return original_connect_ex(sock, address, *args, **kwargs)
    def dns(host, port, *args, **kwargs):
        check((host, port)); return original_dns(host, port, *args, **kwargs)
    async def create_connection(factory, host=None, port=None, *args, **kwargs):
        check((host, port)); return await original_create(factory, host, port, *args, **kwargs)
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, dns
    loop.create_connection = create_connection
    app = runner.app
    app.configure_runtime(path, redis_url, 'synthetic-only-key')
    source = path / 'synthetic.txt'
    source.write_text('Synthetic source', encoding='utf-8')
    chunks = [{'id': f'chunk-{i}', 'source_id': 's',
               'content': f'Project task progress report procedure step synthetic {i}'} for i in range(80)]
    manifest = {'sources': [{'id': 's', 'title': 'synthetic.txt', 'path': str(source)}], 'chunks': chunks}
    calls = []
    contexts = []
    adapter_holder = []
    async def mock_async(request):
        return mock(request)
    def mock(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload))
        contexts.append((adapter_holder[0].context, request.url.path, payload))
        if request.url.path == '/v1/embeddings':
            return httpx.Response(200, request=request, json={'data': [
                {'index': i, 'embedding': [1.0, (i + 1) / 100]} for i, _ in enumerate(payload['input'])]})
        if request.url.path == '/v1/rerank':
            return httpx.Response(200, request=request, json={'results': [
                {'index': i, 'relevance_score': 1.0 - i / 100} for i in range(len(payload['documents']))]})
        assert request.url.path == '/v1/chat/completions'
        assert payload['model'] == app.CHAT_MODEL and payload['enable_thinking'] is False
        messages = payload['messages']
        combined = json.dumps(messages, ensure_ascii=False)
        if payload.get('tools') and not any(m['role'] == 'tool' for m in messages):
            message = {'role': 'assistant', 'content': None, 'tool_calls': [
                {'id': 'call_synthetic', 'type': 'function', 'function': {
                    'name': 'search_knowledge_base', 'arguments': json.dumps({'query': 'project task progress report'})}}]}
            finish = 'tool_calls'
        elif '文档相关性评分员' in combined:
            message = {'role': 'assistant', 'content': '0.95'}
            finish = 'stop'
        else:
            message = {'role': 'assistant', 'content': 'Project task progress report procedure [1].'}
            finish = 'stop'
        data = {'id': 'synthetic', 'object': 'chat.completion', 'created': 0, 'model': app.CHAT_MODEL,
                'choices': [{'index': 0, 'message': message, 'finish_reason': finish}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}}
        if payload.get('stream'):
            chunk = {'id': 'synthetic', 'object': 'chat.completion.chunk', 'created': 0,
                     'model': app.CHAT_MODEL, 'choices': [{'index': 0,
                        'delta': {'role': 'assistant', 'content': message['content']}, 'finish_reason': None}]}
            end = {'id': 'synthetic', 'object': 'chat.completion.chunk', 'created': 0,
                   'model': app.CHAT_MODEL, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': finish}]}
            body = 'data: ' + json.dumps(chunk) + '\n\ndata: ' + json.dumps(end) + '\n\ndata: [DONE]\n\n'
            return httpx.Response(200, request=request, content=body.encode(), headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, request=request, json=data)
    async def run():
        from sqlmodel import Session, SQLModel, create_engine
        from app.models import KnowledgeBase, FileDocument, DocumentChunk, FileStatus
        from app.services.bm25 import BM25Index
        from app.services import memory
        from app.services.memory_commands import resolve_display_preferences
        from app.services.trace_schema import capture_scope
        source_db = path / 'source.db'
        source_engine = create_engine('sqlite:///' + str(source_db))
        SQLModel.metadata.create_all(source_engine)
        with Session(source_engine) as session:
            session.add(KnowledgeBase(id=app.KB_ID, name='synthetic', embedding_model='disabled', vlm_model='disabled'))
            session.add(FileDocument(id='s', kb_id=app.KB_ID, name='synthetic.txt', local_path=str(source),
                                     size=source.stat().st_size, status=FileStatus.READY))
            session.commit()
            bm25 = BM25Index()
            for i, c in enumerate(chunks):
                session.add(DocumentChunk(id=c['id'], file_id='s', content=c['content'], original_index=i))
                session.flush(); bm25.index_chunk(session, c['id'], c['content'], app.KB_ID)
            session.commit()
        service = memory.get_memory_service()
        mock_transport = httpx.MockTransport(mock)
        adapter = runner.CappedAdapter(runner.LimitedCalls(path / 'ledger'), mock_transport, mock_transport)
        adapter_holder.append(adapter)
        case = {'qid': 'synthetic-procedure', 'question': '根据知识库资料，如何执行 project task progress report 流程？'}
        async with app.bound_provider(adapter, 'synthetic-only-key'):
            await app.build_index(manifest, source_db, adapter)
            results = {}
            for mode in ('chat', 'agent'):
                for arm in ('A', 'D'):
                    scope = memory.PreferenceScope(tenant_id='synthetic', user_id=f'{mode}-{arm}')
                    records = []
                    with capture_scope(run_id='synthetic', attempt_id=f'{mode}-{arm}',
                                       qid=case['qid'], dataset_version='synthetic-v1'):
                        result = await app.trial(case, mode, arm, scope, adapter, on_event=records.append)
                    assert result['events'] == records and result['state'] == 'RETURNED', result
                    assert 'synthetic-only-key' not in json.dumps(records, ensure_ascii=False)
                    assert any(e['event'] == 'rag_result' for e in records), result
                    snapshot = await service.get_preferences(scope)
                    policy, _, _ = resolve_display_preferences(snapshot, case['question'])
                    assert bool(policy) == (arm == 'D')
                    results[(mode, arm)] = result
        assert len(results) == 4
        await service.client.aclose()
    try:
        loop.run_until_complete(run())
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = original_connect, original_connect_ex, original_dns
        loop.create_connection = original_create
        loop.close()
        redis_context.__exit__(None, None, None)
    chat_calls = [payload for endpoint, payload in calls if endpoint == '/v1/chat/completions']
    assert chat_calls and any(p.get('tools') for p in chat_calls)
    for mode in ('chat', 'agent'):
        for arm in ('A', 'D'):
            relevant = [p for ctx, endpoint, p in contexts if ctx == f'synthetic-procedure:{mode}:{arm}'
                        and endpoint == '/v1/chat/completions']
            assert relevant
            applied = any('适合操作或推导时分步说明' in json.dumps(p['messages'], ensure_ascii=False) for p in relevant)
            assert applied == (arm == 'D'), (mode, arm)
    assert sum(endpoint == '/v1/rerank' for endpoint, _ in calls) >= 4
    trace = [json.loads(line) for line in (path / 'observations.jsonl').read_text(encoding='utf-8').splitlines()]
    encoded = json.dumps(trace, ensure_ascii=False)
    for mode in ('chat', 'agent'):
        for arm in ('A', 'D'):
            rows = [e for e in trace if e.get('qid') == 'synthetic-procedure' and e.get('attempt_id') == f'{mode}-{arm}']
            assert rows
            for stage in ('retrieval.vector', 'retrieval.bm25', 'rerank.selected'):
                assert any(e.get('stage') == stage and e['metadata'].get('candidates') for e in rows), (mode, arm, stage)
            fused = [e['metadata'] for e in rows if e.get('metadata', {}).get('operation') == 'weighted_fusion']
            assert fused and all(x['vector_weight'] == 0.7 and x['bm25_weight'] == 0.3 and x['scores'] for x in fused)
    # These are generated run artifacts, not the test script or injected HTTP headers.
    paths = [path / 'isolated.db', path / 'observations.jsonl']
    paths += [p for folder in ('ledger', 'chroma') for p in (path / folder).rglob('*') if p.is_file()]
    assert paths and all(p.exists() for p in paths)
    for artifact in paths:
        assert b'synthetic-only-key' not in artifact.read_bytes(), str(artifact)
    print('OFFLINE_FULL_CHAT_AGENT_PASS')


if __name__ == '__main__':
    if sys.argv[1] == '--full':
        full_child(Path(sys.argv[2]))
    else:
        child(Path(sys.argv[1]))

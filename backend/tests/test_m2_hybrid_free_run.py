import asyncio
import importlib.util
import json
from pathlib import Path
import socket
import ipaddress
import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('free_run_test', ROOT / 'scripts/m2_hybrid_free_run.py')
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    old_connect, old_ex = socket.socket.connect, socket.socket.connect_ex
    def safe(address):
        try:
            return ipaddress.ip_address(address[0]).is_loopback and address[1] != 11434
        except (ValueError, TypeError, IndexError):
            return False
    def connect(sock, address):
        if not safe(address): raise AssertionError('network forbidden')
        return old_connect(sock, address)
    def connect_ex(sock, address):
        if not safe(address): raise AssertionError('network forbidden')
        return old_ex(sock, address)
    def deny(*a, **k): raise AssertionError('network forbidden')
    monkeypatch.setattr(socket.socket, 'connect', connect)
    monkeypatch.setattr(socket.socket, 'connect_ex', connect_ex)
    monkeypatch.setattr(socket, 'getaddrinfo', deny)
    monkeypatch.setattr(httpx.AsyncClient, 'send', deny)
    monkeypatch.setattr(httpx.Client, 'send', deny)


def test_plan_frozen_corpus_and_no_calls():
    plan = runner.make_plan()
    assert len(plan['identities']) == 12 and plan['model_calls'] == 0
    assert plan['search']['search_mode'] == 'hybrid'
    assert plan['search']['vector_weight'] == .7 and plan['search']['bm25_weight'] == .3
    assert plan['search']['rerank_enabled'] and plan['search']['top_k'] == 10
    assert len({(i['qid'], i['mode'], i['arm']) for i in plan['identities']}) == 12


def test_guard_caps_qwen_and_captures_usage(tmp_path):
    calls = runner.LimitedCalls(tmp_path / 'calls')
    captured = []
    def response(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={'model':'Qwen/Qwen3.5-4B','choices':[{'finish_reason':'stop'}], 'usage': {'prompt_tokens': 5, 'completion_tokens': 3}})
    inner = httpx.MockTransport(response)
    adapter = runner.CappedAdapter(calls, inner, inner)
    req = httpx.Request('POST', 'https://api.siliconflow.cn/v1/chat/completions', json={'model':'Qwen/Qwen3.5-4B','max_tokens':8000})
    adapter.sync_send(req)
    assert captured[0]['max_tokens'] == 4096 and captured[0]['enable_thinking'] is False
    record = runner.usage_records(calls.directory)[0]
    assert record['transport_state'] == 'COMPLETED' and record['generation_status'] == 'TERMINAL' and record['usage']['prompt_tokens'] == 5


def test_limit_and_unknown_are_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, 'MAX_CALLS', 1)
    calls = runner.LimitedCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={'results':[{'index':0,'relevance_score':.4}]}))
    adapter = runner.CappedAdapter(calls, inner, inner)
    req = httpx.Request('POST','https://api.siliconflow.cn/v1/rerank',json={'model':'BAAI/bge-reranker-v2-m3','documents':['x']})
    adapter.sync_send(req)
    with pytest.raises(runner.guardmod.GuardStop): adapter.sync_send(req)
    assert calls.count == 1 and calls.stopped


def test_transport_reads_inside_dispatch():
    class Network:
        active = False
        def dispatch(self):
            from contextlib import contextmanager
            @contextmanager
            def context():
                self.active = True
                try: yield
                finally: self.active = False
            return context()
    network = Network()
    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            assert network.active
            yield b'hello'
    inner = httpx.MockTransport(lambda req: httpx.Response(200, stream=Stream()))
    response = asyncio.run(runner.NetworkTransport(inner, network).handle_async_request(httpx.Request('GET','https://example.invalid')))
    assert response.content == b'hello' and not network.active


def test_trial_failure_preserves_partial_event_and_stops(tmp_path, monkeypatch):
    import sys
    import types
    from contextlib import asynccontextmanager, contextmanager
    class Memory:
        async def aclose(self): pass
    class Scope:
        def __init__(self, **kwargs): self.kwargs = kwargs
    memory = types.SimpleNamespace(client=Memory())
    monkeypatch.setitem(sys.modules, 'app.services.memory', types.SimpleNamespace(PreferenceScope=Scope, get_memory_service=lambda: memory))
    @contextmanager
    def capture_scope(**kwargs): yield
    monkeypatch.setitem(sys.modules, 'app.services.trace_schema', types.SimpleNamespace(capture_scope=capture_scope))
    @asynccontextmanager
    async def bound(*args): yield
    async def build(*args, **kwargs): return {'chunks':80}
    async def trial(case, mode, arm, scope, adapter, on_event):
        on_event({'event':'reference', 'data':{'id':'synthetic'}})
        raise runner.guardmod.GuardStop('synthetic stop')
    monkeypatch.setattr(runner.app, 'bound_provider', bound)
    monkeypatch.setattr(runner.app, 'build_index', build)
    monkeypatch.setattr(runner.app, 'trial', trial)
    adapter = types.SimpleNamespace(guard=types.SimpleNamespace(stopped=False))
    identity = {'qid':runner.QIDS[0], 'mode':'chat', 'arm':'A', 'repetition':1}
    with pytest.raises(runner.guardmod.GuardStop):
        asyncio.run(runner.run({'identities':[identity], 'source_db_sha256':'synthetic'}, tmp_path, 'mock', adapter, 'redis://127.0.0.1'))
    assert json.loads((tmp_path / 'events.jsonl').read_text())['data']['id'] == 'synthetic'
    assert json.loads((tmp_path / 'trials.jsonl').read_text())['state'] == 'STOPPED'
    assert len((tmp_path / 'started.jsonl').read_text().splitlines()) == 1


def test_incomplete_chat_is_http_200_generation_unknown_and_stops(tmp_path):
    calls = runner.LimitedCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, headers={'content-type':'text/event-stream'}, content=b'data: {"model":"Qwen/Qwen3.5-4B","choices":[]}\n\n'))
    adapter = runner.CappedAdapter(calls, inner, inner)
    req = httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'Qwen/Qwen3.5-4B'})
    with pytest.raises(runner.guardmod.GuardStop): adapter.sync_send(req)
    row = runner.usage_records(calls.directory)[0]
    assert row['http_status'] == 200 and row['generation_status'] == 'UNKNOWN'
    assert row['transport_state'] == 'FAILED_RESPONSE' and calls.stopped


def test_truncation_and_absent_usage_are_reported(tmp_path):
    calls = runner.LimitedCalls(tmp_path / 'calls')
    inner = httpx.MockTransport(lambda req: httpx.Response(200, json={'model':'Qwen/Qwen3.5-4B','choices':[{'finish_reason':'length'}]}))
    adapter = runner.CappedAdapter(calls, inner, inner)
    req = httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'Qwen/Qwen3.5-4B'})
    adapter.sync_send(req)
    row = runner.usage_records(calls.directory)[0]
    assert row['generation_status'] == 'TRUNCATED' and row['usage_status'] == 'UNKNOWN'


def test_restrict_loopback_forbids_model_and_unowned_ports():
    with runner.restrict_loopback('redis://:mock@127.0.0.1:63991/0'):
        with socket.socket() as sock:
            for port in (11434, 8000, 6379):
                with pytest.raises(runner.guardmod.GuardStop): sock.connect(('127.0.0.1', port))


def test_async_connection_restriction_uses_owned_redis_and_allows_cloud(monkeypatch):
    async def run():
        loop = asyncio.get_running_loop()
        seen = []
        async def fake(factory, host=None, port=None, *args, **kwargs):
            seen.append((host, port))
            return 'stub'
        monkeypatch.setattr(loop, 'create_connection', fake)
        with runner.restrict_loopback('redis://:mock@127.0.0.1:63991/0'):
            for host, port in [('127.0.0.1', 11434), ('localhost', 8000), ('::1', 63991)]:
                with pytest.raises(runner.guardmod.GuardStop):
                    await loop.create_connection(None, host, port)
            assert await loop.create_connection(None, '127.0.0.1', 63991) == 'stub'
            assert await loop.create_connection(None, 'api.siliconflow.cn', 443) == 'stub'
        assert seen == [('127.0.0.1', 63991), ('api.siliconflow.cn', 443)]
    asyncio.run(run())


def test_changed_source_snapshot_blocks_before_dispatch(tmp_path, monkeypatch):
    calls = runner.LimitedCalls(tmp_path / 'calls', {'sources': {'changed': 'old'}})
    monkeypatch.setattr(runner, 'snapshot', lambda: {'changed': 'new'})
    req = httpx.Request('POST','https://api.siliconflow.cn/v1/embeddings',json={'model':'BAAI/bge-m3','input':['synthetic']})
    with pytest.raises(runner.guardmod.GuardStop): calls.begin('index:call-1', req)
    assert calls.stopped and calls.count == 0 and not list(calls.directory.iterdir())


def test_nonempty_wal_fails_closed_before_dispatch(tmp_path, monkeypatch):
    db = tmp_path / 'source.db'
    db.write_bytes(b'fixture')
    Path(str(db) + '-wal').write_bytes(b'pending committed rows')
    monkeypatch.setattr(runner, 'SOURCE', db)
    calls = runner.LimitedCalls(tmp_path / 'calls', {})
    req = httpx.Request('POST','https://api.siliconflow.cn/v1/embeddings',json={'model':'BAAI/bge-m3','input':['synthetic']})
    with pytest.raises(runner.guardmod.GuardStop): calls.begin('index:call-1', req)
    assert calls.count == 0 and calls.stopped

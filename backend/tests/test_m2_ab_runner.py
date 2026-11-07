"""Offline checks of the isolated runner; no real model, Redis or production access."""
import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import httpx
import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts" / "m2_preference_ab.py"
SPEC = importlib.util.spec_from_file_location("m2_preference_ab", PATH)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


@pytest.fixture
def manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    source = tmp_path / "var" / "source.txt"
    source.parent.mkdir()
    source.write_text("Authorized public fixture: Mercury is a planet.", encoding="utf-8")
    content = source.read_text(encoding="utf-8")
    return {"schema_version": runner.SCHEMA, "dataset_version": "offline-v1", "authorized": True,
            "model": "zai-org/GLM-5.3-Flash", "base_url": "https://api.siliconflow.cn/v1",
            "budget_cny": 1, "repetitions": 2, "max_output_tokens": 1024,
            "pricing": {"input_cny_per_million": .8, "output_cny_per_million": 2.8,
                        "source_url": "https://example.invalid/test-only", "verified_at": "offline-test"},
            "sources": [{"id": "source-1", "path": str(source), "sha256": runner.digest(source.read_bytes()),
                         "title": "Public test fixture", "authorized": True}],
            "chunks": [{"id": "chunk-1", "source_id": "source-1", "content": content,
                        "sha256": runner.digest(content.encode()), "locator": "line 1"}],
            "cases": [{"qid": f"q{number}", "question": "According to the source, what is Mercury?",
                       "expected_chunk_ids": ["chunk-1"] if number < 5 else [],
                       "answer_points": ["planet"] if number < 5 else ["insufficient evidence"],
                       "kb_scope": [runner.KB_ID], "negative": number == 5,
                       "gold_locator": "line 1"} for number in range(6)]}


def request(manifest, **extra):
    payload = {"model": manifest["model"], "messages": [{"role": "user", "content": "public fixture"}], **extra}
    return httpx.Request("POST", runner.endpoint(manifest["base_url"]), json=payload,
                         headers={"authorization": "Bearer never-write-this-secret"})


def response(manifest, request, **extra):
    body = {"id": "test-id", "model": manifest["model"],
            "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
            "choices": [{"message": {"role": "assistant", "content": "planet"}, "finish_reason": "stop"}], **extra}
    return httpx.Response(200, request=request, json=body)


def test_preflight_has_four_policies_two_modes_two_repetitions_no_calls(manifest):
    result = runner.validate_manifest(manifest)
    assert result["planned_trials"] == 96 and result["model_calls"] == 0
    assert result["arms"] == {"A": {}, "B": {"answer_style": "detailed"},
                               "C": {"knowledge_level": "beginner"},
                               "D": {"output_preferences": ["step_by_step"]}}
    assert result["modes"] == ["chat", "agent"]
    assert result['fixed_search'] == {'search_mode': 'fulltext', 'top_k': 10, 'context_window': 0,
                                      'rerank_enabled': False, 'use_rewrite': False, 'score_threshold': 0}
    plan = list(runner.trial_plan(manifest))
    assert [(x[0]['qid'], x[1], x[2], x[3]) for x in plan[:5]] == [
        ('q0', 'chat', 1, 'A'), ('q0', 'chat', 1, 'B'), ('q0', 'chat', 1, 'C'),
        ('q0', 'chat', 1, 'D'), ('q0', 'chat', 2, 'A')]


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(budget_cny=1.01),
    lambda m: m.update(authorized=False),
    lambda m: m.update(repetitions=1),
    lambda m: m.update(model="other-model"),
    lambda m: m["chunks"][0].update(content="altered"),
    lambda m: m["sources"][0].update(sha256="wrong"),
    lambda m: m["cases"][0].update(kb_scope=["production"]),
    lambda m: m["cases"][0].update(expected_chunk_ids=["unknown"]),
    lambda m: m["cases"][-1].update(expected_chunk_ids=["chunk-1"]),
])
def test_invalid_manifest_blocks_before_provider(manifest, mutate):
    mutate(manifest)
    with pytest.raises(ValueError):
        runner.validate_manifest(manifest)


@pytest.mark.parametrize("url", ["http://api.siliconflow.cn/v1", "https://127.0.0.1/v1",
                                      "https://production.example/v1", "https://api.siliconflow.cn/v1?key=secret"])
def test_endpoint_deny(url):
    with pytest.raises(ValueError):
        runner.endpoint(url)


def test_source_and_output_must_stay_in_private_var(manifest, tmp_path):
    with pytest.raises(ValueError):
        runner.private_path(tmp_path / "public")


def test_bounded_request_reservation_actual_usage_and_no_saved_credentials(manifest, tmp_path):
    output = tmp_path / "evidence"
    output.mkdir()
    guard = runner.BudgetTransport(manifest, output)
    seen = []
    async def sender(client, req, *args, **kwargs):
        seen.append(json.loads(req.content))
        assert guard.committed > 0 and guard.calls[0]["state"] == "UNKNOWN_PENDING"
        assert guard.ledger.read_bytes()
        assert req.headers["authorization"] == "Bearer never-write-this-secret"
        return response(manifest, req)
    result = asyncio.run(guard.send(sender, None, request(manifest, max_tokens=99999)))
    assert result.status_code == 200 and seen[0]["max_tokens"] == 1024
    assert guard.committed == guard.cost(12, 7)
    assert guard.summary()["actual_http_calls"] == 1
    assert guard.summary()["prompt_tokens"] == 12
    assert all(b"never-write-this-secret" not in path.read_bytes() for path in output.iterdir())


def test_budget_blocks_before_send(manifest, tmp_path):
    manifest["budget_cny"] = .000001
    guard = runner.BudgetTransport(manifest, tmp_path)
    async def sender(*args, **kwargs):
        pytest.fail("Budget gate must precede network")
    with pytest.raises(runner.GuardStop, match="BUDGET_BLOCKED"):
        asyncio.run(guard.send(sender, None, request(manifest)))
    assert not guard.calls


@pytest.mark.parametrize("kind", ["timeout", "missing_usage", "model_mismatch", "too_many_tokens", "http_failure"])
def test_unknown_keeps_reservation_and_prevents_retry(manifest, tmp_path, kind):
    guard = runner.BudgetTransport(manifest, tmp_path)
    sends = []
    async def sender(client, req, *args, **kwargs):
        sends.append(True)
        if kind == "timeout":
            raise httpx.ReadTimeout("fixture")
        if kind == "http_failure":
            return httpx.Response(429, request=req, json={"error": "rate limited"})
        extra = {"usage": None} if kind == "missing_usage" else {"model": "unexpected"} if kind == "model_mismatch" else {
            "usage": {"prompt_tokens": 12, "completion_tokens": 99999}}
        return response(manifest, req, **extra)
    with pytest.raises(runner.GuardStop, match="UNKNOWN"):
        asyncio.run(guard.send(sender, None, request(manifest)))
    assert guard.committed == runner.Decimal(guard.calls[0]["reservation_cny"])
    assert guard.calls[0]["state"] == "UNKNOWN"
    with pytest.raises(runner.GuardStop):
        asyncio.run(guard.send(sender, None, request(manifest)))
    assert len(sends) == 1
    with pytest.raises(ValueError, match="replayed"):
        runner.BudgetTransport(manifest, tmp_path)


def test_actual_sse_usage_can_be_accounted_without_sdk_estimate(manifest, tmp_path):
    guard = runner.BudgetTransport(manifest, tmp_path)
    async def sender(client, req, *args, **kwargs):
        assert json.loads(req.content)["stream_options"]["include_usage"] is True
        obj = {"id": "sse-id", "model": manifest["model"], "usage": {
            "prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}, "choices": []}
        return httpx.Response(200, request=req, content=b"data: " + runner.canonical(obj) + b"\n\ndata: [DONE]\n\n")
    asyncio.run(guard.send(sender, None, request(manifest, stream=True)))
    assert guard.summary()["completion_tokens"] == 10


def test_installed_transport_denies_unapproved_sync_and_async_restores_afterward(manifest, tmp_path):
    guard = runner.BudgetTransport(manifest, tmp_path)
    original = httpx.AsyncClient.send
    with guard.installed():
        with pytest.raises(runner.GuardStop, match="Synchronous"):
            httpx.Client().get("https://production.example")
    assert httpx.AsyncClient.send is original


def test_unbound_async_client_is_denied_without_connection(manifest, tmp_path):
    guard = runner.BudgetTransport(manifest, tmp_path)
    async def attempt():
        async with httpx.AsyncClient() as client:
            await client.get('https://production.example')
    with guard.installed(), pytest.raises(runner.GuardStop, match='Unbound'):
        asyncio.run(attempt())
    assert guard.calls == []


def test_real_service_bm25_agent_and_sdk_streaming_with_synthetic_provider(manifest, tmp_path):
    """Real service and tools, public test corpus; only Redis IO/provider are substitutes."""
    for case in manifest["cases"]:
        case["question"] = "根据资料，Mercury 是什么？"
    manifest_path = tmp_path / "synthetic-manifest.json"
    manifest_path.write_bytes(runner.canonical(manifest))
    code = r'''
import asyncio, importlib.util, json, os, socket, sys
from pathlib import Path
import httpx
class NetworkForbidden(BaseException): pass
original_connect=socket.socket.connect
def no_network(sock,address,*a,**kw):
 if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):
  return original_connect(sock,address,*a,**kw)
 raise NetworkForbidden('Synthetic test denies every non-loopback socket connection')
socket.socket.connect=no_network
socket.socket.connect_ex=no_network
spec=importlib.util.spec_from_file_location('m2runner',sys.argv[1]); r=importlib.util.module_from_spec(spec);spec.loader.exec_module(r)
m=json.loads(Path(sys.argv[2]).read_bytes());out=Path(sys.argv[3]);out.mkdir()
os.environ['M2_AB_API_KEY']='synthetic-test-only'
r.configure_runtime(m,out,'redis://127.0.0.1:1/0')
class FakeRedis:
 def __init__(self): self.rows={}
 async def hgetall(self,key): return self.rows.get(key,{}).copy()
 async def hset(self,key,mapping): self.rows.setdefault(key,{}).update(mapping)
 async def delete(self,key): self.rows.pop(key,None)
 async def aclose(self): pass
from app.services import memory
memory_service=memory.MemoryService(client=FakeRedis())
memory.get_memory_service=lambda:memory_service
sent=[]
async def provider(request):
 p=json.loads(request.content);sent.append(p)
 text='Mercury 是一颗行星。[1]'
 msg={'role':'assistant','content':text};finish='stop'
 if p.get('tools') and not any(x.get('role')=='tool' for x in p['messages']):
  msg={'role':'assistant','content':'','tool_calls':[{'id':'call_test','type':'function','function':{'name':'search_knowledge_base','arguments':json.dumps({'query':'Mercury planet'})}}]};finish='tool_calls'
 elif '文档相关性评分员' in str(p['messages']): msg={'role':'assistant','content':'0.95'}
 elif 'Classify intent' in str(p['messages']): msg={'role':'assistant','content':'qa'}
 body={'id':f'synthetic-{len(sent)}','model':m['model'],'object':'chat.completion','created':1,'choices':[{'index':0,'message':msg,'finish_reason':finish}], 'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}}
 if p.get('stream'):
  first={**body,'object':'chat.completion.chunk','choices':[{'index':0,'delta':msg,'finish_reason':None}],'usage':None}
  last={**body,'object':'chat.completion.chunk','choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':None}
  usage={**body,'object':'chat.completion.chunk','choices':[]}
  raw=b''.join(b'data: '+r.canonical(x)+b'\n\n' for x in (first,last,usage))+b'data: [DONE]\n\n'
  return httpx.Response(200,request=request,content=raw,headers={'content-type':'text/event-stream'})
 return httpx.Response(200,request=request,json=body)
g=r.BudgetTransport(m,out,transport=httpx.MockTransport(provider))
with g.installed():
 r.build_index(m)
 trials=asyncio.run(r.run_trials(m,out,g))
assert len(trials)==96,(len(trials),trials[-1])
assert {x['mode'] for x in trials}=={'chat','agent'}
assert {x['arm'] for x in trials}=={'A','B','C','D'}
assert all(x['state']=='RETURNED' for x in trials)
assert all(any(e['event']=='rag_result' and e['data'].get('citations') for e in x['events']) for x in trials)
assert any(p.get('stream') for p in sent),'Actual SDK stream path was not exercised'
assert any(p.get('tools') for p in sent),'Actual Agent tool binding was not exercised'
assert all(c['state']=='COMPLETED' for c in g.calls)
assert all('synthetic-test-only' not in f.read_text(errors='ignore') for f in out.glob('request-*.json'))
assert g.committed<r.Decimal('1')
print(json.dumps({'synthetic_provider_calls':len(sent),'trials':len(trials),'real_network_calls':0}))
'''
    result = subprocess.run([sys.executable, "-c", code, str(PATH), str(manifest_path), str(tmp_path / "integration")],
                            capture_output=True, text=True, encoding="utf-8", timeout=150,
                            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, (result.stdout[-6000:], result.stderr[-6000:])
    assert '"trials": 96' in result.stdout

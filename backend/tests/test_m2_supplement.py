"""Supplement accounting and applicability tests; no authorized model calls."""
import asyncio
from copy import deepcopy
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts" / "m2_preference_supplement.py"
SPEC = importlib.util.spec_from_file_location("m2_supplement_test", PATH)
supp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(supp)
sys.path.insert(0, str(PATH.parents[1] / "backend"))


@pytest.fixture
def spec(tmp_path, monkeypatch):
    monkeypatch.setattr(supp.base, "ROOT", tmp_path)
    directory = tmp_path / "var"
    directory.mkdir()
    source = directory / "original.txt"
    quote = "A tribometer measures coefficient of friction, friction force, and wear volume."
    source.write_text(quote, encoding="utf-8")
    parent = {"schema_version": supp.base.SCHEMA, "dataset_version": "synthetic-primary-v3", "authorized": True,
              "model": "zai-org/GLM-5.3-Flash", "base_url": "https://api.siliconflow.cn/v1", "budget_cny": 1,
              "max_output_tokens": 4096, "repetitions": 2,
              "pricing": {"input_cny_per_million": .8, "output_cny_per_million": 2.8,
                          "source_url": "https://example.invalid/offline-fixture", "verified_at": "offline-only"},
              "sources": [{"id": "fixture", "path": str(source), "sha256": supp.base.digest(source.read_bytes()),
                           "title": "Public synthetic tribometer", "authorized": True}],
              "chunks": [{"id": "hot-context-9", "source_id": "fixture", "content": quote,
                          "sha256": supp.base.digest(quote.encode()), "locator": "line 1"}],
              "cases": [{"qid": f"old-{i}", "question": f"What is the original question {i}?",
                         "expected_chunk_ids": ["hot-context-9"] if i < 5 else [],
                         "answer_points": ["friction"], "kb_scope": [supp.base.KB_ID], "negative": i == 5,
                         "gold_locator": "synthetic line 1"} for i in range(6)]}
    parent_manifest = directory / "original-manifest.json"
    parent_manifest.write_bytes(supp.base.canonical(parent))
    parent_output = directory / "primary-run"
    parent_output.mkdir()
    reserve = (supp.Decimal(2000) * supp.Decimal('.8') + supp.Decimal(4096) * supp.Decimal('2.8')) / 1000000
    pending = {"call_id": 1, "qid": "old-0", "requested_model": parent["model"], "state": "UNKNOWN_PENDING",
               "input_token_bound": 2000, "output_token_bound": 4096, "reservation_cny": str(reserve)}
    completed = {**pending, "state": "COMPLETED", "usage": {"prompt_tokens": 100, "completion_tokens": 50},
                 "actual_model": parent["model"], "price_based_cost_cny": ".00022"}
    unknown = {**pending, "call_id": 2, "qid": "old-1"}
    ledger = parent_output / "provider-ledger.jsonl"
    ledger.write_bytes(b"\n".join(supp.base.canonical(x) for x in (pending, completed, unknown)) + b"\n")
    summary = parent_output / "summary.json"
    summary.write_bytes(supp.base.canonical({"status": "STOPPED", "budget_cny": "1", "actual_http_calls": 2,
                                           "price_based_known_cost_cny": ".00022",
                                           "committed_including_unknown_cny": str(supp.Decimal('.00022') + reserve)}))
    return {"schema_version": supp.SCHEMA, "authorized": True, "authorization_id": "synthetic-disjoint-supp-v1",
            "purpose": "D_ELIGIBILITY_ONLY", "repetitions": 2, "parent_manifest_path": str(parent_manifest),
            "parent_manifest_sha256": supp.base.digest(parent_manifest.read_bytes()), "parent_output": str(parent_output),
            "parent_summary_sha256": supp.base.digest(summary.read_bytes()),
            "parent_ledger_sha256": supp.base.digest(ledger.read_bytes()),
            "case": {"qid": "m2-supp:tribometer-procedure-v1", "question": "如何根据材料查找摩擦仪（Tribometer）可以测量的各项指标？",
                     "expected_chunk_ids": ["hot-context-9"], "answer_points": ["coefficient of friction", "friction force", "wear volume"],
                     "source_quote": quote, "kb_scope": [supp.base.KB_ID], "negative": False, "gold_locator": "synthetic line 1"}}


def refresh_hashes(spec):
    output = Path(spec['parent_output'])
    for role, path in [('summary', output / 'summary.json'), ('ledger', output / 'provider-ledger.jsonl')]:
        spec[f'parent_{role}_sha256'] = supp.base.digest(path.read_bytes())


def test_remaining_counts_latest_once_and_retains_unknown_full_reservation(spec):
    result = supp.validate_spec(spec)
    budget = result['budget']
    assert budget['parent_attempts'] == 2 and budget['parent_unknown_attempts'] == 1
    assert supp.decimal(budget['parent_committed_cny']) == supp.Decimal('.0132888')
    assert supp.decimal(budget['remaining_cny']) == supp.Decimal('.9867112')
    assert result['policy_resolutions']['A']['policy_active'] is False
    assert result['policy_resolutions']['D']['effective_values']['output_preferences'] == ['step_by_step']
    assert result['planned_trials'] == 8 and result['model_calls'] == 0


@pytest.mark.parametrize('mutation', [
    lambda s: s['case'].update(qid='old-0'),
    lambda s: s['case'].update(question='What is the original question 0?'),
    lambda s: s['case'].update(question='摩擦仪测量什么？'),
    lambda s: s['case'].update(expected_chunk_ids=['missing']),
    lambda s: s['case'].update(answer_points=['unsupported claim']),
    lambda s: s.update(purpose='ORIGINAL_FOUR_ARM_ACCEPTANCE'),
    lambda s: s.update(repetitions=3),
])
def test_no_original_replay_or_ineligible_or_unsupported_gold(spec, mutation):
    mutation(spec)
    with pytest.raises(ValueError):
        supp.validate_spec(spec)


def test_parent_changed_and_summary_mismatch_fail_closed(spec):
    output = Path(spec['parent_output'])
    (output / 'provider-ledger.jsonl').write_bytes(b'changed')
    with pytest.raises(ValueError, match='artifact changed'):
        supp.verify_parent(spec)


def test_independent_cost_rejects_forged_parent_summary(spec):
    path = Path(spec['parent_output']) / 'summary.json'
    row = json.loads(path.read_bytes());row['committed_including_unknown_cny']='0'
    path.write_bytes(supp.base.canonical(row));refresh_hashes(spec)
    with pytest.raises(ValueError, match='disagree'):
        supp.verify_parent(spec)


def test_parent_terminal_attempt_cannot_be_rewritten(spec):
    path = Path(spec['parent_output']) / 'provider-ledger.jsonl'
    records = [json.loads(line) for line in path.read_bytes().splitlines()]
    records.append({**records[1], 'price_based_cost_cny': '0'})
    path.write_bytes(b'\n'.join(supp.base.canonical(row) for row in records)+b'\n');refresh_hashes(spec)
    with pytest.raises(ValueError, match='rewritten'):
        supp.verify_parent(spec)


def test_parent_change_prevents_supplementary_network_send(spec, tmp_path):
    parent, budget = supp.verify_parent(spec)
    guard = supp.ParentAwareTransport({**parent, 'budget_cny': budget['remaining_cny']}, tmp_path, spec)
    path = Path(spec['parent_output']) / 'summary.json';path.write_bytes(b'changed')
    async def sender(*args, **kwargs):
        pytest.fail('No send permitted after parent changes')
    request = httpx.Request('POST', supp.base.endpoint(parent['base_url']), json={'model': parent['model']})
    with pytest.raises(supp.base.GuardStop, match='primary evidence'):
        asyncio.run(guard.send(sender, None, request))
    assert not guard.calls


def test_plan_is_disjoint_ad_pair_per_mode_repeat(spec):
    plan = list(supp.supplement_plan({'cases': [spec['case']]}))
    assert len(plan) == 8
    assert [(mode, rep, arm) for case, mode, rep, arm, pref in plan] == [
        (mode, rep, arm) for mode in ('chat', 'agent') for rep in (1, 2) for arm in ('A', 'D')]


def test_supplement_unknown_retains_new_reservation_and_never_retries(spec, tmp_path):
    parent, budget = supp.verify_parent(spec)
    guard = supp.ParentAwareTransport({**parent, 'budget_cny': budget['remaining_cny']}, tmp_path, spec)
    guard.context = {'qid': spec['case']['qid']}
    attempts = []
    async def unknown_sender(client, request, *args, **kwargs):
        attempts.append(True)
        raise httpx.ReadTimeout('Synthetic unknown, no real connection')
    request = httpx.Request('POST', supp.base.endpoint(parent['base_url']),
                           json={'model': parent['model'], 'messages': [{'role': 'user', 'content': 'public synthetic question'}]})
    with pytest.raises(supp.base.GuardStop, match='UNKNOWN'):
        asyncio.run(guard.send(unknown_sender, None, request))
    assert guard.calls[0]['state'] == 'UNKNOWN'
    assert guard.committed == supp.decimal(guard.calls[0]['reservation_cny'])
    assert supp.decimal(budget['parent_committed_cny']) + guard.committed <= 1
    with pytest.raises(supp.base.GuardStop):
        asyncio.run(guard.send(unknown_sender, None, request))
    assert len(attempts) == 1
    assert supp.verify_parent(spec)[1] == budget


def test_zero_send_retry_requires_new_auth_and_no_actual_attempt(spec, tmp_path):
    previous=tmp_path/'var'/'failed-zero-send';previous.mkdir()
    (previous/'supplement-manifest.json').write_bytes(supp.base.canonical(spec))
    summary={'status':'FAILED','actual_http_calls':0,'committed_including_unknown_cny':'0'}
    (previous/'summary.json').write_bytes(supp.base.canonical(summary))
    retry={**spec,'authorization_id':'new-zero-send-retry','zero_send_retry_of':{
        'output':str(previous),'summary_sha256':supp.base.digest((previous/'summary.json').read_bytes())}}
    supp.verify_zero_send_retry(retry)
    (previous/'provider-ledger.jsonl').write_bytes(b'{}\n')
    with pytest.raises(ValueError,match='nonempty'):supp.verify_zero_send_retry(retry)
    (previous/'provider-ledger.jsonl').unlink()
    summary['actual_http_calls']=1;(previous/'summary.json').write_bytes(supp.base.canonical(summary))
    retry['zero_send_retry_of']['summary_sha256']=supp.base.digest((previous/'summary.json').read_bytes())
    with pytest.raises(ValueError,match='zero prior'):supp.verify_zero_send_retry(retry)


def test_fresh_process_preflight_and_real_redis_dry_run_leave_sentinel_db_untouched(spec,tmp_path):
    source=tmp_path/'safe-supplement.json';source.write_bytes(supp.base.canonical(spec))
    environment={**os.environ,'PYTHONIOENCODING':'utf-8'}
    if not environment.get('RAG_TEST_REDIS_CMD'):
        pointer=PATH.parents[1]/'var/preferences-acceptance/redis-test-runtime.txt'
        if pointer.exists() and shutil.which('wsl.exe'):
            environment['RAG_TEST_REDIS_CMD']=json.dumps(['wsl.exe','-d','Ubuntu','--exec',pointer.read_text().strip()])
    code=r'''
import asyncio,importlib.util,json,os,socket,sqlite3,sys
from pathlib import Path
import httpx
original_connect=socket.socket.connect
class ForbiddenExternalNetwork(BaseException):pass
def deny(sock,address,*a,**kw):
 if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):return original_connect(sock,address,*a,**kw)
 raise ForbiddenExternalNetwork('No external sockets in real Redis dry run')
socket.socket.connect=deny;socket.socket.connect_ex=deny
module=importlib.util.spec_from_file_location('safety',sys.argv[1]);s=importlib.util.module_from_spec(module);module.loader.exec_module(s)
s.base.private_path=lambda path:Path(path).resolve()
spec=json.loads(Path(sys.argv[2]).read_bytes());out=Path(sys.argv[3]);out.mkdir()
sentinel=out/'preexisting-sentinel.db';db=sqlite3.connect(sentinel);db.execute('create table sentinel(value text)');db.execute("insert into sentinel values ('preserve')");db.commit();db.close();before=s.base.digest(sentinel.read_bytes())
os.environ.update(DATABASE_URL='sqlite:///'+str(sentinel),ENV='dev',MEMORY_REDIS_URL='redis://127.0.0.1:1/0')
assert s.validate_spec(spec)['status']=='PASS'
assert 'app.core.database' not in sys.modules and 'app.core.config' not in sys.modules
parent,budget=s.verify_parent(spec);m={**parent,'cases':[spec['case']],'budget_cny':budget['remaining_cny']}
os.environ['M2_AB_API_KEY']='synthetic-only';calls=[]
async def provider(request):
 calls.append(True);return httpx.Response(200,request=request,json={'id':f'test-{len(calls)}','model':m['model'],'object':'chat.completion','created':1,'choices':[{'index':0,'message':{'role':'assistant','content':'摩擦系数、摩擦力、磨损体积。[1]'},'finish_reason':'stop'}],'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}})
g=s.ParentAwareTransport(m,out,spec,transport=httpx.MockTransport(provider))
with s.base.isolated_redis(out) as redis_url:
 s.base.configure_runtime(m,out,redis_url)
 with g.installed():
  s.assert_runtime_isolation(out,redis_url);s.base.build_index(m)
  def plan(manifest):
   for arm,prefs in s.ARMS.items():yield manifest['cases'][0],'chat',1,arm,prefs
  s.base.trial_plan=plan
  trials=asyncio.run(s.base.run_trials(m,out,g))
assert len(trials)==2 and len(calls)==2 and all(r['state']=='RETURNED' for r in trials)
assert (out/'isolated.db').exists() and s.base.digest(sentinel.read_bytes())==before
isolation=json.loads((out/'runtime-isolation.json').read_bytes());assert isolation['status']=='PASS' and isolation['redis_host']=='127.0.0.1'
assert not (out/'redis-runtime/redis.conf').exists()
print(json.dumps({'real_redis_trials':2,'real_provider_calls':0,'sentinel_unchanged':True}))
'''
    result=subprocess.run([sys.executable,'-c',code,str(PATH),str(source),str(tmp_path/'real-redis-dry-run')],
                          capture_output=True,text=True,encoding='utf-8',timeout=150,env=environment)
    assert result.returncode==0,(result.stdout[-2000:],result.stderr[-6000:])
    assert '"sentinel_unchanged": true' in result.stdout


def test_actual_services_supplement_8_trials_policy_delivery_under_socket_deny(spec, tmp_path):
    source = tmp_path / 'supplement.json';source.write_bytes(supp.base.canonical(spec))
    code = r'''
import asyncio,importlib.util,json,os,socket,sys
from pathlib import Path
import httpx
original_connect=socket.socket.connect
class ExternalNetworkForbidden(BaseException): pass
def block(sock,address,*a,**kw):
 if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):return original_connect(sock,address,*a,**kw)
 raise ExternalNetworkForbidden('No external sockets in synthetic integration')
socket.socket.connect=block;socket.socket.connect_ex=block
module=importlib.util.spec_from_file_location('supp',sys.argv[1]);s=importlib.util.module_from_spec(module);module.loader.exec_module(s)
s.base.private_path=lambda path:Path(path).resolve()
spec=json.loads(Path(sys.argv[2]).read_bytes());parent,budget=s.verify_parent(spec)
out=Path(sys.argv[3]);out.mkdir();os.environ['M2_AB_API_KEY']='synthetic-only'
m={**parent,'cases':[spec['case']],'budget_cny':budget['remaining_cny'],'dataset_version':'synthetic-supp'}
s.base.configure_runtime(m,out,'redis://127.0.0.1:1/0')
from app.services import memory
class FakeRedis:
 def __init__(self):self.rows={}
 async def hgetall(self,key):return self.rows.get(key,{}).copy()
 async def hset(self,key,mapping):self.rows.setdefault(key,{}).update(mapping)
 async def delete(self,key):self.rows.pop(key,None)
 async def aclose(self):pass
service=memory.MemoryService(client=FakeRedis());memory.get_memory_service=lambda:service
calls=[]
async def provider(request):
 p=json.loads(request.content);calls.append(p);msg={'role':'assistant','content':'摩擦系数、摩擦力及磨损体积。[1]'};finish='stop'
 if p.get('tools') and not any(x.get('role')=='tool' for x in p['messages']):
  msg={'role':'assistant','content':'','tool_calls':[{'id':'call_public','type':'function','function':{'name':'search_knowledge_base','arguments':json.dumps({'query':'tribometer friction'})}}]};finish='tool_calls'
 elif '文档相关性评分员' in str(p['messages']):msg={'role':'assistant','content':'0.95'}
 body={'id':f'synthetic-{len(calls)}','model':m['model'],'object':'chat.completion','created':1,'choices':[{'index':0,'message':msg,'finish_reason':finish}],'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}}
 if p.get('stream'):
  first={**body,'choices':[{'index':0,'delta':msg,'finish_reason':None}],'usage':None}
  last={**body,'choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':None}
  tail={**body,'choices':[]}
  return httpx.Response(200,request=request,content=b''.join(b'data: '+s.base.canonical(x)+b'\n\n' for x in (first,last,tail))+b'data: [DONE]\n\n',headers={'content-type':'text/event-stream'})
 return httpx.Response(200,request=request,json=body)
g=s.ParentAwareTransport(m,out,spec,transport=httpx.MockTransport(provider))
s.base.trial_plan=s.supplement_plan
with g.installed():
 s.base.build_index(m);trials=asyncio.run(s.base.run_trials(m,out,g))
assert len(trials)==8 and all(row['state']=='RETURNED' for row in trials)
audit=s.audit_delivery(out,spec)
assert all(row['any_policy_delivered']==(row['arm']=='D') for row in audit)
assert all(row['response_followed_steps']=='REQUIRES_INDEPENDENT_ANSWER_REVIEW' for row in audit)
assert s.decimal(budget['parent_committed_cny'])+g.committed<=1
assert all(c['qid']==spec['case']['qid'] for c in g.calls)
assert len({(c['mode'],c['arm'],c['repetition']) for c in g.calls})==8
print(json.dumps({'trials':len(trials),'synthetic_provider_calls':len(calls),'external_socket_calls':0}))
'''
    result = subprocess.run([sys.executable, '-c', code, str(PATH), str(source), str(tmp_path / 'integration')],
                            capture_output=True, text=True, encoding='utf-8', timeout=120,
                            env={**os.environ, 'PYTHONIOENCODING': 'utf-8'})
    assert result.returncode == 0, (result.stdout[-3000:], result.stderr[-6000:])
    assert '"trials": 8' in result.stdout

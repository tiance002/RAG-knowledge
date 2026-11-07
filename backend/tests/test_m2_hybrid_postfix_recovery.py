import importlib.util,json
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[2]
def load():
 s=importlib.util.spec_from_file_location('recovery_test',ROOT/'scripts/m2_hybrid_postfix_recovery.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def test_real_prior_zero_send_proof_and_distinct_grant():
 m=load();proof=m.verify_zero_send();assert len(proof)==4
 assert m.base.GRANT!=m.FAILED_GRANT and m.base.PREFIX.endswith('-v5')
 assert not m.base.GRANT.exists()

def test_prior_unexpected_request_or_identity_fails(tmp_path,monkeypatch):
 m=load();failed=tmp_path/'run';failed.mkdir();(failed/'calls').mkdir();(failed/'summary.jsonl').write_text(json.dumps({'calls':0,'error_type':'JSONDecodeError','provider_bill':'UNKNOWN','state':'STOPPED'})+'\n')
 plan={'synthetic':True};b=json.dumps(plan,indent=2).encode();(failed/'plan.json').write_bytes(b);reference=tmp_path/'plan.json';reference.write_bytes(b)
 grant=tmp_path/'grant';grant.write_text(json.dumps({'output':str(failed),'plan_sha256':m.hashlib.sha256(json.dumps(plan,sort_keys=True).encode()).hexdigest()}))
 monkeypatch.setattr(m,'FAILED',failed);monkeypatch.setattr(m,'FAILED_PLAN',reference);monkeypatch.setattr(m,'FAILED_GRANT',grant)
 assert m.verify_zero_send()
 (failed/'calls'/'unresolved.started.json').write_text('{}')
 with pytest.raises(ValueError):m.verify_zero_send()
 (failed/'calls'/'unresolved.started.json').unlink();(failed/'started.jsonl').write_text('{}\n')
 with pytest.raises(ValueError):m.verify_zero_send()

@pytest.mark.parametrize('value',[None,'/var/tmp/redis',json.dumps(['wsl.exe','--exec','wrong'])])
def test_bad_redis_env_blocks_before_execute_and_grant(monkeypatch,value):
 m=load()
 if value is None:monkeypatch.delenv('RAG_TEST_REDIS_CMD',raising=False)
 else:monkeypatch.setenv('RAG_TEST_REDIS_CMD',value)
 with pytest.raises(ValueError):m.execute({},'unused')
 assert not m.base.GRANT.exists()

def test_valid_redis_env_has_expected_argv(monkeypatch):
 m=load();binary=(m.VAR/'redis-test-runtime.txt').read_text(encoding='utf8').strip();expected=['wsl.exe','-d','Ubuntu','--exec',binary]
 monkeypatch.setenv('RAG_TEST_REDIS_CMD',json.dumps(expected));assert m.verify_redis_env()==expected

def test_v2_zero_send_evidence_and_utf8_guard(monkeypatch):
 import sys,types
 m=load();assert len(m.verify_v2_zero_send())==5
 with monkeypatch.context() as patch:
  patch.setattr(sys,'stdout',types.SimpleNamespace(encoding='gbk'))
  with pytest.raises(ValueError,match='UTF-8'):m.verify_output_encoding()

def test_actual_guard_with_mock_provider_and_frozen_prior(tmp_path,monkeypatch):
 import httpx
 m=load();plan=m.base.make_plan();monkeypatch.setattr(m.base,'GRANT',tmp_path/'v3.started');m.base.GRANT.write_bytes(b'owned')
 guard=m.base.Guard(tmp_path/'calls',plan,b'owned');seen=[]
 def reply(request):
  seen.append(json.loads(request.content));return httpx.Response(200,json={'model':'Qwen/Qwen3.5-4B','choices':[{'finish_reason':'stop'}]})
 mock=httpx.MockTransport(reply);adapter=m.base.Adapter(guard,mock,mock);adapter.context='synthetic:chat:A'
 request=httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'Qwen/Qwen3.5-4B','messages':[{'role':'user','content':'synthetic'}]})
 response=adapter.sync_send(request)
 assert response.status_code==200 and len(seen)==1 and guard.count==1
 row=json.loads(next(guard.directory.glob('*.started.json')).read_bytes());assert row['identity'].startswith('m2-hybrid-postfix-v5:')

def test_plan_excludes_v3_and_v4_started_including_unknown():
 m=load();plan=m.base.make_plan();prior=m.base.rows(m.RUN_V3/'started.jsonl')+m.base.rows(m.RUN_V4/'started.jsonl')
 excluded={m.base.identity(x) for x in prior}
 assert len(plan['identities'])==3 and len(excluded)==9
 assert not excluded.intersection(m.base.identity(x) for x in plan['identities'])
 assert plan['max_calls']==34 and plan['v3_unknown_503']=='RETAINED_DO_NOT_REPLAY' and plan['v4_unknown_connect_error']=='RETAINED_DO_NOT_REPLAY'
 assert plan['current_sources']==__import__('json').loads((m.RUN_V3/'plan.json').read_bytes())['current_sources']

def test_combined_call_cap_stops_before_provider_dispatch(tmp_path,monkeypatch):
 m=load();plan=m.base.make_plan();monkeypatch.setattr(m.base,'GRANT',tmp_path/'v4.started');m.base.GRANT.write_bytes(b'owned')
 g=m.base.Guard(tmp_path/'calls',plan,b'owned');g.continuation_attempts=34
 with pytest.raises(m.base.old.guardmod.GuardStop):g.begin('m2-hybrid-postfix-v5:synthetic:call-000001',None)
 assert g.count==0 and not list(g.directory.iterdir())

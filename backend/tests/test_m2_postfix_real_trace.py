import asyncio, importlib.util, json, socket
from pathlib import Path
import httpx, pytest
ROOT=Path(__file__).resolve().parents[2]

def runner():
 s=importlib.util.spec_from_file_location('postfix_test',ROOT/'scripts/m2_postfix_real_trace.py'); m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def test_historical_budget_and_current_code_contract():
 m=runner(); spec=m.make_spec(); history,parent,fresh,policy=m.validate(spec)
 assert fresh['budget_cny']=='0.1892580' and fresh['cases'][0]['question']==m.QUESTION
 assert spec['identities']==[m.IDENTITY] and policy['policy_active']
 spec['identities']=[]
 with pytest.raises(ValueError): m.validate(spec)

def test_ollama_port_blocked_before_connection():
 m=runner()
 with m.block_ollama(), socket.socket() as s:
  with pytest.raises(RuntimeError,match='Ollama port blocked'): s.connect(('127.0.0.1',11434))
  with pytest.raises(RuntimeError,match='Ollama port blocked'): s.connect_ex(('127.0.0.1',11434))

def test_claim_is_single_use(tmp_path,monkeypatch):
 m=runner();monkeypatch.setattr(m,'GRANT',tmp_path/'synthetic.started')
 data=m.claim({'synthetic':True},tmp_path/'out')
 with pytest.raises(FileExistsError):m.claim({'synthetic':True},tmp_path/'out2')
 assert m.GRANT.read_bytes()==data

@pytest.mark.parametrize('failure',['grant','identity','history','recorded','budget'])
def test_guard_blocks_dispatch(tmp_path,monkeypatch,failure):
 m=runner();monkeypatch.setattr(m,'validate',lambda spec:None);monkeypatch.setattr(m,'GRANT',tmp_path/'synthetic.started');m.GRANT.write_bytes(b'grant')
 manifest={'base_url':'https://api.siliconflow.cn/v1','model':'zai-org/GLM-5.3-Flash','budget_cny':str(m.REMAINING),'max_output_tokens':4096,'pricing':{'input_cny_per_million':'.8','output_cny_per_million':'2.8'}}
 g=m.Transport(manifest,tmp_path,{'identities':[m.IDENTITY]},b'grant');g.context=dict(m.IDENTITY)
 if failure=='grant':m.GRANT.write_bytes(b'changed')
 if failure=='identity':g.context['mode']='chat'
 if failure=='history':monkeypatch.setattr(m,'validate',lambda spec:(_ for _ in ()).throw(ValueError('changed')))
 if failure=='recorded':(tmp_path/'trials.jsonl').write_bytes(m.base.canonical(m.IDENTITY)+b'\n')
 content='x'*100000 if failure=='budget' else ''
 req=httpx.Request('POST',m.base.endpoint(manifest['base_url']),json={'model':manifest['model'],'messages':[{'role':'user','content':content}]})
 async def forbidden(*a,**k):pytest.fail('Must not dispatch')
 with pytest.raises(m.base.GuardStop):asyncio.run(g.send(forbidden,None,req))
 assert not g.calls and g.committed==0

def test_unknown_reserves_and_stops(tmp_path,monkeypatch):
 m=runner();monkeypatch.setattr(m,'validate',lambda spec:None);monkeypatch.setattr(m,'GRANT',tmp_path/'synthetic.started');m.GRANT.write_bytes(b'grant')
 manifest={'base_url':'https://api.siliconflow.cn/v1','model':'zai-org/GLM-5.3-Flash','budget_cny':str(m.REMAINING),'max_output_tokens':4096,'pricing':{'input_cny_per_million':'.8','output_cny_per_million':'2.8'}}
 g=m.Transport(manifest,tmp_path,{'identities':[m.IDENTITY]},b'grant');g.context=dict(m.IDENTITY)
 req=httpx.Request('POST',m.base.endpoint(manifest['base_url']),json={'model':manifest['model'],'messages':[]}); attempts=[]
 async def timeout(*a,**k):attempts.append(1);raise httpx.ReadTimeout('synthetic')
 with pytest.raises(m.base.GuardStop):asyncio.run(g.send(timeout,None,req))
 assert g.calls[0]['state']=='UNKNOWN' and g.committed==m.Decimal(g.calls[0]['reservation_cny'])
 with pytest.raises(m.base.GuardStop):asyncio.run(g.send(timeout,None,req))
 assert attempts==[1]

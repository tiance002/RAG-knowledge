import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace


def load_runner():
    path=Path(__file__).resolve().parents[2]/'scripts/ecs_glm_selected_replay.py'
    spec=importlib.util.spec_from_file_location('glm_runner',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module


def setup(monkeypatch,tmp_path,*,wrong_identity=False):
    import openai
    import hashlib
    m=load_runner();m.ROOT=tmp_path;m.guard=lambda:None;m.resource=lambda:{}
    rows=[]
    for i in range(44):
        request=dict(model=m.MODEL,messages=[dict(role='user',content=str(i))],max_tokens=4096,temperature=.7,stream=False,enable_thinking=False)
        m.write(f'requests/{i:03d}.json',request)
        rows.append(dict(index=i,item_id=f'q{i}',request_path=f'requests/{i:03d}.json',request_sha256=hashlib.sha256(json.dumps(request,ensure_ascii=False,sort_keys=True).encode()).hexdigest()))
    m.write('requests-manifest.json',rows)
    class DB:
        def execute(self,*args):return self
        def fetchone(self):return dict(model_name=m.MODEL,base_url='https://example.invalid',api_key='not-real')
        def close(self):pass
    monkeypatch.setattr(m.sqlite3,'connect',lambda *args,**kwargs:DB())
    calls=[];active=[0,0]
    class Client:
        def __init__(self,**kwargs):
            assert kwargs['max_retries']==0
            self.http=kwargs['http_client'];self.chat=SimpleNamespace(completions=SimpleNamespace(create=self.create))
        async def __aenter__(self):return self
        async def __aexit__(self,*a):await self.http.aclose()
        async def create(self,**kwargs):
            assert kwargs['model']==m.MODEL and kwargs['extra_body']=={'enable_thinking':False}
            i=int(kwargs['messages'][0]['content']);calls.append(i);active[0]+=1;active[1]=max(active)
            try:
                await asyncio.sleep(.001)
                if i==3:raise ValueError('synthetic question failure')
                model='wrong-provider-model' if wrong_identity else m.MODEL
                return SimpleNamespace(model=model,choices=[SimpleNamespace(message=SimpleNamespace(content='answer'),finish_reason='stop')],usage=None,
                    model_dump=lambda:dict(model=model,answer='answer'))
            finally:active[0]-=1
    monkeypatch.setattr(openai,'AsyncOpenAI',Client)
    return m,calls,active


def test_bounded_44_calls_skip_one_failure_without_extra_probe_or_retry(monkeypatch,tmp_path):
    m,calls,active=setup(monkeypatch,tmp_path);asyncio.run(m.run())
    results=json.loads((tmp_path/'results.json').read_text())
    assert len(calls)==len(set(calls))==len(results)==44 and active[1]==2
    summary=json.loads((tmp_path/'summary.json').read_text())
    assert summary['complete']==43 and summary['errors']==1 and summary['identity_matches']==43
    assert json.loads((tmp_path/'status.json').read_text())['phase']=='complete'


def test_wrong_first_model_stops_before_remaining_paid_calls(monkeypatch,tmp_path):
    m,calls,_=setup(monkeypatch,tmp_path,wrong_identity=True);asyncio.run(m.run())
    assert calls==[0]
    status=json.loads((tmp_path/'status.json').read_text());assert status['phase']=='blocked_model_probe'

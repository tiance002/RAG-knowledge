import asyncio, importlib.util, json, socket
from pathlib import Path
import httpx, pytest
ROOT=Path(__file__).resolve().parents[2]

def load():
 s=importlib.util.spec_from_file_location('continuation_test',ROOT/'scripts/m2_hybrid_postfix_continuation.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

@pytest.fixture(autouse=True)
def no_network(monkeypatch):
 loop=asyncio.new_event_loop()  # Windows asyncio creates a private loopback socketpair here.
 def deny(*a,**kw):raise AssertionError('Offline test forbids sockets and DNS')
 monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny);monkeypatch.setattr(socket,'getaddrinfo',deny)
 monkeypatch.setattr(asyncio,'run',lambda coroutine:loop.run_until_complete(coroutine))
 yield
 loop.close()

def test_prior_audit_plan_is_new_version_and_all_twelve():
 m=load(); plan=m.make_plan();assert len(plan['identities'])==12
 assert len({x['run_identity'] for x in plan['identities']})==12
 assert all(x['run_identity'].startswith(m.PREFIX+':') for x in plan['identities'])
 assert plan['model_calls']==0 and plan['reuse'].endswith('no corpus re-embedding')
 assert [x['arm'] for x in plan['identities'][:2]]==['A','D']
 assert plan['source_version'].startswith('post-fix')

def test_copy_existing_db_and_index_leaves_old_bytes(tmp_path):
 m=load();plan=m.make_plan();before=m.sha(m.PRIOR/'isolated.db');index=m.tree_inventory(m.PRIOR/'chroma')
 m.copy_reuse(tmp_path,plan)
 assert m.sha(m.PRIOR/'isolated.db')==before and m.sha(tmp_path/'isolated.db')==before
 assert m.tree_inventory(m.PRIOR/'chroma')==m.tree_inventory(tmp_path/'chroma')==index
 import chromadb
 from chromadb.config import Settings
 client=chromadb.PersistentClient(path=str(tmp_path/'chroma'),settings=Settings(anonymized_telemetry=False))
 collection=client.get_collection('kb_m2_ab_kb')
 assert collection.count()==80
 assert collection.query(query_embeddings=[[0.1]*1024],n_results=1)['ids'][0]
 assert m.sha(m.PRIOR/'isolated.db')==before and m.tree_inventory(m.PRIOR/'chroma')==index

def test_changed_plan_or_duplicate_grant_fails(tmp_path,monkeypatch):
 m=load();plan=m.make_plan();changed=dict(plan,models={})
 with pytest.raises(ValueError):m.validate(changed)
 monkeypatch.setattr(m,'GRANT',tmp_path/'once.started')
 data=m.claim({'test':True},tmp_path/'out')
 with pytest.raises(FileExistsError):m.claim({'test':True},tmp_path/'other')
 assert m.GRANT.read_bytes()==data

@pytest.mark.parametrize('failure',['grant','source','identity','prior'])
def test_dispatch_checks_fail_before_send(tmp_path,monkeypatch,failure):
 m=load();proof={'current_sources':{'test':'original'},'runner_sha256':m.sha(Path(m.__file__)), 'prior_db_sha256':'x','prior_index_files':{},'prior_grant_sha256':'y','prior_receipts_sha256':'z'}
 monkeypatch.setattr(m,'GRANT',tmp_path/'once.started');m.GRANT.write_bytes(b'owned')
 monkeypatch.setattr(m.old,'snapshot',lambda:{'test':'changed' if failure=='source' else 'original'})
 monkeypatch.setattr(m.old,'assert_empty_source_wal',lambda:None)
 monkeypatch.setattr(m,'prior_check',lambda:(None,{k:proof[k] for k in ('prior_db_sha256','prior_index_files','prior_grant_sha256','prior_receipts_sha256')} if failure!='prior' else {'prior_db_sha256':'changed'}))
 g=m.Guard(tmp_path/'calls',proof,b'owned')
 if failure=='grant':m.GRANT.write_bytes(b'changed')
 rid=('bad' if failure=='identity' else m.PREFIX+':case')+':call-000001'
 req=httpx.Request('POST','https://api.siliconflow.cn/v1/embeddings',json={'model':'BAAI/bge-m3','input':['synthetic']})
 with pytest.raises(m.old.guardmod.GuardStop):g.begin(rid,req)
 assert g.count==0 and not list(g.directory.iterdir())

def test_adapter_keeps_new_identity_and_400_stops_no_retry(tmp_path,monkeypatch):
 m=load();proof={'current_sources':{},'runner_sha256':m.sha(Path(m.__file__)),'prior_db_sha256':'x','prior_index_files':{},'prior_grant_sha256':'y','prior_receipts_sha256':'z'}
 monkeypatch.setattr(m,'GRANT',tmp_path/'once.started');m.GRANT.write_bytes(b'owned');monkeypatch.setattr(m.old,'snapshot',lambda:{})
 monkeypatch.setattr(m.old,'assert_empty_source_wal',lambda:None);monkeypatch.setattr(m,'prior_check',lambda:(None,{k:proof[k] for k in ('prior_db_sha256','prior_index_files','prior_grant_sha256','prior_receipts_sha256')}))
 g=m.Guard(tmp_path/'calls',proof,b'owned');seen=[]
 def response(req):seen.append(1);return httpx.Response(400,json={'code':20015})
 transport=httpx.MockTransport(response);adapter=m.Adapter(g,transport,transport);adapter.context='synthetic:chat:D'
 req=httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'Qwen/Qwen3.5-4B','messages':[{'role':'system','content':'a'},{'role':'system','content':'b'},{'role':'user','content':'c'}]})
 with pytest.raises(m.old.guardmod.GuardStop):adapter.sync_send(req)
 with pytest.raises(m.old.guardmod.GuardStop):adapter.sync_send(req)
 assert seen==[1] and g.stopped
 record=json.loads(next(g.directory.glob('*.started.json')).read_bytes());assert record['identity'].startswith(m.PREFIX+':synthetic:chat:D:call-')
 payload=json.loads(next(g.directory.glob('*.request.json')).read_bytes());assert [v['role'] for v in payload['messages']]==['system','system','user']

@pytest.mark.parametrize('stop_after',[None,3])
def test_run_sequence_records_new_ids_and_stops_on_failure(tmp_path,monkeypatch,stop_after):
 import sys,types
 from contextlib import asynccontextmanager,contextmanager
 m=load();plan=m.make_plan();plan['identities']=plan['identities'][:4]
 class Redis:
  async def aclose(self):pass
 class Scope:
  def __init__(self,**kwargs):self.kwargs=kwargs
 monkeypatch.setitem(sys.modules,'app.services.memory',types.SimpleNamespace(PreferenceScope=Scope,get_memory_service=lambda:types.SimpleNamespace(client=Redis())))
 @contextmanager
 def capture_scope(**kwargs):yield
 monkeypatch.setitem(sys.modules,'app.services.trace_schema',types.SimpleNamespace(capture_scope=capture_scope))
 monkeypatch.setitem(sys.modules,'app.services.vector_store',types.SimpleNamespace(VectorStoreService=lambda:types.SimpleNamespace(get_count=lambda _:80)))
 @asynccontextmanager
 async def bound(*a,**kw):yield
 monkeypatch.setattr(m.old.app,'bound_provider',bound)
 seen=[]
 async def trial(case,mode,arm,scope,adapter,on_event):
  seen.append((case['qid'],mode,arm));on_event({'event':'synthetic','data':{}})
  if stop_after and len(seen)==stop_after:raise m.old.guardmod.GuardStop('synthetic')
  return {'state':'RETURNED','events':[]}
 monkeypatch.setattr(m.old.app,'trial',trial)
 adapter=types.SimpleNamespace(guard=types.SimpleNamespace(stopped=False))
 if stop_after:
  with pytest.raises(m.old.guardmod.GuardStop):asyncio.run(m.run(plan,tmp_path,adapter,'synthetic'))
 else:asyncio.run(m.run(plan,tmp_path,adapter,'synthetic'))
 started=m.rows(tmp_path/'started.jsonl');trials=m.rows(tmp_path/'trials.jsonl');events=m.rows(tmp_path/'events.jsonl')
 assert len(started)==len(trials)==len(seen)==len(events)==(stop_after or 4)
 assert all(x['run_identity'].startswith(m.PREFIX+':') for x in started)
 if stop_after:assert trials[-1]['state']=='STOPPED'

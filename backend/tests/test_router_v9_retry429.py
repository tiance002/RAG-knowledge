import asyncio, importlib.util, pathlib, tempfile, unittest
ROOT=pathlib.Path(__file__).resolve().parents[2]
def load(name,path):
 s=importlib.util.spec_from_file_location(name,path); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
v=load('retry429',ROOT/'scripts/ecs_router_v9_retry429.py'); runner=load('oldrunner',ROOT/'scripts/ecs_dev200_router_validation.py')
runner.resources=lambda p:{'mem_available':2**31,'disk_free':2**33}
class Pacer:
 def __init__(self): self.state={'cooldown_until':0};self.finishes=[]
 async def before(self,e): pass
 def reserve(self,e,p): pass
 def finish(self,p,a,is429,h): self.finishes.append((is429,h))
 def save(self): pass
class Err(Exception):
 def __init__(self,code): self.status_code=code; self.response=None
class Response:
 def model_dump(self,mode): return {'model':'model','choices':[{'message':{'content':'ok'},'finish_reason':'stop'}],'usage':{'total_tokens':25}}
class Client:
 def __init__(self,seq): self.chat=self;self.completions=self;self.seq=list(seq);self.calls=0
 async def create(self,**kw):
  self.calls+=1;v0=self.seq.pop(0)
  if isinstance(v0,Exception): raise v0
  return v0
class Tests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.root=pathlib.Path(self.t.name);self.messages=[{'role':'user','content':'hi'}];self.sha=v.sha(v.canonical(self.messages));self.binding={'id':'id','model':'model','api_key':'secret'};self.original={'index':2,'item_id':'q','role':'cheap','state':'UNKNOWN','status_code':429,'error_type':'RateLimitError','messages_sha256':self.sha,'model_binding_id':'id','requested_model':'model'};self.prepared={'index':2,'item_id':'q','role':'cheap','messages':self.messages,'messages_sha256':self.sha};self.op=self.root/'original.json';runner.atomic_json(self.op,self.original)
 def tearDown(self): self.t.cleanup()
 async def test_explicit429_then_success_no_duplicate(self):
  client=Client([Err(429),Response()]);p=Pacer();result=await v.handle(self.op,self.prepared,self.binding,client,self.root,runner,p);self.assertEqual((result['status'],client.calls),('COMPLETED',2));again=await v.handle(self.op,self.prepared,self.binding,client,self.root,runner,p);self.assertEqual((again['status'],client.calls),('COMPLETED',2))
 async def test_reserved_and_other_unknown_never_resent(self):
  path=self.root/'attempts/002-cheap/429-01-503-00.json'; runner.atomic_json(path,dict(index=2,item_id='q',role='cheap',original_path=str(self.op),messages_sha256=self.sha,model_binding_id='id',requested_model='model',state='RESERVED')); c=Client([]);r=await v.handle(self.op,self.prepared,self.binding,c,self.root,runner,Pacer());self.assertEqual((r['status'],c.calls),('RESERVED',0));self.assertFalse(v.is_eligible(dict(self.original,status_code=None)))
 async def test_503_and_429_limits(self):
  c=Client([Err(503),Response()]);r=await v.handle(self.op,self.prepared,self.binding,c,self.root,runner,Pacer());self.assertEqual((r['status'],c.calls),('COMPLETED',2))
 async def test_three_429_exhaustion(self):
  c=Client([Err(429),Err(429),Err(429)]);r=await v.handle(self.op,self.prepared,self.binding,c,self.root,runner,Pacer());self.assertEqual((r['status'],c.calls),('RETRY_429_EXHAUSTED',3))
 def test_mismatch_and_headers(self):
  with self.assertRaisesRegex(RuntimeError,'ORIGINAL_IDENTITY_MISMATCH'):v.verify_original(self.original,dict(self.prepared,messages=[{'role':'user','content':'changed'}]),self.binding)
  class E: response=type('R',(),{'headers':{'Retry-After':'75','x-ratelimit-limit-tokens':'80000','authorization':'secret'}})()
  self.assertEqual(v.headers_from_exception(E()),{'retry-after':'75','x-ratelimit-limit-tokens':'80000'})
 def test_cooldown_persist_and_adaptive_limit(self):
  p=v.Pacer(self.root,runner,80000,20);p.reserve(12000,'x');p.finish('x',None,True,{'retry-after':'75'});self.assertEqual(p.state['working_limit'],56000);self.assertGreaterEqual(p.state['cooldown_until']-__import__('time').time(),74);p2=v.Pacer(self.root,runner,80000,20);self.assertEqual(p2.state['working_limit'],56000)
if __name__=='__main__': unittest.main()

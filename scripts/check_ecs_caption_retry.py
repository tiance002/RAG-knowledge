"""Exercise the actual request ledger using a simulated timeout; no provider calls."""
import base64,json,tempfile
from pathlib import Path
from types import SimpleNamespace
import httpx
from ecs_benchmark200_protocol import Ledger,sha

image=b'caption-retry-test-image'
asset=sha(image)
models={name:SimpleNamespace(id=name,model_name=name,base_url='https://example.test/v1')
        for name in ('embedding','rerank','llm','vlm')}
bounds={'embedding_inputs':10,'rerank_calls':10,'generation_calls':10,'caption_calls':1}
manifest={'caption_retry_until_success':True}
body={'model':'vlm','max_tokens':1000,'messages':[{'role':'user','content':[{'type':'image_url',
      'image_url':{'url':'data:image/png;base64,'+base64.b64encode(image).decode()}}]}]}
attempts=[]
def handle(request):
 attempts.append(1)
 if len(attempts)==1:raise httpx.ReadTimeout('simulated timeout',request=request)
 return httpx.Response(200,json={'choices':[{'message':{'content':'ok'}}]},request=request)
with tempfile.TemporaryDirectory() as tmp:
 ledger=Ledger(Path(tmp),models,bounds,manifest)
 ledger.state['allowed_caption_assets']=[asset];ledger.state['status']='RUNNING';ledger.save();ledger.install()
 with httpx.Client(transport=httpx.MockTransport(handle)) as client:
  try:client.post('https://example.test/v1/chat/completions',json=body)
  except httpx.ReadTimeout:pass
  else:raise AssertionError('Expected simulated timeout')
  assert ledger.state['status']=='RUNNING' and not ledger.stop.is_set()
  assert client.post('https://example.test/v1/chat/completions',json=body).status_code==200
 assert [v['status'] for v in ledger.state['requests']]==['UNKNOWN_RETRYING','HTTP_SUCCESS']
 assert ledger.state['caption_calls']==2 and ledger.state['caption_assets_seen']==[asset]
print(json.dumps({'result':'PASS','simulated_provider_calls':2,'real_provider_calls':0,'reused_asset':True}))

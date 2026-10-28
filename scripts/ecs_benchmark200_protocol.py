"""Bounded request ledger for the isolated200-case experiment (no automatic replay)."""
import ast,base64,hashlib,json,threading,time
from pathlib import Path

def sha(value):return hashlib.sha256(value).hexdigest()
def template_sha(path):
 tree=ast.parse(Path(path).read_text(encoding='utf-8'))
 values=[n.value for n in ast.walk(tree) if isinstance(n,ast.Assign) and isinstance(n.value,ast.JoinedStr)
         and any(isinstance(t,ast.Name) and t.id=='prompt' for t in n.targets)]
 if len(values)!=1:raise ValueError('One fixed answer prompt required')
 return sha(ast.dump(values[0],include_attributes=False).encode())

class Ledger:
 def __init__(self,root,models,bounds,manifest):
  self.root=root;self.models=models;self.bounds=bounds;self.manifest=manifest;self.lock=threading.RLock();self.stop=threading.Event()
  path=root/'state.json'
  prior=json.loads(path.read_bytes()) if path.exists() else None
  if prior and (prior.get('requests') or prior['status']!='PREPARED' or prior['manifest_sha256']!=sha(json.dumps(manifest,sort_keys=True).encode())):
   raise RuntimeError('Existing dispatch journal: no automatic replay')
  self.state={'status':'PREPARED','phase':'PREFLIGHT','manifest_sha256':sha(json.dumps(manifest,sort_keys=True).encode()),
    'models':{r:{'id':m.id,'model':m.model_name,'endpoint_sha256':sha(m.base_url.encode())} for r,m in models.items()},
    'bounds':bounds,'embedding_inputs':0,'rerank_calls':0,'generation_calls':0,'caption_calls':0,
    'caption_assets_seen':[],'requests':[]}
  self.save()
 def save(self):
  with self.lock:
   path=self.root/'state.tmp';path.write_text(json.dumps(self.state,ensure_ascii=False),encoding='utf-8');path.replace(self.root/'state.json')
 def phase(self,value):
  with self.lock:self.state['phase']=value;self.save()
 def reserve(self,request):
  with self.lock:
   if self.stop.is_set():raise RuntimeError('Previous provider failure/unknown: no new dispatch')
   body=json.loads(request.content);model=body.get('model');path=request.url.path
   role=None
   if path.endswith('/embeddings') and model==self.models['embedding'].model_name:
    role='embedding';count=len(body['input']) if isinstance(body['input'],list) else 1;key='embedding_inputs'
   elif path.endswith('/rerank') and model==self.models['rerank'].model_name:role='rerank';count=1;key='rerank_calls'
   elif path.endswith('/chat/completions') and model==self.models['llm'].model_name:
    role='llm';count=1;key='generation_calls'
    if body.get('max_tokens')!=2000 or body.get('temperature')!=.7:raise RuntimeError('Generation parameter drift')
   elif path.endswith('/chat/completions') and model==self.models['vlm'].model_name:
    role='vlm';count=1;key='caption_calls'
    if body.get('max_tokens')!=1000:raise RuntimeError('Caption output bound drift')
   if role is None or not str(request.url).startswith(self.models[role].base_url.rstrip('/')+'/'):
    self.stop.set();raise RuntimeError('Unexpected endpoint/model; fallback prohibited')
   asset=None
   retry_caption=False
   if role=='vlm':
    asset=None
    for message in body.get('messages',[]):
     for part in message.get('content',[]) if isinstance(message.get('content'),list) else []:
      if part.get('type')=='image_url':
       url=part.get('image_url',{}).get('url','')
       if url.startswith('data:') and ',' in url:
        asset=sha(base64.b64decode(url.split(',',1)[1]));break
     if asset:break
    if asset not in set(self.state.get('allowed_caption_assets',[])):
     self.stop.set();raise RuntimeError('Unexpected caption asset; refusing provider dispatch')
    self.state['caption_assets_seen']=sorted(set(self.state['caption_assets_seen'])|{asset})
    retry_caption=self.manifest.get('caption_retry_until_success') is True
   elif self.state[key]+count>self.bounds[key]:raise RuntimeError(key+' bound exceeded')
   self.state[key]+=count
   record={'role':role,'request_sha256':sha(request.content),'status':'RESERVED','ordinal':len(self.state['requests'])}
   if asset:record['asset_sha256']=asset
   if retry_caption:record['retry_caption']=True
   self.state['requests'].append(record);self.save();return record
 def finish(self,record,response=None):
  with self.lock:
   if response is None or response.status_code>=400:
    if record.get('role')=='vlm' and record.get('retry_caption'):
     record['status']='UNKNOWN_RETRYING';self.state['status']='RUNNING'
    else:record['status']='UNKNOWN_OR_FAILED';self.state['status']='UNKNOWN_STOP';self.stop.set()
   else:record['status']='HTTP_SUCCESS'
   if response is not None:
    record['http_status']=response.status_code
    try:record['usage']=response.json().get('usage')
    except Exception:pass
   self.save()
 def install(self):
  import httpx
  sync_send=httpx.Client.send;async_send=httpx.AsyncClient.send
  def sync(client,request,*a,**k):
   record=self.reserve(request)
   try:response=sync_send(client,request,*a,**k)
   except BaseException:self.finish(record);raise
   self.finish(record,response);return response
  async def async_(client,request,*a,**k):
   record=self.reserve(request)
   try:response=await async_send(client,request,*a,**k)
   except BaseException:self.finish(record);raise
   self.finish(record,response);return response
  httpx.Client.send=sync;httpx.AsyncClient.send=async_

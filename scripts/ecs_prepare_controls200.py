"""Materialize official text retrieval corpora with stable source IDs on ECS."""
from dataclasses import asdict
import hashlib,json,os,pathlib,sys,time

def main():
 root=pathlib.Path(sys.argv[1]);runtime=root/'runtime200-r1'
 os.environ.update(ENV='test',DATABASE_URL='sqlite:///:memory:')
 sys.path.insert(0,str(runtime/'parser_source/backend'))
 import httpx
 def no_network(*a,**k):raise RuntimeError('Control preprocessing has no provider calls')
 async def ano_network(*a,**k):raise RuntimeError('Control preprocessing has no provider calls')
 httpx.Client.send=no_network;httpx.AsyncClient.send=ano_network
 from app.services.parser import FileParser
 output=runtime/'text-profiles.jsonl'
 if output.exists():raise ValueError('No control corpus overwrite')
 count=0;docs=0
 with output.open('w') as handle:
  for dataset in ('scifact','miracl-zh'):
   source=pathlib.Path('/opt/rag-p7-r1/data/v6-dev24-inputs/inputs/public')/dataset/'corpus.jsonl'
   folder=runtime/'text-sources'/dataset;folder.mkdir(parents=True,exist_ok=True)
   parser=FileParser('v200-'+dataset)
   for line in source.open():
    if not line.strip():continue
    doc=json.loads(line);docid=str(doc['doc_id'])
    title=doc.get('title') or '';text=doc.get('text') or ''
    raw=(title+'\n\n'+text).strip();key=hashlib.sha256((dataset+':'+docid).encode()).hexdigest()
    path=folder/(key+'.txt');path.write_text(raw,encoding='utf-8')
    chunks=parser.parse(str(path),chunk_mode='custom',chunk_size=500,chunk_overlap=50,use_semantic=False,embedding_model=None)
    record={'profile':dataset,'source_id':docid,'name':title or docid,'raw_source_sha256':hashlib.sha256(raw.encode()).hexdigest(),
            'chunks':[asdict(c) for c in chunks]}
    handle.write(json.dumps(record,ensure_ascii=False)+'\n');docs+=1;count+=len(chunks)
    if docs%500==0:print(json.dumps({'documents':docs,'chunks':count}),flush=True)
 (runtime/'text-preparation.json').write_text(json.dumps({'status':'COMPLETE','documents':docs,'chunks':count,'model_calls':0}))
 print(json.dumps({'status':'COMPLETE','documents':docs,'chunks':count,'model_calls':0}),flush=True)

if __name__=='__main__':main()

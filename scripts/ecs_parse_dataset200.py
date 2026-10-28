"""Single-document parser worker, isolated on ECS; network calls prohibited."""
from dataclasses import asdict
import hashlib,json,os,pathlib,sys,time


def main():
 root=pathlib.Path(sys.argv[1]);source=json.loads(sys.argv[2]);out=root/'parsed'/source['sha256']
 if out.exists():raise ValueError('No parser result overwrite')
 out.mkdir(parents=True)
 os.environ.update(ENV='test',DATABASE_URL='sqlite:///:memory:',PDF_TESSDATA_DIR=str(root/'tessdata'))
 sys.path.insert(0,str(root/'parser_source/backend'))
 import httpx
 def forbidden(*a,**k):raise RuntimeError('Parser-phase network forbidden')
 async def aforbidden(*a,**k):raise RuntimeError('Parser-phase network forbidden')
 httpx.Client.send=forbidden;httpx.AsyncClient.send=aforbidden
 from app.services.parser import FileParser,settings
 settings.PDF_PARSER_MODE='enhanced';settings.PDF_SCANNED_TABLES_ENABLED=True
 settings.PDF_TESSDATA_DIR=str(root/'tessdata');settings.PDF_AUDIT_DIR=str(out/'manifests')
 for arm,enabled in [('before',False),('after',True)]:
  settings.PDF_TABLES_ENABLED=enabled;settings.IMAGE_DIR=str(out/arm/'images')
  p=FileParser('pdf200');start=time.monotonic()
  chunks=p.parse(source['path'],chunk_mode='custom',chunk_size=500,chunk_overlap=50,use_semantic=False,embedding_model=None)
  payload={'source_sha256':source['sha256'],'source':source,'pages':p.pdf_page_records,
           'chunks':[asdict(c) for c in chunks],'seconds':time.monotonic()-start}
  (out/(arm+'.json')).write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
 a=json.loads((out/'before.json').read_bytes());b=json.loads((out/'after.json').read_bytes())
 assert len(a['pages'])==len(b['pages'])
 for x,y in zip(a['pages'],b['pages']):assert x['page_text_sha256']==y['page_text_sha256']
 status={'sha256':source['sha256'],'pages':len(a['pages']),'before':len(a['chunks']),'after':len(b['chunks']),
         'base_OCR_hashes_equal':True,'model_calls':0}
 (out/'complete.json').write_text(json.dumps(status));print(json.dumps(status),flush=True)


if __name__=='__main__':main()

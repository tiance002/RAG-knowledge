"""Freeze ECS-only sources, parser outputs and a credential-free control snapshot."""
import concurrent.futures,hashlib,json,os,pathlib,shutil,sqlite3,subprocess,sys,time

def digest(p):return hashlib.sha256(p.read_bytes()).hexdigest()

def main():
 root=pathlib.Path(sys.argv[1]);runtime=root/'runtime200-r1'
 if runtime.exists():raise ValueError('No runtime overwrite')
 runtime.mkdir(mode=0o700)
 frozen=json.loads((root/'dataset200/freeze.json').read_bytes())
 for name,sha in frozen['files'].items():assert digest(root/'dataset200'/name)==sha
 stage=pathlib.Path('/opt/amrag/experiments/pdf-table-fix-20261027')
 shutil.copytree(stage/'backend/app',runtime/'parser_source/backend/app',ignore=shutil.ignore_patterns('__pycache__'))
 (runtime/'parser_source/backend/app/__init__.py').write_text('')
 (runtime/'parser_source/backend/app/services/__init__.py').write_text('')
 shutil.copytree(stage/'var/tessdata',runtime/'tessdata')
 sources=json.loads((root/'dataset200/new-source-manifest.json').read_bytes())
 for sha,row in sources.items():row.update(sha256=sha,name=pathlib.Path(row['path']).name,profile='pdf-new')
 identity=json.loads((stage/'identity-before.json').read_bytes())
 for sha,row in identity['pdfs'].items():
  assert digest(pathlib.Path(row['path']))==sha
  sources[sha]={'sha256':sha,'path':row['path'],'name':pathlib.Path(row['path']).name.split('_',1)[1],
                'profile':'pdf-regression','pages':row['pages']}
 (runtime/'sources.json').write_text(json.dumps(sources,ensure_ascii=False,indent=2))
 # Copy only source/index artifacts; no production CustomModel rows are copied.
 prod=pathlib.Path('/opt/amrag/backend/storage/chroma_db')
 stat=lambda:{str(p.relative_to(prod)):(p.stat().st_size,p.stat().st_mtime_ns) for p in prod.rglob('*') if p.is_file()}
 before=stat();shutil.copytree(prod,runtime/'chroma')
 assert before==stat(),'Production Chroma changed during snapshot; do not use this attempt'
 for name in before:assert digest(prod/name)==digest(runtime/'chroma'/name),name
 con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True);con.row_factory=sqlite3.Row
 kbs=[dict(r) for r in con.execute("select * from knowledge_bases where name in ('prod-longbench-zh','prod-hotpotqa')")]
 assert len(kbs)==2
 with (runtime/'control-sql.jsonl').open('w') as f:
  for kb in kbs:
   f.write(json.dumps({'kind':'kb','value':kb})+'\n')
   for file in con.execute('select * from file_documents where kb_id=?',(kb['id'],)):
    file=dict(file);f.write(json.dumps({'kind':'file','value':file})+'\n')
    for chunk in con.execute('select * from document_chunks where file_id=? order by original_index',(file['id'],)):
     f.write(json.dumps({'kind':'chunk','value':dict(chunk)},ensure_ascii=False)+'\n')
 con.close()
 (runtime/'control-snapshot.json').write_text(json.dumps({'knowledge_bases':[{k:v for k,v in kb.items() if k in ('id','name','embedding_model')} for kb in kbs],
     'chroma_files':len(before),'control_sql_sha256':digest(runtime/'control-sql.jsonl'),'custom_models_copied':0}))
 def parse(source):
  log=runtime/(source['sha256']+'.log')
  with log.open('w') as f:
   r=subprocess.run([sys.executable,str(root/'parse_worker.py'),str(runtime),json.dumps(source)],stdout=f,stderr=subprocess.STDOUT)
  result={'source':source['name'],'sha256':source['sha256'],'exit_code':r.returncode}
  if r.returncode==0:result.update(json.loads((runtime/'parsed'/source['sha256']/'complete.json').read_bytes()))
  print(json.dumps(result),flush=True)
  return result
 with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(parse,sources.values()))
 (runtime/'preparation.json').write_text(json.dumps({'status':'PARSED' if all(r['exit_code']==0 for r in results) else 'FAILED',
      'results':results,'model_calls':0},ensure_ascii=False,indent=2))
 if any(r['exit_code'] for r in results):raise SystemExit(1)


if __name__=='__main__':main()

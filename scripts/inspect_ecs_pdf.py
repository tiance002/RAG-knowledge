"""Read-only ECS source/corpus identity. No credentials or model calls in output."""
import argparse
import json
from pathlib import Path
import subprocess

REMOTE = r'''
import hashlib,json,pathlib,sqlite3,sys,os
import fitz
root=pathlib.Path('/opt/amrag')
files={}
for p in (root/'backend/app').rglob('*.py'):
    files[str(p.relative_to(root))]=hashlib.sha256(p.read_bytes()).hexdigest()
pdfs={}
for base in (root/'backend/storage/uploads',root/'storage/uploads'):
    for p in base.rglob('*.pdf'):
        sha=hashlib.sha256(p.read_bytes()).hexdigest()
        if sha not in pdfs:
            with fitz.open(p) as doc:
                pdfs[sha]={'path':str(p),'bytes':p.stat().st_size,'pages':len(doc)}
db=root/'backend/local.db'
con=sqlite3.connect('file:'+str(db)+'?mode=ro',uri=True)
models=[dict(zip(('id','type','model_name','context_length'),r)) for r in con.execute('select id,model_type,model_name,context_length from custom_models')]
counts={table:con.execute('select count(*) from '+table).fetchone()[0] for table in ('knowledge_bases','file_documents','document_chunks')}
con.close()
print(json.dumps({'root':str(root),'pymupdf':fitz.VersionBind,'python':sys.version,'source_files':files,'pdfs':pdfs,'models':models,'counts':counts}))
'''


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--host', required=True)
    p.add_argument('--identity', required=True)
    p.add_argument('--output', required=True)
    args=p.parse_args()
    command=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','IdentitiesOnly=yes',
             '-o','ConnectTimeout=12','-i',args.identity,'root@'+args.host,
             '/opt/amrag/.venv/bin/python','-']
    result=subprocess.run(command,input=REMOTE.encode(),capture_output=True,timeout=60)
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors='replace'))
    value=json.loads(result.stdout)
    target=Path(args.output)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'READ_ONLY_COMPLETE','unique_pdfs':len(value['pdfs']),
                      'models':value['models'],'counts':value['counts'],'output':str(target)}))


if __name__=='__main__':
    main()

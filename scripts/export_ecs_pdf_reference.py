"""Read-only export of indexed PDF text/captions and source identities."""
import argparse
import json
from pathlib import Path
import subprocess

REMOTE = r'''
import hashlib,json,pathlib,sqlite3
root=pathlib.Path('/opt/amrag')
con=sqlite3.connect('file:/opt/amrag/backend/local.db?mode=ro',uri=True)
con.row_factory=sqlite3.Row
files=[]
for row in con.execute("select f.id,f.kb_id,f.name,f.local_path,k.name as kb_name from file_documents f join knowledge_bases k on k.id=f.kb_id where lower(f.name) like '%.pdf'"):
    item=dict(row);p=pathlib.Path(item['local_path'])
    if not p.is_absolute():p=root/'backend'/p
    if not p.is_file():continue
    item['source_sha256']=hashlib.sha256(p.read_bytes()).hexdigest()
    item['chunks']=[dict(c) for c in con.execute('select id,content,page_number,content_type,image_path,original_index,parent_id from document_chunks where file_id=? order by original_index',(item['id'],))]
    files.append(item)
con.close()
print(json.dumps({'files':files}))
'''


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--host',required=True)
    ap.add_argument('--identity',required=True)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    cmd=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','IdentitiesOnly=yes',
         '-i',args.identity,'root@'+args.host,'/opt/amrag/.venv/bin/python','-']
    result=subprocess.run(cmd,input=REMOTE.encode(),capture_output=True,timeout=60)
    if result.returncode:raise RuntimeError(result.stderr.decode(errors='replace'))
    data=json.loads(result.stdout)
    target=Path(args.output)
    if target.exists():raise SystemExit('Refuse to overwrite reference')
    target.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'files':len(data['files']),'KBs':sorted({f['kb_name'] for f in data['files']}),
                      'chunks':sum(len(f['chunks']) for f in data['files'])}))


if __name__=='__main__':main()

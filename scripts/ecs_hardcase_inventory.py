"""Run ONLY on ECS: source metadata download; never transports PDFs to Windows."""
import argparse
import hashlib
import json
from pathlib import Path
import urllib.request
import socket

REV='2ff6aa9237fc777b6627dc57a486e9225ac5fb86'
BASE='https://huggingface.co/datasets/yubo2333/MMLongBench-Doc'


def fetch(url,target):
    if target.exists():raise ValueError('No overwrite: '+str(target))
    request=urllib.request.Request(url,headers={'User-Agent':'PDF-table-evaluation/1.0'})
    with urllib.request.urlopen(request,timeout=40) as response:
        data=response.read(16_000_001)
    if len(data)>16_000_000:raise ValueError('Metadata download bound')
    target.write_bytes(data)
    return data


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True)
    ap.add_argument('--endpoint',choices=['https://huggingface.co','https://hf-mirror.com'],default='https://huggingface.co')
    args=ap.parse_args();root=Path(args.root);root.mkdir(parents=True,exist_ok=True)
    old=socket.getaddrinfo
    socket.getaddrinfo=lambda host,port,*a,**k:old(host,port,socket.AF_INET,socket.SOCK_STREAM)
    meta=root/('source-metadata' if args.endpoint==BASE.split('/datasets')[0] else 'source-metadata-mirror');meta.mkdir(exist_ok=True)
    url=f'{args.endpoint}/api/datasets/yubo2333/MMLongBench-Doc/tree/{REV}?recursive=true&limit=1000'
    files=json.loads(fetch(url,meta/'hf-tree.json'))
    parquet=[f for f in files if f['path'].endswith('.parquet')]
    for item in parquet:
        raw=fetch(f'{args.endpoint}/datasets/yubo2333/MMLongBench-Doc/resolve/{REV}/'+item['path'],meta/Path(item['path']).name)
        expected=(item.get('lfs') or {}).get('oid')
        if expected and len(expected)==64:assert hashlib.sha256(raw).hexdigest()==expected
    import pandas as pd
    rows=[]
    for path in meta.glob('*.parquet'):
        rows.extend(pd.read_parquet(path).to_dict('records'))
    (meta/'all-rows.json').write_text(json.dumps(rows,ensure_ascii=False,default=str),encoding='utf-8')
    tables=[dict(index=i,**r) for i,r in enumerate(rows)
            if 'table' in str(r.get('evidence_sources','')).lower()
            and str(r.get('answer','')).lower() not in ('not answerable','unanswerable')]
    (meta/'table-candidates.json').write_text(json.dumps(tables,ensure_ascii=False,default=str),encoding='utf-8')
    print(json.dumps({'rows':len(rows),'table_candidates':len(tables),'files':len(files),'columns':list(rows[0])}))
    for r in tables[:60]:print(json.dumps(r,ensure_ascii=False,default=str))


if __name__=='__main__':main()

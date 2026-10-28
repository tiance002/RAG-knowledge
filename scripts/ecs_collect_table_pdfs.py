"""ECS-only bounded downloads from versioned original arXiv sources."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import fitz


def download(url,path,limit=32*1024*1024):
    if path.exists() or path.with_suffix(path.suffix+'.part').exists():raise ValueError('No source overwrite')
    partial=path.with_suffix(path.suffix+'.part')
    result=subprocess.run(['curl','-4','--fail','-L','--connect-timeout','10','--max-time','100',
        '--max-filesize',str(limit),'-sS',url,'-o',str(partial)],capture_output=True)
    if result.returncode:
        return {'status':'DOWNLOAD_FAILED','exit_code':result.returncode,'error':result.stderr.decode(errors='replace')}
    if partial.stat().st_size>limit:raise ValueError('Size bound')
    partial.rename(path)
    return {'status':'DOWNLOADED','bytes':path.stat().st_size,'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def main():
    root=Path(sys.argv[1]);raw=root/'downloads';raw.mkdir(exist_ok=True)
    records=[]
    for paper in ('2005.12872v3','2307.09288v2','2401.18059v1','2405.09818v1'):
        url='https://arxiv.org/pdf/'+paper;target=raw/(paper+'.pdf')
        print(json.dumps({'start':url}),flush=True)
        record={'paper':paper,'official_source_url':url,'path':str(target),**download(url,target)}
        if record['status']=='DOWNLOADED':
            with fitz.open(target) as doc:
                record['pages']=len(doc)
                record['native_table_mentions']=[p.number+1 for p in doc if 'Table ' in p.get_text()]
        records.append(record)
        (root/'download-manifest.json').write_text(json.dumps(records,indent=2),encoding='utf-8')
        print(json.dumps(record),flush=True)
        time.sleep(3)
    api=root/'source-metadata-mirror/github-samples-api.json'
    result=download('https://api.github.com/repos/mayubo2333/MMLongBench-Doc/contents/data/samples.json',api,4*1024*1024)
    print(json.dumps({'github_metadata':result}),flush=True)
    if result['status']=='DOWNLOADED':
        payload=json.loads(api.read_bytes())
        body=base64.b64decode(payload['content'])
        (root/'source-metadata-mirror/official-samples.json').write_bytes(body)
        rows=json.loads(body)
        selected=[dict(official_index=i,**r) for i,r in enumerate(rows) if r['doc_id'] in {r['paper']+'.pdf' for r in records}
                  and 'table' in str(r.get('evidence_sources','')).lower()]
        (root/'source-metadata-mirror/selected-questions.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2))
        print(json.dumps({'official_questions':selected},ensure_ascii=False),flush=True)


if __name__=='__main__':main()

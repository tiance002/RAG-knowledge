"""Recover official page qrels; never infer relevance from retrieved results."""
import argparse
import ast
import csv
import hashlib
import io
import json
from pathlib import Path
import tarfile

import httpx


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--bundle',required=True)
    ap.add_argument('--identity',required=True)
    ap.add_argument('--output',required=True)
    args=ap.parse_args()
    target=Path(args.output)
    if target.exists():raise SystemExit('Do not overwrite frozen cases')
    identity=json.loads(Path(args.identity).read_text())
    by_name={Path(v['path']).name.split('_',1)[1]:sha for sha,v in identity['pdfs'].items()}
    with tarfile.open(args.bundle) as archive:
        rows=list(csv.DictReader(io.StringIO(archive.extractfile('answers-and-metrics.private.csv').read().decode('utf-8-sig'))))
        gold={int(r['ordinal']):json.loads(r['gold_answers']) for r in rows if r['dataset']=='mmlongbench-doc'}
        originals=[json.load(archive.extractfile(m))['case'] for m in archive.getmembers() if m.name.endswith('/result.json')]
    cases=[]
    with httpx.Client(timeout=30) as client:
        for case in sorted(originals,key=lambda c:c['ordinal']):
            if case['dataset']!='mmlongbench-doc':continue
            index=int(case['qid'].rsplit('row-',1)[1])
            response=client.get('https://datasets-server.huggingface.co/rows',params={
                'dataset':'yubo2333/MMLongBench-Doc','config':'default','split':'train','offset':index,'length':1})
            response.raise_for_status()
            row=response.json()['rows'][0]['row']
            if row['question']!=case['question'] or str(row['answer'])!=str(gold[case['ordinal']]):
                raise ValueError('Official row differs from frozen ECS question/answer')
            cases.append({'ordinal':case['ordinal'],'qid':case['qid'],'question':case['question'],
                'answer_points':[gold[case['ordinal']]],'expected_chunk_ids':[],
                'expected_sources':[row['doc_id']],'expected_source_sha256':by_name[row['doc_id']],
                'expected_pages':ast.literal_eval(row['evidence_pages']),
                'kb_scope':['pdf-ab-same-three-sources'],'dataset_version':'MMLongBench-Doc/2ff6aa9237fc777b6627dc57a486e9225ac5fb86',
                'official_row':row})
    content=''.join(json.dumps(c,ensure_ascii=False,sort_keys=True)+'\n' for c in cases)
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(content,encoding='utf-8')
    print(json.dumps({'cases':len(cases),'sha256':hashlib.sha256(content.encode()).hexdigest(),
                      'scorable_page_qrels':sum(bool(c['expected_pages']) for c in cases)}))


if __name__=='__main__':main()

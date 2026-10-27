"""Add paired context/table evidence diagnostics to the unchanged v2 scoring rubric."""
import argparse
import hashlib
import json
from pathlib import Path

from score_pdf_v2_eval import main as score


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--directory',required=True)
    args=ap.parse_args();root=Path(args.directory)
    payload=json.loads((root/'input.json').read_bytes())
    assert payload['schema']=='pdf-table-ablation/v1'
    assert hashlib.sha256((root/'cases.jsonl').read_bytes()).hexdigest()==payload['provenance']['gold_sha256']
    score()
    rows=[json.loads(s) for s in (root/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    chunks={}
    for arm,values in payload['arms'].items():
        for index,c in enumerate(values):
            cid=arm+'-'+hashlib.sha256(json.dumps([c['source_sha256'],index,c['content']],sort_keys=True).encode()).hexdigest()
            chunks[cid]=c
    audit=[]
    for ordinal in sorted({r['ordinal'] for r in rows}):
        pair={r['arm']:r for r in rows if r['ordinal']==ordinal}
        item={'ordinal':ordinal,'identical_context':pair['before']['context_sha256']==pair['after']['context_sha256']}
        item['arms']={}
        for arm,r in pair.items():
            item['arms'][arm]={'prompt_tokens':r['usage']['prompt_tokens'],'context_chars':r['context_chars'],
                'table_rows_at_10':[chunks[s['chunk_id']]['content'] for s in r['sources_at_10']
                                    if chunks[s['chunk_id']]['content_type']=='table']}
        audit.append(item)
    (root/'evidence-audit.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    result=json.loads((root/'scores.json').read_bytes())
    result.update(arm_labels=payload['arm_labels'],identical_context_pairs=[p['ordinal'] for p in audit if p['identical_context']])
    (root/'scores.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'identical_context_pairs':result['identical_context_pairs']}))


if __name__=='__main__':main()

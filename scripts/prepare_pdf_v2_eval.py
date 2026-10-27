"""Freeze source-reviewed table probes and a reproducible v2 paired protocol."""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

from run_ecs_pdf_eval_v2 import prompt_template_sha256, validate_protocol


def main():
    root=Path(__file__).resolve().parents[1]
    out=root/'var/pdf-v2-eval-20261027'
    original=root/'var/ecs-reference/pdf-cases.jsonl'
    cases=[json.loads(line) for line in original.read_text(encoding='utf-8').splitlines()]
    report='8f49f93f1665fd5ff604e66ffed9649d8b18ce7f114ab43463aeac46cd3d00cb'
    physics='43be9592854c7da10bf011335faec017b726ff2e23bb4ccae7e8fe88d7801f22'
    probes=[
        (118,report,45,'According to the sampling-error table, what are the unweighted sample size and the plus-or-minus sampling error for Trump voters?', ['533','5.1 percentage points']),
        (119,report,45,'According to the sampling-error table, what are the unweighted sample size and the plus-or-minus sampling error for Clinton voters?', ['587','4.9 percentage points']),
        (120,physics,13,'According to the Infrared Radiation versus Microwaves table, which molecules receive kinetic energy during microwave cooking?', ['water or fat molecules']),
        (121,physics,13,'According to the Infrared Radiation versus Microwaves table, how far into the food are microwaves sent?', ['1 cm']),
    ]
    for ordinal,source,page,question,answer in probes:
        cases.append(dict(ordinal=ordinal,qid=f'ecs-table-probe/v2:{ordinal}',question=question,
            answer_points=answer,expected_chunk_ids=[],expected_source_sha256=source,
            expected_pages=[page],expected_sources=[source+'.pdf'],kb_scope=['pdf-ab-same-three-sources'],
            dataset_version='ecs-table-probes/v2-20261027',
            annotation='Agent annotated before inference against rendered original PDF; not official benchmark gold'))
    gold=out/'cases.jsonl'
    if gold.exists():raise ValueError('Frozen gold already exists')
    gold.write_text(''.join(json.dumps(c,ensure_ascii=False,sort_keys=True)+'\n' for c in cases),encoding='utf-8')
    subprocess.run([sys.executable,str(root/'scripts/prepare_pdf_eval.py'),
        '--parses',str(out/'parses'),'--indexed',str(root/'var/ecs-reference/indexed-pdfs.json'),
        '--cases',str(gold),'--output',str(out/'base-input.json')],check=True)
    payload=json.loads((out/'base-input.json').read_bytes())
    payload['schema']='pdf-parser-paired/v2'
    payload['bounds'].update(generation_calls=24,rerank_calls=96)
    payload['expected_models']=json.loads((root/'var/ecs-reference/paired-state-final.json').read_bytes())['models']
    payload['expected_production_source_hashes']=json.loads((out/'identity-before.json').read_bytes())['source_files']
    old=prompt_template_sha256(root/'scripts/run_ecs_pdf_eval.py')
    assert old==prompt_template_sha256(root/'scripts/run_ecs_pdf_eval_v2.py')
    payload['expected_prompt_template_sha256']=old
    payload['provenance']={'parser_commit':'b681671cdbfcb02e6a6e8ad6b9213c47eb4be100',
        'gold_sha256':hashlib.sha256(gold.read_bytes()).hexdigest(),
        'parse_summary_sha256':hashlib.sha256((out/'parses/summary.json').read_bytes()).hexdigest(),
        'caption_snapshot_sha256':hashlib.sha256((root/'var/ecs-reference/indexed-pdfs.json').read_bytes()).hexdigest(),
        'scanned_tables_enabled':True,'original_questions':8,'source_reviewed_table_probes':4}
    validate_protocol(payload)
    count=len({c['content'] for arm in payload['arms'].values() for c in arm})+len(cases)
    assert count<=payload['bounds']['embedding_inputs']
    assert all('answer_points' not in c for c in payload['cases'])
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()
    target=out/'input.json'
    if target.exists():raise ValueError('Frozen input already exists')
    target.write_bytes(raw)
    print(json.dumps({'input_sha256':hashlib.sha256(raw).hexdigest(),'unique_embedding_inputs':count,
        'arm_chunks':{k:len(v) for k,v in payload['arms'].items()},'bounds':payload['bounds'],
        'prompt_template_sha256':old}))


if __name__=='__main__':main()

"""Freeze same-parser table-off/on inputs; preserve the previous12-case gold."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from run_ecs_pdf_eval_v2 import prompt_template_sha256,validate_protocol


def main():
    root=Path(__file__).resolve().parents[1]
    out=root/'var/pdf-table-ablation-20261027'
    if (out/'input.json').exists() or (out/'cases.jsonl').exists():raise ValueError('No frozen-input overwrite')
    summary=json.loads((out/'parses/summary.json').read_bytes())
    assert summary['comparison']=='same_v2_tables_off_on' and summary['scan_tables']
    audit={}
    for before in sorted((out/'parses/before').glob('*.json')):
        off=json.loads(before.read_bytes());on=json.loads((out/'parses/after'/before.name).read_bytes())
        assert off['source_sha256']==on['source_sha256']==before.stem
        assert len(off['pages'])==len(on['pages'])
        for a,b in zip(off['pages'],on['pages']):
            for key in ('page','page_text_sha256','route','text_chars','image_coverage','parser_version'):
                assert a[key]==b[key],(before.name,a['page'],key)
            assert not a['tables_enabled'] and b['tables_enabled']
        assert not any(c['content_type']=='table' for c in off['chunks'])
        audit[before.stem]=[p['page_text_sha256'] for p in off['pages']]
    for name,value in summary['candidate_source_hashes'].items():
        assert hashlib.sha256((root/'backend/app/services'/name).read_bytes()).hexdigest()==value
    shutil.copyfile(root/'var/pdf-v2-eval-20261027/cases.jsonl',out/'cases.jsonl')
    subprocess.run([sys.executable,str(root/'scripts/prepare_pdf_eval.py'),'--parses',str(out/'parses'),
        '--indexed',str(root/'var/ecs-reference/indexed-pdfs.json'),'--cases',str(out/'cases.jsonl'),
        '--output',str(out/'base-input.json')],check=True)
    payload=json.loads((out/'base-input.json').read_bytes())
    images=lambda arm:[(c['source_sha256'],c['page_number'],c['content']) for c in arm if c['content_type']=='image']
    assert images(payload['arms']['before'])==images(payload['arms']['after'])
    payload.update(schema='pdf-table-ablation/v1',arm_labels={'before':'tables_off','after':'tables_on'},
        expected_models=json.loads((root/'var/pdf-v2-eval-20261027/state.json').read_bytes())['models'],
        expected_production_source_hashes=json.loads((out/'identity-before.json').read_bytes())['source_files'],
        expected_prompt_template_sha256=prompt_template_sha256(root/'scripts/run_ecs_pdf_eval_v2.py'))
    payload['bounds'].update(generation_calls=24,rerank_calls=96)
    payload['baseline_note']='Same repaired v2.1 code, native/scanned table feature off/on; base page OCR and image captions identical.'
    payload['provenance']={'parser_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=root,text=True).strip(),
        'gold_sha256':hashlib.sha256((out/'cases.jsonl').read_bytes()).hexdigest(),
        'parse_summary_sha256':hashlib.sha256((out/'parses/summary.json').read_bytes()).hexdigest(),
        'parser_source_hashes':summary['candidate_source_hashes'],'page_ocr_audit':audit,
        'page_ocr_dpi':150,'table_cell_ocr_dpi':300,'images_frozen':True}
    validate_protocol(payload)
    count=len({c['content'] for arm in payload['arms'].values() for c in arm})+len(payload['cases'])
    assert count<=payload['bounds']['embedding_inputs']
    raw=json.dumps(payload,ensure_ascii=False,sort_keys=True).encode()
    (out/'input.json').write_bytes(raw)
    print(json.dumps({'input_sha256':hashlib.sha256(raw).hexdigest(),'unique_embedding_inputs':count,
        'arm_chunks':{k:len(v) for k,v in payload['arms'].items()},'base_OCR_pages_verified':sum(map(len,audit.values()))}))


if __name__=='__main__':main()

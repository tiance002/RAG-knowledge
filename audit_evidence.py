"""Read-only receipt and existing-PDF readiness audit."""
import json, pathlib, hashlib, sys, collections
ROOT=pathlib.Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'scripts'))
import rsi_candidates as c
from repair_round import save

def main():
    out=ROOT/'var/repair-round'
    runs=[out/'run-local',out/'independent-engineering-local']
    receipts=[]
    for run in runs:
        for p in run.glob('case-*/[AB]/capture.json'):
            cap=c.read_json(p)
            for member, proof in zip(cap['members'],cap['proof'],strict=True):
                assert member['id']==proof['id']
                assert cap['context'][proof['context_start']:proof['context_end']]==member['content']
                assert cap['messages'][0]['content'][proof['prompt_start']:proof['prompt_end']]==member['content']
                assert hashlib.sha256(member['content'].encode()).hexdigest()==member['content_hash']
                assert member['file_id']=='f1' and member['kb_id']=='public'
            receipts.append({'path':str(p.relative_to(ROOT)), 'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'request_calls':cap['request_calls'],'ids':[m['id'] for m in cap['members']]})
    legacy=pathlib.Path(r'D:\RAG-R0\var\r1\engineering-failure\run')
    assert (out/'run-local/case-001/A/capture.json').read_bytes()==(legacy/'case-001/B/capture.json').read_bytes()
    assert (out/'run-local/case-001/B/capture.json').read_bytes()==(legacy/'case-001/A/capture.json').read_bytes()
    repaired=c.candidate(out/'registry',c.read_json(out/'identity.json')['child'])
    assert c.get(out/'registry',repaired['worker'])==(ROOT/'scripts/rsi_delivery_worker.py').read_bytes()
    save(out/'receipt-audit.json',{'status':'PASS','receipts':receipts,'actual_local_request_captures':sum(r['request_calls'] for r in receipts),'historical_correct_capture_byte_equal':True,'historical_failed_capture_byte_equal':True,'repaired_worker_equals_fixed_correct_worker':True,'external_calls':0})
    corpus=pathlib.Path(r'D:\RAG knowledge\var\corpus')
    pdfs=[]
    for p in sorted(corpus.glob('*.pdf')):
        sha=hashlib.sha256(p.read_bytes()).hexdigest()
        assert sha==p.stem
        pdfs.append({'path':str(p),'bytes':p.stat().st_size,'sha256':sha})
    parser_path=pathlib.Path(r'D:\RAG knowledge\var\pdf-discovery-20261027\native-paired-v2\after')
    parses=[]
    for p in parser_path.glob('*.json'):
        d=c.read_json(p); chunks=d['chunks']
        parses.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'chunks':len(chunks),'region_located_chunks':sum(bool(x.get('metadata',{}).get('source_locator',{}).get('regions')) for x in chunks),'strict_top_level_locator_chunks':sum(bool(x.get('source_locator',{}).get('bbox')) and bool(x.get('parent_id')) and bool(x.get('heading_text')) for x in chunks)})
    readiness=c.read_json(ROOT/'experiments/r1/e1c-readiness.json')
    bindings=[]
    for key,ref in readiness['inputs'].items():
        p=pathlib.Path(ref['path'])
        actual=hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
        bindings.append({'name':key,'path':str(p),'expected_sha256':ref['raw_sha256'],'actual_sha256':actual,'matches':actual==ref['raw_sha256']})
    save(out/'pdf-readiness-audit.json',{'status':'BLOCKED','scope':'Existing local corpus PDFs and already archived native parse; no new parsing, index rebuild, ECS or file copying. Not a whole-disk search.','pdf_files':pdfs,'total_source_documents':len(pdfs),'documents_with_existing_region_located_parse':len([p for p in parses if p['region_located_chunks']]),'documents_with_E1c_ready_source_ownership_and_locator':0,'documents_with_proven_deletable_nonseed_same_location_duplicate':0,'documents_missing_independently_frozen_E1c_question_scoring':len(pdfs),'parses':parses,'historical_office_readiness_input_bindings':bindings,'historical_office_documents':readiness['candidate_applicability']['historical_formats'],'ready_E1c_question_samples':0,'minimum_missing':['Frozen allowed file_id/KB/source hash/index version ownership mapping','Actual frozen PDF expansion: seed IDs + complete members + exact content hash + page/bbox + parent_id/heading_text','At least one removable non-seed duplicate at the same source location; matching text at distinct locations is insufficient','Independently reviewed necessary values, header, units and source locators plus untouched holdout questions'],'next_step':'Stop formal E1c; acquire minimum frozen materials. No production promotion.'})
    print(json.dumps({'receipt_audit':'PASS','captures':len(receipts),'PDF_documents':len(pdfs),'E1c_ready':0}))

if __name__=='__main__':main()

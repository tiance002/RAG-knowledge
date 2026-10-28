"""Freeze independently sourced references before any expanded model evaluation."""
import ast
from decimal import Decimal,ROUND_HALF_UP
import hashlib
import json
from pathlib import Path
import sys
import fitz

A=[1004,1006,1009,1012,1019,1037,1040,305,306,307,308,676,677,678,681,980,981,218,220,223,69,70,72,73,74]
B=[1007,1008,1010,1011,1014,1015,1016,1017,1018,1033,1034,1036,1039,1041,309,311,312,679,680,979,982,221,225,227,71]


def main():
 root=Path(sys.argv[1]);out=root/'dataset200'
 assert len(A)==len(B)==25 and not set(A)&set(B)
 if (out/'new-mml-gold25.jsonl').exists():raise ValueError('No reference overwrite')
 meta=root/'source-metadata-mirror'
 originals=json.loads((meta/'official-samples.json').read_bytes())
 corrections=json.loads((meta/'corrections.json').read_bytes())
 sources={r['doc']:r for r in json.loads((root/'github-download-manifest.json').read_bytes()) if r['status']=='VERIFIED_PDF'}
 raptor=json.loads((root/'resume-2401.18059v1.json').read_bytes())
 sources['2401.18059v1.pdf']=raptor
 answers=[];retrieval=[];changes=[]
 for split,indices in [('answer',A),('retrieval',B)]:
  for index in indices:
   original=originals[index];row=dict(original);source=sources[row['doc_id']]
   path=Path(source['path']);assert hashlib.sha256(path.read_bytes()).hexdigest()==source['sha256']
   matches=[c for c in corrections if c['doc_id']==row['doc_id'] and c['question'].strip()==row['question'].strip()]
   assert len(matches)<=1
   if matches:
    correction=matches[0]
    row.update(question=correction.get('manual_question',row['question']),answer=correction['manual_answer'],
        evidence_pages=correction.get('manual_evidence_pages',row['evidence_pages']),answer_format=correction.get('manual_answer_format',row['answer_format']))
    changes.append({'index':index,'correction':correction})
   curator_changes=[]
   if index==305:
    row['question']='According to the recommended separation-distance table for M270TF-XXX / M320TF-XXX, what distance is listed for a 10 W transmitter at 500 MHz? Give the tabulated distance in metres to two decimal places.'
    row['answer']='3.80';curator_changes.append('Pin printedtablevalue rather than separatelyroundedformula;sourcePDF23row10W/80-800MHz=3.8')
   if index==676:
    row['evidence_pages']='[22]';row['answer']="['GS5002', 'GS5101', 'GS6001', 'GS6883A']"
    curator_changes.append('Annex1isPDF22,notbodyPDF3;sortfourcodesascendingasrequested')
   if index==1006:
    row['question']="Using EBITDA defined as operating income plus depreciation and amortization, what EBITDA in USD millions does Costco's FY2021 report imply?"
    assert Decimal('6708')+Decimal('1781')==Decimal(row['answer'])
    curator_changes.append('State EBITDA definition explicitly;6708+1781=8489')
   if index==1009:assert (Decimal('11407')/Decimal('8489')).quantize(Decimal('.001'),rounding=ROUND_HALF_UP)==Decimal(row['answer'])
   if index==1012:assert (Decimal('29441')/Decimal('41190')).quantize(Decimal('.001'),rounding=ROUND_HALF_UP)==Decimal(row['answer'])
   if index==1037:assert (Decimal('26291')/Decimal('9674')).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)==Decimal(row['answer'])
   pages=ast.literal_eval(row['evidence_pages'])
   with fitz.open(path) as doc:
    for p in pages:assert 1<=p<=len(doc)
    if index==676:
     text=doc[21].get_text();assert all(code in text for code in ('GS5002','GS5101','GS6001','GS6883A'))
    if index==306:assert 'M250' not in ''.join(p.get_text() for p in doc)
    if index==220:assert '2022' not in ''.join(p.get_text() for p in doc)
    support=[{'page':p,'native_chars':len(doc[p-1].get_text()),'embedded_images':len(doc[p-1].get_images())} for p in pages]
   is_na=str(row['answer']).strip().lower()=='not answerable'
   assert split=='answer' or (pages and not is_na)
   canonical=row['answer']
   if row['answer_format']=='List':
    try:canonical=ast.literal_eval(row['answer'])
    except (SyntaxError,ValueError):pass
   item={'qid':f'dataset200-mml:{index:04d}','question':row['question'],
    'answer_points':[row['answer']] if split=='answer' else [],'gold_canonical':canonical if split=='answer' else None,
    'expected_chunk_ids':[],'kb_scope':['dataset200-new-pdfs'],'expected_sources':[source['sha256']],
    'expected_source_sha256':source['sha256'],'expected_pages':pages,'relevance_unit':'pdf_page',
    'dataset_version':'dataset200-MMLongBenchDoc-source-reviewed/v1','evaluation_split':split,
    'language':'en','source_type':row['doc_type'],'evidence_types':ast.literal_eval(row['evidence_sources']),
    'answerability':'not_answerable' if is_na else ('answerable_empty_set' if canonical==[] else 'answerable'),
    'official_index':index,'original_official_row':original,'reference_after_source_review':row,
    'source_provenance':source,'reference_correction':matches[0] if matches else None,
    'curator_corrections':curator_changes,'support_page_audit':support,
    'gold_status':'benchmark_reference_plus_preinference_source_review_not_owner_accepted'}
   if index in (69,70,72,73):item['visual_review']='Primaryagent inspected originalRAPTORfigures1/3/4; nodes16/19/25,axesContextLength/F1,green/yellowrepeated,noall3colors verified'
   (answers if split=='answer' else retrieval).append(item)
 for name,rows in [('new-mml-gold25.jsonl',answers),('new-mml-qrel25.jsonl',retrieval)]:
  (out/name).write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in rows),encoding='utf-8')
 (out/'new-mml-label-review.json').write_text(json.dumps({'upstream_corrections_applied':changes,
  'excluded_near_duplicate_indices':[1013,1038,229],'excluded_ambiguous_candidate_indices':[1032,310],
  'selection_rule':'coverage/sourcequality only; no modelanswers or table-on results used'},ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'answer':len(answers),'retrieval':len(retrieval),'source_documents':len({r['expected_source_sha256'] for r in answers+retrieval}),
                   'corrections_applied':len(changes),'status':'SOURCE_REVIEWED_CANDIDATES'}))


if __name__=='__main__':main()

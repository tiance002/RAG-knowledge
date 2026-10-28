"""Source-checked Chinese fiscal table cases; run on ECS, no model-derived gold."""
import hashlib
import json
from pathlib import Path
import sys
import fitz

# Transcribed from complete native source pages, before any answer-model run.
# Values are in10,000CNY; first/fiscal pages are physicalPDF1-based, not printed page labels.
DATA=[
 ('cn-office-2024','国务院办公厅',8,11,('51971.46','54488.17'),('20557.47','18040.76'),('12797.71','10226.38'),('2545.30','2481.23'),[9],[10],[13],[16]),
 ('cn-stats-2024','国家统计局',8,13,('820124.05','811528.94'),('193858.86','202061.21'),('38751.41','40727.84'),('43974.94','40654.80'),[9,10],[11,12],[15,16],[19]),
 ('cn-moe-2024','教育部',9,20,('42828112.97','43665710.31'),('17933106.74','19260529.73'),('1135231.15','1674071.61'),('1200510.37','674335.78'),list(range(10,15)),list(range(15,20)),[25,26],[29]),
 ('cn-audit-2024','审计署',10,15,('190395.32','189274.27'),('27221.02','26504.17'),('10528.25','11413.15'),('14009.54','13898.15'),[11,12],[13,14],[17],[20]),
]


def main():
 root=Path(sys.argv[1]);out=root/'dataset200'
 if (out/'new-cn-gold16.jsonl').exists():raise ValueError('No question overwrite')
 manifest={r['id']:r for r in json.loads((root/'cn-download-manifest.json').read_bytes())}
 answers=[];retrieval=[];support=[]
 for item in DATA:
  ident,agency,first,fiscal,inout,carry,fc,housing,income_pages,expense_pages,basic_pages,three_pages=item
  source=manifest[ident];assert source['status']=='VERIFIED_PDF'
  assert hashlib.sha256(Path(source['path']).read_bytes()).hexdigest()==source['sha256']
  with fitz.open(source['path']) as pdf:
   for page,values in [(first,inout+carry+(housing[0],)),(fiscal,fc+(housing[1],))]:
    text=''.join(pdf[page-1].get_text().replace(',','').split())
    for value in values:assert value in text,(ident,page,value)
   specs=[
    (f'{agency}2024年度《收入支出决算总表》中，本年收入合计和本年支出合计分别是多少万元？',inout,[first],['本年收入合计','本年支出合计'],'row_column_alignment'),
    (f'{agency}2024年度总收支口径下，年初结转和结余、年末结转和结余分别是多少万元？',carry,[first],['年初结转和结余','年末结转和结余'],'carry_balance_vs_annual_flow'),
    (f'只看{agency}2024年度《财政拨款收入支出决算总表》，年初和年末财政拨款结转结余分别是多少万元？',fc,[fiscal],['年初财政拨款结转和结余','年末财政拨款结转和结余'],'similar_table_scope'),
    (f'{agency}2024年度“住房保障支出”在总收支表与财政拨款收支表中分别是多少万元？请按这两个口径的顺序回答。',housing,[first,fiscal],['总收支口径住房保障支出','财政拨款口径住房保障支出'],'cross_page_same_label_different_scope')]
   for i,(question,values,pages,labels,kind) in enumerate(specs):
    row={'qid':f'cn-fiscal2024/v1:{ident}:answer:{i+1}','question':question,
     'answer_points':[label+'='+value+'万元' for label,value in zip(labels,values)],'expected_chunk_ids':[],
     'kb_scope':['dataset200-new-pdfs'],'expected_sources':[source['sha256']],
     'expected_source_sha256':source['sha256'],'expected_pages':pages,'dataset_version':'dataset200-cn-fiscal2024/v1',
     'evaluation_split':'answer','language':'zh','source_type':'native_pdf_table','difficulty':kind,
     'numeric_answer_constraints':[{'label':label,'value':value,'unit':'万元','absolute_tolerance':'0.005'} for label,value in zip(labels,values)],
     'source_provenance':source,'gold_annotation':'Primary agent checked against originalnativePDFvalues; not generated from candidateparser or modelanswers'}
    answers.append(row)
   rspecs=[
    (f'请查找{agency}2024年度完整的收入决算明细表：需要按支出功能科目列出财政拨款收入、事业收入和其他收入的对应情况。',income_pages,'income_table_complete_pages'),
    (f'我需要{agency}2024年度按功能分类列示的支出决算明细，尤其是各科目的基本支出与项目支出，请返回完整表格的证据。',expense_pages,'expenditure_table_complete_pages'),
    (f'哪里能查到{agency}2024年度一般公共预算财政拨款基本支出的经济分类明细，包括人员经费和公用经费？',basic_pages,'economic_classification'),
    (f'请定位{agency}2024年度财政拨款“三公”经费支出表，我要核对因公出国、公务用车和公务接待的预算、决算列。',three_pages,'merged_budget_actual_header')]
   for i,(question,pages,kind) in enumerate(rspecs):
    for page in pages:
     assert 1<=page<=len(pdf)
     support.append({'source':ident,'page':page,'text':pdf[page-1].get_text()[:4500]})
    retrieval.append({'qid':f'cn-fiscal2024/v1:{ident}:retrieval:{i+1}','question':question,'answer_points':[],
     'expected_chunk_ids':[],'kb_scope':['dataset200-new-pdfs'],'expected_sources':[source['sha256']],
     'expected_source_sha256':source['sha256'],'expected_pages':pages,'relevance_unit':'pdf_page',
     'dataset_version':'dataset200-cn-fiscal2024/v1','evaluation_split':'retrieval','language':'zh',
     'source_type':'native_pdf_table','difficulty':kind,'source_provenance':source,
     'qrel_annotation':'Originaltablepage span, checkedbeforeinference; not inferredfromretrievedresults'})
 for name,rows in [('new-cn-gold16.jsonl',answers),('new-cn-qrel16.jsonl',retrieval)]:
  (out/name).write_text(''.join(json.dumps(r,ensure_ascii=False,sort_keys=True)+'\n' for r in rows),encoding='utf-8')
 (out/'cn-qrel-page-support.json').write_text(json.dumps(support,ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps({'answer':len(answers),'retrieval':len(retrieval),'status':'DRAFT_REQUIRES_PAGE_TITLE_SPAN_AUDIT'}))


if __name__=='__main__':main()

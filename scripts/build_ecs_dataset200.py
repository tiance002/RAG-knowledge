"""ECS-only dataset200 construction. Never substitutes model outputs for gold/qrels."""
import argparse
import collections
import csv
import hashlib
import json
from pathlib import Path


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
def write_new(path,rows):
    if path.exists():raise ValueError('No dataset overwrite: '+str(path))
    path.write_text(''.join(json.dumps(row,ensure_ascii=False,sort_keys=True)+'\n' for row in rows),encoding='utf-8')


def reuse(root):
    target=root/'dataset200';target.mkdir(exist_ok=True)
    base=Path('/opt/amrag/eval118')
    with (base/'answers-and-metrics.private.csv').open(encoding='utf-8-sig') as f:csvrows=list(csv.DictReader(f))
    byordinal={int(r['ordinal']):r for r in csvrows}
    cases=[json.loads(p.read_bytes())['case'] for p in base.glob('case-*/result.json')]
    assert len(cases)==118
    regression=[json.loads(line) for line in (root/'regression12.jsonl').read_text(encoding='utf-8').splitlines()]
    assert len(regression)==12
    answers=[];retrieval=[]
    qmap={int(r['ordinal']):r for r in json.loads(Path('/opt/amrag/qid_map.json').read_bytes())}
    for case in sorted(cases,key=lambda c:c['ordinal']):
        ordinal=case['ordinal'];ds=case['dataset'];gold=json.loads(byordinal[ordinal]['gold_answers'])
        if ds=='mmlongbench-doc':
            row=next(r for r in regression if r['ordinal']==ordinal)
            assert row['question']==case['question'] and row['answer_points']==[gold]
            continue
        row={'ordinal':ordinal,'qid':ds+':'+case['qid'],'question':case['question'],
             'expected_chunk_ids':[],'kb_scope':case['knowledge_base_ids'],
             'dataset_version':'reused-ECS-eval118/csv-sha256:'+digest(base/'answers-and-metrics.private.csv'),
             'original_case':case,'dataset':ds}
        if ds in ('longbench-zh','hotpotqa'):
            assert gold not in (None,[],'')
            row.update(answer_points=gold if isinstance(gold,list) else [gold],
                       gold_semantics='alternative_references',evaluation_split='answer')
            answers.append(row)
        else:
            assert gold==[] and ds in ('scifact','miracl-zh')
            qrels=Path('/opt/amrag/qrels-'+('scifact' if ds=='scifact' else 'miracl')+'-train.tsv')
            corpus=Path('/opt/rag-p7-r1/data/v6-dev24-inputs/inputs/public')/ds/'corpus.jsonl'
            qid=str(qmap[ordinal]['qid']);positives={}
            for line in qrels.read_text(encoding='utf-8-sig').splitlines():
                fields=line.split()
                if len(fields)<3 or fields[0]!=qid:continue
                if float(fields[-1])>0:positives[fields[-2] if len(fields)==4 else fields[1]]=float(fields[-1])
            assert positives
            found={}
            for line in corpus.open():
                if not line.strip():continue
                doc=json.loads(line);docid=str(doc['doc_id'])
                if docid in positives:
                    found[docid]=hashlib.sha256(json.dumps(doc,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            assert set(found)==set(positives),(ds,qid)
            row.update(answer_points=[],evaluation_split='retrieval',relevance_unit='source_document',
                source_qid=qid,positive_document_ids=positives,expected_sources=sorted(positives),
                qrel_provenance={'path':str(qrels),'sha256':digest(qrels),'corpus_path':str(corpus),
                                 'positive_document_hashes':found})
            retrieval.append(row)
    answers.extend(regression)
    assert len(answers)==59 and len(retrieval)==63
    assert {r['ordinal'] for r in answers+retrieval}==set(range(122))
    groups={}
    for row in retrieval:
        key=row['question'].strip().casefold()
        if key not in groups:
            groups[key]=row
            row['source_qids']=[row['source_qid']]
            row['original_case_aliases']=[row['original_case']]
        else:
            first=groups[key]
            assert first['dataset']==row['dataset'] and first['kb_scope']==row['kb_scope']
            first['source_qids'].append(row['source_qid'])
            first['original_case_aliases'].append(row['original_case'])
            for docid,grade in row['positive_document_ids'].items():
                first['positive_document_ids'][docid]=max(grade,first['positive_document_ids'].get(docid,0))
            first['qrel_provenance']['positive_document_hashes'].update(row['qrel_provenance']['positive_document_hashes'])
            first['expected_sources']=sorted(first['positive_document_ids'])
    retrieval=list(groups.values())
    assert len(retrieval)==59
    assert len({r['question'].strip().casefold() for r in answers+retrieval})==118
    write_new(target/'reused-gold59.jsonl',sorted(answers,key=lambda r:r['ordinal']))
    write_new(target/'reused-qrel59.jsonl',retrieval)
    manifest={'status':'REUSED_PREFIX_VALIDATED_NEW82_PENDING','target':{'answer':100,'retrieval':100,'unique_questions':200},
              'reused':{'answer':59,'retrieval':59,'unique_questions':118},'raw_existing_records':122,
              'merged_duplicate_aliases':4,'remaining':{'answer':41,'retrieval':41},
              'regression12_sha256':digest(root/'regression12.jsonl'),
              'files':{p.name:digest(p) for p in target.glob('reused-*.jsonl')},
              'positive_qrel_labels':sum(len(r['positive_document_ids']) for r in retrieval)}
    (target/'reuse-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False))


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--root',required=True)
    ap.add_argument('--finalize',action='store_true');args=ap.parse_args()
    if args.finalize:finalize(Path(args.root))
    else:reuse(Path(args.root))


def finalize(root):
    out=root/'dataset200'
    if (out/'freeze.json').exists():raise ValueError('Frozen dataset is immutable')
    def read(name):return [json.loads(l) for l in (out/name).read_text(encoding='utf-8').splitlines()]
    old_a=read('reused-gold59.jsonl');old_b=read('reused-qrel59.jsonl')
    new_a=read('new-cn-gold16.jsonl')+read('new-mml-gold25.jsonl')
    new_b=read('new-cn-qrel16.checked.jsonl')+read('new-mml-qrel25.jsonl')
    assert len(old_a)==len(old_b)==59 and len(new_a)==len(new_b)==41
    for ordinal,row in enumerate(new_a+new_b,122):row['ordinal']=ordinal
    answer=old_a+new_a;retrieval=old_b+new_b
    assert len(answer)==len(retrieval)==100
    allrows=answer+retrieval
    assert len({r['qid'] for r in allrows})==200
    def norm(text):
        import re
        return re.sub(r'[^0-9a-z\u3400-\u9fff]+','',text.casefold())
    assert len({norm(r['question']) for r in allrows})==200
    required={'question','expected_chunk_ids','answer_points','kb_scope'}
    for row in allrows:
        assert required<=row.keys() and row['question'] and row['kb_scope']
    assert all(r['answer_points'] for r in answer)
    assert all(r.get('positive_document_ids') or r.get('expected_pages') for r in retrieval)
    regression=[json.loads(l) for l in (root/'regression12.jsonl').read_text(encoding='utf-8').splitlines()]
    for r in regression:assert next(v for v in answer if v['qid']==r['qid'])==r
    sources={r['source_provenance']['sha256']:r['source_provenance'] for r in new_a+new_b}
    for sha,source in sources.items():assert digest(Path(source['path']))==sha
    write_new(out/'gold100.jsonl',answer);write_new(out/'qrel100.jsonl',retrieval)
    registry=[]
    for split,rows in [('answer',answer),('retrieval',retrieval)]:
        for row in rows:
            if row['ordinal']<63:profile=row['dataset'];cohort='reused_text_retrieval'
            elif row['ordinal']<110:profile=row['dataset'];cohort='reused_text_answer'
            elif row['ordinal']<122:profile='pdf-regression';cohort='regression12'
            else:profile='pdf-new';cohort='new_pdf'
            lang=row.get('language','zh' if profile in ('longbench-zh','miracl-zh') else 'en')
            registry.append({'qid':row['qid'],'ordinal':row['ordinal'],'split':split,'profile':profile,
                             'cohort':cohort,'language':lang,'is_pdf':profile.startswith('pdf-')})
    write_new(out/'registry200.jsonl',registry)
    sourcefile=out/'new-source-manifest.json'
    sourcefile.write_text(json.dumps(sources,ensure_ascii=False,indent=2),encoding='utf-8')
    manifest={'schema':'dataset200/frozen-v1','status':'FROZEN_BEFORE_MODEL_INFERENCE',
        'counts':{'answer':100,'retrieval':100,'unique_questions':200,'reused_unique':118,'new':82},
        'question_set_disjoint':True,'old_raw_records':122,'merged_alias_records':4,
        'regression12_exactly_preserved':True,'new_pdf_documents':len(sources),
        'cohorts':dict(collections.Counter(r['cohort'] for r in registry)),
        'languages':dict(collections.Counter(r['language'] for r in registry)),
        'pdf_queries':sum(r['is_pdf'] for r in registry),
        'files':{name:digest(out/name) for name in ('gold100.jsonl','qrel100.jsonl','registry200.jsonl','new-source-manifest.json')},
        'metrics_contract':{'answer':'semanticcorecorrectnesswithreferencealternatives;emptyAPIbodyfails;noqrel-onlygeneration',
           'retrieval':'macroRecall@10onfrozenpositiveIDs;source-documentandPDF-pagecohortsreportedseparately;judgmentsnotclaimedexhaustive',
           'legacy_regression':'same12questions/answers/qrels;rawtop10chunkpageRecallkept'},
        'resolved_chunk_ids':'NOT_RUN;immutable source-document/pageqrels will map to each arm after parsing; never infer qrels from retrieved results',
        'model_calls_at_freeze':0,'production_service_switch':'NOT_RUN'}
    (out/'freeze.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(manifest,ensure_ascii=False))


if __name__=='__main__':main()

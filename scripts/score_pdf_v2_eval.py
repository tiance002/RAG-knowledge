"""Aggregate frozen page qrels and explicit per-answer semantic review."""
import argparse
import json
from pathlib import Path


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--directory',required=True)
    args=ap.parse_args()
    root=Path(args.directory)
    cases=[json.loads(s) for s in (root/'cases.jsonl').read_text(encoding='utf-8').splitlines()]
    rows=[json.loads(s) for s in (root/'results.jsonl').read_text(encoding='utf-8').splitlines()]
    review=json.loads((root/'semantic-review.json').read_text(encoding='utf-8'))
    expected={(c['ordinal'],a) for c in cases for a in ('before','after')}
    assert len(rows)==len(expected)==24
    assert {(r['ordinal'],r['arm']) for r in rows}==expected
    assert set(review['items'])=={f'{n}-{a}' for n,a in expected}
    state=json.loads((root/'state.json').read_text(encoding='utf-8'))
    assert state['status']=='COMPLETED'
    assert all(r['status']=='HTTP_SUCCESS' for r in state['requests'])
    result={'schema':'pdf-v2-scores/v1','reviewer':review['reviewer'],'metrics':{}}
    for cohort in ('original8','table4','all12'):
        result['metrics'][cohort]={}
        for arm in ('before','after'):
            selected=[r for r in rows if r['arm']==arm and (cohort=='all12' or (r['ordinal']<118)==(cohort=='original8'))]
            judged=[review['items'][f'{r["ordinal"]}-{arm}'] for r in selected]
            metrics={'answers':len(selected),'core_correct':sum(j['core_correct'] for j in judged),
                'core_accuracy':sum(j['core_correct'] for j in judged)/len(judged),
                'empty_answers':sum(not (r['answer'] or '').strip() for r in selected),
                'length_finishes':sum(r['finish_reason']=='length' for r in selected),
                'ambiguous_gold_mentions':sum(j.get('ambiguous_gold_mention',False) for j in judged),
                'prompt_tokens':sum(r['usage']['prompt_tokens'] for r in selected),
                'completion_tokens':sum(r['usage']['completion_tokens'] for r in selected),
                'context_chars':sum(r['context_chars'] for r in selected)}
            for key in ('raw_source_recall_at_10','source_recall_at_10','raw_page_recall_at_10','page_recall_at_10'):
                values=[r[key] for r in selected if r[key] is not None]
                metrics[key]=sum(values)/len(values)
                metrics['recall_eligible_questions']=len(values)
            result['metrics'][cohort][arm]=metrics
    result['calls']={k:state[k] for k in ('embedding_inputs','rerank_calls','generation_calls')}
    result['usage_by_role']={}
    for request in state['requests']:
        target=result['usage_by_role'].setdefault(request['role'],{})
        for key,value in (request.get('usage') or {}).items():
            if isinstance(value,(int,float)):target[key]=target.get(key,0)+value
    (root/'scores.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()

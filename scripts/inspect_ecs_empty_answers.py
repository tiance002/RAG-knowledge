"""Print aggregate metadata about empty answers; no answer content or credentials."""
import pathlib,json,collections
b=pathlib.Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
groups=[('original',b/'runtime200-r4/results.jsonl'),('continue',b/'answers200-continuation-r1/results.jsonl'),
 ('fast',b/'answers200-fast-r2/results.jsonl'),('office',b/'office50-v1/evaluation/results.jsonl'),
 ('office_retry',b/'office50-v1/retry429-r1/results.jsonl')]
empty=[]
for name,p in groups:
 rows=[json.loads(l) for l in p.open(encoding='utf-8')];ok=[x for x in rows if x.get('status','COMPLETED')=='COMPLETED'];e=[x for x in ok if not (x.get('answer') or '').strip()]
 print('GROUP',name,'responses',len(ok),'empty',len(e),'finish',dict(collections.Counter(x.get('finish_reason') for x in e)))
 for x in e:
  u=x.get('usage') or {};d=u.get('completion_tokens_details') or {}
  empty.append({'group':name,'qid':x.get('qid'),'arm':x.get('arm'),'finish':x.get('finish_reason'),
   'completion_tokens':u.get('completion_tokens'),'reasoning_tokens':d.get('reasoning_tokens'),'prompt_tokens':u.get('prompt_tokens'),'answer_type':type(x.get('answer')).__name__})
print('EMPTY_FINISH',dict(collections.Counter(x['finish'] for x in empty)))
print('EMPTY_COMPLETION_TOKENS',dict(collections.Counter(x['completion_tokens'] for x in empty)))
print('EMPTY_REASONING_TOKENS',dict(collections.Counter(x['reasoning_tokens'] for x in empty)))
print('EMPTY_ANSWER_TYPES',dict(collections.Counter(x['answer_type'] for x in empty)))
print('EMPTY_SAMPLE',json.dumps(empty[:6],ensure_ascii=False))

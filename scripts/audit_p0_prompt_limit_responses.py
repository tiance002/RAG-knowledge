import json,pathlib
b=pathlib.Path('/opt/amrag/experiments/pdf-table-hardcases-20261027')
for sub in ('prompt-limit-factorial-2x2-20261028-v2','prompt-limit-factorial-2x2-continuation-20261028-v1','prompt-limit-factorial-2x2-traced-20261028-v3'):
 r=b/sub;print('\n###',sub)
 for x in map(json.loads,(r/'results.jsonl').read_text().splitlines()):
  p=r/f"response-{x['ordinal']}-{x['variant']}-{x['max_tokens']}.json";o=json.loads(p.read_text());print('\nCELL',x['ordinal'],x['variant'],x['max_tokens'],x['finish_reason'],x['empty'],x.get('usage'),x.get('trace_id'));print('ANSWER',repr(o['choices'][0]['message'].get('content')))

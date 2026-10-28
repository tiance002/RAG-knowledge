"""Audit frozen paired evaluations without guessing semantic correctness."""
import collections,hashlib,json,pathlib,sys,time

base=pathlib.Path(sys.argv[1]).resolve();dataset=base/'dataset200';office=base/'office50-v1';sha=lambda b:hashlib.sha256(b).hexdigest()
frozen=json.loads((dataset/'freeze.json').read_bytes())
for name,digest in frozen['files'].items():assert sha((dataset/name).read_bytes())==digest
newfreeze=json.loads((office/'dataset250/freeze250.json').read_bytes())
assert newfreeze['old200_freeze_sha256']==sha((dataset/'freeze.json').read_bytes())
raw=(office/'dataset250/office50.jsonl').read_bytes();assert sha(raw)==newfreeze['office50_sha256']
gold=[json.loads(l) for l in (dataset/'gold100.jsonl').open(encoding='utf-8')];additional=[json.loads(l) for l in raw.splitlines()]
registry={x['qid']:x for x in (json.loads(l) for l in (dataset/'registry200.jsonl').open(encoding='utf-8'))}
assert len(gold)==100 and len(additional)==50
expected={(x['qid'],arm) for x in gold+additional for arm in ('before','after')};assert len(expected)==300
rows={}
for sub in ('runtime200-r4','answers200-continuation-r1','answers200-fast-r2'):
 for row in (json.loads(l) for l in (base/sub/'results.jsonl').open(encoding='utf-8')):
  key=(row['qid'],row['arm']);assert key in expected and key not in rows;rows[key]=row
assert len(rows)==200
office_rows=[json.loads(l) for l in (office/'evaluation/results.jsonl').open(encoding='utf-8')]
retry={(x['qid'],x['arm']):x for x in (json.loads(l) for l in (office/'retry429-r1/results.jsonl').open(encoding='utf-8'))}
assert len(office_rows)==100 and len(retry)==14
for row in office_rows:
 key=(row['qid'],row['arm']);assert key in expected and key not in rows
 if key in retry:
  assert row['status']=='SKIPPED' and row['reason']=='generation_failed';row=retry[key]
 rows[key]=row
assert set(rows)==expected
cohorts={**{x['qid']:registry[x['qid']]['cohort'] for x in gold},**{x['qid']:x['format'] for x in additional}}
summary={'pairs_planned':300,'old_answer_questions':100,'office_answer_questions':50,'groups':{},
 'semantic_accuracy':'NOT_SCORED','literal_matches_are_not_correctness':True,'created_at':time.time()}
for cohort in sorted(set(cohorts.values())):
 for arm in ('before','after'):
  values=[row for (qid,a),row in rows.items() if cohorts[qid]==cohort and a==arm]
  completed=[x for x in values if x.get('status','COMPLETED')=='COMPLETED']
  empty=[x for x in completed if not (x.get('answer') or '').strip()]
  summary['groups'][cohort+'_'+arm]={'planned':len(values),'completed':len(completed),'empty':len(empty),
   'nonempty_pending_semantic_review':len(completed)-len(empty),'skipped':len(values)-len(completed),
   'empty_finish_reasons':dict(collections.Counter(x.get('finish_reason') for x in empty))}
summary['totals']={'completed':sum(x['completed'] for x in summary['groups'].values()),
 'empty':sum(x['empty'] for x in summary['groups'].values()),'skipped':sum(x['skipped'] for x in summary['groups'].values()),
 'nonempty_pending_semantic_review':sum(x['nonempty_pending_semantic_review'] for x in summary['groups'].values())}
ret=[json.loads(l) for l in (base/'runtime200-r4/retrieval-results.jsonl').open(encoding='utf-8')]
summary['retrieval']={}
for subset in ('all','pdf','text'):
 for metric in ('raw_recall_at_10','recall_at_10'):
  for arm in ('before','after'):
   values=[x[metric] for x in ret if registry[x['qid']]['split']=='retrieval' and x['arm']==arm and x[metric] is not None
           and (subset=='all' or registry[x['qid']]['is_pdf']==(subset=='pdf'))]
   summary['retrieval'][f'{subset}_{metric}_{arm}']={'n':len(values),'mean':sum(values)/len(values) if values else None}
target=base/'audit250-20261028.json'
if target.exists():raise RuntimeError('Refuse to overwrite prior audit')
target.write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(summary,ensure_ascii=False))

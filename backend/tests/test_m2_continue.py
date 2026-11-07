"""Continuation tests use synthetic history, guarded HTTP and an owned Redis."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import httpx
import pytest

ROOT=Path(__file__).resolve().parents[2]
PATH=ROOT/'scripts/m2_preference_continue.py'
MODULE=importlib.util.spec_from_file_location('m2_continue_test',PATH)
cont=importlib.util.module_from_spec(MODULE);MODULE.loader.exec_module(cont)
FIXTURES=importlib.util.spec_from_file_location('m2_continue_public_fixtures',Path(__file__).with_name('test_m2_supplement.py'))
fixtures=importlib.util.module_from_spec(FIXTURES);FIXTURES.loader.exec_module(fixtures)


def write_rows(path,records):
    path.write_bytes(b''.join(cont.base.canonical(row)+b'\n' for row in records))


@pytest.fixture
def history(tmp_path,monkeypatch):
    spec=fixtures.spec.__wrapped__(tmp_path,monkeypatch)
    monkeypatch.setattr(cont.base,'ROOT',tmp_path)
    parent_path=Path(spec['parent_manifest_path']);manifest=json.loads(parent_path.read_bytes())
    for index,case in enumerate(manifest['cases']):
        case.update(question=f'根据资料， Tribometer 的指标是什么？ {index}',negative=index==4,
                    expected_chunk_ids=[] if index==4 else ['hot-context-9'])
    parent_path.write_bytes(cont.base.canonical(manifest))
    plan=list(cont.ORIGINAL_PLAN(manifest));primary=Path(spec['parent_output'])
    firstcase,firstmode,firstrep,firstarm,prefs=plan[0]
    reserve='.0130688'
    pending={'call_id':1,'qid':firstcase['qid'],'mode':firstmode,'arm':firstarm,'repetition':firstrep,
             'requested_model':manifest['model'],'input_token_bound':2000,'output_token_bound':4096,
             'reservation_cny':reserve,'state':'UNKNOWN_PENDING'}
    completed={**pending,'state':'COMPLETED','actual_model':manifest['model'],'provider_request_id':'primary-one',
               'usage':{'prompt_tokens':100,'completion_tokens':50},'price_based_cost_cny':'.00022'}
    write_rows(primary/'provider-ledger.jsonl',[pending,completed])
    records=[{'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep,'state':'NO_DONE' if n==72 else 'RETURNED',
              'provider_call_ids':[1] if n==0 else []} for n,(case,mode,rep,arm,prefs) in enumerate(plan[:73])]
    write_rows(primary/'trials.jsonl',records)
    summary={'status':'STOPPED','budget_cny':'1','actual_http_calls':1,'recorded_trials':73,
             'price_based_known_cost_cny':'.00022','committed_including_unknown_cny':'.00022'}
    (primary/'summary.json').write_bytes(cont.base.canonical(summary))
    supplement=tmp_path/'var'/'completed-d-supplement';supplement.mkdir()
    prior_trials=[{'qid':'m2-supp:procedure','mode':mode,'arm':arm,'repetition':rep,'state':'RETURNED',
                   'provider_call_ids':[1] if (mode,rep,arm)==('chat',1,'A') else []}
                  for mode in ('chat','agent') for rep in (1,2) for arm in ('A','D')]
    supppending={**pending,'qid':'m2-supp:procedure'}
    suppcompleted={**completed,'qid':'m2-supp:procedure','provider_request_id':'supplement-one'}
    write_rows(supplement/'provider-ledger.jsonl',[supppending,suppcompleted]);write_rows(supplement/'trials.jsonl',prior_trials)
    (supplement/'summary.json').write_bytes(cont.base.canonical({'status':'RECORDED_REQUIRES_EVIDENCE_REVIEW',
        'purpose':'D_ELIGIBILITY_ONLY','budget_cny':'.99978','actual_http_calls':1,'recorded_trials':8,
        'committed_including_unknown_cny':'.00022','price_based_known_cost_cny':'.00022',
        'parent_budget':{'parent_committed_cny':'.00022'},'global_committed_including_unknown_cny':'.00044'}))
    result={**spec,'schema_version':cont.SCHEMA,'authorization_id':'synthetic-continue-only-unstarted',
            'purpose':'ONLY_NEVER_STARTED','base_runner_sha256':cont.base.digest(Path(cont.base.__file__).read_bytes()),
            'expected_remaining_trials':23,'prior_runs':[{'kind':'D_ELIGIBILITY_ONLY','output':str(supplement)}]}
    refresh(result)
    return result


def refresh(spec):
    spec['parent_manifest_sha256']=cont.base.digest(Path(spec['parent_manifest_path']).read_bytes())
    parent=Path(spec['parent_output'])
    for role in ('summary','ledger','trials'):
        name='provider-ledger.jsonl' if role=='ledger' else 'trials.jsonl' if role=='trials' else 'summary.json'
        spec[f'parent_{role}_sha256']=cont.base.digest((parent/name).read_bytes())
    for entry in spec['prior_runs']:
        for role in ('summary','ledger','trials'):
            name='provider-ledger.jsonl' if role=='ledger' else 'trials.jsonl' if role=='trials' else 'summary.json'
            entry[f'{role}_sha256']=cont.base.digest((Path(entry['output'])/name).read_bytes())
        if entry.get('kind')=='ONLY_NEVER_STARTED':
            entry['continuation_manifest_sha256']=cont.base.digest((Path(entry['output'])/'continuation-manifest.json').read_bytes())


def add_prior_continuation(history,tmp_path):
    manifest,plan,budget=cont.verify_history(history)
    previous=tmp_path/'var'/'continuation-r1';previous.mkdir()
    # The immutable r1 authorization has only its original primary+D prefix.
    (previous/'continuation-manifest.json').write_bytes(cont.base.canonical(history))
    selected=plan[:17]
    attempts=[];trials=[]
    ids_by_identity={}
    for index in range(29):
        case,mode,rep,arm,prefs=selected[min(index,16)]
        prompt=200000 if index<4 else 53855 if index==4 else 0
        reserve=(cont.Decimal(prompt)*cont.Decimal('.8')+cont.Decimal(4096)*cont.Decimal('2.8'))/1000000
        cost=cont.Decimal(prompt)*cont.Decimal('.8')/1000000
        pending={'call_id':index+1,'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep,
                 'state':'UNKNOWN_PENDING','requested_model':manifest['model'],'input_token_bound':prompt,
                 'output_token_bound':4096,'reservation_cny':str(reserve)}
        completed={**pending,'state':'COMPLETED','actual_model':manifest['model'],
                   'usage':{'prompt_tokens':prompt,'completion_tokens':0},'price_based_cost_cny':str(cost),
                   'provider_request_id':f'continuation-r1-provider-{index+1}'}
        attempts.extend([pending,completed]);ids_by_identity.setdefault(cont.identity(pending),[]).append(index+1)
    for index,(case,mode,rep,arm,prefs) in enumerate(selected):
        trials.append({'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep,
                       'state':'GUARD_STOP' if index==16 else 'RETURNED',
                       'provider_call_ids':ids_by_identity[(case['qid'],mode,arm,rep)]})
    write_rows(previous/'provider-ledger.jsonl',attempts);write_rows(previous/'trials.jsonl',trials)
    (previous/'summary.json').write_bytes(cont.base.canonical({'status':'STOPPED','purpose':'ONLY_NEVER_STARTED',
        'budget_cny':budget['remaining_cny'],'actual_http_calls':29,'recorded_trials':17,
        'committed_including_unknown_cny':'.683084','price_based_known_cost_cny':'.683084',
        'prior_budget':{'prior_combined_committed_cny':'.00044'},'global_committed_including_unknown_cny':'.683524'}))
    history['authorization_id']='synthetic-continue-r2-six-only';history['expected_remaining_trials']=6
    history['prior_runs'].append({'kind':'ONLY_NEVER_STARTED','output':str(previous)})
    refresh(history)
    return previous


def test_exact23_unstarted_excludes_failed_primary_and_combined_budget(history):
    result=cont.validate_spec(history)
    assert result['planned_trials']==23 and result['model_calls']==0
    assert result['budget']['remaining_cny']=='0.99956'
    assert result['budget']['prior_combined_committed_cny']=='0.00044'
    remaining={(row['qid'],row['mode'],row['arm'],row['repetition']) for row in result['remaining_identities']}
    assert ('old-4','agent','A',1) not in remaining
    assert ('old-4','agent','B',1) in remaining
    assert len([row for row in remaining if row[0]=='old-5'])==16
    assert [row['qid'] for row in result['remaining_identities'][:16]]==['old-5']*16
    assert [row['arm'] for row in result['remaining_identities'][:4]]==['A','B','C','D']


def test_changed_primary_or_prior_is_rejected(history):
    path=Path(history['prior_runs'][0]['output'])/'trials.jsonl';path.write_bytes(path.read_bytes()+b'\n')
    with pytest.raises(ValueError,match='changed'):cont.validate_spec(history)


def test_running_supplement_refused_attributed_unknown_keeps_full_reservation(history):
    entry=history['prior_runs'][0];output=Path(entry['output'])
    data=json.loads((output/'summary.json').read_bytes());data['status']='RUNNING'
    (output/'summary.json').write_bytes(cont.base.canonical(data));refresh(history)
    with pytest.raises(ValueError,match='final D supplement'):cont.validate_spec(history)
    data['status']='STOPPED';(output/'summary.json').write_bytes(cont.base.canonical(data))
    records=[json.loads(line) for line in (output/'provider-ledger.jsonl').read_bytes().splitlines()]
    records[-1]['state']='UNKNOWN';write_rows(output/'provider-ledger.jsonl',records)
    data.update(committed_including_unknown_cny='.0130688',price_based_known_cost_cny='0',
                global_committed_including_unknown_cny='.0132888')
    (output/'summary.json').write_bytes(cont.base.canonical(data));refresh(history)
    assert cont.validate_spec(history)['budget']['remaining_cny']=='0.9867112'
    records[-1].pop('qid');write_rows(output/'provider-ledger.jsonl',records);refresh(history)
    with pytest.raises(ValueError,match='attributed'):cont.validate_spec(history)


def test_duplicate_prior_and_provider_identity_overlap_rejected(history):
    original=history['prior_runs'][0]['output'];history['prior_runs'][0]['output']=history['parent_output']
    with pytest.raises(ValueError,match='Duplicate prior'):cont.validate_spec(history)
    history['prior_runs'][0]['output']=original
    path=Path(original)/'provider-ledger.jsonl';records=[json.loads(line) for line in path.read_bytes().splitlines()]
    records[-1]['provider_request_id']='primary-one';write_rows(path,records);refresh(history)
    with pytest.raises(ValueError,match='reused across runs'):cont.validate_spec(history)


def test_paid_but_unrecorded_identity_is_started_never_replayed(history):
    parent=Path(history['parent_output']);manifest,plan,budget=cont.verify_history(history)
    case,mode,rep,arm,prefs=plan[0]
    records=[json.loads(line) for line in (parent/'provider-ledger.jsonl').read_bytes().splitlines()]
    pending={**records[0],'call_id':2,'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep}
    completed={**records[1],**pending,'state':'COMPLETED','provider_request_id':'orphan-started-two'}
    write_rows(parent/'provider-ledger.jsonl',records+[pending,completed])
    summary=json.loads((parent/'summary.json').read_bytes());summary.update(actual_http_calls=2,
        price_based_known_cost_cny='.00044',committed_including_unknown_cny='.00044')
    (parent/'summary.json').write_bytes(cont.base.canonical(summary))
    prior_summary=Path(history['prior_runs'][0]['output'])/'summary.json'
    prior=json.loads(prior_summary.read_bytes());prior.update(budget_cny='.99956',
        parent_budget={'parent_committed_cny':'.00044'},global_committed_including_unknown_cny='.00066')
    prior_summary.write_bytes(cont.base.canonical(prior));refresh(history)
    with pytest.raises(ValueError,match='Never-started count'):cont.validate_spec(history)
    history['expected_remaining_trials']=22
    assert cont.validate_spec(history)['planned_trials']==22


def test_guard_rejects_already_started_context_without_send(history,tmp_path):
    manifest,plan,budget=cont.verify_history(history)
    allowed={(case['qid'],mode,arm,rep) for case,mode,rep,arm,prefs in plan}
    guard=cont.HistoryTransport({**manifest,'budget_cny':budget['remaining_cny']},tmp_path,history,allowed)
    guard.context={'qid':'old-4','mode':'agent','arm':'A','repetition':1}
    async def sender(*a,**kw):pytest.fail('Started primary identity must not reach network')
    with pytest.raises(cont.base.GuardStop,match='identity check failed'):
        asyncio.run(guard.send(sender,None,httpx.Request('POST',cont.base.endpoint(manifest['base_url']),json={'model':manifest['model']})))
    assert not guard.calls


def test_unknown_new_attempt_holds_reservation_no_retry_and_global_limit(history,tmp_path):
    manifest,plan,budget=cont.verify_history(history);allowed={(case['qid'],mode,arm,rep) for case,mode,rep,arm,prefs in plan}
    guard=cont.HistoryTransport({**manifest,'budget_cny':budget['remaining_cny']},tmp_path,history,allowed)
    case,mode,rep,arm,prefs=plan[0];guard.context={'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep};sent=[]
    async def sender(*a,**kw):sent.append(True);raise httpx.ReadTimeout('synthetic only')
    request=httpx.Request('POST',cont.base.endpoint(manifest['base_url']),json={'model':manifest['model'],'messages':[]})
    with pytest.raises(cont.base.GuardStop,match='UNKNOWN'):asyncio.run(guard.send(sender,None,request))
    assert guard.committed==cont.safety.decimal(guard.calls[0]['reservation_cny'])
    assert cont.safety.decimal(budget['prior_combined_committed_cny'])+guard.committed<=1
    with pytest.raises(cont.base.GuardStop):asyncio.run(guard.send(sender,None,request))
    assert len(sent)==1


def test_skip_recorded_application_failure_but_never_guard_unknown(history,tmp_path,monkeypatch):
    manifest,plan,budget=cont.verify_history(history)
    allowed={(case['qid'],mode,arm,rep) for case,mode,rep,arm,prefs in plan}
    guard=cont.HistoryTransport({**manifest,'budget_cny':budget['remaining_cny']},tmp_path,history,allowed)
    seen=[]
    async def fake_trials(manifest,output,guard):
        case,mode,rep,arm,prefs=next(iter(cont.base.trial_plan(manifest)));seen.append((case['qid'],mode,arm,rep))
        record={'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep,'state':'GUARD_STOP' if len(seen)==1 else 'RETURNED'}
        with (output/'trials.jsonl').open('ab') as stream:stream.write(cont.base.canonical(record)+b'\n')
        if len(seen)==1:raise cont.base.GuardStop('Synthetic application no-done, no provider unknown')
    monkeypatch.setattr(cont.base,'run_trials',fake_trials)
    skipped=asyncio.run(cont.run_plan(manifest,tmp_path,guard,plan[:2]))
    assert len(seen)==2 and len(set(seen))==2 and len(skipped)==1
    guard.stopped=True
    with pytest.raises(cont.base.GuardStop):
        # The same application guard exception must not override stopped guard.
        seen.clear();(tmp_path/'trials.jsonl').unlink()
        asyncio.run(cont.run_plan(manifest,tmp_path,guard,plan[:2]))
    assert len(seen)==1


def test_chained17_preserves_failed_a_b_and_derives_six_exact_budget(history,tmp_path):
    add_prior_continuation(history,tmp_path)
    result=cont.validate_spec(history)
    assert result['planned_trials']==6
    assert result['budget']['prior_combined_committed_cny']=='0.683524'
    assert result['budget']['remaining_cny']=='0.316476'
    assert result['budget']['all_prior_started_original_identities']==90
    assert result['budget']['primary_trial_records']==73
    assert result['budget']['prior_continuation_trial_records']==17
    assert [(r['arm'],r['repetition']) for r in result['remaining_identities']]==[
        ('C',1),('D',1),('A',2),('B',2),('C',2),('D',2)]
    assert all(r['qid']=='old-4' and r['mode']=='agent' for r in result['remaining_identities'])


def test_prior_continuation_ancestry_hash_and_repeated_identity_fail(history,tmp_path):
    previous=add_prior_continuation(history,tmp_path)
    path=previous/'continuation-manifest.json';original=path.read_bytes()
    contract=json.loads(original);contract['prior_runs']=[];path.write_bytes(cont.base.canonical(contract));refresh(history)
    with pytest.raises(ValueError,match='ancestry'):cont.validate_spec(history)
    path.write_bytes(original);refresh(history)
    trial_path=previous/'trials.jsonl';records=[json.loads(line) for line in trial_path.read_bytes().splitlines()]
    records[0].update(qid='old-4',mode='agent',arm='A',repetition=1)
    write_rows(trial_path,records);refresh(history)
    with pytest.raises(ValueError,match='repeats a started'):cont.validate_spec(history)


def test_unknown_in_prior_continuation_keeps_reservation_and_excludes_identity(history,tmp_path):
    previous=add_prior_continuation(history,tmp_path)
    path=previous/'provider-ledger.jsonl';records=[json.loads(line) for line in path.read_bytes().splitlines()]
    records[-1]['state']='UNKNOWN';write_rows(path,records)
    summary_path=previous/'summary.json';summary=json.loads(summary_path.read_bytes())
    summary.update(committed_including_unknown_cny='.6945528',global_committed_including_unknown_cny='.6949928')
    summary_path.write_bytes(cont.base.canonical(summary));refresh(history)
    result=cont.validate_spec(history)
    assert result['planned_trials']==6 and result['budget']['remaining_cny']=='0.3050072'
    records[-1].pop('qid');write_rows(path,records);refresh(history)
    with pytest.raises(ValueError,match='attributed'):cont.validate_spec(history)


def test_continuation_real_redis_actual_chat_guarded_http_sentinel_unchanged(history,tmp_path):
    add_prior_continuation(history,tmp_path)
    source=tmp_path/'continue.json';source.write_bytes(cont.base.canonical(history))
    environment={**os.environ,'PYTHONIOENCODING':'utf-8'}
    if not environment.get('RAG_TEST_REDIS_CMD'):
        pointer=ROOT/'var/preferences-acceptance/redis-test-runtime.txt'
        if pointer.exists() and shutil.which('wsl.exe'):
            environment['RAG_TEST_REDIS_CMD']=json.dumps(['wsl.exe','-d','Ubuntu','--exec',pointer.read_text().strip()])
    code=r'''
import asyncio,importlib.util,json,os,socket,sqlite3,sys
from pathlib import Path
import httpx
original_connect=socket.socket.connect
class NoExternalNetwork(BaseException):pass
def deny(sock,address,*a,**kw):
 if isinstance(address,tuple) and address[0] in ('127.0.0.1','::1'):return original_connect(sock,address,*a,**kw)
 raise NoExternalNetwork('No external sockets during continuation dry run')
socket.socket.connect=deny;socket.socket.connect_ex=deny
module=importlib.util.spec_from_file_location('c',sys.argv[1]);c=importlib.util.module_from_spec(module);module.loader.exec_module(c)
c.base.private_path=lambda path:Path(path).resolve()
spec=json.loads(Path(sys.argv[2]).read_bytes());out=Path(sys.argv[3]);out.mkdir()
sentinel=out/'preexisting.db';db=sqlite3.connect(sentinel);db.execute('create table sentinel(value text)');db.execute("insert into sentinel values ('keep')");db.commit();db.close();before=c.base.digest(sentinel.read_bytes())
os.environ.update(DATABASE_URL='sqlite:///'+str(sentinel),ENV='dev',MEMORY_REDIS_URL='redis://127.0.0.1:1/0')
assert c.validate_spec(spec)['planned_trials']==6
assert 'app.core.database' not in sys.modules and 'app.core.config' not in sys.modules
parent,plan,budget=c.verify_history(spec);m={**parent,'budget_cny':budget['remaining_cny']};os.environ['M2_AB_API_KEY']='synthetic-only'
selected=plan[:2];allowed={(case['qid'],mode,arm,rep) for case,mode,rep,arm,prefs in plan};calls=[]
async def provider(request):
 p=json.loads(request.content);calls.append(True);message={'role':'assistant','content':'摩擦系数、摩擦力及磨损体积。[1]'};finish='stop'
 if p.get('tools') and not any(msg.get('role')=='tool' for msg in p['messages']):
  message={'role':'assistant','content':'','tool_calls':[{'id':'call-public','type':'function','function':{'name':'search_knowledge_base','arguments':json.dumps({'query':'tribometer friction'})}}]};finish='tool_calls'
 elif '文档相关性评分员' in str(p['messages']):message={'role':'assistant','content':'0.95'}
 body={'id':f'synthetic-continuation-{len(calls)}','model':m['model'],'object':'chat.completion','created':1,'choices':[{'index':0,'message':message,'finish_reason':finish}],'usage':{'prompt_tokens':20,'completion_tokens':10,'total_tokens':30}}
 if p.get('stream'):
  first={**body,'choices':[{'index':0,'delta':message,'finish_reason':None}],'usage':None}
  last={**body,'choices':[{'index':0,'delta':{},'finish_reason':finish}],'usage':None}
  tail={**body,'choices':[]}
  return httpx.Response(200,request=request,content=b''.join(b'data: '+c.base.canonical(obj)+b'\n\n' for obj in (first,last,tail))+b'data: [DONE]\n\n',headers={'content-type':'text/event-stream'})
 return httpx.Response(200,request=request,json=body)
g=c.HistoryTransport(m,out,spec,allowed,transport=httpx.MockTransport(provider))
with c.base.isolated_redis(out) as redis_url:
 c.base.configure_runtime(m,out,redis_url)
 with g.installed():
  c.safety.assert_runtime_isolation(out,redis_url);c.base.build_index(m)
  skipped=asyncio.run(c.run_plan(m,out,g,selected))
trials=c.rows((out/'trials.jsonl').read_bytes())
assert len(trials)==2 and len(calls)==6 and all(row['state']=='RETURNED' for row in trials) and not skipped
assert c.base.digest(sentinel.read_bytes())==before and (out/'isolated.db').exists()
assert c.safety.decimal(budget['prior_combined_committed_cny'])+g.committed<=1
assert len(c.verify_history(spec)[1])==6
print(json.dumps({'sentinel_unchanged':True,'real_redis_trials':2,'real_provider_calls':0}))
'''
    result=subprocess.run([sys.executable,'-c',code,str(PATH),str(source),str(tmp_path/'dry-run')],capture_output=True,text=True,
                          encoding='utf-8',timeout=120,env=environment)
    assert result.returncode==0,(result.stdout[-2000:],result.stderr[-5000:])
    assert '"sentinel_unchanged": true' in result.stdout


def add_http_history(history, tmp_path):
    manifest, plan, budget = cont.verify_history(history)
    output = tmp_path/'var'/'http-run'; output.mkdir()
    ids = [{'qid':'office50:031','mode':mode,'arm':'A','repetition':3,
            'run_identity':f'm2-http-real:test:{mode}'} for mode in ('chat','agent')]
    contract = {'schema':'m2-http-real/v1','purpose':'NEW_HTTP_SSE_ONLY','authorized':True,
        'authorization_id':'test','max_new_requests':2,'history':json.loads(json.dumps(history)),
        'identities':ids,'qid':'office50:031','model':manifest['model'],
        'max_output_tokens':manifest['max_output_tokens'],'total_budget_cny':'1',
        'fixed_search':cont.base.FIXED_SEARCH,
        'http_runner_sha256':cont.base.digest((ROOT/'scripts/m2_http_real_smoke.py').read_bytes())}
    (output/'manifest.json').write_bytes(cont.base.canonical(contract))
    write_rows(output/'started.jsonl',ids)
    request=b'example request'; (output/'request-0001.json').write_bytes(request)
    pending={**ids[0], 'dataset_version':manifest['dataset_version'],'call_id':1,
             'requested_model':manifest['model'],'input_token_bound':2000,'output_token_bound':4096,
             'reservation_cny':'.0130688','request_sha256':cont.base.digest(request),'state':'UNKNOWN_PENDING'}
    completed={**pending,'state':'COMPLETED','actual_model':manifest['model'],
               'provider_request_id':'http-unique','usage':{'prompt_tokens':100,'completion_tokens':50},
               'price_based_cost_cny':'.00022'}
    write_rows(output/'provider-ledger.jsonl',[pending,completed])
    write_rows(output/'trials.jsonl',[{**ids[0],'provider_call_ids':[1]}, {**ids[1],'provider_call_ids':[]}])
    before=cont.Decimal(budget['prior_combined_committed_cny'])
    summary={'purpose':'NEW_HTTP_SSE_ONLY','status':'RECORDED_REQUIRES_EVIDENCE_REVIEW',
        'budget_cny':budget['remaining_cny'],'actual_http_calls':1,
        'committed_including_unknown_cny':'.00022','price_based_known_cost_cny':'.00022',
        'prior_budget':{'prior_combined_committed_cny':str(before)},
        'global_committed_including_unknown_cny':str(before+cont.Decimal('.00022'))}
    (output/'summary.json').write_bytes(cont.base.canonical(summary))
    entry={'kind':'NEW_HTTP_SSE_ONLY','output':str(output)}
    for role,name in [('http_manifest','manifest.json'),('started','started.jsonl'),('ledger','provider-ledger.jsonl'),('summary','summary.json'),('trials','trials.jsonl')]:
        entry[role+'_sha256']=cont.base.digest((output/name).read_bytes())
    history['prior_runs'].append(entry)
    return output,entry


def test_http_prior_cost_once_and_original_identities_preserved(history,tmp_path):
    before=cont.validate_spec(history)
    output,entry=add_http_history(history,tmp_path)
    after=cont.validate_spec(history)
    assert after['remaining_identities']==before['remaining_identities']
    assert cont.Decimal(after['budget']['prior_combined_committed_cny']) == cont.Decimal(before['budget']['prior_combined_committed_cny'])+cont.Decimal('.00022')
    history['prior_runs'].append(entry)
    with pytest.raises(ValueError): cont.validate_spec(history)


def test_http_tamper_and_misattribution_rejected(history,tmp_path):
    output,entry=add_http_history(history,tmp_path)
    (output/'request-0001.json').write_bytes(b'tampered')
    with pytest.raises(ValueError): cont.validate_spec(history)
    (output/'request-0001.json').write_bytes(b'example request')
    data=cont.rows((output/'provider-ledger.jsonl').read_bytes())
    for record in data: record['run_identity']='m2-http-real:other:chat'
    write_rows(output/'provider-ledger.jsonl',data)
    entry['ledger_sha256']=cont.base.digest((output/'provider-ledger.jsonl').read_bytes())
    with pytest.raises(ValueError): cont.validate_spec(history)


def test_http_unknown_retains_reservation(history,tmp_path):
    output,entry=add_http_history(history,tmp_path)
    ledger=output/'provider-ledger.jsonl'; write_rows(ledger,cont.rows(ledger.read_bytes())[:1])
    entry['ledger_sha256']=cont.base.digest(ledger.read_bytes())
    summary=json.loads((output/'summary.json').read_bytes())
    summary['committed_including_unknown_cny']='.0130688';summary['price_based_known_cost_cny']='0'
    summary['global_committed_including_unknown_cny']=str(cont.Decimal(summary['prior_budget']['prior_combined_committed_cny'])+cont.Decimal('.0130688'))
    (output/'summary.json').write_bytes(cont.base.canonical(summary));entry['summary_sha256']=cont.base.digest((output/'summary.json').read_bytes())
    assert cont.Decimal(cont.validate_spec(history)['budget']['prior_combined_committed_cny']) == cont.Decimal('.00044')+cont.Decimal('.0130688')


def test_http_prior_ancestry_and_provider_reuse_rejected(history,tmp_path):
    output,entry=add_http_history(history,tmp_path)
    manifest=output/'manifest.json'; obj=json.loads(manifest.read_bytes())
    obj['history']['parent_ledger_sha256']='0'*64
    manifest.write_bytes(cont.base.canonical(obj));entry['http_manifest_sha256']=cont.base.digest(manifest.read_bytes())
    with pytest.raises(ValueError): cont.validate_spec(history)
    obj['history']['parent_ledger_sha256']=history['parent_ledger_sha256']
    manifest.write_bytes(cont.base.canonical(obj));entry['http_manifest_sha256']=cont.base.digest(manifest.read_bytes())
    ledger=output/'provider-ledger.jsonl'; data=cont.rows(ledger.read_bytes());data[-1]['provider_request_id']='primary-one'
    write_rows(ledger,data);entry['ledger_sha256']=cont.base.digest(ledger.read_bytes())
    with pytest.raises(ValueError): cont.validate_spec(history)


def test_persistent_http_grant_rejects_omission(history,tmp_path):
    original=json.loads(json.dumps(history))
    output,entry=add_http_history(history,tmp_path)
    grantdir=tmp_path/'var/preferences-acceptance/m2-http-smoke-real-locks';grantdir.mkdir(parents=True)
    contract=json.loads((output/'manifest.json').read_bytes())
    grant=grantdir/('chain-'+history['parent_manifest_sha256']+'.started')
    grant.write_bytes(cont.base.canonical({'authorization_id':contract['authorization_id'],
        'output':str(output),'spec_sha256':cont.base.digest(cont.base.canonical(contract))}))
    assert cont.validate_spec(history)['planned_trials']==23
    with pytest.raises(ValueError): cont.validate_spec(original)


def test_reuse_validates_and_copies_exact_frozen_index(tmp_path,monkeypatch):
    import sqlite3
    monkeypatch.setattr(cont.base,'ROOT',tmp_path)
    (tmp_path/'var').mkdir()
    source=tmp_path/'var/source.db'; target=tmp_path/'var/isolated.db'
    manifest={'chunks':[{'id':'c1','source_id':'f1','content':'frozen'}], 'sources':[{'id':'f1'}]}
    with sqlite3.connect(source) as db:
        db.executescript('''CREATE TABLE document_chunks(id TEXT,file_id TEXT,content TEXT);
            CREATE TABLE knowledge_bases(id TEXT,chunk_count INT);
            CREATE TABLE file_documents(id TEXT,kb_id TEXT);
            CREATE TABLE chunk_index(chunk_id TEXT,kb_id TEXT,terms TEXT,doc_length INT);
            CREATE TABLE index_stats(kb_id TEXT,total_docs INT);
            CREATE TABLE retrieval_logs(query TEXT);''')
        db.execute('INSERT INTO document_chunks VALUES (?,?,?)',('c1','f1','frozen'))
        db.execute('INSERT INTO knowledge_bases VALUES (?,?)',(cont.base.KB_ID,1))
        db.execute('INSERT INTO file_documents VALUES (?,?)',('f1',cont.base.KB_ID))
        db.execute('INSERT INTO chunk_index VALUES (?,?,?,?)',('c1',cont.base.KB_ID,'terms',1))
        db.execute('INSERT INTO index_stats VALUES (?,?)',(cont.base.KB_ID,1))
        db.execute("INSERT INTO retrieval_logs VALUES ('old')")
    spec={'source_db':str(source),'source_db_sha256':cont.base.digest(source.read_bytes())}
    cont.backup_reuse(spec,manifest,target)
    assert cont.base.digest(source.read_bytes())==spec['source_db_sha256']
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT * FROM chunk_index').fetchall()==[('c1',cont.base.KB_ID,'terms',1)]
        assert db.execute('SELECT * FROM retrieval_logs').fetchall()==[]
    with sqlite3.connect(source) as db: db.execute("UPDATE document_chunks SET content='changed'")
    with pytest.raises(ValueError,match='hash mismatch'): cont.validate_reuse(spec,manifest)
    spec['source_db_sha256']=cont.base.digest(source.read_bytes())
    with pytest.raises(ValueError,match='frozen chunks'): cont.validate_reuse(spec,manifest)


def test_lock_and_startup_failure_do_not_burn_grant(history,tmp_path,monkeypatch):
    monkeypatch.setenv('M2_AB_API_KEY','synthetic')
    output=tmp_path/'out'; locks=tmp_path/'var/preferences-acceptance/m2-continuation-locks';locks.mkdir(parents=True)
    active=locks/(history['parent_manifest_sha256']+'.active');active.write_bytes(b'existing')
    with pytest.raises(FileExistsError): cont.execute(history,output)
    assert list(locks.glob('*.started'))==[] and active.read_bytes()==b'existing'
    active.unlink()
    monkeypatch.setattr(cont,'validate_reuse',lambda *_: (_ for _ in ()).throw(ValueError('bad index')))
    with pytest.raises(ValueError,match='bad index'): cont.execute(history,output)
    assert list(locks.glob('*.started'))==[] and not active.exists() and not output.exists()


def test_budget_block_skips_only_known_completed_attempts(history,tmp_path,monkeypatch):
    manifest,plan,budget=cont.verify_history(history)
    guard=cont.HistoryTransport({**manifest,'budget_cny':budget['remaining_cny']},tmp_path,history,set())
    seen=[]
    async def fake_trials(manifest,output,guard):
        case,mode,rep,arm,_=next(iter(cont.base.trial_plan(manifest)));seen.append(case['qid'])
        record={'qid':case['qid'],'mode':mode,'arm':arm,'repetition':rep,'state':'GUARD_STOP'}
        with (output/'trials.jsonl').open('ab') as f:f.write(cont.base.canonical(record)+b'\n')
        if len(seen)==1:
            guard.stopped=True;guard.budget_blocked=True
            raise cont.base.GuardStop('wrapped budget failure')
    monkeypatch.setattr(cont.base,'run_trials',fake_trials)
    skipped=asyncio.run(cont.run_plan(manifest,tmp_path,guard,plan[:2]))
    assert len(seen)==2 and 'BUDGET_BLOCKED' in skipped[0]['reason']
    (tmp_path/'trials.jsonl').unlink();seen.clear();guard.calls=[{'state':'UNKNOWN','call_id':1}]
    with pytest.raises(cont.base.GuardStop): asyncio.run(cont.run_plan(manifest,tmp_path,guard,plan[:2]))
    assert len(seen)==1 and guard.stopped


def test_reuse_real_wal_mode_baseline_is_sealed_before_validation():
    from contextlib import closing
    import sqlite3
    import tempfile
    spec_path=ROOT/'var/preferences-acceptance/m2-reuse-spec.json'
    if not spec_path.exists():
        pytest.skip('real frozen local baseline unavailable')
    spec=json.loads(spec_path.read_bytes())
    source=Path(spec['source_db'])
    if not source.exists():
        pytest.skip('real frozen local database unavailable')
    manifest=json.loads(Path(spec['parent_manifest_path']).read_bytes())
    before=cont.base.digest(source.read_bytes())
    assert before==spec['source_db_sha256']
    with tempfile.TemporaryDirectory(prefix='m2-reuse-test-',dir=ROOT/'var/preferences-acceptance') as temp:
        target=Path(temp)/'isolated.db'
        cont.backup_reuse(spec,manifest,target)
        with closing(sqlite3.connect(f'{source.as_uri()}?mode=ro&immutable=1',uri=True)) as old, closing(sqlite3.connect(f'{target.as_uri()}?mode=ro&immutable=1',uri=True)) as new:
            for table in ('document_chunks','chunk_index','index_stats'):
                assert old.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()==new.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
            assert new.execute('SELECT COUNT(*) FROM retrieval_logs').fetchone()==(0,)
        wal=Path(str(target)+'-wal')
        assert not wal.exists() or wal.stat().st_size==0
    assert cont.base.digest(source.read_bytes())==before

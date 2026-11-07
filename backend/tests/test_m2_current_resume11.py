import asyncio, importlib.util, json
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal
import httpx,pytest
ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('m2_resume11_test',ROOT/'scripts/m2_current_resume11.py');m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
SPEC=ROOT/'var/preferences-acceptance/m2-current-resume11-manifest-v2.json'

def test_prior_reconciliation_and_order():
    a=m.inspect(json.loads(SPEC.read_bytes())); ids=a['remaining_identities']
    assert (a['started_identities'],a['planned_trials'])==(85,11)
    assert Decimal(a['committed_cny'])==Decimal('1.6456800') and Decimal(a['remaining_cny'])==Decimal('.3543200')
    assert Decimal(a['prior_unknown_171_reservation_cny'])==Decimal('.075968')
    assert all('tribometer' in x['qid'] for x in ids[:8]) and all('row-0685' in x['qid'] and x['mode']=='agent' for x in ids[8:])
    assert not {m.cont.identity(x) for x in ids} & set(m.cont.indexed_trials((m.PRIOR/'trials.jsonl').read_bytes()))

def test_frozen_files_tampering_rejected():
    for suffix in ('provider-ledger.jsonl','guard-diagnostics.jsonl','once.started','m2_current_resume16.py'):
        x=json.loads(SPEC.read_bytes());k=next(k for k in x['files'] if k.endswith(suffix));x['files'][k]='0'*64
        with pytest.raises(ValueError):m.inspect(x)

def test_only_budget_block_can_skip_and_never_retry(tmp_path,monkeypatch):
    plan=[({'qid':'a'},'chat',1,'A',{}),({'qid':'b'},'chat',1,'A',{})]; seen=[]
    guard=SimpleNamespace(calls=[{'state':'COMPLETED','call_id':1}],stopped=False,budget_blocked=False)
    async def fake(manifest,output,g):
        c,mode,rep,arm,_=next(m.base.trial_plan(manifest));seen.append(c['qid']); row={'qid':c['qid'],'mode':mode,'repetition':rep,'arm':arm,'state':'GUARD_STOP' if c['qid']=='a' else 'RETURNED'}
        with (output/'trials.jsonl').open('ab') as f:f.write(m.base.canonical(row)+b'\n')
        if c['qid']=='a':
            (output/'guard-diagnostics.jsonl').write_bytes(m.base.canonical({'code':'BUDGET_BLOCKED: test','identity':row})+b'\n');g.stopped=True;g.budget_blocked=True;raise m.base.GuardStop('wrapped')
    monkeypatch.setattr(m.base,'run_trials',fake);asyncio.run(m.run_plan({},tmp_path,guard,plan))
    assert seen==['a','b'] and not guard.stopped and len(m.cont.rows((tmp_path/'skipped-untestable.jsonl').read_bytes()))==1

@pytest.mark.parametrize('failure',['unclassified','unknown'])
def test_never_skip_unknown_or_unclassified(tmp_path,monkeypatch,failure):
    plan=[({'qid':'a'},'chat',1,'A',{}),({'qid':'b'},'chat',1,'A',{})];seen=[];g=SimpleNamespace(calls=[],stopped=False,budget_blocked=False)
    async def fake(manifest,output,guard):
        c,mode,rep,arm,_=next(m.base.trial_plan(manifest));seen.append(c['qid']);row={'qid':c['qid'],'mode':mode,'repetition':rep,'arm':arm,'state':'GUARD_STOP'}
        (output/'trials.jsonl').write_bytes(m.base.canonical(row)+b'\n');guard.stopped=True
        if failure=='unknown':guard.budget_blocked=True;guard.calls.append({'state':'UNKNOWN','call_id':1})
        raise m.base.GuardStop('stop')
    monkeypatch.setattr(m.base,'run_trials',fake)
    with pytest.raises(m.base.GuardStop):asyncio.run(m.run_plan({},tmp_path,g,plan))
    assert seen==['a'] and not (tmp_path/'skipped-untestable.jsonl').exists()

def test_actual_budget_diagnostic_and_changed_grant(tmp_path,monkeypatch):
    grant=tmp_path/'grant';grant.write_bytes(b'once');monkeypatch.setattr(m,'GRANT',grant);monkeypatch.setattr(m,'inspect',lambda _: {'remaining_cny':'0.000001'});monkeypatch.setattr(m,'no_writers',lambda:None)
    fresh={'base_url':'https://api.siliconflow.cn/v1','budget_cny':'0.000001','model':'fixed','max_output_tokens':128,'pricing':{'input_cny_per_million':'1','output_cny_per_million':'1'}}
    g=m.Transport(fresh,tmp_path,{}, {('q','chat','A',1)});g.grant_bytes=b'once';g.context={'qid':'q','mode':'chat','arm':'A','repetition':1}
    request=httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'fixed'})
    with pytest.raises(m.base.GuardStop,match='BUDGET_BLOCKED'):asyncio.run(g.send(None,None,request))
    d=json.loads((tmp_path/'guard-diagnostics.jsonl').read_bytes());assert g.budget_blocked and Decimal(d['reservation_cny'])>g.limit and not g.calls
    g.stopped=False;g.budget_blocked=False;grant.write_bytes(b'changed')
    with pytest.raises(m.base.GuardStop):asyncio.run(g.send(None,None,request))
    assert not g.budget_blocked


@pytest.mark.parametrize('kind',['timeout','429'])
def test_transport_unknown_never_skips(tmp_path,monkeypatch,kind):
    grant=tmp_path/'grant';grant.write_bytes(b'once');monkeypatch.setattr(m,'GRANT',grant);monkeypatch.setattr(m,'inspect',lambda _:{'remaining_cny':'0.3543200'});monkeypatch.setattr(m,'no_writers',lambda:None)
    fresh={'base_url':'https://api.siliconflow.cn/v1','budget_cny':'0.3543200','model':'fixed','max_output_tokens':128,'pricing':{'input_cny_per_million':'1','output_cny_per_million':'1'}}
    g=m.Transport(fresh,tmp_path,{}, {('q','chat','A',1)});g.grant_bytes=b'once';g.context={'qid':'q','mode':'chat','arm':'A','repetition':1}
    req=httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'fixed'})
    async def fake(*a,**k):
        if kind=='timeout':raise TimeoutError()
        return httpx.Response(429)
    with pytest.raises(m.base.GuardStop):asyncio.run(g.send(fake,None,req))
    assert g.calls[0]['state']=='UNKNOWN' and g.stopped and not g.budget_blocked
    with pytest.raises(m.base.GuardStop):asyncio.run(g.send(fake,None,req))
    assert len(g.calls)==1

@pytest.mark.parametrize('failure',['source','redis','grant_collision'])
def test_startup_never_burns_grant_or_sends(tmp_path,monkeypatch,failure):
    from contextlib import contextmanager
    source=json.loads((m.old.r.RUN/'manifest.json').read_bytes()); data=m.old.r.current.validate(source); audit=m.inspect(json.loads(SPEC.read_bytes()))
    d=tmp_path/'var/preferences-acceptance';d.mkdir(parents=True);grant=d/'m2-current-resume11-once.started'
    if failure=='grant_collision':grant.write_bytes(b'untouched')
    monkeypatch.setattr(m,'ROOT',tmp_path);monkeypatch.setattr(m,'GRANT',grant);monkeypatch.setattr(m,'inspect',lambda _:audit);monkeypatch.setattr(m,'no_writers',lambda:None)
    monkeypatch.setattr(m.old.r.current,'validate',lambda _:data);monkeypatch.setattr(m.base,'private_path',lambda p:Path(p));monkeypatch.setenv('M2_AB_API_KEY','synthetic-only')
    def backup(*a):
        if failure=='source':raise ValueError('corrupt')
    monkeypatch.setattr(m.cont,'backup_reuse',backup)
    @contextmanager
    def redis(_):
        if failure=='redis':raise RuntimeError('redis unavailable')
        yield 'redis://synthetic'
    monkeypatch.setattr(m.base,'isolated_redis',redis);monkeypatch.setattr(m.base,'configure_runtime',lambda *a:None);monkeypatch.setattr(m.cont.safety,'assert_runtime_isolation',lambda *a:None)
    result=m.execute({},d/'m2-current-resume11-synthetic');assert result['status']=='FAILED' and result['recorded_trials']==0
    assert grant.read_bytes()==b'untouched' if failure=='grant_collision' else not grant.exists()

def test_budget_followed_by_another_guard_event_never_skips(tmp_path,monkeypatch):
    value=({'qid':'a'},'chat',1,'A',{});g=SimpleNamespace(calls=[],stopped=True,budget_blocked=False)
    async def fake(manifest,output,guard):
        row={'qid':'a','mode':'chat','arm':'A','repetition':1,'state':'GUARD_STOP'}
        (output/'trials.jsonl').write_bytes(m.base.canonical(row)+b'\n')
        with (output/'guard-diagnostics.jsonl').open('wb') as f:
            for code in ('BUDGET_BLOCKED: test','Unbound asynchronous HTTP client blocked'):f.write(m.base.canonical({'code':code,'identity':row})+b'\n')
        guard.stopped=True;guard.budget_blocked=True;raise m.base.GuardStop('stop')
    monkeypatch.setattr(m.base,'run_trials',fake)
    with pytest.raises(m.base.GuardStop):asyncio.run(m.run_plan({},tmp_path,g,[value]))
    assert not (tmp_path/'skipped-untestable.jsonl').exists()

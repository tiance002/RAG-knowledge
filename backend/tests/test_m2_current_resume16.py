import asyncio
import importlib.util
import json
from pathlib import Path
from decimal import Decimal
import httpx
import pytest

ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('resume16_test',ROOT/'scripts/m2_current_resume16.py'); m=importlib.util.module_from_spec(s);s.loader.exec_module(m)
SPEC=ROOT/'var/preferences-acceptance/m2-current-resume16-manifest-v2.json'

def test_frozen_accounting_and_never_started():
    x=m.inspect(json.loads(SPEC.read_bytes()))
    assert (x['started_identities'],x['planned_trials'])==(80,16)
    assert Decimal(x['committed_cny'])+Decimal(x['remaining_cny'])==Decimal('2')
    assert Decimal(x['prior_unknown_171_reservation_cny'])==Decimal('0.075968')
    started=set(m.cont.indexed_trials((m.OLD/'trials.jsonl').read_bytes()))
    assert not started & {tuple(y) for y in x['remaining_identities']}

def test_tampered_frozen_evidence_blocks():
    for suffix in ('provider-ledger.jsonl','once.started','m2_current_resume.py','summary.json'):
        x=json.loads(SPEC.read_bytes()); k=next(k for k in x['files'] if k.endswith(suffix));x['files'][k]='0'*64
        with pytest.raises(ValueError):m.inspect(x)

def guard(tmp_path,monkeypatch,limit='0.3937804'):
    grant=tmp_path/'grant';grant.write_bytes(b'once');monkeypatch.setattr(m,'GRANT',grant)
    monkeypatch.setattr(m,'inspect',lambda _: {'remaining_cny':limit})
    monkeypatch.setattr(m,'no_writers',lambda:None)
    fresh={'base_url':'https://api.siliconflow.cn/v1','budget_cny':limit,'model':'fixed','max_output_tokens':128,'pricing':{'input_cny_per_million':'1','output_cny_per_million':'1'}}
    g=m.Transport(fresh,tmp_path,{}, {('q','chat','A',1)});g.grant_bytes=b'once';g.context={'qid':'q','mode':'chat','arm':'A','repetition':1}
    return g

def request():return httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'fixed'})

def test_reject_started_and_changed_grant(tmp_path,monkeypatch):
    g=guard(tmp_path,monkeypatch);g.context['qid']='already-started'
    with pytest.raises(m.base.GuardStop):asyncio.run(g.send(None,None,request()))
    assert 'PRE_SEND_IDENTITY' in (tmp_path/'guard-diagnostics.jsonl').read_text()
    g.stopped=False;g.context['qid']='q';m.GRANT.write_bytes(b'changed')
    with pytest.raises(m.base.GuardStop):asyncio.run(g.send(None,None,request()))
    assert 'PRE_SEND_GRANT' in (tmp_path/'guard-diagnostics.jsonl').read_text()

@pytest.mark.parametrize('outcome',['timeout','429'])
def test_unknown_stops_once(tmp_path,monkeypatch,outcome):
    g=guard(tmp_path,monkeypatch)
    async def fake(*a,**kw):
        if outcome=='timeout': raise TimeoutError()
        return httpx.Response(429)
    with pytest.raises(m.base.GuardStop):asyncio.run(g.send(fake,None,request()))
    assert len(g.calls)==1 and g.calls[0]['state']=='UNKNOWN' and g.committed==Decimal(g.calls[0]['reservation_cny'])
    with pytest.raises(m.base.GuardStop):asyncio.run(g.send(fake,None,request()))
    assert len(g.calls)==1

def test_budget_denial_records_reservation_without_send(tmp_path,monkeypatch):
    g=guard(tmp_path,monkeypatch,'0.000001')
    with pytest.raises(m.base.GuardStop,match='BUDGET_BLOCKED'):asyncio.run(g.send(None,None,request()))
    d=json.loads((tmp_path/'guard-diagnostics.jsonl').read_text().splitlines()[-1])
    assert d['code'].startswith('BUDGET_BLOCKED') and Decimal(d['reservation_cny'])>g.limit and not g.calls

def test_unbound_client_block_logged(tmp_path,monkeypatch):
    g=guard(tmp_path,monkeypatch)
    async def attempt():
        async with httpx.AsyncClient() as client: await client.get('https://example.invalid/')
    with g.installed(), pytest.raises(m.base.GuardStop):asyncio.run(attempt())
    assert 'Unbound asynchronous' in (tmp_path/'guard-diagnostics.jsonl').read_text()
    assert not g.calls

@pytest.mark.parametrize('failure',['source','redis','grant_collision'])
def test_setup_failure_or_existing_grant_never_sends(tmp_path,monkeypatch,failure):
    from contextlib import contextmanager
    orig=json.loads((m.r.RUN/'manifest.json').read_bytes()); data=m.r.current.validate(orig); audit=m.inspect(json.loads(SPEC.read_bytes()))
    d=tmp_path/'var/preferences-acceptance';d.mkdir(parents=True)
    grant=d/'m2-current-resume16-once.started'
    if failure=='grant_collision':grant.write_bytes(b'untouched')
    monkeypatch.setattr(m,'ROOT',tmp_path);monkeypatch.setattr(m,'GRANT',grant)
    monkeypatch.setattr(m,'inspect',lambda _:audit);monkeypatch.setattr(m,'no_writers',lambda:None)
    monkeypatch.setattr(m.r.current,'validate',lambda _:data);monkeypatch.setattr(m.base,'private_path',lambda p:Path(p));monkeypatch.setenv('M2_AB_API_KEY','synthetic-only')
    def backup(*a):
        if failure=='source':raise ValueError('corrupt')
    monkeypatch.setattr(m.cont,'backup_reuse',backup)
    @contextmanager
    def redis(_):
        if failure=='redis':raise RuntimeError('redis unavailable')
        yield 'redis://synthetic'
    monkeypatch.setattr(m.base,'isolated_redis',redis);monkeypatch.setattr(m.base,'configure_runtime',lambda *a:None);monkeypatch.setattr(m.cont.safety,'assert_runtime_isolation',lambda *a:None)
    result=m.execute({},d/'m2-current-resume16-synthetic')
    assert result['status']=='FAILED' and result['recorded_trials']==0
    assert grant.read_bytes()==b'untouched' if failure=='grant_collision' else not grant.exists()

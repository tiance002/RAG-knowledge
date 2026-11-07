import asyncio
import importlib.util
import json
from pathlib import Path
from decimal import Decimal
import pytest

ROOT=Path(__file__).resolve().parents[2]
s=importlib.util.spec_from_file_location('m2_resume_test',ROOT/'scripts/m2_current_resume.py')
m=importlib.util.module_from_spec(s); s.loader.exec_module(m)
SPEC=ROOT/'var/preferences-acceptance/m2-current-resume-manifest-v4.json'

def test_read_only_audit_includes_unknown_and_excludes_started():
    a=m.inspect(json.loads(SPEC.read_bytes()))
    assert (a['started_trials'],a['planned_trials'],a['unknown_call'])==(74,22,171)
    assert Decimal(a['unknown_reservation_cny'])==Decimal('0.075968')
    assert Decimal(a['committed_cny'])+Decimal(a['remaining_cny'])==Decimal('2')
    assert len({tuple(x) for x in a['remaining_identities']})==22

def test_changed_ledger_or_lease_digest_blocks():
    for name in ('provider-ledger.jsonl','.active'):
        a=json.loads(SPEC.read_bytes()); k=next(x for x in a['files'] if x.endswith(name)); a['files'][k]='0'*64
        with pytest.raises(ValueError,match='changed'): m.inspect(a)

def test_sender_blocks_changed_grant_and_forbidden_identity(tmp_path,monkeypatch):
    monkeypatch.setattr(m,'ROOT',tmp_path)
    d=tmp_path/'var/preferences-acceptance'; d.mkdir(parents=True)
    grant=d/'m2-current-resume-once.started'; grant.write_bytes(b'once')
    monkeypatch.setattr(m,'inspect',lambda _: {'remaining_cny':'0.4512548'})
    monkeypatch.setattr(m,'no_live_original',lambda:None)
    fresh={'base_url':'https://api.siliconflow.cn/v1','budget_cny':'0.4512548','pricing':{'input_cny_per_million':'1','output_cny_per_million':'1'}}
    guard=m.ResumeTransport(fresh,tmp_path,{}, {('q','chat','A',1)})
    guard.grant_bytes=grant.read_bytes()
    guard.context={'qid':'old','mode':'chat','arm':'A','repetition':1}
    with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(None,None,None))
    guard.stopped=False; guard.context={'qid':'q','mode':'chat','arm':'A','repetition':1}; grant.write_bytes(b'changed')
    with pytest.raises(m.base.GuardStop): asyncio.run(guard.send(None,None,None))


def test_fake_new_unknown_stops_and_keeps_reservation(tmp_path,monkeypatch):
    import httpx
    monkeypatch.setattr(m,'ROOT',tmp_path)
    d=tmp_path/'var/preferences-acceptance'; d.mkdir(parents=True)
    (d/'m2-current-resume-once.started').write_bytes(b'once')
    monkeypatch.setattr(m,'inspect',lambda _: {'remaining_cny':'0.4512548'})
    monkeypatch.setattr(m,'no_live_original',lambda:None)
    fresh={'base_url':'https://api.siliconflow.cn/v1','model':'fixed','max_output_tokens':128,'budget_cny':'0.4512548','pricing':{'input_cny_per_million':'1','output_cny_per_million':'1'}}
    g=m.ResumeTransport(fresh,tmp_path,{}, {('q','chat','A',1)})
    g.grant_bytes=(d/'m2-current-resume-once.started').read_bytes()
    g.context={'qid':'q','mode':'chat','arm':'A','repetition':1}
    req=httpx.Request('POST','https://api.siliconflow.cn/v1/chat/completions',json={'model':'fixed'})
    async def timeout(*args,**kw): raise TimeoutError()
    with pytest.raises(m.base.GuardStop,match='UNKNOWN'): asyncio.run(g.send(timeout,None,req))
    assert g.calls[0]['state']=='UNKNOWN' and g.committed==Decimal(g.calls[0]['reservation_cny'])
    with pytest.raises(m.base.GuardStop): asyncio.run(g.send(timeout,None,req))
    assert len(g.calls)==1

def test_unbound_client_is_blocked_without_network(tmp_path):
    import httpx
    fresh={'base_url':'https://api.siliconflow.cn/v1','budget_cny':'0.4512548','pricing':{'input_cny_per_million':'1','output_cny_per_million':'1'}}
    g=m.base.BudgetTransport(fresh,tmp_path)
    async def call():
        async with httpx.AsyncClient() as client:
            await client.get('https://example.invalid/')
    with g.installed(), pytest.raises(m.base.GuardStop,match='Unbound'):
        asyncio.run(call())
    assert not g.calls


@pytest.mark.parametrize('failure', ['source','redis'])
def test_startup_failure_keeps_one_shot_grant_unused(tmp_path,monkeypatch,failure):
    from contextlib import contextmanager
    spec=json.loads((m.RUN/'manifest.json').read_bytes())
    history,parent,fresh,budget,plan=m.current.validate(spec)
    audit=m.inspect(json.loads(SPEC.read_bytes()))
    monkeypatch.setattr(m,'ROOT',tmp_path)
    d=tmp_path/'var/preferences-acceptance'; d.mkdir(parents=True)
    monkeypatch.setattr(m,'inspect',lambda _:audit)
    monkeypatch.setattr(m,'no_live_original',lambda:None)
    monkeypatch.setattr(m.current,'validate',lambda _: (history,parent,fresh,budget,plan))
    monkeypatch.setattr(m.base,'private_path',lambda p:Path(p))
    monkeypatch.setenv('M2_AB_API_KEY','synthetic-only')
    def backup(*args):
        if failure=='source': raise ValueError('corrupt source')
    monkeypatch.setattr(m.cont,'backup_reuse',backup)
    @contextmanager
    def redis(_):
        raise RuntimeError('synthetic startup failure')
        yield
    monkeypatch.setattr(m.base,'isolated_redis',redis)
    result=m.execute({},d/'m2-current-resume-synthetic')
    assert result['status']=='FAILED' and result['recorded_trials']==0
    assert not (d/'m2-current-resume-once.started').exists()

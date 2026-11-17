"""Synthetic interruption controls through session reconstruction and requests."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from rsik.budgets import Budget
from rsik.handbook.store import Handbook
from rsik.openai_roles import OpenAIRoles
from rsik.query_learning import ExperienceSession
from rsik.redact import LeakScanner,make_visible,project_summary
from rsik.state import canonical
from rsik.tests.test_handbook_and_promotion import entry,evidence_context,delta
from rsik.tests.test_openai_roles import response


def inputs(tmp_path):
    observation=project_summary('REPLAY_SUMMARY',{'questions':20,'verified':1},include_count_evidence=True)
    other=deepcopy(observation['safe_evidence'][0]);other['evidence_ref']='evidence-'+'b'*32
    observation['safe_evidence'].append(other)
    public=make_visible([observation],parent_code='SYNTHETIC selector')
    refs=list(public['evidence_quotes'])
    body=entry();body['support_refs']=refs
    proposal=delta(body);proposal['evidence_refs']=refs
    context=evidence_context(True)
    context['refs']={k:v for k,v in zip(refs,context['refs'].values())}
    context['evidence_mode']='SYNTHETIC_CONTROL'
    for name,value in [('public.json',public),('context.json',context),('response.json',{'status':'OK','result':proposal,'reason':None})]:
        (tmp_path/name).write_bytes(canonical(value))
    return public,{'response_path':tmp_path/'response.json','public_path':tmp_path/'public.json',
                   'context_path':tmp_path/'context.json'}


@pytest.mark.parametrize('point',['intent','partial_events','entry_created','entry_activated',
    'completed_receipt','frontier_updated','before_intent_cleanup'])
def test_reconstruct_recover_then_next_actual_request_at_every_checkpoint(tmp_path,point):
    public,paths=inputs(tmp_path)
    book=Handbook(tmp_path/'book',evidence_mode='SYNTHETIC_CONTROL')
    session=ExperienceSession(tmp_path/'session',book)
    initial=session.initial.read_bytes()
    def crash(current):
        if current==point:raise SystemExit('SYNTHETIC crash at '+point)
    session.checkpoint=crash
    with pytest.raises(SystemExit):
        session.learn_delta('synthetic-evidence',**paths,prefix_events=[{'key':'holdout:synthetic',
            'kind':'HOLDOUT_PASSED','payload':{'candidate_id':'candidate','evidence_mode':'SYNTHETIC_CONTROL'}}])
    before=len(book.log.read())
    with pytest.raises(ValueError,match='RECONCILIATION_REQUIRED'):
        ExperienceSession(tmp_path/'session',book)
    resumed=ExperienceSession(tmp_path/'session',book,recovery=True)
    result=resumed.recover()
    assert result['entries'] and resumed.initial.read_bytes()==initial
    assert not (resumed.directory/'learning-intent.json').exists()
    events=book.log.read()
    assert len(events)>=before
    assert len({e['idempotency_key'] for e in events})==len(events)
    assert sum(e['event_type']=='ENTRY_CREATED' for e in events)==1
    assert sum(e['event_type']=='ENTRY_ACTIVATED' for e in events)==1
    assert book.view()['states'][result['entries'][0]]=='ACTIVE'
    assert resumed.recover()=={'status':'NO_PENDING_TRANSACTION'} and book.log.read()==events
    entries=book.retrieve('dedup','OTHER',{'same_source':True},'SYNTHETIC-compatible')
    public['handbook_entries']=entries;public['handbook_entry_ids']=[e['entry_id'] for e in entries]
    sent=[]
    def transport(body,*_):
        sent.append(body)
        return response({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC next request'})
    roles=OpenAIRoles(Budget(tmp_path/'test-budget.sqlite'),tmp_path/'roles',tmp_path/'unused',
        LeakScanner(),transport=transport,before_request=resumed.verify,strategy_context=resumed.verify)
    assert roles.diagnose(public)['status']=='ABSTAIN'
    assert result['entries'][0] in json.loads(sent[0]['input'][1]['content'])['public']['handbook_entry_ids']
    assert roles.budget.snapshot()['attempts']==1


@pytest.mark.parametrize('corruption',['external_event','context','operation','unknown_callback'])
def test_recovery_blocks_unknown_or_changed_provenance_without_request(tmp_path,corruption):
    public,paths=inputs(tmp_path)
    book=Handbook(tmp_path/'book',evidence_mode='SYNTHETIC_CONTROL')
    session=ExperienceSession(tmp_path/'session',book)
    def crash(point):
        if point=='intent':raise SystemExit('SYNTHETIC')
    session.checkpoint=crash
    if corruption=='unknown_callback':
        def unknown():raise SystemExit('request delivery unknowable')
        with pytest.raises(SystemExit):session.learn('unknown',unknown)
    else:
        with pytest.raises(SystemExit):session.learn_delta('known',**paths)
        if corruption=='external_event':book.log.append('foreign','EXTERNAL_POLICY_INJECTION',{})
        elif corruption=='context':paths['context_path'].write_bytes(b'{}')
        elif corruption=='operation':
            operation=next(session.directory.glob('operation-*.json'));operation.write_bytes(b'{}')
    resumed=ExperienceSession(session.directory,book,recovery=True)
    events=book.log.read();initial=session.initial.read_bytes()
    with pytest.raises(ValueError,match='RECOVERY_BLOCKED'):resumed.recover()
    assert book.log.read()==events and initial==session.initial.read_bytes()
    assert (session.directory/'learning-intent.json').exists()
    assert json.loads((session.directory/'recovery-status.json').read_text())['status']=='RECOVERY_BLOCKED'


def test_round_event_transaction_recovery_is_registered_and_idempotent(tmp_path):
    public,_=inputs(tmp_path)
    public['handbook_entry_ids']=[]
    record={'round_id':public['round_id'],'proposal_instances':{},'candidate_decisions':[],
            'qualified':[],'attempted_replays':[],'adopted_candidate_id':None,'verdict':'NO_ADOPTION'}
    paths=[tmp_path/'round.json',tmp_path/'round-public.json']
    for path,value in zip(paths,[record,public]):path.write_bytes(canonical(value))
    book=Handbook(tmp_path/'book',evidence_mode='SYNTHETIC_CONTROL');session=ExperienceSession(tmp_path/'session',book)
    session.checkpoint=lambda point:(_ for _ in ()).throw(SystemExit('SYNTHETIC')) if point=='completed_receipt' else None
    with pytest.raises(SystemExit):session.learn_round(record_path=paths[0],public_path=paths[1],compatibility_fingerprint='SYNTHETIC-compatible')
    resumed=ExperienceSession(session.directory,book,recovery=True)
    assert resumed.recover() is None
    assert resumed.verify()['sequence']==0

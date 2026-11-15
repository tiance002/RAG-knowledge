"""Synthetic end-to-end control: real archives/readers, explicit non-execution doubles.

These invented fixtures cannot qualify real candidates or measure business gains.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.events import EventLog
from rsik.experiment import executor,load
from rsik.execution_evidence import ExecutionEvidence
from rsik.foundation import check_split,digest
from rsik.handbook.store import Handbook
from rsik.query_aware import QueryRegistry,adapt_legacy
from rsik.query_learning import (ExperienceSession,VerifiedCandidateEvidence,
    curate_verified_candidate,quarantine_candidate_learning)
from rsik.query_verification import verify_sealed
from rsik.query_workflow import run_query_round
from rsik.redact import LeakScanner,make_visible,project_summary
from rsik.role_profiles import DEPENDENCY
from rsik.tests.portable_fixtures import ROOT,write,stress_split,SyntheticCapsule,builder
from rsik.tests.test_handbook_and_promotion import entry,evidence_context,delta
from rsik.tests.test_workflow import FakeRoles
from run_rsi_loop import selector


def split(path,label):
    items,sources=[],{}
    for i in range(20):
        body=f'SYNTHETIC {label} source {i}: required value {i}. '+''.join(digest(f'{label}-{i}-{j}'.encode()) for j in range(14))
        raw=body.encode();sha=digest(raw);quote=f'required value {i}.';start=body.index(quote)
        item={'content':{'qid':f'SYNTHETIC-{label}-{i}','question':f'SYNTHETIC {label} question {i}',
              'evidence':[{'quote':quote,'original_location':{'char_start_0based':start,'char_end_exclusive_0based':start+len(quote)}}]},
              'core_sha256':digest(f'SYNTHETIC-{label}-{i}'.encode()),
              'content_review':{'decision':'PASS','reviewer':'SYNTHETIC_NOT_REAL_REVIEW'}}
        source={'sha256':sha,'file_id':f'SYNTHETIC-{label}-{i}','kb_id':'SYNTHETIC-kb',
                'name':f'SYNTHETIC-{label}-{i}.md','source_path':'SYNTHETIC_NO_REAL_SOURCE'}
        items.append((item,source));sources[sha]=raw
    builder.build_split(items,sources,path,'SYNTHETIC');write(path/'freeze.json',check_split(path,expected_count=20))
    return path


class CaptureDouble(SyntheticCapsule):
    """Does not execute candidate code; deliberately only SYNTHETIC_CONTROL."""
    def setup(self):pass
    def close(self):pass
    def child(self,stage,paths,cwd,**kwargs):
        if Path(cwd).name=='probes':
            result={'status':'PASS','scope':'SYNTHETIC_NON_EXECUTION_DOUBLE'};write(paths[-1],result);return result
        result=super().child(stage,paths,cwd,**kwargs)
        if stage=='deliver' and Path(kwargs['worker']).name=='B_worker.py':
            snapshot=load(paths[0]);seeds=snapshot['seed_ids']
            members=[m for m in result['members'] if m['id'] in seeds or 'required value' in m['content']]
            context='\n'.join(m['content'] for m in members);proof=[];offset=0
            for m in members:
                end=offset+len(m['content']);proof.append({'id':m['id'],'context_start':offset,'context_end':end,'prompt_start':offset,'prompt_end':end});offset=end+1
            result.update(members=members,context=context,messages=[{'content':context}],proof=proof)
            write(paths[-1],result)
        return result


class Proposers(FakeRoles):
    models={};seam_profile='query-autonomous-v1';role_profile=DEPENDENCY
    def propose(self,angle,public,diagnosis):
        result=super().propose('P2_BUDGET',public,diagnosis)
        p=result['result'];p.update(angle=angle,proposal_id='SYNTHETIC-'+angle,
                                  handbook_entry_ids=list(public['handbook_entry_ids']))
        p['worker_patch']['new']={'P1_DEDUP':'    return selected[:]',
            'P2_BUDGET':'    return list(selected)','P3_DEPENDENCY':'    return selected[::1]'}[angle]
        return result


class Curator:
    scanner=LeakScanner()
    def __init__(self):self.calls=[]
    def call(self,role,public):
        self.calls.append((role,deepcopy(public)))
        if role!='curator' or 'completed_candidate' not in public:
            return {'status':'ABSTAIN','result':None,'reason':'SYNTHETIC_NO_COMPLETED_EVIDENCE'}
        c=public['completed_candidate'];refs=c['evidence_refs'][:2]
        proposed={'section':'budget','applicable_format':['OTHER'],
            'conditions':[{'field':'same_source','op':'eq','value':True}],
            'action':c['implemented_action'],'explanation':'SYNTHETIC verified association only',
            'falsification':'Any source-clause loss refutes this limited control.',
            'support_refs':refs,'compatibility_fingerprint':public['compatibility_fingerprint'],'supersedes':[]}
        return {'status':'OK','reason':None,'result':{'schema_version':'rsi-v2.2','op':'ADD','target_entry_id':None,
            'proposed_entry':proposed,'evidence_refs':refs,'reason':'SYNTHETIC completed source-clause control'}}


@pytest.fixture
def completed(tmp_path):
    train=split(tmp_path/'train','TRAIN');held=split(tmp_path/'held','HELD')
    train_fixtures=[train,stress_split(train,tmp_path/'train-query',query=True),
        stress_split(train,tmp_path/'train-next'),stress_split(train,tmp_path/'train-prev',direction='PREVIOUS_ELSE_NEXT')]
    sealed=[held,stress_split(held,tmp_path/'held-next'),stress_split(held,tmp_path/'held-prev',direction='PREVIOUS_ELSE_NEXT')]
    registry=QueryRegistry(tmp_path/'registry')
    pid=registry.put(adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),b'{"selection":"all"}',
                     parent_id='SYNTHETIC-origin',origin='SYNTHETIC_CONTROL',evidence={})
    parent=registry.get(pid);handbook=Handbook(tmp_path/'handbook',evidence_mode='SYNTHETIC_CONTROL')
    seed=handbook.commit_delta(delta(entry()),evidence_context(True),idempotency_key='SYNTHETIC-seed')
    session=ExperienceSession(tmp_path/'experience-session',handbook)
    public=make_visible([project_summary('REPLAY_SUMMARY',{'questions':20},include_count_evidence=True)],
                        parent_code=selector(parent['worker'].decode())[1])
    curator=Curator()
    def train_operation():
        return run_query_round(evidence=ExecutionEvidence.synthetic_control(),public=public,roles=Proposers(),advisory_roles=curator,parent=parent,
            fixtures=train_fixtures,registry=registry,directory=tmp_path/'round',
            capsule_factory=lambda _:CaptureDouble(),scanner=LeakScanner(),handbook=handbook,
            compatibility_fingerprint='SYNTHETIC-compatible',features={'same_source':True},role_profile=DEPENDENCY,experience_session=session)
    result=train_operation()
    assert result['adopted_candidate_id'] is not None
    training=Path(result['selected_training_directory']);cid=result['adopted_candidate_id']
    verification=tmp_path/'verification'
    sealed_result=verify_sealed(registry=registry.directory,training=training,train_fixtures=train_fixtures,
        sealed_fixtures=sealed,used_holdout=train,candidate_id=cid,trusted_id=pid,
        capsule=CaptureDouble(),output=verification,handbook=handbook,advisory_roles=curator,
        compatibility_fingerprint='SYNTHETIC-compatible',experience_session=session)
    return dict(root=tmp_path,registry=registry,training=training,train_fixtures=train_fixtures,
        sealed=sealed,verification=verification,handbook=handbook,session=session,curator=curator,
        result=result,sealed_result=sealed_result,cid=cid,seed=seed)


def bridge(c,**kwargs):
    args=dict(registry=c['registry'].directory,training=c['training'],
        train_fixtures=c['train_fixtures'],verification=c['verification'],handbook=c['handbook'],
        roles=c['curator'],compatibility_fingerprint='SYNTHETIC-compatible',
        evidence=ExecutionEvidence.synthetic_control(),session=c['session'])
    args.update(kwargs)
    return curate_verified_candidate(**args)


def test_complete_control_loop_recomputes_archives_projects_and_retrieves(completed):
    c=completed
    assert c['sealed_result']['verdict']=='CONTROL_VERIFIED' and c['sealed_result']['real_eligible'] is False
    result=bridge(c);assert result['association_only'] and not result['real_eligible']
    assert len(result['entries'])==1;key=result['entries'][0]
    view=c['handbook'].view();assert view['states'][key]=='ACTIVE'
    assert view['counts'][c['seed']]['helpful']==1
    assert sum(e['event_type']=='EXPERIENCE_USED' for e in c['handbook'].log.read())==3
    entries=c['handbook'].retrieve('budget','OTHER',{'same_source':True},'SYNTHETIC-compatible')
    assert [e['entry_id'] for e in entries]==[key]
    assert not c['handbook'].retrieve('budget','OTHER',{'same_source':True},'WRONG-profile')
    before=c['handbook'].log.read();calls=len(c['curator'].calls)
    assert bridge(c)==result and c['handbook'].log.read()==before and len(c['curator'].calls)==calls
    # A cached receipt is bound to one handbook location and compatibility scope.
    other=Handbook(c['root']/'other-handbook',evidence_mode='SYNTHETIC_CONTROL')
    for changed in ({'handbook':other,'session':None},
                    {'compatibility_fingerprint':'SYNTHETIC-other-compatible'}):
        with pytest.raises(ValueError,match='HANDBOOK_BINDING_CHANGED'):
            bridge(c,**changed)
        assert c['handbook'].log.read()==before and len(c['curator'].calls)==calls
        assert other.log.read()==[]
    # Cached success must also revalidate committed effects, not just the model
    # response digest. Restore these owned control artifacts for later assertions.
    evidence_event=next(e for e in before if e['event_type']=='ENTRY_EVIDENCE_BOUND' and e['payload']['entry_id']==key)
    evidence_file=c['handbook'].directory/'evidence'/evidence_event['payload']['evidence_bundle_sha256']
    for missing in (c['handbook'].directory/'entries'/key,evidence_file):
        original=missing.read_bytes();missing.unlink()
        try:
            with pytest.raises((ValueError,RuntimeError,OSError)):
                bridge(c)
            assert c['handbook'].log.read()==before and len(c['curator'].calls)==calls
        finally:missing.write_bytes(original)
    assert bridge(c)==result

    projected=next(p for role,p in c['curator'].calls if role=='curator' and 'completed_candidate' in p)
    encoded=json.dumps(projected)
    for item in load(c['sealed'][0]/'cases.json'):
        assert item['case']['question'] not in encoded
    for p in load(c['sealed'][0]/'provenance.restricted.json').values():
        for fact in p['facts']:assert fact['text'] not in encoded
    assert 'source_group' not in projected['completed_candidate']
    assert projected['completed_candidate']['quality_status']=='CONTROL_VERIFIED'
    assert projected['completed_candidate']['evidence_mode']=='SYNTHETIC_CONTROL'
    assert projected['completed_candidate']['real_eligible'] is False
    # The next actual coordinator request receives the learned compatible rule.
    seen=[]
    class NextDiagnosis:
        def diagnose(self,value):
            seen.append(deepcopy(value));return {'status':'ABSTAIN','result':None,'reason':'SYNTHETIC_RETRIEVAL_ONLY'}
    parent=c['registry'].get(c['cid'])
    next_public=make_visible([project_summary('REPLAY_SUMMARY',{'questions':20},include_count_evidence=True)],
        parent_code=selector(parent['worker'].decode())[1])
    run_query_round(evidence=ExecutionEvidence.synthetic_control(),public=next_public,roles=NextDiagnosis(),advisory_roles=c['curator'],parent=parent,
        fixtures=c['train_fixtures'],registry=c['registry'],directory=c['root']/'next-round',
        capsule_factory=lambda _:pytest.fail('ABSTAIN must not create replay'),scanner=LeakScanner(),
        handbook=c['handbook'],compatibility_fingerprint='SYNTHETIC-compatible',features={'same_source':True},
        role_profile=DEPENDENCY,experience_session=c['session'])
    assert key in seen[0]['handbook_entry_ids']
    assert next(e for e in seen[0]['handbook_entries'] if e['entry_id']==key)['action']['name']=='REUSE_VERIFIED_SELECTOR'
    assert c['session'].verify()['sequence']==len(c['handbook'].log.read())


@pytest.mark.parametrize('fault',['tamper','missing','real-mode'])
def test_bridge_refuses_receipt_tamper_missing_and_control_as_real(completed,fault):
    c=completed;receipt=c['verification']/'receipt.restricted.json'
    before=c['handbook'].log.read();calls=len(c['curator'].calls)
    if fault=='tamper':
        value=load(receipt);value['statistical_denominator']=19;write(receipt,value)
    elif fault=='missing':receipt.unlink()
    with pytest.raises((ValueError,FileNotFoundError)):
        if fault=='real-mode':
            VerifiedCandidateEvidence.from_archives(registry=c['registry'].directory,training=c['training'],
                train_fixtures=c['train_fixtures'],verification=c['verification'],evidence=None)
        else:bridge(c)
    assert c['handbook'].log.read()==before and len(c['curator'].calls)==calls


def test_candidate_quarantine_excludes_association_and_isolates_learned_rule(completed):
    c=completed;result=bridge(c);key=result['entries'][0]
    receipt=c['verification']/'receipt.restricted.json'
    c['session'].learn('synthetic-quarantine',lambda:c['registry'].quarantine(c['cid'],receipt,'SYNTHETIC_POST_VERIFICATION_REGRESSION'))
    # QueryRegistry quarantine dispatches through its immutable learned-book links.
    assert c['handbook'].view()['states'][key]=='QUARANTINED'
    assert not c['handbook'].retrieve('budget','OTHER',{'same_source':True},'SYNTHETIC-compatible')
    assert c['handbook'].view()['counts'][c['seed']]['harmful']==0
    with pytest.raises(ValueError,match='QUARANTINED'):bridge(c)


def test_experience_session_rejects_external_events_and_preserves_initial_snapshot(tmp_path):
    handbook=Handbook(tmp_path/'handbook',evidence_mode='SYNTHETIC_CONTROL')
    session=ExperienceSession(tmp_path/'session',handbook);initial=session.initial.read_bytes()
    handbook.log.append('SYNTHETIC-external','EXTERNAL_POLICY_INJECTION',{'evidence_mode':'SYNTHETIC_CONTROL'})
    with pytest.raises(ValueError,match='EXTERNAL_EXPERIENCE_CHANGE'):session.verify()
    with pytest.raises(ValueError,match='EXTERNAL_EXPERIENCE_CHANGE'):session.learn('invalid',lambda:None)
    assert session.initial.read_bytes()==initial


def test_delta_retry_completes_interrupted_activation_once(tmp_path,monkeypatch):
    handbook=Handbook(tmp_path/'handbook',evidence_mode='SYNTHETIC_CONTROL')
    proposal=delta(entry());context=evidence_context(True)
    activate=handbook.activate_if_qualified
    def interrupted(*args,**kwargs):
        raise SystemExit('SYNTHETIC interruption after ENTRY_CREATED before activation')
    monkeypatch.setattr(handbook,'activate_if_qualified',interrupted)
    with pytest.raises(SystemExit):handbook.commit_delta(proposal,context,idempotency_key='SYNTHETIC-retry')
    created=[e for e in handbook.log.read() if e['event_type']=='ENTRY_CREATED']
    assert len(created)==1
    key=created[0]['payload']['entry_id'];assert handbook.view()['states'][key]=='DRAFT'
    monkeypatch.setattr(handbook,'activate_if_qualified',activate)
    assert handbook.commit_delta(proposal,context,idempotency_key='SYNTHETIC-retry')==key
    assert handbook.view()['states'][key]=='ACTIVE'
    events=handbook.log.read()
    assert sum(e['event_type']=='ENTRY_CREATED' for e in events)==1
    assert sum(e['event_type']=='ENTRY_ACTIVATED' for e in events)==1
    assert sum(e['event_type']=='ENTRY_EVIDENCE_BOUND' for e in events)==1
    assert handbook.commit_delta(proposal,context,idempotency_key='SYNTHETIC-retry')==key
    assert handbook.log.read()==events

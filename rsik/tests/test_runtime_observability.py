"""Synthetic controls over actual coordinator, retrieval and selection paths."""
from copy import deepcopy
import json

from rsik.events import canonical
from rsik.observability import Observer, observation_for
from rsik.handbook.store import Handbook
from rsik.lineage import ParentPool, LineagePolicy
from rsik.workflow import execute_round
from rsik.tests.test_workflow import public_context, FakeRoles, callbacks
from rsik.tests.test_handbook_and_promotion import entry, delta, evidence_context


def test_duplicate_patch_instances_retained_and_inputs_unchanged(tmp_path):
    public = public_context()
    before = deepcopy(public)
    observer = Observer(tmp_path/'observations.sqlite')
    roles = FakeRoles(same_patch=True)
    review, replay, reviewed, replayed = callbacks()
    result = execute_round(public, roles, review, replay, observer=observer)
    events = observer.log.read()
    generated = [e['payload'] for e in events if e['event_type']=='PROPOSAL_GENERATED']
    assert len(generated)==3 and len({p['proposal_instance_id'] for p in generated})==3
    assert len({p['patch_hash'] for p in generated})==1
    assert reviewed==replayed==list(result['attempted_replays'])
    assert public==before and all(v==before for v in roles.inputs.values())
    assert all('family_id' not in e['payload'] for e in events)
    assert len([e for e in events if e['event_type']=='PROPOSAL_REPLAY_ENTERED'])==3


def test_raw_retrieval_logs_filters_without_mutating_frontier(tmp_path):
    store = Handbook(tmp_path/'handbook')
    key = store.commit_delta(delta(entry()), evidence_context(True), idempotency_key='first')
    before = canonical(store.log.read())
    observer = Observer(tmp_path/'run'/'observations.sqlite', {'round_id':'round'})
    rows = store.retrieve('dedup','OTHER',{'same_source':True},'SYNTHETIC-compatible', observer=observer)
    assert [e['entry_id'] for e in rows]==[key]
    assert not store.retrieve('dedup','OTHER',{'same_source':True},'WRONG', observer=observer)
    assert canonical(store.log.read())==before
    events = observer.log.read()
    assert len([e for e in events if e['event_type']=='MEMORY_RETRIEVAL_COMPLETED'])==2
    assert any(e['event_type']=='MEMORY_COMPATIBILITY_REJECTED' for e in events)
    assert not any(e['event_type'] in {'MEMORY_INJECTED','MEMORY_USED'} for e in events)


def test_parent_admission_exclusion_and_selection_are_distinct(tmp_path):
    policy=LineagePolicy(tmp_path/'policy')
    pool=ParentPool(tmp_path/'pool', evidence_mode='SYNTHETIC_CONTROL')
    receipt=tmp_path/'receipt.json'
    receipt.write_text(json.dumps({'verdict':'VALIDATED'}))
    pool.admit('candidate','lineage',receipt,kind='EXPLORATION',chars=100,replay_seconds=1)
    before=canonical(pool.log.read())
    assert pool.select(policy,round_id='r') is None
    observer=observation_for(pool)
    assert not any(e['event_type']=='PARENT_SELECTED' for e in observer.log.read())
    assert pool.select(policy,round_id='r',exploration=True)['candidate_id']=='candidate'
    selected=[e['payload'] for e in observer.log.read() if e['event_type']=='PARENT_SELECTED']
    assert len(selected)==1 and selected[0]['round_id']=='r' and selected[0]['exploration'] is True
    assert canonical(pool.log.read())==before


def test_static_rejection_has_no_replay_and_references_precede_review(tmp_path):
    from rsik.errors import StageFailure
    observer=Observer(tmp_path/'observations.sqlite')
    public=public_context()
    public['handbook_entry_ids']=['memory']
    class RefRoles(FakeRoles):
        def propose(self, *args):
            value=super().propose(*args)
            value['result']['handbook_entry_ids']=['memory']
            return value
    def review(proposal):
        assert any(e['event_type']=='PROPOSAL_REFERENCED' for e in observer.log.read())
        raise StageFailure('SEAM_ERROR','PATCH_INVALID')
    result=execute_round(public,RefRoles(),review,lambda *_: (_ for _ in ()).throw(AssertionError()),observer=observer)
    events=observer.log.read()
    assert len([e for e in events if e['event_type']=='PROPOSAL_STATIC_REJECTED'])==3
    assert not any(e['event_type']=='PROPOSAL_REPLAY_ENTERED' for e in events)
    assert not result['attempted_replays']

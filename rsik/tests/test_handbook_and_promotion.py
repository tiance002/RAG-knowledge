"""Synthetic event causality/conditions and real isolated-file CAS/rollback."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rsik.contracts import ContractError
from rsik.handbook.contracts import conflicting, conditions_match, validate_entry
from rsik.handbook.projection import project
from rsik.handbook.store import Handbook
from rsik.promotion import PromotionSandbox, production_promote


def entry(action='PRESERVE_BOTH'):
    return {'section': 'dedup', 'applicable_format': ['OTHER'],
        'conditions': [{'field': 'same_source', 'op': 'eq', 'value': True}],
        'action': {'name': action, 'target': 'MEMBER_PAIR', 'params': {}},
        'explanation': 'SYNTHETIC source-bound rule', 'falsification': 'Any independent evidence lost refutes it.',
        'support_refs': ['evidence-one', 'evidence-two'], 'compatibility_fingerprint': 'SYNTHETIC-compatible', 'supersedes': []}


def evidence_context(verified=False):
    return {'compatibility_fingerprint': 'SYNTHETIC-compatible', 'allowed_formats': ['OTHER'],
        'verified_candidates': {'candidate': 'h' * 64} if verified else {},
        'refs': {ref: {'source_group': group, 'candidate_id': 'candidate', 'parent_id': 'parent',
                      'observation_id': 'observation', 'run_id': 'run', 'result_hash': 'r' * 64,
                      'features': {'format': 'OTHER', 'same_source': True}}
                 for ref, group in [('evidence-one', 'group-one'), ('evidence-two', 'group-two')]}}


def delta(body):
    return {'schema_version': 'rsi-v2.2', 'op': 'ADD', 'target_entry_id': None, 'proposed_entry': body,
            'evidence_refs': ['evidence-one', 'evidence-two'], 'reason': 'SYNTHETIC completed experiment'}


def test_conflict_uses_real_condition_intersection_not_keywords():
    left, right = entry(), entry('DROP_DUPLICATE')
    assert conflicting(left, right)
    right['conditions'] = [{'field': 'same_source', 'op': 'eq', 'value': False}]
    assert not conflicting(left, right)
    assert not conditions_match(left, {'format': 'OTHER', 'same_source': 'UNKNOWN'})
    assert not conditions_match(left, {'format': 'OTHER'})
    assert conditions_match(left, {'format': 'OTHER', 'same_source': True})


def test_unverified_entries_stay_draft_and_conflicts_quarantine_both(tmp_path):
    store = Handbook(tmp_path)
    first = store.commit_delta(delta(entry()), evidence_context(), idempotency_key='first')
    assert store.view()['states'][first] == 'DRAFT'
    assert not store.retrieve('dedup', 'OTHER', {'same_source': True}, 'SYNTHETIC-compatible')
    second = store.commit_delta(delta(entry('DROP_DUPLICATE')), evidence_context(True), idempotency_key='second')
    assert store.view()['states'] == {first: 'QUARANTINED', second: 'QUARANTINED'}
    assert not store.retrieve('dedup', 'OTHER', {'same_source': True}, 'SYNTHETIC-compatible')


def test_activation_requires_two_groups_verified_evidence_compatibility_and_known_features(tmp_path):
    store = Handbook(tmp_path)
    key = store.commit_delta(delta(entry()), evidence_context(), idempotency_key='first')
    assert not store.activate_if_qualified(key, evidence_context())
    assert store.activate_if_qualified(key, evidence_context(True))
    result = store.retrieve('dedup', 'OTHER', {'same_source': True}, 'SYNTHETIC-compatible')
    assert len(result) == 1 and result[0]['entry_id'] == key and 'support_refs' not in result[0]
    assert not store.retrieve('dedup', 'OTHER', {'same_source': True}, 'other-fixture')
    assert not store.retrieve('dedup', 'OTHER', {'same_source': 'UNKNOWN'}, 'SYNTHETIC-compatible')
    assert store.commit_delta(delta(entry()), evidence_context(), idempotency_key='first') == key


def test_curator_cannot_write_counts_states_or_deprecate_by_itself(tmp_path):
    store = Handbook(tmp_path)
    bad = entry();bad['helpful'] = 100
    with pytest.raises(ContractError): store.commit_delta(delta(bad), evidence_context(), idempotency_key='bad')
    key = store.commit_delta(delta(entry()), evidence_context(), idempotency_key='good')
    request = {'schema_version': 'rsi-v2.2', 'op': 'DEPRECATE', 'target_entry_id': key, 'proposed_entry': None,
               'evidence_refs': ['evidence-one'], 'reason': 'SYNTHETIC suggestion only'}
    store.commit_delta(request, evidence_context(), idempotency_key='deprecate')
    assert store.view()['states'][key] == 'DRAFT'


def test_helpful_pending_harmful_transition_counts_one_equivalent_episode():
    events = []
    def add(kind, **payload): events.append({'event_type': kind, 'payload': payload})
    add('ENTRY_CREATED', entry_id='entry')
    for index in range(3):
        add('PROPOSAL_REFERENCED', round_id='round', equivalence_hash='same-worker', entry_id='entry',
            candidate_id='candidate', proposal_id='proposal-' + str(index))
    add('CANDIDATE_ADOPTED', candidate_id='candidate')
    add('HOLDOUT_PASSED', candidate_id='candidate')
    assert project(events)['counts']['entry']['helpful'] == 1
    add('PROMOTED', candidate_id='candidate')
    assert project(events)['counts']['entry'] == {'pending': 1, 'helpful': 0, 'miss': 0, 'harmful': 0, 'excluded': 0}
    add('ROLLED_BACK', candidate_id='candidate', attribution='RELATED')
    assert project(events)['counts']['entry'] == {'pending': 0, 'helpful': 0, 'miss': 0, 'harmful': 1, 'excluded': 0}


def test_unrelated_or_unknown_rollback_and_infrastructure_are_excluded_not_harmful():
    events = [{'event_type': 'ENTRY_CREATED', 'payload': {'entry_id': 'e'}},
              {'event_type': 'PROPOSAL_REFERENCED', 'payload': {'entry_id': 'e', 'round_id': 'r',
               'equivalence_hash': 'w', 'candidate_id': 'c', 'proposal_id': 'p'}},
              {'event_type': 'CANDIDATE_ADOPTED', 'payload': {'candidate_id': 'c'}},
              {'event_type': 'PROMOTED', 'payload': {'candidate_id': 'c'}},
              {'event_type': 'ROLLED_BACK', 'payload': {'candidate_id': 'c', 'attribution': 'UNKNOWN'}}]
    assert project(events)['counts']['e']['excluded'] == 1
    assert project(events)['counts']['e']['harmful'] == 0
    events[-2:] = [{'event_type': 'CANDIDATE_INCONCLUSIVE', 'payload': {'candidate_id': 'c'}}]
    assert project(events)['counts']['e']['excluded'] == 1


def test_handbook_view_rebuild_is_identical_and_unreferenced_matches_get_no_credit():
    events = [{'event_type': 'ENTRY_CREATED', 'payload': {'entry_id': 'e'}},
              {'event_type': 'HANDBOOK_MATCHED', 'payload': {'entry_id': 'e', 'candidate_id': 'c'}}]
    assert project(events) == project(deepcopy(events))
    assert all(v == 0 for v in project(events)['counts']['e'].values())


def test_isolated_approval_switch_and_explicit_rollback_preserve_artifacts(tmp_path):
    sandbox = PromotionSandbox(tmp_path / 'sandbox', allowed_root=tmp_path)
    old = sandbox.initialize('old', b'SYNTHETIC original worker')
    new = b'SYNTHETIC candidate worker';new_hash = hashlib.sha256(new).hexdigest()
    approval = sandbox.approval('candidate', new_hash, 'old')
    result = sandbox.promote(approval, new, quality='VERIFIED', rollback_hash=old['artifact_hash'])
    assert result['mode'] == 'SIMULATION_ONLY' and result['production_changed'] is False
    rollback = sandbox.approval('old', old['artifact_hash'], 'candidate', operation='ROLLBACK')
    result = sandbox.rollback(rollback)
    assert result['current'] == old and result['status'] == 'ROLLED_BACK'
    assert (sandbox.directory / 'artifacts' / new_hash).read_bytes() == new


@pytest.mark.parametrize('fault', ['version', 'artifact', 'expiry', 'missing_rollback', 'not_verified', 'signature'])
def test_promotion_preconditions_fail_without_switching(fault, tmp_path):
    now = [100.0]
    sandbox = PromotionSandbox(tmp_path / 'sandbox', allowed_root=tmp_path, clock=lambda: now[0])
    old = sandbox.initialize('old', b'SYNTHETIC old')
    new = b'SYNTHETIC new';digest = hashlib.sha256(new).hexdigest()
    approval = sandbox.approval('new', digest, 'old', ttl=1)
    quality = 'VERIFIED'
    if fault == 'version':
        value = sandbox.current();value['version_id'] = 'concurrent-change'
        (sandbox.directory / 'current.json').write_text(json.dumps(value))
    elif fault == 'artifact': new = b'changed after approval'
    elif fault == 'expiry': now[0] += 2
    elif fault == 'missing_rollback': (sandbox.directory / 'artifacts' / old['artifact_hash']).unlink()
    elif fault == 'not_verified': quality = 'VALIDATED'
    elif fault == 'signature': approval['signature'] = 'fake'
    before = (sandbox.directory / 'current.json').read_bytes()
    with pytest.raises(ValueError): sandbox.promote(approval, new, quality=quality, rollback_hash=old['artifact_hash'])
    assert (sandbox.directory / 'current.json').read_bytes() == before
    assert not any(e['event_type'] == 'PROMOTED' for e in sandbox.events.read())


def test_no_production_adapter_or_self_declared_human_approval_is_accepted():
    with pytest.raises(PermissionError, match='PRODUCTION_ADAPTER_UNAVAILABLE'):
        production_promote({'human': 'I approve'}, 'candidate')


def test_switch_interruption_reconciles_actual_pointer_without_repeating_write(tmp_path,monkeypatch):
    sandbox=PromotionSandbox(tmp_path/'sandbox',allowed_root=tmp_path)
    old=sandbox.initialize('old',b'SYNTHETIC old')
    artifact=b'SYNTHETIC new';approval=sandbox.approval('new',hashlib.sha256(artifact).hexdigest(),'old')
    append=sandbox.events.append
    def interrupted(key,kind,payload,**kwargs):
        if kind=='PROMOTED':raise SystemExit('SYNTHETIC crash after atomic pointer write')
        return append(key,kind,payload,**kwargs)
    monkeypatch.setattr(sandbox.events,'append',interrupted)
    with pytest.raises(SystemExit):sandbox.promote(approval,artifact,quality='VERIFIED',rollback_hash=old['artifact_hash'])
    assert sandbox.current()['version_id']=='new'
    monkeypatch.setattr(sandbox.events,'append',append)
    import rsik.promotion as module
    original=module.atomic_write;writes=[]
    def counted(path,*args,**kwargs):
        writes.append(Path(path).name)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(module,'atomic_write',counted)
    assert sandbox.reconcile_switch(approval['signature'])['status']=='PROMOTED'
    assert sandbox.reconcile_switch(approval['signature'])['status']=='PROMOTED'
    assert 'current.json' not in writes
    assert sum(e['event_type']=='PROMOTED' for e in sandbox.events.read())==1


def test_simulated_switches_cannot_count_as_real_harmful():
    events=[{'event_type':'ENTRY_CREATED','payload':{'entry_id':'e'}},
        {'event_type':'PROPOSAL_REFERENCED','payload':{'entry_id':'e','round_id':'r','equivalence_hash':'w','candidate_id':'c','proposal_id':'p'}},
        {'event_type':'CANDIDATE_ADOPTED','payload':{'candidate_id':'c'}},
        {'event_type':'PROMOTED','payload':{'candidate_id':'c','mode':'SIMULATION_ONLY'}},
        {'event_type':'ROLLED_BACK','payload':{'candidate_id':'c','attribution':'RELATED','mode':'SIMULATION_ONLY'}}]
    assert project(events)['counts']['e']['harmful']==0

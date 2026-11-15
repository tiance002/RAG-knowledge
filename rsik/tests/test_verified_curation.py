from copy import deepcopy
import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.contracts import ContractError,validate_completed_candidate
from rsik.handbook.contracts import validate_entry
from rsik.openai_roles import OpenAIRoles
from rsik.budgets import Budget
from rsik.redact import LeakScanner

from portable_fixtures import write
from test_handbook_and_promotion import entry,evidence_context,delta
from rsik.redact import make_visible,project_summary
import hashlib


def proposal():
    body=entry()
    body['section']='budget'
    body['action']={'name':'FILTER_SHORT_NONSEED_TAILS','target':'NONSEED_MEMBERS',
        'params':{'reference':'LONGEST_SEED_CONTENT_LENGTH','numerator':3,'denominator':4}}
    return body


def visible():
    public=make_visible([project_summary('REPLAY_SUMMARY',{'questions':20},include_count_evidence=True)],
        parent_code='SYNTHETIC verified selector')
    public['compatibility_fingerprint']='SYNTHETIC-compatible'
    public['completed_candidate']={'candidate_alias':'candidate-'+'a'*32,
        'code_sha256':hashlib.sha256(public['parent_code'].encode()).hexdigest(),'quality_status':'VERIFIED',
        'evidence_refs':list(public['evidence_quotes']),'implemented_action':proposal()['action'],
        'chars_before':100,'chars_after':90,'robustness_passed':True,'scope':'FROZEN_SOURCE_CLAUSE_PRESERVATION_ONLY'}
    return public


def test_verified_code_binding_accepts_readonly_role_and_rejects_changed_code():
    public=visible();validate_completed_candidate(public['completed_candidate'],public,'curator')
    public['parent_code']+='\n# changed after verification'
    with pytest.raises(ContractError,match='UNBOUND_COMPLETED_CANDIDATE'):
        validate_completed_candidate(public['completed_candidate'],public,'curator')


def test_completed_verification_summary_cannot_be_sent_to_a_proposer(tmp_path):
    roles=OpenAIRoles(Budget(tmp_path/'budget.sqlite'),tmp_path/'roles',tmp_path/'unused.env',
                      LeakScanner(),transport=lambda *_:pytest.fail('must not send'))
    with pytest.raises(ContractError,match='UNAUTHORIZED_PROJECTION_FIELD'):
        roles.call('P2_BUDGET',visible())
    assert roles.budget.snapshot()['attempts']==0


@pytest.mark.parametrize('ratio',[(True,4),(3,0),(4,3),(1,9)])
def test_handbook_ratio_contract_rejects_unbounded_or_invalid_actions(ratio):
    entry=deepcopy(proposal())
    entry['action']['params']['numerator'],entry['action']['params']['denominator']=ratio
    with pytest.raises(ContractError,match='INVALID_HANDBOOK_ACTION'):validate_entry(entry)


def test_synthetic_quarantined_parent_cannot_retrieve_old_active_rule(tmp_path,synthetic_split,monkeypatch):
    from rsik.experiment import Experiment,registry_api
    from rsik.lineage import ParentPool,LineagePolicy
    from rsik.handbook.store import Handbook
    root=tmp_path/'registry';receipt=tmp_path/'receipt.json';write(receipt,{'verdict':'VERIFIED','scope':'SYNTHETIC'})
    objects={name:registry_api.put(root,raw) for name,raw in {
        'worker':b'SYNTHETIC worker bytes', 'config':b'{}','template':b'{}',
        'cases':(synthetic_split/'cases.json').read_bytes(),
        'evaluation':(synthetic_split/'evaluation.json').read_bytes()}.items()}
    parent=registry_api.record(root,{'type':'rsi-candidate-v1',**objects,'parent':None,'origin':'SYNTHETIC_TEST'})
    pool=ParentPool(root/'policy',evidence_mode='SYNTHETIC_CONTROL');pool.admit(parent,'lineage',receipt,kind='VERIFIED',chars=100,replay_seconds=1)
    book=Handbook(root/'handbook');key=book.commit_delta(delta(entry()),evidence_context(True),idempotency_key='SYNTHETIC')
    assert book.view()['states'][key]=='ACTIVE'
    # Quarantine events must revoke both parent eligibility and active handbook retrieval.
    pool.quarantine(parent,receipt,'SYNTHETIC_FACT_LOSS')
    book.log.append('synthetic-entry-invalidated','ENTRY_QUARANTINED',{'entry_id':key,'reason':'SYNTHETIC_FACT_LOSS'})
    # This one test explicitly wires the synthetic pool; real startup rejects its scope.
    monkeypatch.setattr('rsik.experiment.ParentPool',lambda directory:ParentPool(directory,evidence_mode='SYNTHETIC_CONTROL'))
    experiment=Experiment(registry=root,fixture=synthetic_split,parent_id=parent,
        source_run=tmp_path/'NEVER_OPENED',observation_id='SYNTHETIC',directory=tmp_path/'projection',
        business_root=tmp_path/'UNUSED',host='SYNTHETIC.invalid',identity=tmp_path/'unused',round_id='round-'+'a'*32)
    with pytest.raises(ValueError,match='PARENT_QUARANTINED'):experiment.public_context()
    assert book.view()['states'][key]=='QUARANTINED'
    assert book.retrieve('dedup','OTHER',{'same_source':True},'SYNTHETIC-compatible')==[]
    assert not experiment.capsule.ready and experiment.capsule.transport is None

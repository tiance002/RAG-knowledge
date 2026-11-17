"""Recorded-capture adapters and fake roles, explicitly not a sealed evaluation."""
import json
from pathlib import Path
import sys
import time

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.contracts import ContractError, validate_readonly
from rsik.holdout import external_pair, verify_training_adoption
from rsik.postround import run_advisory
from rsik.redact import LeakScanner, make_visible, project_summary
from rsik.state import canonical

from portable_fixtures import write, SyntheticCapsule


def test_unadopted_candidate_rejected_before_sealed_fixture_access(tmp_path):
    training=tmp_path/'training';write(training/'round.json',{'adopted_candidate_id':None})
    with pytest.raises(ValueError,match='NO_TRAINING_ADOPTION'):
        verify_training_adoption(training,'NEVER_OPENED','NEVER_OPENED','not-adopted')


def test_external_identity_uses_frozen_assessor_with_synthetic_captures(tmp_path,synthetic_replay):
    from rsik.holdout import replay_artifacts
    result=replay_artifacts(SyntheticCapsule(),synthetic_replay['fixture'],synthetic_replay['artifacts'],
        tmp_path/'external',deadline=time.monotonic()+300,schema='rsi-external-holdout/v2.2',
        scope='SYNTHETIC_CAPTURE_DOUBLE_NOT_SEALED_EVALUATION')
    assert result['verdict']=='REJECTED' and result['reason_codes']==['NO_GAIN']
    manifest=json.loads((tmp_path/'external/manifest.json').read_text())
    assert manifest['schema']=='rsi-external-holdout/v2.2' and 'r1' not in manifest
    assert len(result['per_question'])==20


def test_postround_excludes_raw_private_fields_and_cannot_mutate_decision(tmp_path):
    source=tmp_path/'round';source.mkdir()
    result={'execution_state':'COMPLETED','proposal_instances':{},'qualified':[],
        'agent_call_results':{'P1_DEDUP':'ABSTAIN'},'adopted_candidate_id':None,
        'candidate_decisions':[], 'private_field':'PRIVATE_SOURCE_SENTINEL_665544'}
    data=canonical(result);(source/'round.json').write_bytes(data)
    class FakeRoles:
        scanner=LeakScanner(['PRIVATE_SOURCE_SENTINEL_665544'])
        def call(self,role,public):
            assert 'PRIVATE_SOURCE' not in json.dumps(public)
            return {'status':'ABSTAIN','result':None,'reason':'SYNTHETIC no candidate evidence'}
    summary=run_advisory(FakeRoles(),[source],tmp_path/'advisory')
    assert summary['committed_entries']==[] and not summary['judgments_modified']
    assert (source/'round.json').read_bytes()==data


def test_meta_cannot_smuggle_dataset_mutation_or_invent_evidence():
    visible=make_visible([project_summary('REPLAY_SUMMARY',{'rounds':1})],parent_code='')
    body={'schema_version':'rsi-v2.2','findings':[{'category':'UNKNOWN_ATTRIBUTION','certainty':'UNKNOWN',
        'observation':'No admissible attribution available.','evidence_refs':['invented']}],'limitations':[]}
    with pytest.raises(ContractError,match='HALLUCINATED_QUOTE'):validate_readonly(body,visible,'meta')
    body['findings']=[];body['replace_fixture']={}
    with pytest.raises(ContractError):validate_readonly(body,visible,'meta')

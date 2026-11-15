"""Synthetic capture-reader controls; no backend, models or real receipts."""
import hashlib
import json
from pathlib import Path
import pytest
from rsik.experiment import read_decision, unit_worker
from rsik.errors import StageFailure
from portable_fixtures import ROOT, write


def test_synthetic_twenty_question_identity_proof_is_rejected_for_no_gain(synthetic_replay):
    result=synthetic_replay['decision']
    assert result['verdict']=='REJECTED' and result['reason_codes']==['NO_GAIN']
    assert len(result['per_question'])==20
    assert all(r['pair_identity'] and r['chars_A']==r['chars_B'] for r in result['per_question'])
    assert all(r['provenance_A']==r['provenance_B']=='PRESENT' for r in result['per_question'])


def test_wrong_candidate_identity_is_global_failure(synthetic_replay):
    with pytest.raises(StageFailure,match='INVALID_PAIR') as caught:
        read_decision(synthetic_replay['run'],synthetic_replay['fixture'],'0'*64,
            {'angle':'P1_DEDUP','proposal_id':'SYNTHETIC'},'1'*64,'2'*64)
    assert caught.value.global_stop


def test_unit_worker_runs_twenty_frozen_invariant_probes_without_backend_imports():
    parent=(ROOT/'scripts/rsi_delivery_worker.py').read_bytes()
    code=unit_worker(parent,parent,'P3_ORDER')
    compile(code,'<invariant-probes>','exec')
    assert b'ANGLE_VIOLATION' in code and b'UNIT_SEED_LOSS' in code
    assert b'from app' not in code and b'API_KEY' not in code


def test_synthetic_receipts_bind_raw_config_bytes_and_canonical_config_separately(synthetic_replay):
    run=synthetic_replay['run'];manifest=json.loads((run/'manifest.json').read_bytes())
    # Invented receipt metadata tests the verifier, never attests a container run.
    for arm in ('A','B'):
        raw=json.dumps({'selection':'all'},indent=2).encode()
        (run/(arm+'.json')).write_bytes(raw)
        assert hashlib.sha256(raw).hexdigest()!=manifest['arms'][arm]['sha256']
        for directory in run.glob('case-*'):
            write(directory/arm/'execution-receipt.json',{'worker_runtime':'SYNTHETIC_NOT_EXECUTED',
                'image':'SYNTHETIC','code_archive_sha256':'SYNTHETIC','container_removed':True,
                'worker_sha256':manifest['arms'][arm]['worker_sha256'],'config_sha256':hashlib.sha256(raw).hexdigest(),
                'input_sha256':hashlib.sha256((directory/'arm-input.json').read_bytes()).hexdigest()})
    spec=manifest['arms']['B']
    result=read_decision(run,synthetic_replay['fixture'],spec['candidate_id'],
        {'angle':'P2_BUDGET','proposal_id':'SYNTHETIC'},spec['worker_sha256'],spec['sha256'])
    assert len(result['per_question'])==20 and all(r['pair_identity'] for r in result['per_question'])
    assert result['gates']['G1'] and result['gates']['G3']
    receipt=run/'case-001/A/execution-receipt.json'
    value=json.loads(receipt.read_bytes());value['config_sha256']=manifest['arms']['A']['sha256'];write(receipt,value)
    with pytest.raises(StageFailure,match='EXECUTION_IDENTITY_UNKNOWN'):
        read_decision(run,synthetic_replay['fixture'],spec['candidate_id'],
            {'angle':'P2_BUDGET','proposal_id':'SYNTHETIC'},spec['worker_sha256'],spec['sha256'])

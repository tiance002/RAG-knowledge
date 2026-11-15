"""Synthetic capture parity tests; no remote model or container execution."""
from pathlib import Path
import json
import shutil
import pytest
from rsik.query_preview import frames,replay
from rsik.execution_evidence import ExecutionEvidence
from rsik.query_aware import adapt_legacy
from rsik.experiment import load
from portable_fixtures import ROOT,write


def prepared(tmp_path,synthetic_replay):
    ordinary=tmp_path/'ordinary';ordinary.mkdir()
    for index in range(1,5):shutil.copytree(synthetic_replay['run'],ordinary/f'suite-{index}')
    return frames(ordinary,[synthetic_replay['fixture']]*4,evidence=ExecutionEvidence.synthetic_control()),synthetic_replay['decision']


class SyntheticRecordedCapsule:
    business_root=Path('SYNTHETIC_RECORDED_ONLY_NOT_REMOTE_EXECUTION')
    deadline=None
    def __init__(self,frame):self.frame=frame
    def child(self,stage,paths,cwd,**kwargs):
        result=({'status':'PASS','scope':'SYNTHETIC_UNIT_DOUBLE'} if Path(cwd).name=='unit' else
            {'captures':[{'key':key,'capture':base['capture']} for key,base in self.frame['baseline'].items()]})
        write(paths[-1],result);return result


def test_synthetic_batch_and_ordinary_replay_produce_identical_source_gates(tmp_path,synthetic_replay):
    frame,original=prepared(tmp_path,synthetic_replay)
    worker=adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes())
    diagnostic=replay(SyntheticRecordedCapsule(frame),frame,worker,tmp_path/'preview','SYNTHETIC')
    assert diagnostic['formal_admission'] is False and diagnostic['recommend_ordinary_replay'] is False
    assert len(diagnostic['suite_decisions'])==4
    for actual in diagnostic['suite_decisions']:
        assert actual['gates']==original['gates']
        assert actual['delta_total']==original['delta_total'] and actual['reason_codes']==original['reason_codes']
        assert [r['candidate_facts'] for r in actual['per_question']]==[r['candidate_facts'] for r in original['per_question']]


def test_synthetic_frame_coverage_rejects_missing_batch_capture(tmp_path,synthetic_replay):
    frame,_=prepared(tmp_path,synthetic_replay)
    assert len(frame['entries'])==len(frame['baseline'])==80
    capsule=SyntheticRecordedCapsule(frame)
    capsule.frame={**frame,'baseline':dict(list(frame['baseline'].items())[:-1])}
    worker=adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes())
    with pytest.raises(ValueError,match='PREVIEW_OUTPUT_INCOMPLETE'):
        replay(capsule,frame,worker,tmp_path/'preview','SYNTHETIC')

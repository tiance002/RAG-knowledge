from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.query_verification import claim_bundle,verify_training
from rsik.events import EventLog
from rsik.experiment import load
from rsik.foundation import check_split

from portable_fixtures import ROOT,write,build_split,stress_split
from rsik.query_aware import QueryRegistry,adapt_legacy


def test_repackaging_same_sealed_sources_cannot_reset_one_use_claim(tmp_path):
    log=EventLog(tmp_path/'sealed.sqlite');sources={format(i,'064x') for i in range(10)}
    claim_bundle(log,'candidate',sources,['a','b','c'],tmp_path/'first')
    with pytest.raises(ValueError,match='SEALED_SOURCES_ALREADY_USED'):
        claim_bundle(log,'different-candidate',sources,['changed-a','changed-b','changed-c'],tmp_path/'second')
    event=log.read()[0]['payload']
    assert event['statistical_denominator']==10 and event['auxiliary_cases']==20


def test_unadopted_query_candidate_cannot_open_any_sealed_data(tmp_path):
    registry=tmp_path/'registry';training=tmp_path/'training'
    cid=QueryRegistry(registry).put(adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),
        b'{"selection":"all"}',parent_id='SYNTHETIC',origin='SYNTHETIC_TEST',evidence={})
    write(training/'round.json',{'candidate_id':cid,'adopted_candidate_id':None})
    with pytest.raises(ValueError,match='QUERY_TRAINING_ADOPTION_REQUIRED'):
        verify_training(registry,training,['NEVER_OPENED'],cid)


def test_synthetic_auxiliary_freezes_keep_every_source_including_singleton(tmp_path):
    source=build_split(tmp_path/'base',count=18,singleton=True)
    original=load(source/'cases.json')
    for index,direction in enumerate(('NEXT_ELSE_PREVIOUS','PREVIOUS_ELSE_NEXT')):
        path=stress_split(source,tmp_path/f'stress-{index}',direction=direction,count=18)
        assert check_split(path,expected_count=18)==load(path/'freeze.json')
        changed=load(path/'cases.json')
        assert sum(a['case']['seed_ids']!=b['case']['seed_ids'] for a,b in zip(original,changed,strict=True))==17
        assert len(changed)==18 and len(load(path/'sources.manifest.json'))==18
        assert (path/'evaluation.json').read_bytes()==(source/'evaluation.json').read_bytes()

"""Binding tests over invented synthetic source/query fixtures; no remote calls."""
from copy import deepcopy
from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik import foundation as f
from rsik.lineage import ParentPool,LineagePolicy

from portable_fixtures import stress_split

@pytest.fixture
def query_fixture(tmp_path,synthetic_split):
    return stress_split(synthetic_split,tmp_path/'query',query=True)


def changed_fixture(monkeypatch,change,query_fixture):
    original=f.load
    cases=deepcopy(original(query_fixture/'cases.json'))
    change(cases)
    manifest=deepcopy(original(query_fixture/'manifest.json'))
    manifest['cases']['sha256']=f.digest(f.canonical(cases))
    def read(path):
        if Path(path)==query_fixture/'cases.json':return cases
        if Path(path)==query_fixture/'manifest.json':return manifest
        return original(path)
    monkeypatch.setattr(f,'load',read)


def test_synthetic_alternate_queries_remain_source_bound(query_fixture):
    result=f.check_split(query_fixture,expected_count=20)
    assert result['questions']==result['source_groups']==20
    assert len(f.load(query_fixture/'qa_origins.restricted.json'))==17


def test_rehashed_question_change_cannot_detach_reviewed_origin(monkeypatch,query_fixture):
    origins=f.load(query_fixture/'qa_origins.restricted.json');qid=next(iter(origins))
    def change(cases):
        next(i for i in cases if i['qid']==qid)['case']['question']='unreviewed question'
    changed_fixture(monkeypatch,change,query_fixture)
    with pytest.raises(ValueError,match='QUERY_ORIGIN_BINDING_MISMATCH'):
        f.check_split(query_fixture,expected_count=20)


def test_query_probe_cannot_change_selector_inputs_even_with_rehashed_cases(monkeypatch,query_fixture):
    def change(cases):cases[0]['case']['seed_ids']=[]
    changed_fixture(monkeypatch,change,query_fixture)
    with pytest.raises(ValueError,match='QUERY_STRESS_SELECTOR_INPUT_CHANGED'):
        f.check_split(query_fixture,expected_count=20)


def test_historical_verified_receipt_does_not_override_later_confirmed_regression(tmp_path):
    pool=ParentPool(tmp_path/'pool',evidence_mode='SYNTHETIC_CONTROL');policy=LineagePolicy(tmp_path/'policy')
    receipt=tmp_path/'verified.json';receipt.write_text('{"verdict":"VERIFIED","candidate_id":"parent"}')
    pool.admit('parent','lineage',receipt,kind='VERIFIED',chars=100,replay_seconds=1)
    assert pool.require_verified('parent',policy)['kind']=='VERIFIED'
    loss=tmp_path/'loss.json';loss.write_text('{"passed":false,"candidate_fact_loss_questions":2}')
    pool.quarantine('parent',loss,'CONFIRMED_REAL_QUERY_FACT_LOSS')
    with pytest.raises(ValueError,match='PARENT_QUARANTINED'):pool.require_verified('parent',policy)
    assert pool.select(policy) is None

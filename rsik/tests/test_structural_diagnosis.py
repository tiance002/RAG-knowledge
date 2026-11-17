import json
from pathlib import Path
import sys

import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.selector_contract import interface_contract,structural_counts
from rsik.redact import LeakScanner,project_summary,make_visible
from rsik.contracts import ContractError
from rsik.openai_roles import OpenAIRoles
from rsik.budgets import Budget
from rsik.foundation import check_split

from portable_fixtures import stress_split


def test_synthetic_zero_eligible_pdf_members_without_reading_gold(monkeypatch,synthetic_replay):
    original=Path.read_text
    read=[]
    def checked(path,*args,**kwargs):
        assert path.name not in {'evaluation.json','provenance.restricted.json'}
        read.append(path.name)
        return original(path,*args,**kwargs)
    monkeypatch.setattr(Path,'read_text',checked)
    counts=structural_counts(synthetic_replay['run'],'A')
    assert counts['pdf_members']==counts['duplicate_key_eligible_members']==counts['exact_content_duplicate_pairs']==0
    assert counts['other_members']==60 and counts['nonseed_content_chars']==9780
    assert counts['nonseed_members_le200']==20 and counts['nonseed_members_gt200']==20
    assert 'snapshot.json' in read and 'capture.json' in read


def test_public_interface_is_frozen_code_and_contains_no_dataset_identifiers():
    contract=interface_contract();body=json.dumps(contract)
    assert 'duplicate_key_source' in contract and ".endswith('.pdf')" in body
    assert 'dev-001' not in body and 'evaluation.json' not in body


def test_untrusted_nested_interface_metadata_never_reaches_transport(tmp_path):
    public=make_visible([project_summary('NONSEED_EXPANSION',{'nonseed_content_chars':100})],parent_code='public')
    public['selector_contract']={**interface_contract(),'nested_gold':'PRIVATE_SENTINEL_0123456789'}
    client=OpenAIRoles(Budget(tmp_path/'budget.sqlite'),tmp_path/'roles',tmp_path/'unused.env',
                      LeakScanner(),transport=lambda *_:pytest.fail('must not send'))
    with pytest.raises(ContractError):client.diagnose(public)
    assert client.budget.snapshot()['attempts']==0


def test_neighbor_stress_changes_only_seed_placement_and_keeps_all_frozen_facts(tmp_path,synthetic_split):
    old=synthetic_split;stress=stress_split(old,tmp_path/'stress')
    assert (old/'evaluation.json').read_bytes()==(stress/'evaluation.json').read_bytes()
    assert check_split(stress,expected_count=20)['source_groups']==20
    original=json.loads((old/'cases.json').read_text(encoding='utf-8'))
    changed=json.loads((stress/'cases.json').read_text(encoding='utf-8'))
    for a,b in zip(original,changed,strict=True):
        assert a['qid']==b['qid'] and a['case']['chunks']==b['case']['chunks']
        assert a['case']['question']==b['case']['question'] and a['case']['files']==b['case']['files']
        assert a['case']['seed_ids']!=b['case']['seed_ids']

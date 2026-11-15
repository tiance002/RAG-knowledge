from pathlib import Path
import json,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.query_aware import adapt_legacy,apply_patch,validate_function,interface_contract,QueryRegistry,invariant_probe_source
from rsik.experiment import registry_api
from rsik.foundation import verify_boundary
from rsik.openai_roles import OpenAIRoles
from rsik.budgets import Budget
from rsik.redact import make_visible,project_summary,LeakScanner
from rsik.contracts import ContractError
from run_rsi_loop import selector

ROOT=Path(__file__).resolve().parents[2]



def adapted():return adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes())


def test_adapter_only_changes_function_signature_and_runtime_call():
    original=(ROOT/'scripts/rsi_delivery_worker.py').read_bytes()
    changed=adapted()
    assert b"config['selection'], snapshot['question'])" in changed
    assert [a.arg for a in selector(changed.decode())[0].args.args][-1]=='query'
    assert verify_boundary()
    compile(invariant_probe_source(changed),'<capsule-probes>','exec')
    # Body semantics are unchanged in the bootstrap adapter.
    import ast
    assert ast.dump(selector(original.decode())[0].body[1])==ast.dump(selector(changed.decode())[0].body[1])


@pytest.mark.parametrize('body',[
    '    import os\n    return []',
    "    members[0]['content']='changed'\n    return []",
    '    return eval(query)',
    '    return list(range(len(members)*2))',
    "    return query.__class__",
])
def test_new_seam_reuses_strict_body_guard(body):
    with pytest.raises(ValueError):validate_function('def select_member_indices(members, chunks, files, seed_ids, selection, query):\n'+body)


def test_patch_cannot_change_signature_or_outside_function():
    parent=adapted();old=selector(parent.decode())[1]
    with pytest.raises(ValueError):apply_patch(parent,{'old':old,'new':old.replace(', query):','):')})
    with pytest.raises(ValueError):apply_patch(parent,{'old':"snapshot['question']",'new':"'faked'"})


def test_query_registry_is_content_addressed_and_detects_worker_tamper(tmp_path):
    registry=QueryRegistry(tmp_path)
    cid=registry.put(adapted(),b'{"selection":"all"}',parent_id='legacy-repair',origin='SYNTHETIC_TEST',evidence={})
    record=registry.get(cid)
    assert record['record']['parent_id']=='legacy-repair'
    (tmp_path/'workers'/record['record']['worker_sha256']).write_bytes(b'changed')
    with pytest.raises(ValueError,match='QUERY_ARTIFACT_CHANGED'):registry.get(cid)


def test_quarantine_does_not_erase_artifacts_but_excludes_parent_use(tmp_path):
    registry=QueryRegistry(tmp_path/'registry')
    cid=registry.put(adapted(),b'{"selection":"all"}',parent_id='legacy-repair',origin='SYNTHETIC_TEST',evidence={})
    receipt=tmp_path/'receipt.json';receipt.write_text('{"status":"FAIL"}')
    registry.quarantine(cid,receipt,'SYNTHETIC_TEST_FAILURE')
    assert registry.get(cid)['candidate_id']==cid
    with pytest.raises(ValueError,match='QUERY_PARENT_QUARANTINED'):registry.require_not_quarantined(cid)


def test_synthetic_one_character_counterexample_quarantines_candidate(tmp_path):
    from portable_fixtures import write
    registry=QueryRegistry(tmp_path/'registry')
    cid=registry.put(adapted(),b'{"selection":"all"}',parent_id='SYNTHETIC',origin='SYNTHETIC_TEST',evidence={})
    receipt=tmp_path/'receipt.json'
    write(receipt,{'status':'FAIL','reason':'QUERY_SHORT_INPUT_FALLBACK_FAILED','candidate_id':cid,
        'scope':'SYNTHETIC_NOT_ACTUAL_CAPSULE_RECEIPT'})
    registry.quarantine(cid,receipt,'QUERY_SHORT_INPUT_FALLBACK_FAILED')
    assert json.loads(receipt.read_bytes())['status']=='FAIL'
    with pytest.raises(ValueError,match='QUERY_PARENT_QUARANTINED'):
        registry.require_not_quarantined(cid)


def test_query_profile_requires_explicit_model_send_profile(tmp_path):
    public=make_visible([project_summary('REPLAY_SUMMARY',{'questions':20},include_count_evidence=True)],parent_code=selector(adapted().decode())[1])
    public.update(selector_contract=interface_contract(),experiment_intent='QUERY_OVERLAP_ABLATION')
    calls=[]
    def transport(body,*_):
        calls.append(body)
        return {'transport_status':'RECEIVED','response':{'model':'gpt-6-luna','status':'completed',
            'usage':{'input_tokens':10,'output_tokens':10},'output':[{'type':'message','content':[{'type':'output_text',
            'text':json.dumps({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC_TEST'})}]}]}}
    budget=Budget(tmp_path/'budget.sqlite')
    legacy=OpenAIRoles(budget,tmp_path/'legacy',tmp_path/'unused',LeakScanner(),transport=transport)
    with pytest.raises(ContractError):legacy.diagnose(public)
    assert calls==[]
    query=OpenAIRoles(budget,tmp_path/'query',tmp_path/'unused',LeakScanner(),transport=transport,seam_profile='query-aware-v1')
    assert query.diagnose(public)['status']=='ABSTAIN'
    assert len(calls)==1 and 'SIX arguments' in calls[0]['input'][0]['content']

from pathlib import Path
import hashlib,json,sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.test_phase import freeze,assert_frozen
from rsik.openai_roles import OpenAIRoles
from rsik.query_aware import autonomy_interface_contract as interface_contract
from rsik.budgets import Budget
from rsik.redact import make_visible,project_summary,LeakScanner
from rsik.contracts import ContractError
from rsik.autonomous import proposer_model,history_item


def test_system_edit_during_autonomous_test_stops_instead_of_contaminating_results(tmp_path):
    code=tmp_path/'engine.py';code.write_text('VERSION=1')
    path=tmp_path/'freeze.json';key=freeze(tmp_path,[code],path,baseline={'origin':'HUMAN_DEVELOPMENT'},
        validation={'exit_code':0,'failed':0,'skipped':0,'passed':1},budget_snapshot={'recorded_usd_upper':'0.06','limit_usd':'5'})
    assert assert_frozen(path,key)['ownership']['candidate_changes']=='RSI_MODEL_ONLY'
    code.write_text('VERSION=2')
    with pytest.raises(ValueError,match='FROZEN_SYSTEM_CHANGED'):assert_frozen(path,key)


def public():
    value=make_visible([project_summary('REPLAY_SUMMARY',{'questions':20},include_count_evidence=True)],
        parent_code='def select_member_indices(members, chunks, files, seed_ids, selection, query):\n    return []')
    value.update(selector_contract=interface_contract(),experiment_intent='STRUCTURAL_POLICY_SEARCH',search_history=[])
    return value


def test_autonomous_role_has_no_prescribed_development_strategy(tmp_path):
    sent=[]
    def transport(body,*_):
        sent.append(body)
        return {'transport_status':'RECEIVED','response':{'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':10,'output_tokens':10},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC_TEST'})}]}]}}
    roles=OpenAIRoles(Budget(tmp_path/'budget.sqlite'),tmp_path/'roles',tmp_path/'unused',LeakScanner(),transport=transport,seam_profile='query-autonomous-v1')
    roles.call('P2_BUDGET',public())
    policy=sent[0]['input'][0]['content']
    assert 'Independently design' in policy and 'QUERY_BIGRAM_RATIO' not in policy and 'QUERY_TRIGRAM' not in policy
    assert 'LONGEST_SEED_CONTENT_LENGTH' not in policy and '9/10' not in policy
    assert 'longest-seed' not in policy


def test_monitor_cannot_supply_fixed_strategy_or_raw_gold_history_in_autonomy_test(tmp_path):
    budget=Budget(tmp_path/'budget.sqlite')
    roles=OpenAIRoles(budget,tmp_path/'roles',tmp_path/'unused',LeakScanner(),
        transport=lambda *_:pytest.fail('must not dispatch'),seam_profile='query-autonomous-v1')
    value=public();value['experiment_intent']='QUERY_BIGRAM_RATIO'
    with pytest.raises(ContractError,match='PRESELECTED_STRATEGY_FORBIDDEN'):roles.diagnose(value)
    value=public();value['search_history']=[{'question':'private','answer':'private'}]
    with pytest.raises(ContractError):roles.diagnose(value)
    assert budget.snapshot()['attempts']==0


def test_no_gain_does_not_stop_search_or_trigger_expensive_coding_model():
    history=[history_item({'hypothesis':'h','change':'c'},'NO_GAIN',['NO_GAIN']) for _ in range(12)]
    assert proposer_model(history)=='gpt-6-luna'
    history.extend([history_item({'hypothesis':'h','change':'c'},'PATCH_INVALID',['AST_PATCH_INVALID']) for _ in range(2)])
    assert proposer_model(history)=='gpt-6.2-sol'
    history.append(history_item({'hypothesis':'h','change':'c'},'OBSERVED',['STATISTICAL_EVIDENCE_INSUFFICIENT']))
    assert proposer_model(history)=='gpt-6-luna'


def test_long_failure_memory_fits_request_without_deleting_local_history(tmp_path):
    from copy import deepcopy
    from rsik.autonomy_contract import remember,project_search_memory
    from rsik.tests.test_workflow import FakeRoles
    value=public();value['parent_code']+='\n# '+('SYNTHETIC_PARENT_COMMENT ' * 125)
    item={'hypothesis':'h'*512,'change':'c'*512,'outcome':'REJECTED',
        'feedback_schema':'rsi-search-feedback/v2','quality_verdict':'REJECTED',
        'execution_status':'COMPLETED','failure_origin':'CANDIDATE',
        'reason_codes':['FACT_LOSS','SOURCE_SUITE_HARD_GATE_FAILED'],
        'counts':{'primary_questions':20,'auxiliary_cases':60,'fact_loss_cases':2,'failed_suites':2}}
    memory={'schema':'rsi-autonomy-memory/v1','rounds':0,'evicted_entries':0,'entries':[]}
    for i in range(1,33):remember(memory,format(i,'064x'),i,item)
    recent=[{**item, 'hypothesis':prefix*512} for prefix in ('a','b')]+[deepcopy(item)]
    memory['entries'][-1]['history']={**item, 'hypothesis':'different hypothesis'}
    value.update(search_history=recent,search_memory=project_search_memory(memory,recent=recent))
    before=deepcopy(value);sent=[]
    def transport(body,*_):
        sent.append(body)
        return {'transport_status':'RECEIVED','response':{'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':10,'output_tokens':10},
            'output':[{'type':'message','content':[{'type':'output_text','text':json.dumps({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC_TEST'})}]}]}}
    roles=OpenAIRoles(Budget(tmp_path/'budget.sqlite'),tmp_path/'roles',tmp_path/'unused',LeakScanner(),transport=transport,seam_profile='query-autonomous-v1')
    roles.propose('P2_BUDGET',value,FakeRoles().diagnose(value)['result'])
    wire_bytes=json.dumps(sent[0],ensure_ascii=False,allow_nan=False).encode('utf-8')
    assert len(wire_bytes)<=16384
    assert value==before and len(memory['entries'])==32
    actual=json.loads(sent[0]['input'][1]['content'])['public']
    assert actual['parent_code']==value['parent_code'] and actual['observations']==value['observations']
    assert len(actual['search_history'])<3 or len(actual['search_memory']['entries'])<8
    assert len(actual['search_history'])<3
    references=[entry['history']['recent_history_index'] for entry in actual['search_memory']['entries']
                if 'recent_history_index' in entry['history']]
    assert references and all(actual['search_history'][index]==item for index in references)
    assert list((tmp_path/'roles').glob('*.projection.json'))
    receipt_path, = (tmp_path/'roles').glob('*.input-receipt.json')
    input_receipt=json.loads(receipt_path.read_bytes())
    request_body=json.loads((receipt_path.parent/input_receipt['request_file']).read_bytes())
    from rsik.state import canonical,digest
    assert request_body==sent[0]
    wire_path=receipt_path.parent/input_receipt['request_wire_file']
    assert wire_path.read_bytes()==wire_bytes
    assert input_receipt['request_hash']==digest(sent[0])
    assert input_receipt['request_wire_sha256']==hashlib.sha256(wire_bytes).hexdigest()
    assert input_receipt['request_body_bytes']==len(wire_bytes)
    assert input_receipt['search_history_items_in_request']==len(actual['search_history'])
    assert input_receipt['search_memory_entries_in_request']==len(actual['search_memory']['entries'])
    assert input_receipt['search_context_canonical_bytes']==len(canonical({
        'search_history':actual['search_history'],'search_memory':actual['search_memory']}))

from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.autonomous import history_item,patch_identity,static_patch_reason,stop_guard
from rsik.autonomy_contract import remember,project_search_memory,validate_search_memory
from rsik.errors import StageFailure
from rsik.contracts import ContractError

WORKER=b"def select_member_indices(members, chunks, files, seed_ids, selection, query):\n    return []\n"

def test_global_failure_survives_long_gap_and_projection_is_bounded():
    memory={'schema':'rsi-autonomy-memory/v1','rounds':0,'evicted_entries':0,'entries':[]}
    item=history_item({'hypothesis':'h'*512,'change':'c'*512},'PATCH_INVALID',['STATIC_OPERATOR_MOD_FORBIDDEN'])
    repeated='a'*64
    for number in range(1,51):
        remember(memory,repeated if number in (1,2,7,13,20) else format(number,'064x'),number,item)
    assert len(memory['entries'])==46
    projected=project_search_memory(memory)
    assert len(projected['entries'])==8
    entry=next(e for e in projected['entries'] if e['selector_sha256']==repeated)
    assert (entry['first_round'],entry['last_round'],entry['attempts'])==(1,20,5)
    assert len(entry['history']['hypothesis'])==96
    assert len(memory['entries'][0]['history']['hypothesis'])==512
    assert projected==project_search_memory(memory)
    projected['entries'][0]['history']['question']='private'
    with pytest.raises(ContractError):validate_search_memory(projected)

def test_identity_ignores_ast_formatting_and_rejection_does_not_relax_guard():
    first={'old':'return []','new':'return [1 % 2]'}
    second={'old':'return []','new':'return [ 1%2 ]'}
    assert patch_identity(WORKER,first)==patch_identity(WORKER,second)
    from rsik.query_aware import apply_patch
    with pytest.raises(ValueError) as error:apply_patch(WORKER,first)
    assert static_patch_reason(WORKER,first,error.value)=='STATIC_OPERATOR_MOD_FORBIDDEN'
    assert static_patch_reason(WORKER,{'old':'missing','new':'x'},ValueError('QUERY_PATCH_NOT_UNIQUE'))=='STATIC_PATCH_NOT_UNIQUE'

def test_stop_and_invalidation_use_supported_distinct_categories(tmp_path):
    (tmp_path/'stop-request.json').write_text('{}')
    with pytest.raises(StageFailure) as owner:stop_guard(tmp_path,None,None)
    assert (owner.value.category,owner.value.reason_code)==('CANCELLED','OWNER_REQUESTED_STOP')
    (tmp_path/'invalidation-request.json').write_text('{}')
    with pytest.raises(StageFailure) as engineer:stop_guard(tmp_path,None,None)
    assert (engineer.value.category,engineer.value.reason_code)==('CONTRACT_ERROR','TEST_INVALIDATED_BY_ENGINEER')
    assert engineer.value.global_stop


@pytest.mark.parametrize('replacement,reason',[
    ('return [1 % 2]','STATIC_OPERATOR_MOD_FORBIDDEN'),
    ('return [1 // 2]','STATIC_OPERATOR_FLOORDIV_FORBIDDEN'),
    ('return sorted([], key=lambda item: item)','STATIC_KEYWORD_ARGUMENT_FORBIDDEN'),
])
def test_static_rejection_codes_use_portable_synthetic_proposals(replacement,reason):
    from rsik.query_aware import apply_patch
    patch={'old':'return []','new':replacement}
    with pytest.raises(ValueError) as error:apply_patch(WORKER,patch)
    assert static_patch_reason(WORKER,patch,error.value)==reason


@pytest.mark.parametrize('profile,expected',[
    ('single',('P2_BUDGET',)),('parallel',('P1_DEDUP','P2_BUDGET','P3_ORDER'))])
def test_controller_dispatches_integrated_pipeline_and_shared_advisory_budget(tmp_path,monkeypatch,profile,expected):
    import json
    import rsik.autonomous as controller
    import rsik.query_workflow as integrated
    import rsik.selector_contract as structural
    import rsik.launch as launch_boundary
    # This orchestration control replaces every external boundary. Its explicit
    # launch double cannot authorize a real model request or capsule execution.
    synthetic_launch=object()
    def synthetic_admission(context,**kwargs):
        assert context is synthetic_launch
        assert kwargs['phase']=='AUTONOMOUS_SEARCH'
    monkeypatch.setattr(launch_boundary,'validate_launch',synthetic_admission)
    directory=tmp_path/'run';directory.mkdir()
    registry=tmp_path/'registry';registry.mkdir()
    baseline=tmp_path/'baseline';baseline.mkdir();(baseline/'round.json').write_text('{}')
    receipt=tmp_path/'verified.json';receipt.write_text(json.dumps({'verdict':'VERIFIED','candidate_id':'parent-candidate'}))
    complete={'value':False};calls=[];role_calls=[]
    class LocalBudget:
        limit_nano=5
        def __init__(self,*args):pass
        def snapshot(self):return {'unbounded_usage_requests':0,'pending_request_keys':[],
            'recorded_cost_nano_upper':5 if complete['value'] else 0}
    class LocalLog:
        def __init__(self,*args):pass
        def append(self,*args):pass
        def read(self):return [{'event_type':'QUERY_SEALED_BUNDLE_FINISHED','payload':{
            'candidate_id':'parent-candidate','verdict':'VERIFIED','receipt_sha256':controller.bytes_hash(receipt.read_bytes())}}]
    class LocalRegistry:
        def __init__(self,*args):pass
        def get(self,*args):return {'candidate_id':'parent-candidate','worker':WORKER,'config':b'{}'}
    def roles(*args,**kwargs):
        value=object();role_calls.append((args,kwargs,value));return value
    def run_round(**kwargs):
        calls.append(kwargs);complete['value']=True
        return {'execution_state':'COMPLETED','proposal_instances':{},'stop_reason':'DIAGNOSIS_ABSTAIN'}
    monkeypatch.setattr(controller,'Budget',LocalBudget);monkeypatch.setattr(controller,'EventLog',LocalLog)
    monkeypatch.setattr(controller,'QueryRegistry',LocalRegistry);monkeypatch.setattr(controller,'OpenAIRoles',roles)
    monkeypatch.setattr(controller,'stop_guard',lambda *args:None)
    monkeypatch.setattr(controller,'verify_training',lambda *args:None)
    monkeypatch.setattr(structural,'structural_counts',lambda *args:{})
    monkeypatch.setattr(controller,'frames',lambda *args:{'parent_id':'parent-candidate','worker':WORKER,
        'config':b'{"selection":"all"}','baseline':{}})
    monkeypatch.setattr(integrated,'run_query_round',run_round)
    result=controller.run({'directory':str(directory),'query_registry':str(registry),'fixtures':[],
        'budget_ledger':'unused','baseline_round':str(baseline),'baseline_id':'parent-candidate',
        'baseline_verification':str(receipt),'freeze':'unused','freeze_sha256':'f'*64,
        'key_file':'unused','profile':profile},launch=synthetic_launch)
    assert result['stop_reason']=='BUDGET_EXHAUSTED' and result['rounds']==1
    assert tuple(calls[0]['enabled_slots'])==expected
    assert calls[0]['directory']==directory/'round-000001/integrated'
    assert role_calls[0][0][0] is role_calls[1][0][0]
    assert role_calls[1][1]['seam_profile']=='legacy'
    assert calls[0]['roles'] is role_calls[0][2] and calls[0]['advisory_roles'] is role_calls[1][2]
    assert (directory/'search-memory.json').is_file()



def test_duplicate_proposal_instances_all_enter_memory_without_replay_filter():
    from rsik.autonomous import remember_round
    memory={'schema':'rsi-autonomy-memory/v1','rounds':0,'evicted_entries':0,'entries':[]}
    proposal={'hypothesis':'synthetic','change':'synthetic','worker_patch':{'old':'return []','new':'return [0]'}}
    slots=('P1_DEDUP','P2_BUDGET','P3_ORDER')
    result={'proposal_instances':{slot:proposal for slot in slots},'qualified':list(slots),
        'candidate_decisions':[{'angle':slot,'verdict':'REJECTED','reason_codes':['NO_GAIN'],'delta_total':0} for slot in slots]}
    history=[]
    items=remember_round(memory,history,result,WORKER,1)
    assert len(items)==3 and len(history)==3 and len(memory['entries'])==1
    assert memory['entries'][0]['attempts']==3
    validate_search_memory(project_search_memory(memory))

"""Review counterexamples: public next-request feedback and isolated revision CAS."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from rsik.autonomous import remember_round
from rsik.autonomy_contract import project_search_memory
from rsik.budgets import Budget
from rsik.openai_roles import OpenAIRoles
from rsik.promotion import PromotionSandbox
from rsik.redact import LeakScanner
from rsik.tests.test_autonomy_boundary import public

WORKER=b'def select_member_indices(members, chunks, files, seed_ids, selection, query):\n    return []\n'
PROPOSAL={'hypothesis':'SYNTHETIC hypothesis','change':'SYNTHETIC change',
          'worker_patch':{'old':'return []','new':'return [0]'}}


@pytest.mark.parametrize('kind,decision,qualified,state,expected',[
    ('statistics',{'verdict':'OBSERVED','reason_codes':['STATISTICAL_EVIDENCE_INSUFFICIENT'],
      'delta_total':-15,'reduction_rate':0.015,'group_counts':{'favorable':8,'unfavorable':1,'ties':11}},True,'COMPLETED',
     ('OBSERVED','OBSERVED','COMPLETED','NONE')),
    ('infrastructure',{'verdict':'INCONCLUSIVE','reason_codes':['INFRA_FAILURE']},True,'COMPLETED',
     ('INCONCLUSIVE','INCONCLUSIVE','FAILED','INFRASTRUCTURE')),
    ('facts',{'verdict':'REJECTED','reason_codes':['FACT_LOSS'],
      'per_question':[{'candidate_facts':{'f1':'ABSENT','f2':'PRESENT'}}],
      'feedback_counts':{'failed_suites':2,'fact_loss_cases':3,'auxiliary_cases':60}},True,'COMPLETED',
     ('REJECTED','REJECTED','COMPLETED','CANDIDATE')),
    ('no_gain',{'verdict':'REJECTED','reason_codes':['NO_GAIN'],'delta_total':0,
      'question_counts':{'favorable':0,'unfavorable':0,'ties':20}},True,'COMPLETED',
     ('NO_GAIN','REJECTED','COMPLETED','NONE')),
    ('not_replayed',{},True,'COMPLETED',('NOT_EVALUATED','NOT_EVALUATED','NOT_STARTED','UNKNOWN')),
    ('global_stop',{},True,'FAILED',('STOPPED','NOT_EVALUATED','STOPPED','GLOBAL_STOP')),
])
def test_feedback_reaches_actual_next_diagnosis_request(tmp_path,kind,decision,qualified,state,expected):
    memory={'schema':'rsi-autonomy-memory/v1','rounds':0,'evicted_entries':0,'entries':[]}
    history=[]
    result={'proposal_instances':{'P2_BUDGET':PROPOSAL},'qualified':['P2_BUDGET'] if qualified else [],
            'candidate_decisions':[{'angle':'P2_BUDGET',**decision}] if decision else [],
            'execution_state':state,'stop_reason':'OWNER_REQUESTED_STOP' if state=='FAILED' else None}
    remember_round(memory,history,result,WORKER,1)
    value=public();value.update(search_history=history,search_memory=project_search_memory(memory, recent=history))
    sent=[]
    def transport(body,*_):
        sent.append(body)
        return {'transport_status':'RECEIVED','response':{'model':'gpt-6-luna','status':'completed',
            'usage':{'input_tokens':1,'output_tokens':1},'output':[{'type':'message','content':[{
            'type':'output_text','text':json.dumps({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC_CONTROL'})}]}]}}
    roles=OpenAIRoles(Budget(tmp_path/'budget.sqlite'),tmp_path/'roles',tmp_path/'unused',LeakScanner(),
                      transport=transport,seam_profile='query-autonomous-v1')
    roles.diagnose(value)
    actual=json.loads(sent[0]['input'][1]['content'])['public']
    feedback=actual['search_history'][0]
    assert tuple(feedback[k] for k in ('outcome','quality_verdict','execution_status','failure_origin'))==expected
    assert feedback['reason_codes']==history[0]['reason_codes']
    memory_entries=actual['search_memory']['entries']
    if kind in {'statistics','facts','no_gain'}:
        entry,=memory_entries
        assert entry['history']=={'recent_history_index':0}
        assert (entry['attempts'],entry['first_round'],entry['last_round'])==(1,1,1)
        assert actual['search_history'][entry['history']['recent_history_index']]==feedback
    else:
        assert memory_entries==[]
    assert memory['entries'][0]['history']==feedback
    receipts=list((tmp_path/'roles').glob('*.input-receipt.json'))
    receipt,=receipts
    input_receipt=json.loads(receipt.read_bytes())
    request_body=json.loads((receipt.parent/input_receipt['request_file']).read_bytes())
    wire_body=json.dumps(request_body,ensure_ascii=False,allow_nan=False).encode('utf-8')
    from rsik.state import canonical,digest
    assert input_receipt['request_hash']==digest(request_body)
    assert (receipt.parent/input_receipt['request_wire_file']).read_bytes()==wire_body
    assert input_receipt['request_wire_sha256']==hashlib.sha256(wire_body).hexdigest()
    assert input_receipt['request_body_bytes']==len(wire_body)
    assert input_receipt['search_history_items_in_request']==len(actual['search_history'])
    assert input_receipt['search_memory_entries_in_request']==len(memory_entries)
    assert input_receipt['user_message_utf8_bytes']==len(sent[0]['input'][1]['content'].encode('utf-8'))
    assert input_receipt['search_context_canonical_bytes']==len(canonical({
        'search_history':actual['search_history'],'search_memory':actual['search_memory']}))
    if kind=='statistics':assert feedback['counts']=={'favorable':8,'unfavorable':1,'ties':11,'delta_total':-15,'reduction_rate':0.015}
    if kind=='facts':assert feedback['counts']=={'failed_suites':2,'fact_loss_cases':3,'auxiliary_cases':60,'primary_questions':1}
    if kind=='no_gain':assert feedback['counts']=={'favorable':0,'unfavorable':0,'ties':20,'delta_total':0}


def test_stop_without_proposals_is_explicit_not_abstention():
    memory={'schema':'rsi-autonomy-memory/v1','rounds':0,'evicted_entries':0,'entries':[]};history=[]
    remember_round(memory,history,{'execution_state':'FAILED','stop_reason':'FROZEN_SYSTEM_CHANGED'},WORKER,1)
    assert history[0]['outcome']=='STOPPED' and history[0]['failure_origin']=='GLOBAL_STOP'


def test_auxiliary_fact_loss_takes_precedence_over_primary_no_gain():
    memory={'schema':'rsi-autonomy-memory/v1','rounds':0,'evicted_entries':0,'entries':[]};history=[]
    remember_round(memory,history,{'proposal_instances':{'P2_BUDGET':PROPOSAL},'qualified':['P2_BUDGET'],
        'candidate_decisions':[{'angle':'P2_BUDGET','verdict':'REJECTED',
            'reason_codes':['NO_GAIN','SOURCE_SUITE_HARD_GATE_FAILED'],
            'feedback_counts':{'fact_loss_cases':1,'failed_suites':1}}]},WORKER,1)
    assert history[0]['outcome']=='REJECTED' and history[0]['failure_origin']=='CANDIDATE'


@pytest.mark.parametrize('bad',[
    {'question':'private'}, {'delta_total':True}, {'favorable':-1}, {'reduction_rate':float('nan')},
])
def test_feedback_counts_reject_private_fields_and_invalid_numbers(bad):
    from rsik.autonomous import history_item
    from rsik.contracts import ContractError
    with pytest.raises(ContractError):history_item(PROPOSAL,'OBSERVED',['STATISTICAL_EVIDENCE_INSUFFICIENT'],bad)


def test_a_b_a_old_approval_only_reconciles_original_operation(tmp_path,monkeypatch):
    sandbox=PromotionSandbox(tmp_path/'sandbox',allowed_root=tmp_path)
    initial=sandbox.initialize('A',b'A');artifact=b'B'
    approval=sandbox.approval('B',hashlib.sha256(artifact).hexdigest(),'A')
    first=sandbox.promote(approval,artifact,quality='VERIFIED',rollback_hash=initial['artifact_hash'])
    repeated=sandbox.promote(approval,artifact,quality='VERIFIED',rollback_hash=initial['artifact_hash'])
    assert first['current']==repeated['current'] and repeated['status']=='PROMOTED'
    back=sandbox.approval('A',initial['artifact_hash'],'B',operation='ROLLBACK')
    sandbox.rollback(back);before=sandbox.current()
    assert before['deployment_revision']==2 and before['version_id']=='A'
    import rsik.promotion as module
    writes=[];original=module.atomic_write
    def counted(path,*args,**kwargs):
        writes.append(Path(path).name);return original(path,*args,**kwargs)
    monkeypatch.setattr(module,'atomic_write',counted)
    replay=sandbox.promote(approval,artifact,quality='VERIFIED',rollback_hash=initial['artifact_hash'])
    assert replay['status']=='APPLIED_SUPERSEDED' and replay['operation_status']=='PROMOTED'
    assert sandbox.reconcile_switch(approval['signature'])['status']=='APPLIED_SUPERSEDED'
    assert sandbox.current()==before and 'current.json' not in writes
    assert sum(e['event_type']=='PROMOTED' for e in sandbox.events.read())==1
    assert sum(e['event_type']=='ROLLED_BACK' for e in sandbox.events.read())==1


def test_distinct_approval_at_consumed_revision_cannot_switch(tmp_path):
    sandbox=PromotionSandbox(tmp_path/'sandbox',allowed_root=tmp_path)
    old=sandbox.initialize('A',b'A');artifact=b'B';digest=hashlib.sha256(artifact).hexdigest()
    used=sandbox.approval('B',digest,'A');unused=sandbox.approval('B',digest,'A')
    sandbox.promote(used,artifact,quality='VERIFIED',rollback_hash=old['artifact_hash'])
    sandbox.rollback(sandbox.approval('A',old['artifact_hash'],'B',operation='ROLLBACK'))
    before=sandbox.current()
    with pytest.raises(ValueError,match='EXPECTED_CURRENT_VERSION_CHANGED'):
        sandbox.promote(unused,artifact,quality='VERIFIED',rollback_hash=old['artifact_hash'])
    assert sandbox.current()==before


@pytest.mark.parametrize('after_pointer',[False,True])
def test_interrupted_operation_recovery_distinguishes_applied_and_not_applied(tmp_path,monkeypatch,after_pointer):
    sandbox=PromotionSandbox(tmp_path/'sandbox',allowed_root=tmp_path)
    old=sandbox.initialize('A',b'A');artifact=b'B'
    approval=sandbox.approval('B',hashlib.sha256(artifact).hexdigest(),'A')
    if after_pointer:
        original=sandbox.events.append
        def interrupt(key,kind,payload,**kwargs):
            if kind=='PROMOTED':raise SystemExit('SYNTHETIC interruption after pointer')
            return original(key,kind,payload,**kwargs)
        monkeypatch.setattr(sandbox.events,'append',interrupt)
    else:
        import rsik.promotion as module
        original=module.atomic_write
        def interrupt(path,*args,**kwargs):
            if Path(path).name=='current.json':raise SystemExit('SYNTHETIC interruption before pointer')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(module,'atomic_write',interrupt)
    with pytest.raises(SystemExit):sandbox.promote(approval,artifact,quality='VERIFIED',rollback_hash=old['artifact_hash'])
    monkeypatch.undo()
    assert sandbox.reconcile_switch(approval['signature'])['status']==('PROMOTED' if after_pointer else 'INTENT_NOT_APPLIED')
    assert sandbox.current()['deployment_revision']==(1 if after_pointer else 0)

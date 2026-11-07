import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch
import pytest

p = Path(__file__).resolve().parents[2] / 'scripts/m2_current_reconcile.py'
spec = importlib.util.spec_from_file_location('m2_reconcile_test', p)
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

@pytest.fixture
def sample(tmp_path):
    request = b'{}'; (tmp_path/'request-0001.json').write_bytes(request)
    usage = {'prompt_tokens':10,'completion_tokens':5}
    (tmp_path/'response-0001.body').write_text(json.dumps({'usage':usage,'model':'test','id':'provider-1'}))
    pending = {'call_id':1,'state':'UNKNOWN_PENDING','qid':'q','mode':'chat','arm':'A','repetition':1,'requested_model':'test','input_token_bound':100,'output_token_bound':20,'reservation_cny':'0.00014','request_sha256':hashlib.sha256(request).hexdigest()}
    done = {**pending,'state':'COMPLETED','actual_model':'test','usage':usage,'price_based_cost_cny':'0.00002','provider_request_id':'provider-1'}
    fresh = {'model':'test','max_output_tokens':20,'pricing':{'input_cny_per_million':1,'output_cny_per_million':2}}
    trials = {m.cont.identity(pending): {'provider_call_ids':[1]}}
    return tmp_path, trials, pending, done, fresh

def test_unknown_keeps_full_reservation(sample):
    path,trials,pending,_,fresh=sample
    calls,_=m.audit_run(path,trials,[pending,{**pending,'state':'UNKNOWN'}],fresh,1,'0.00014',(1,))
    assert calls[0]['committed_cny']=='0.00014'

def test_completed_and_tampering_fail_closed(sample):
    path,trials,pending,done,fresh=sample
    m.audit_run(path,trials,[pending,done],fresh,1,'0.00002')
    bad=copy.deepcopy(done); bad['price_based_cost_cny']='0'
    with pytest.raises(ValueError,match='Cost mismatch'): m.audit_run(path,trials,[pending,bad],fresh,1,'0.00002')
    (path/'request-0001.json').write_text('tampered')
    with pytest.raises(ValueError,match='Request hash mismatch'): m.audit_run(path,trials,[pending,done],fresh,1,'0.00002')

def test_usage_response_tampering(sample):
    path,trials,pending,done,fresh=sample
    (path/'response-0001.body').write_text(json.dumps({'usage':{'prompt_tokens':9,'completion_tokens':5},'model':'test','id':'provider-1'}))
    with pytest.raises(ValueError,match='Response usage'): m.audit_run(path,trials,[pending,done],fresh,1,'0.00002')

def test_duplicate_reference_and_unstarted_identity(sample):
    path,trials,pending,done,fresh=sample
    trials[next(iter(trials))]['provider_call_ids']=[1,1]
    with pytest.raises(ValueError,match='Duplicate trial'): m.audit_run(path,trials,[pending,done],fresh,1,'0.00002')
    plan=[(str(i),'chat','A',1) for i in range(96)]
    with pytest.raises(ValueError,match='Unstarted'): m.validate_identities(plan, plan[:-1])
    with pytest.raises(ValueError,match='duplicate'): m.validate_identities(plan[:-1]+[plan[0]], plan)

def test_budget_and_pending_fail_closed(sample):
    m.validate_budget_block('0.3','0.4','0.2')
    with pytest.raises(ValueError,match='Budget block'): m.validate_budget_block('0.2','0.4','0.2')
    path,trials,pending,_,fresh=sample
    with pytest.raises(ValueError,match='UNKNOWN or pending'): m.audit_run(path,trials,[pending],fresh,1,'0.00014')

def test_process_failure_is_blocked():
    with patch.object(m.subprocess,'run') as run, patch.object(m,'sha',return_value='lease-hash'):
        run.return_value.returncode=3
        assert m.no_writers()['result']=='BLOCKED_INACCESSIBLE_COMMAND_LINES'

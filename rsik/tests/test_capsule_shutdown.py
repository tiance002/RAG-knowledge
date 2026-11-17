"""Synthetic dead-pipe and receipt-binding controls; no SSH, archive, or model."""
import base64
import hashlib
import json
import queue
from pathlib import Path
from types import SimpleNamespace

import pytest

from rsik.capsule import Capsule
from rsik.execution_evidence import ExecutionEvidence
from rsik.state import canonical


class Pipe:
    def __init__(self, fail_write=False):
        self.writes=0;self.fail_write=fail_write;self.closes=0
    def write(self, value):
        self.writes+=1
        if self.fail_write:raise BrokenPipeError(32,'SYNTHETIC_DEAD_PIPE')
    def flush(self):pass
    def close(self):
        self.closes+=1
        raise OSError(22,'SYNTHETIC_CLOSE_FAILURE')


class Transport:
    def __init__(self,pipe):self.stdin=pipe;self.waits=0
    def poll(self):return None
    def wait(self,**kwargs):self.waits+=1;return 255


def capsule(tmp_path):
    value=Capsule('synthetic-host',tmp_path/'unused-key',tmp_path,tmp_path/'evidence')
    value.ready=True
    return value


def journal(value):
    return json.loads((value.evidence_root/('capsule-'+value.job+'.json')).read_bytes())


def test_dead_pipe_keeps_original_exception_and_journal_without_cleanup_resend(tmp_path):
    value=capsule(tmp_path);pipe=Pipe(True);value.transport=Transport(pipe)
    with pytest.raises(RuntimeError,match='RPC write failed') as caught:
        try:
            value.rpc({'op':'run','stage':'prepare'})
        finally:
            outcome=value.close()
    assert isinstance(caught.value.__cause__,BrokenPipeError)
    assert pipe.writes==1 and pipe.closes==1
    assert outcome['cleanup_status']=='UNKNOWN' and outcome['journal_status']=='PERSISTED'
    assert value.ready is True
    saved=journal(value)
    assert saved['primary_exception_type']=='RuntimeError'
    assert saved['cleanup_status']=='UNKNOWN'
    assert any(r['operation']=='rpc_failure' and r['reason']=='PIPE_WRITE_FAILED' and r['errno']==32 for r in saved['receipts'])
    assert any(r.get('reason')=='NOT_ATTEMPTED_UNCERTAIN_TRANSPORT' for r in saved['receipts'])
    assert any(e['step']=='stdin_close' for e in saved['shutdown_errors'])
    assert not any(r.get('status')=='REMOVED' for r in saved['receipts'])
    value.close()
    assert pipe.writes==1 and pipe.closes==1


@pytest.mark.parametrize('failure',[{'transport_error':'EOF'},{'rpc_error':'SYNTHETIC'}])
def test_terminal_response_is_never_resent(tmp_path,failure):
    value=capsule(tmp_path);pipe=Pipe();value.transport=Transport(pipe)
    value.responses.put(failure)
    with pytest.raises(RuntimeError,match='RPC failed'):value.rpc({'op':'run'})
    with pytest.raises(RuntimeError,match='no blind replay'):value.rpc({'op':'run'})
    value.close()
    assert pipe.writes==1
    assert journal(value)['cleanup_status']=='UNKNOWN'


def test_cleanup_failure_preserves_caller_error_and_never_claims_removed(tmp_path):
    value=capsule(tmp_path)
    calls=[]
    def failed(request,**kwargs):
        calls.append(request)
        raise OSError(22,'SYNTHETIC_CLEANUP_FAILED')
    value.rpc=failed
    with pytest.raises(ValueError,match='PRIMARY_COLLECTION_FAILURE'):
        try:raise ValueError('PRIMARY_COLLECTION_FAILURE')
        finally:value.close()
    saved=journal(value)
    assert calls==[{'op':'cleanup'}]
    assert saved['cleanup_status']=='UNKNOWN'
    assert saved['primary_exception_type']=='ValueError'
    assert saved['receipts'][-1]['status']=='UNKNOWN'


def test_confirmed_cleanup_is_only_removed_case(tmp_path):
    value=capsule(tmp_path);value.rpc=lambda request:{'status':'REMOVED','path':'SYNTHETIC'}
    assert value.close()['cleanup_status']=='REMOVED'
    assert value.ready is False
    assert journal(value)['receipts'][-1]['status']=='REMOVED'


def test_collection_receipt_and_journal_bind_full_context(tmp_path,monkeypatch):
    import rsik.launch as launch_module
    value=capsule(tmp_path)
    plan=tmp_path/'launch.json';plan.write_bytes(canonical({'coverage_collection':{'sha256':'c'*64}}))
    value.launch_context=SimpleNamespace(run_id='SYNTHETIC-COLLECTION',plan_sha256='d'*64,
        plan_path=str(plan),phase='COVERAGE_COLLECTION',registry=str(tmp_path/'unused-registry'))
    inp=tmp_path/'input.json';inp.write_bytes(canonical({'SYNTHETIC':'CASE'}))
    monkeypatch.setattr(Capsule,'evidence_policy',property(lambda self:ExecutionEvidence.synthetic_control()))
    monkeypatch.setattr(launch_module,'validate_launch',lambda *args,**kwargs:{'SYNTHETIC':'NOT_ADMISSION_PROOF'})
    monkeypatch.setattr(launch_module,'collection_tasks',lambda *args:[{'prepare_input_sha256':hashlib.sha256(inp.read_bytes()).hexdigest()}])
    snapshot={'expanded':[],'chunks':[],'files':[],'allowed_kb_ids':[]}
    def rpc(request,**kwargs):
        output=b'RSI_CAPTURE_BASE64='+base64.b64encode(canonical(snapshot))+b'\nRSI_RUNTIME_BASE64='+base64.b64encode(canonical({'SYNTHETIC':'NO_EXECUTION'}))+b'\n'
        return {'output_base64':base64.b64encode(output).decode(),'exit_code':0,'reason':None}
    value.rpc=rpc;value.code_archive_sha256='e'*64
    value.child('prepare',[inp,tmp_path/'snapshot.json'],tmp_path)
    receipt=json.loads((tmp_path/'execution-receipt.json').read_bytes())
    expected={'capsule_job_id':value.job,'sequence':1,'launch_run_id':'SYNTHETIC-COLLECTION',
              'launch_plan_sha256':'d'*64,'coverage_collection_sha256':'c'*64}
    assert len(value.job)==32
    for key,expected_value in expected.items():assert receipt[key]==expected_value
    value._transport_uncertain=True
    value.close()
    saved=journal(value)
    for key,expected_value in expected.items():assert saved[key]==expected_value
    for key,expected_value in expected.items():assert saved['receipts'][0][key]==expected_value

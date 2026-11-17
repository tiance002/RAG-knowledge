"""Host qualification binding in an isolated promotion sandbox only."""
import hashlib
import json
from pathlib import Path

import pytest

from rsik.promotion import PromotionSandbox
from rsik.state import canonical


def setup(tmp_path):
    now=[100.0]
    sandbox=PromotionSandbox(tmp_path/'sandbox',allowed_root=tmp_path,clock=lambda:now[0])
    initial=sandbox.initialize('A',b'A');artifact=b'B';sha=hashlib.sha256(artifact).hexdigest()
    receipt=tmp_path/'qualification.json'
    record={'quality_status':'CONTROL_QUALIFIED','candidate_id':'B','artifact_hash':sha,'evidence_mode':'SYNTHETIC_CONTROL'}
    receipt.write_bytes(canonical(record))
    def validator(path,*,candidate_id,artifact_hash,target_environment):
        value=json.loads(Path(path).read_bytes())
        if value!=record:raise ValueError('SYNTHETIC qualification invalid')
        return {**value,'qualification_id':hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            'environment':target_environment,'eligible_for_production':False}
    approval=sandbox.qualified_approval('B',sha,'A',qualification_path=receipt,
        qualification_validator=validator,rollback_hash=initial['artifact_hash'],ttl=20)
    return sandbox,initial,artifact,receipt,validator,approval,now


def test_qualified_simulation_binds_scope_and_old_approval_cannot_switch_again(tmp_path):
    sandbox,initial,artifact,receipt,validator,approval,now=setup(tmp_path)
    p=approval['payload']
    assert p['qualification']['quality_status']=='CONTROL_QUALIFIED'
    assert p['qualification_sha256']==hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert p['rollback_artifact_hash']==initial['artifact_hash'] and p['target_environment']==sandbox.environment
    assert p['human_authenticated'] is False
    assert sandbox.promote_qualified(approval,artifact,qualification_validator=validator)['production_changed'] is False
    sandbox.rollback(sandbox.approval('A',initial['artifact_hash'],'B',operation='ROLLBACK'))
    before=sandbox.current()
    assert sandbox.promote_qualified(approval,artifact,qualification_validator=validator)['status']=='APPLIED_SUPERSEDED'
    assert sandbox.current()==before and before['deployment_revision']==2


@pytest.mark.parametrize('fault',['changed_qualification','missing_validator','bool_validator','expired','concurrent'])
def test_qualified_approval_rejects_invalid_identity_before_pointer_write(tmp_path,fault):
    sandbox,initial,artifact,receipt,validator,approval,now=setup(tmp_path)
    if fault=='changed_qualification':receipt.write_bytes(b'{}')
    elif fault=='missing_validator':validator=None
    elif fault=='bool_validator':validator=lambda *a,**k:True
    elif fault=='expired':now[0]=121
    elif fault=='concurrent':
        other=sandbox.qualified_approval('B',hashlib.sha256(artifact).hexdigest(),'A',qualification_path=receipt,
            qualification_validator=validator,rollback_hash=initial['artifact_hash'])
        sandbox.promote_qualified(other,artifact,qualification_validator=validator)
    before=sandbox.current()
    with pytest.raises(ValueError):sandbox.promote_qualified(approval,artifact,qualification_validator=validator)
    assert sandbox.current()==before


@pytest.mark.parametrize('after_pointer',[False,True])
def test_qualified_switch_crash_reconciles_without_qualification_bypass(tmp_path,monkeypatch,after_pointer):
    sandbox,initial,artifact,receipt,validator,approval,now=setup(tmp_path)
    if after_pointer:
        original=sandbox.events.append
        def interrupt(key,kind,payload,**kwargs):
            if kind=='PROMOTED':raise SystemExit('SYNTHETIC crash')
            return original(key,kind,payload,**kwargs)
        monkeypatch.setattr(sandbox.events,'append',interrupt)
    else:
        import rsik.promotion as module
        original=module.atomic_write
        def interrupt(path,*args,**kwargs):
            if Path(path).name=='current.json':raise SystemExit('SYNTHETIC crash')
            return original(path,*args,**kwargs)
        monkeypatch.setattr(module,'atomic_write',interrupt)
    with pytest.raises(SystemExit):sandbox.promote_qualified(approval,artifact,qualification_validator=validator)
    monkeypatch.undo()
    expected='PROMOTED' if after_pointer else 'INTENT_NOT_APPLIED'
    assert sandbox.reconcile_switch(approval['signature'])['status']==expected
    assert sandbox.current()['deployment_revision']==(1 if after_pointer else 0)

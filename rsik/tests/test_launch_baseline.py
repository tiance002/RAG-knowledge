"""Host admission controls and A/A root qualification, without real execution."""
from pathlib import Path
import json
import pytest

from rsik.launch import LaunchContext,validate_launch,guard_real
from rsik.execution_evidence import ExecutionEvidence
from rsik.openai_roles import OpenAIRoles
from rsik.budgets import Budget
from rsik.capsule import Capsule
from rsik.redact import LeakScanner
from rsik.state import canonical
from rsik.query_aware import QueryRegistry,adapt_legacy
from rsik.baseline_qualification import qualify_baseline,verify_baseline_qualification
from portable_fixtures import ROOT,SyntheticCapsule,write,build_split
from test_openai_roles import public


def test_preparation_approved_boolean_is_never_a_launch_context(tmp_path):
    with pytest.raises(ValueError,match='TRUSTED_LAUNCH_CONTEXT_REQUIRED'):
        validate_launch({'ready_for_real_run':True,'approved':True})


@pytest.mark.parametrize('mutation',['hash','run','phase','implementation'])
def test_stale_launch_inputs_fail_before_effects(tmp_path,mutation):
    p=tmp_path/'plan.json'
    value={'schema':'rsi-launch/v1','run_id':'run','allowed_phases':['QUERY_ROUND'],'implementation':{},'protected':{}}
    p.write_bytes(canonical(value))
    import hashlib
    sha=hashlib.sha256(p.read_bytes()).hexdigest()
    context=LaunchContext(str(p),'0'*64 if mutation=='hash' else sha,'wrong' if mutation=='run' else 'run',
        'WRONG' if mutation=='phase' else 'QUERY_ROUND',str(tmp_path/'budget'),str(tmp_path/'registry'),str(tmp_path))
    with pytest.raises(ValueError,match='LAUNCH_'):
        validate_launch(context)


def test_real_openai_boundary_rejects_without_any_dispatch_or_charge(tmp_path):
    budget=Budget(tmp_path/'budget.sqlite')
    client=OpenAIRoles(budget,tmp_path/'roles',tmp_path/'NEVER_READ_KEY',LeakScanner())
    with pytest.raises(ValueError,match='TRUSTED_LAUNCH_CONTEXT_REQUIRED'):
        client.diagnose(public())
    assert budget.snapshot()['attempts']==0


def test_real_capsule_setup_rejects_before_rpc(tmp_path,monkeypatch):
    policy=ExecutionEvidence('REAL_REPLAY','1'*64,'2'*64,'image','3'*40,'4'*64)
    monkeypatch.setattr(Capsule,'evidence_policy',property(lambda self:policy))
    capsule=Capsule('NEVER_CONNECT',tmp_path/'no-key',tmp_path,tmp_path/'capsule')
    monkeypatch.setattr(capsule,'rpc',lambda *a,**k:pytest.fail('must not create remote capsule'))
    with pytest.raises(ValueError,match='TRUSTED_LAUNCH_CONTEXT_REQUIRED'):capsule.setup()
    assert capsule.transport is None


def test_top_query_and_autonomous_entries_reject_before_roles_or_capsules(tmp_path):
    from rsik.query_workflow import run_query_round
    from rsik.autonomous import run
    with pytest.raises(ValueError,match='TRUSTED_LAUNCH_CONTEXT_REQUIRED'):
        run_query_round(public={},roles=None,advisory_roles=None,parent={},fixtures=[],registry=None,
            directory=tmp_path/'round',capsule_factory=lambda _:pytest.fail('no capsule'),scanner=LeakScanner())
    with pytest.raises(ValueError,match='TRUSTED_LAUNCH_CONTEXT_REQUIRED'):
        run({'fixtures':[]})
    assert not (tmp_path/'round').exists()


def test_independent_and_legacy_real_entry_reject_before_setup(tmp_path):
    from rsik.query_verification import verify_sealed
    from rsik.holdout import verify_sealed as legacy_sealed
    policy=ExecutionEvidence('REAL_REPLAY','1'*64,'2'*64,'image','3'*40,'4'*64)
    class Never:
        evidence_policy=policy
        def setup(self):pytest.fail('must not setup')
    with pytest.raises(ValueError,match='TRUSTED_LAUNCH_CONTEXT_REQUIRED'):
        verify_sealed(registry=tmp_path,training=tmp_path,train_fixtures=[],sealed_fixtures=[],
            used_holdout=tmp_path,candidate_id='x',trusted_id='y',capsule=Never(),output=tmp_path/'out')
    with pytest.raises(ValueError,match='TRUSTED_LAUNCH_CONTEXT_REQUIRED'):
        legacy_sealed(registry=tmp_path,training=tmp_path,train_fixture=tmp_path,sealed_fixture=tmp_path,
            candidate_id='x',trusted_id='y',lineage_id='l',policy=None,capsule=Never(),output=tmp_path/'out')


class AAControl(SyntheticCapsule):
    def setup(self):pass
    def close(self):pass
    def child(self,stage,paths,cwd,**kwargs):
        if Path(cwd).name=='probes':
            result={'status':'PASS','scope':'SYNTHETIC_NON_EXECUTION'};write(paths[-1],result);return result
        return super().child(stage,paths,cwd,**kwargs)


@pytest.fixture
def qualified_control(tmp_path):
    registry=QueryRegistry(tmp_path/'registry')
    cid=registry.put(adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),b'{"selection":"all"}',
        parent_id='OWNER_ROOT',origin='OWNER_SPECIFIED',evidence={})
    fixtures=[build_split(tmp_path/f'f{i}') for i in range(4)]
    output=tmp_path/'qualification'
    result=qualify_baseline(registry=registry.directory,candidate_id=cid,fixtures=fixtures,capsule=AAControl(),
        output=output,origin='OWNER_SPECIFIED')
    return registry,cid,fixtures,output,result


def test_aa_qualification_has_zero_gain_without_forging_adoption(qualified_control):
    registry,cid,fixtures,output,result=qualified_control
    assert result['status']=='CONTROL_BASELINE_QUALIFIED' and not result['real_eligible']
    assert not result['autonomous_adoption'] and not result['helpful_credit'] and not result['improvement_claim']
    assert all(d['delta_total']==0 and d['verdict']=='REJECTED' and d['reason_codes']==['NO_GAIN'] for d in result['suite_decisions'])
    candidate,receipt=verify_baseline_qualification(registry=registry.directory,receipt=output/'baseline-qualification.json',
        fixtures=fixtures,evidence=ExecutionEvidence.synthetic_control())
    assert candidate['candidate_id']==cid and receipt==result
    assert not (output/'round.json').exists() and not (output/'family-decision.json').exists()


def test_control_cannot_become_real_qualified_by_relabeling(qualified_control):
    registry,cid,fixtures,output,result=qualified_control
    policy=ExecutionEvidence('REAL_REPLAY','1'*64,'2'*64,'image','3'*40,'4'*64)
    with pytest.raises(ValueError,match='BASELINE_QUALIFICATION_INVALID'):
        verify_baseline_qualification(registry=registry.directory,receipt=output/'baseline-qualification.json',fixtures=fixtures,evidence=policy)


def test_generated_child_cannot_self_nominate_as_baseline(tmp_path):
    registry=QueryRegistry(tmp_path/'registry')
    cid=registry.put(adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),b'{"selection":"all"}',
        parent_id='parent',origin='OPENAI_MODEL_PROPOSAL',evidence={})
    with pytest.raises(ValueError,match='ORDINARY_CHILD_CANNOT_SELF_QUALIFY'):
        qualify_baseline(registry=registry.directory,candidate_id=cid,fixtures=[],capsule=AAControl(),
                         output=tmp_path/'out',origin='MIGRATED')


def test_quarantine_remains_binding_for_root_qualification(qualified_control):
    registry,cid,fixtures,output,result=qualified_control
    registry.quarantine(cid,output/'baseline-qualification.json','SYNTHETIC_FACT_REGRESSION')
    with pytest.raises(ValueError,match='QUARANTINED'):
        verify_baseline_qualification(registry=registry.directory,receipt=output/'baseline-qualification.json',fixtures=fixtures,
                                     evidence=ExecutionEvidence.synthetic_control())


@pytest.fixture
def launch_binding_control(tmp_path,monkeypatch):
    """Fabricated admission-parser control; no real data/execute qualification."""
    import rsik.query_workflow as workflow
    import rsik.source_relations as relations
    import rsik.exposure as exposure
    from rsik.launch import file_binding,fixture_identity
    from rsik.query_learning import ExperienceSession
    from rsik.handbook.store import Handbook
    from rsik.foundation import verify_boundary
    from rsik.budgets import PRICE_VERSION
    from rsik.state import digest
    monkeypatch.setattr(workflow,'implementation_hashes',lambda:{'SYNTHETIC':'NOT_REAL_IMPLEMENTATION'})
    matrix={'SYNTHETIC':{'status':'PASS'}}
    # Dedicated contract controls isolate the launch joins; the actual source
    # adapter's positive/negative cases have their own independent tests.
    monkeypatch.setattr(relations,'recompute_coverage',lambda _: {'status':'PASS','matrix':matrix})
    monkeypatch.setattr(relations,'verify_coverage_execution',lambda *args:None,raising=False)
    sources={'SYNTHETIC':{'source_family_id':'SYNTHETIC_FAMILY'}}
    monkeypatch.setattr(exposure,'validate_snapshot',lambda *a,**k:{'sources':sources})
    registry=QueryRegistry(tmp_path/'query'/'registry')
    cid=registry.put(adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),b'{"selection":"all"}',
        parent_id='OWNER_ROOT',origin='OWNER_SPECIFIED',evidence={})
    candidate=registry.get(cid)
    fixture=build_split(tmp_path/'fixture')
    budget=Budget(tmp_path/'task-model-budget.sqlite')
    session=ExperienceSession(tmp_path/'learning',Handbook(tmp_path/'book'))
    policy=ExecutionEvidence('REAL_REPLAY','1'*64,'2'*64,'CONTROL_NOT_EXECUTED','3'*40,'4'*64)
    review=lambda h:{'status':'AI_REVIEWED','reviewer':'SYNTHETIC_CONTROL_NOT_REAL_REVIEW',
                     'reviewed_at':'2026-11-15T00:00:00+00:00','object_sha256':h}
    write(tmp_path/'coverage.json',{'matrix':matrix,'review':review(digest(matrix))})
    write(tmp_path/'families.json',sources);write(tmp_path/'exposure.json',{'SYNTHETIC':True})
    prices={'gpt-6-luna':{'input':'SYNTHETIC_NOT_PRICE_PROOF'}}
    write(tmp_path/'prices.json',{'prices':prices,'review':review(digest(prices)),
        'price_version':PRICE_VERSION,'official_source':'https://openai.com/api/pricing/',
        'valid_until':'2099-01-01T00:00:00+00:00'})
    plan={'schema':'rsi-launch/v1','run_id':'SYNTHETIC-run','allowed_phases':['BASELINE_QUALIFICATION'],
        'implementation':workflow.implementation_hashes(),'protected':verify_boundary(),
        'execution_policy':policy.to_dict(),'registry':str(registry.directory.resolve()),
        'candidate':{'candidate_id':cid,'worker_sha256':__import__('hashlib').sha256(candidate['worker']).hexdigest(),
                     'config_sha256':__import__('hashlib').sha256(candidate['config']).hexdigest()},
        'budget':{'path':str((tmp_path/'task-model-budget.sqlite').resolve()),'price_version':PRICE_VERSION,
            'limit_usd':'5','reserve_usd':'0','event_prefix':{'sequence':0,'event_hash':'0'*64}},
        'price_review':file_binding(tmp_path/'prices.json'),'models':{'diagnose':'gpt-6-luna'},
        'fixtures':{str(fixture.resolve()):fixture_identity(fixture)},'evaluator_profile':'SYNTHETIC',
        'seam_profile':'rsi-query-autonomous-selector/v1','source_families':file_binding(tmp_path/'families.json'),
        'exposure_snapshot':file_binding(tmp_path/'exposure.json'),'coverage':file_binding(tmp_path/'coverage.json'),
        'initial_memory':file_binding(session.initial),'experiment':{'epoch_id':'SYNTHETIC','group':'A',
            'repetition':1,'final_nomination_limit':1,'final_holdout_uses':1,'analysis_points':['SYNTHETIC'],
            'stop_rules':['EVIDENCE_UNKNOWN']}}
    path=tmp_path/'plan.json';write(path,plan)
    context=LaunchContext(str(path),__import__('hashlib').sha256(path.read_bytes()).hexdigest(),
        'SYNTHETIC-run','BASELINE_QUALIFICATION',str(tmp_path/'task-model-budget.sqlite'),
        str(registry.directory),str(tmp_path),session)
    return context,policy,fixture,candidate,plan


def test_complete_launch_binding_control_validates_without_effects(launch_binding_control):
    context,policy,fixture,candidate,_=launch_binding_control
    result=validate_launch(context,evidence=policy,fixtures=[fixture],candidate=candidate)
    assert result['status']=='PASS'
    assert Budget(context.budget_ledger).snapshot()['attempts']==0


@pytest.mark.parametrize('fault',['model','fixture','budget-prefix','coverage','family','memory'])
def test_bound_launch_control_rejects_wrong_identities(launch_binding_control,fault):
    from dataclasses import replace
    from rsik.launch import file_binding
    from rsik.state import digest
    context,policy,fixture,candidate,plan=launch_binding_control
    path=Path(context.plan_path)
    if fault=='model':plan['models']['diagnose']='gpt-6-astra'
    elif fault=='fixture':plan['fixtures'][str(fixture.resolve())]['cases.json']='0'*64
    elif fault=='budget-prefix':plan['budget']['event_prefix']['sequence']=1
    elif fault=='coverage':
        cov=Path(plan['coverage']['path']);value=json.loads(cov.read_bytes());value['matrix']={'forged':'PASS'}
        value['review']['object_sha256']=digest(value['matrix']);write(cov,value);plan['coverage']=file_binding(cov)
    elif fault=='family':
        fam=Path(plan['source_families']['path']);write(fam,{'WRONG':True});plan['source_families']=file_binding(fam)
    elif fault=='memory':
        plan['initial_memory']=file_binding(Path(context.session.initial).parent/'learning-frontier.json')
    write(path,plan);context=replace(context,plan_sha256=__import__('hashlib').sha256(path.read_bytes()).hexdigest())
    with pytest.raises(ValueError,match='LAUNCH_'):
        validate_launch(context,evidence=policy,fixtures=[fixture],candidate=candidate)

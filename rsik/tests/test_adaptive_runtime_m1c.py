"""No-network runtime integration for evidence-bound adaptive assignments."""
from copy import deepcopy
import json
import pytest
from rsik.adjudicate import SLOTS
from rsik.adaptive_dispatch import build_dispatch_plan, DispatchUnavailable
from rsik.contracts import ADAPTIVE_DIAGNOSIS_VERSION, ContractError
from rsik.errors import StageFailure
from rsik.observability import Observer
from rsik.role_profiles import M1_ADAPTIVE_V1, roles_for
from rsik.workflow import execute_round, prepare_dispatch
from rsik.tests.test_workflow import FakeRoles, public_context, callbacks
from rsik.tests.test_openai_roles import response
from rsik.openai_roles import OpenAIRoles, runtime_profile_identity
from rsik.budgets import Budget
from rsik.redact import LeakScanner


def diagnosis(public):
    result = FakeRoles().diagnose(public)['result']
    result.update(schema_version=ADAPTIVE_DIAGNOSIS_VERSION, task_signals=[{
        'task_type': 'BUDGET', 'disposition': 'SUPPORT', 'evidence_refs': result['evidence_refs']}])
    return result


class AdaptiveRoles(FakeRoles):
    def __init__(self, mode='OK'):
        super().__init__(same_patch=True)
        self.mode, self.assignments, self.order = mode, {}, []
    def diagnose(self, public):
        self.order.append('diagnose')
        if self.mode == 'ABSTAIN':
            return {'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC'}
        result = diagnosis(public)
        if self.mode == 'NONFIXABLE': result['fixable_within_seam'] = False
        return {'status': 'OK', 'result': result, 'reason': None}
    def propose(self, role, public, diagnosis, *, assignment):
        assert self.order[0] == 'diagnose'
        self.order.append(role)
        slot = next(s for s, r in roles_for(M1_ADAPTIVE_V1).items() if r == role)
        assert assignment['slot'] == slot
        self.assignments[slot] = deepcopy(assignment)
        # Mutation of this isolated context must not reach peers or allocation.
        assignment['forbidden_actions'].append('LOCAL_ONLY')
        result = super().propose(slot, public, diagnosis)
        result['result']['angle'] = role
        return result


def test_structured_diagnosis_precedes_independent_assignment_and_unchanged_gates(tmp_path):
    public = public_context(); roles = AdaptiveRoles()
    review, replay, reviewed, replayed = callbacks()
    result = execute_round(public, roles, review, replay, role_profile=M1_ADAPTIVE_V1,
                           dispatch_directory=tmp_path)
    assert len(roles.assignments) == 3
    assert {a['task_type'] for a in roles.assignments.values()} == {'BUDGET'}
    assert reviewed == replayed == list(SLOTS)
    assert result['family_size'] == 3
    durable = json.loads((tmp_path/'dispatch-plan.json').read_bytes())
    assert durable == result['dispatch_plan']
    assert all('LOCAL_ONLY' not in a['forbidden_actions'] for a in durable['assignments'])
    events = Observer(tmp_path/'observations.sqlite').log.read()
    assert sum(e['event_type'] == 'DISPATCH_SLOT_ASSIGNED' for e in events) == 3


@pytest.mark.parametrize('mode', ['ABSTAIN', 'NONFIXABLE'])
def test_no_admissible_diagnosis_retains_three_abstain_family_slots(tmp_path, mode):
    roles = AdaptiveRoles(mode)
    def forbidden(*_): pytest.fail('ABSTAIN may not trigger candidate effects')
    result = execute_round(public_context(), roles, forbidden, forbidden,
        role_profile=M1_ADAPTIVE_V1, dispatch_directory=tmp_path)
    assert roles.order == ['diagnose'] and not roles.assignments
    assert result['family_size'] == 3 and len(result['candidate_decisions']) == 3
    assert set(result['agent_call_results']) == set(SLOTS)
    assert {a['task_type'] for a in result['dispatch_plan']['assignments']} == {'ABSTAIN'}


def test_durable_plan_reuse_does_not_emit_duplicate_assignment_effects(tmp_path, monkeypatch):
    public = public_context(); d = diagnosis(public); observer = Observer(tmp_path/'obs.sqlite')
    first = prepare_dispatch(public, d, directory=tmp_path, observer=observer)
    import rsik.adaptive_dispatch as dispatch
    # Validation reproduces the pure decision, but there is no second write/event.
    assert prepare_dispatch(public, d, directory=tmp_path, observer=observer) == first
    assert len(observer.log.read()) == 4
    changed = deepcopy(d); changed['hypothesis'] += ' changed'
    with pytest.raises(ContractError): prepare_dispatch(public, changed, directory=tmp_path, observer=observer)
    stored = json.loads((tmp_path/'dispatch-plan.json').read_bytes())
    stored['assignments'][0]['task_type'] = 'GENERALIST'
    (tmp_path/'dispatch-plan.json').write_text(json.dumps(stored))
    with pytest.raises(ContractError): prepare_dispatch(public, d, directory=tmp_path, observer=observer)


def test_typed_dispatch_outage_explicit_frozen_fallback_only(tmp_path, monkeypatch):
    public = public_context(); d = diagnosis(public); observer = Observer(tmp_path/'obs.sqlite')
    import rsik.adaptive_dispatch as dispatch
    original = dispatch.build_dispatch_plan
    monkeypatch.setattr(dispatch, 'build_dispatch_plan', lambda *_: (_ for _ in ()).throw(DispatchUnavailable()))
    plan = prepare_dispatch(public, d, directory=tmp_path, observer=observer)
    assert plan['mode'] == 'FROZEN_GENERALIST_FALLBACK'
    assert plan['fallback_profile'] == 'M1_ADAPTIVE_FROZEN_GENERALIST_FALLBACK_V1'
    assert {a['task_type'] for a in plan['assignments']} == {'GENERALIST'}
    assert sum(e['event_type'] == 'DISPATCH_FALLBACK' for e in observer.log.read()) == 1
    monkeypatch.setattr(dispatch, 'build_dispatch_plan', original)
    assert prepare_dispatch(public, d, directory=tmp_path, observer=observer) == plan


@pytest.mark.parametrize('error', [ContractError('DISPATCH_PLAN_INVALID'), OSError('disk'),
    StageFailure('RECOVERY_ERROR','CHECKPOINT_CHANGED'), ValueError('security')])
def test_contract_storage_recovery_and_security_failures_never_fallback(tmp_path, monkeypatch, error):
    import rsik.adaptive_dispatch as dispatch
    monkeypatch.setattr(dispatch, 'build_dispatch_plan', lambda *_: (_ for _ in ()).throw(error))
    with pytest.raises(type(error)):
        prepare_dispatch(public_context(), diagnosis(public_context()), directory=tmp_path)
    assert not (tmp_path/'dispatch-plan.json').exists()


def test_mock_model_sees_only_own_assignment_and_resumes_without_transport(tmp_path):
    public = public_context(); d = diagnosis(public); seen = []
    def transport(body, *_):
        seen.append(deepcopy(body))
        return response({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC'})
    client = OpenAIRoles(Budget(tmp_path/'budget.sqlite'), tmp_path/'roles', tmp_path/'unused',
                        LeakScanner(), transport=transport, role_profile=M1_ADAPTIVE_V1)
    plan = build_dispatch_plan(public, d)
    for assignment in plan['assignments']:
        role = roles_for(M1_ADAPTIVE_V1)[assignment['slot']]
        client.propose(role, public, d, assignment=assignment)
        client.propose(role, public, d, assignment=assignment)
    assert len(seen) == 3 and client.budget.snapshot()['attempts'] == 3
    for body, assignment in zip(seen, plan['assignments'], strict=True):
        context = json.loads(body['input'][1]['content'])
        assert context['assignment'] == assignment
        assert 'assignments' not in context and 'dispatch_plan' not in context
    binding = runtime_profile_identity(M1_ADAPTIVE_V1, {}, 'legacy')
    assert 'dispatcher' in binding
    with pytest.raises(ContractError): client.propose('P1_GENERALIST', public, d)
    with pytest.raises(ContractError): client.propose('P2_GENERALIST', public, d, assignment=plan['assignments'][0])
    assert len(seen) == 3


def test_persistent_dispatch_outage_still_sends_frozen_neutral_prompt(tmp_path, monkeypatch):
    import rsik.adaptive_dispatch as dispatch
    from rsik.openai_roles import validate_runtime_profile_identity
    public = public_context(); d = diagnosis(public); seen = []
    monkeypatch.setattr(dispatch, 'build_dispatch_plan', lambda *_: (_ for _ in ()).throw(DispatchUnavailable()))
    plan = prepare_dispatch(public, d, directory=tmp_path, observer=Observer(tmp_path/'obs.sqlite'))
    client = OpenAIRoles(Budget(tmp_path/'budget.sqlite'), tmp_path/'roles', tmp_path/'unused',
        LeakScanner(), transport=lambda body, *_: (seen.append(deepcopy(body)) or response(
            {'status':'ABSTAIN','result':None,'reason':'SYNTHETIC'})), role_profile=M1_ADAPTIVE_V1)
    for assignment in plan['assignments']:
        role = roles_for(M1_ADAPTIVE_V1)[assignment['slot']]
        client.propose(role, public, d, assignment=assignment)
        assert seen[-1]['input'][0]['content'] == client.runtime_identity['fallback']['roles'][role]['system_prompt']
        assert seen[-1]['text']['format']['schema'] == client.runtime_identity['fallback']['roles'][role]['response_schema']
    assert len(seen) == 3
    changed = deepcopy(client.runtime_identity)
    changed['fallback']['roles']['P1_GENERALIST']['system_prompt'] += ' changed'
    with pytest.raises(ValueError, match='M1_RUNTIME_PROFILE_IDENTITY_CHANGED'):
        validate_runtime_profile_identity(changed, M1_ADAPTIVE_V1, {}, 'legacy')


def test_plan_write_failure_does_not_call_proposers_or_fallback(tmp_path, monkeypatch):
    import rsik.locking as locking
    roles = AdaptiveRoles()
    monkeypatch.setattr(locking, 'atomic_write', lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError):
        execute_round(public_context(), roles, lambda *_: None, lambda *_: None,
            role_profile=M1_ADAPTIVE_V1, dispatch_directory=tmp_path)
    assert roles.order == ['diagnose'] and not roles.assignments

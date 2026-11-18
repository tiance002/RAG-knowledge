"""Synthetic-only M1 runtime identities and isolated fixed-slot proposals."""
from copy import deepcopy
import pytest
from rsik.role_profiles import (M1_PROFILES, M1_SPECIALIZED_V1, M1_INDEPENDENT_GENERALIST_V1,
    M1_GENERALIST_TASK, m1_tasks, roles_for, profile_manifest, LEGACY, DEPENDENCY)
from rsik.openai_roles import (OpenAIRoles, runtime_profile_identity, validate_runtime_profile_identity,
                              effective_role_prompt, decode_settings)
from rsik.contracts import ContractError, role_schema
from rsik.budgets import Budget
from rsik.redact import LeakScanner
from rsik.tests.test_openai_roles import public, response
from rsik.tests.test_workflow import FakeRoles, public_context, callbacks
from rsik.workflow import execute_round
from rsik.adjudicate import SLOTS

@pytest.mark.parametrize('profile', M1_PROFILES)
def test_six_effective_prompts_and_decode_are_bound_at_mock_send(tmp_path, profile):
    seen = []
    def transport(body, *_):
        seen.append(body)
        return response({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC'})
    client = OpenAIRoles(Budget(tmp_path/'budget.sqlite'),tmp_path/'roles',tmp_path/'unused',
                        LeakScanner(),transport=transport,role_profile=profile,seam_profile='query-autonomous-v1')
    binding = runtime_profile_identity(profile, {}, 'query-autonomous-v1')
    context = public(); context['experiment_intent']='STRUCTURAL_POLICY_SEARCH'
    roles = ('diagnose',) + tuple(roles_for(profile).values()) + ('curator','meta')
    for role in roles:
        # Read-only projections deliberately contain no search intent/history.
        supplied = public() if role in ('curator','meta') else context
        assert client.call(role, supplied)['status']=='ABSTAIN'
        body = seen[-1]; spec = binding['roles'][role]
        assert body['input'][0]['content'] == spec['system_prompt']
        assert {k:body[k] for k in spec['decode']} == spec['decode']
        assert body['text']['format']['schema'] == spec['response_schema']
    assert len(seen)==6 and client.budget.snapshot()['attempts']==6
    assert len(binding['models'])==6
    assert 'longest' not in binding['roles']['P2_BUDGET' if profile==M1_SPECIALIZED_V1 else 'P2_GENERALIST']['system_prompt'].lower()

@pytest.mark.parametrize('field', ['policy','task','system_prompt','decode','model','response_schema'])
def test_runtime_identity_rejects_effective_spec_tampering(field):
    p=M1_SPECIALIZED_V1; binding=runtime_profile_identity(p,{},'query-autonomous-v1')
    binding['roles']['P2_BUDGET'][field]='tampered'
    with pytest.raises(ValueError,match='M1_RUNTIME_PROFILE_IDENTITY_CHANGED'):
        validate_runtime_profile_identity(binding,p,{},'query-autonomous-v1')

@pytest.mark.parametrize('field', ['seam_profile','implementation','models','sha256'])
def test_runtime_identity_rejects_outer_tampering(field):
    p=M1_SPECIALIZED_V1; binding=runtime_profile_identity(p,{},'query-autonomous-v1');binding[field]='tampered'
    with pytest.raises(ValueError,match='M1_RUNTIME_PROFILE_IDENTITY_CHANGED'):
        validate_runtime_profile_identity(binding,p,{},'query-autonomous-v1')

def test_generalists_have_distinct_identities_same_neutral_task():
    tasks=m1_tasks(M1_INDEPENDENT_GENERALIST_V1)
    roles=tuple(roles_for(M1_INDEPENDENT_GENERALIST_V1).values())
    assert len(set(roles))==3 and {tasks[r] for r in roles}=={M1_GENERALIST_TASK}
    assert len({str(role_schema(r)) for r in roles}) == 3

@pytest.mark.parametrize('profile', M1_PROFILES)
def test_all_three_instances_use_same_review_replay_gates(profile):
    mapping=roles_for(profile)
    class Roles(FakeRoles):
        def propose(self, role, context, diagnosis):
            slot=next(s for s,r in mapping.items() if r==role)
            value=super().propose(slot,context,diagnosis)
            value['result']['angle']=role
            return value
    roles=Roles(same_patch=True,barrier=True)
    review,replay,reviewed,replayed=callbacks()
    outcome=execute_round(public_context(),roles,review,replay,role_profile=profile)
    assert reviewed==replayed==list(SLOTS)
    assert len(outcome['qualified'])==3 and outcome['family_size']==3
    assert {v['angle'] for v in outcome['raw_role_proposals'].values()}==set(mapping.values())
    assert {v['angle'] for v in outcome['proposal_instances'].values()}==set(SLOTS)

def test_old_manifest_shape_unchanged():
    assert set(profile_manifest(LEGACY))==set(profile_manifest(DEPENDENCY))=={'version','statistical_slots','strategy_sha256'}
    assert roles_for(LEGACY)['P3_ORDER']=='P3_ORDER'
    assert roles_for(DEPENDENCY)['P3_ORDER']=='P3_DEPENDENCY'


def test_client_frozen_model_mutation_stops_before_mock_dispatch(tmp_path):
    client=OpenAIRoles(Budget(tmp_path/'budget.sqlite'),tmp_path/'roles',tmp_path/'unused',LeakScanner(),
        transport=lambda *_:pytest.fail('must not send'),role_profile=M1_SPECIALIZED_V1,seam_profile='query-autonomous-v1')
    client.models['diagnose']='gpt-6.1-sol'
    with pytest.raises(ValueError,match='M1_RUNTIME_PROFILE_IDENTITY_CHANGED'):
        client.diagnose(public())
    assert client.budget.snapshot()['attempts']==0

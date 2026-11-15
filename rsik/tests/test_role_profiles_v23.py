"""Synthetic role/request controls; no remote model calls."""
import json
from copy import deepcopy
import pytest
from rsik.role_profiles import DEPENDENCY, LEGACY, profile_manifest, roles_for
from rsik.workflow import execute_round
from rsik.tests.test_workflow import FakeRoles, callbacks, public_context
from rsik.tests.test_openai_roles import response
from rsik.tests.test_autonomy_boundary import public
from rsik.openai_roles import OpenAIRoles
from rsik.budgets import Budget
from rsik.redact import LeakScanner


def test_three_independent_actual_roles_bound_to_fixed_slots():
    class NewRoles(FakeRoles):
        def propose(self, angle, context, diagnosis):
            # The old synthetic helper understands only the old slot labels.
            slot = 'P3_ORDER' if angle == 'P3_DEPENDENCY' else angle
            raw = super().propose(slot, context, diagnosis)
            raw['result']['angle'] = angle
            raw['result']['proposal_id'] = 'new-' + angle
            return raw
    roles = NewRoles(same_patch=True, barrier=True)
    review, replay, reviewed, replayed = callbacks()
    result = execute_round(public_context(), roles, review, replay, role_profile=DEPENDENCY)
    assert result['proposal_roles'] == roles_for(DEPENDENCY)
    assert result['raw_role_proposals']['P3_ORDER']['angle'] == 'P3_DEPENDENCY'
    assert result['proposal_instances']['P3_ORDER']['angle'] == 'P3_ORDER'
    assert len(result['qualified']) == len(result['attempted_replays']) == 3
    assert result['family_size'] == 3
    assert roles_for(LEGACY)['P3_ORDER'] == 'P3_ORDER'


def test_actual_dependency_prompt_and_strategy_receipt(tmp_path):
    sent = []
    def transport(body, *_):
        sent.append(deepcopy(body))
        return response({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC'})
    client = OpenAIRoles(Budget(tmp_path/'budget.sqlite'), tmp_path/'roles', tmp_path/'unused',
        LeakScanner(), transport=transport, seam_profile='query-autonomous-v1', role_profile=DEPENDENCY)
    value = public()
    client.propose('P3_DEPENDENCY', value, None)
    policy = sent[0]['input'][0]['content']
    assert 'Cross-member dependencies' in policy and 'shifted seeds' in policy
    assert 'Ordering only, with invariant selected-member set' not in policy
    assert 'Preserve original relative order' in policy
    schema = sent[0]['text']['format']['schema']
    assert 'P3_DEPENDENCY' in json.dumps(schema)
    receipt = json.loads(next((tmp_path/'roles').glob('*.strategy.json')).read_text())
    assert receipt['role_profile'] == profile_manifest(DEPENDENCY)
    assert receipt['role'] == 'P3_DEPENDENCY'
    assert receipt['handbook_entry_ids'] == [] and len(receipt['prompt_sha256']) == 64


def test_unknown_profile_does_not_issue_model_request(tmp_path):
    with pytest.raises(ValueError, match='UNKNOWN_ROLE_PROFILE'):
        OpenAIRoles(Budget(tmp_path/'budget.sqlite'), tmp_path/'roles', tmp_path/'unused',
                    LeakScanner(),role_profile='unversioned-change')

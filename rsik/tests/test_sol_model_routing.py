"""Offline compatibility checks for the current Sol provider route."""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))

import rsi_quality_loop
import rsi_roles

from rsik.autonomous import proposer_model
from rsik.budgets import Budget, PRICE_VERSION, PRICES
from rsik.contracts import ContractError
from rsik.events import digest
from rsik.openai_roles import OpenAIRoles, decode_settings, runtime_profile_identity
from rsik.redact import LeakScanner
from rsik.role_profiles import M1_SPECIALIZED_V1
from rsik.tests.test_openai_roles import public, response


def test_6_2_sol_medium_route_keeps_historical_6_1_effort_and_prices():
    assert decode_settings('P2_BUDGET', 'gpt-6.2-sol')['reasoning']['effort'] == 'medium'
    assert decode_settings('P2_BUDGET', 'gpt-6.1-sol')['reasoning']['effort'] == 'low'
    assert PRICES['gpt-6.1-sol'] == {'input': 2000, 'cached': 100, 'write': 2500, 'output': 10000}
    assert PRICES['gpt-6.2-sol'] == {'input': 2000, 'cached': 100, 'write': 2500, 'output': 10000}
    assert PRICE_VERSION == 'official-model-pages-2026-11-17-standard-short-context'


def test_frozen_runtime_identity_accepts_6_2_sol_with_medium_decode():
    identity = runtime_profile_identity(
        M1_SPECIALIZED_V1,
        {'P2_BUDGET': 'gpt-6.2-sol'},
        'query-autonomous-v1',
    )

    assert identity['models']['P2_BUDGET'] == 'gpt-6.2-sol'
    assert identity['roles']['P2_BUDGET']['decode']['reasoning']['effort'] == 'medium'


def test_direct_response_role_tables_route_analysis_to_sol_medium_and_implementation_to_luna_max():
    expected = {'analysis': ('gpt-6.2-sol', 'medium'), 'implementation': ('gpt-6-luna', 'max')}
    for role, model_effort in expected.items():
        model, effort = rsi_roles.ROLES[role]
        cfg = {
            'roles': {role: {'model': model, 'reasoning_effort': effort}},
            'budget': {'max_output_tokens': 128, 'max_input_bytes': 4096},
        }
        request = rsi_roles.request_for(role, {'fixture': 'offline'}, cfg)

        assert (model, effort) == model_effort
        assert request['model'] == model_effort[0]
        assert request['reasoning']['effort'] == model_effort[1]

    cfg = {'budget': {'max_output_tokens': 128, 'max_input_bytes': 4096}}
    for role, model_effort in expected.items():
        request = rsi_quality_loop.role_request(role, {'fixture': 'offline'}, cfg)
        assert rsi_quality_loop.ROLE_MODELS[role] == model_effort
        assert request['model'] == model_effort[0]
        assert request['reasoning']['effort'] == model_effort[1]


def test_dev_decode_overrides_are_bounded_hashed_settings_and_omit_luna_temperature(tmp_path):
    models = {role: 'gpt-6-luna' for role in
              ('diagnose', 'P1_DEDUP', 'P2_BUDGET', 'P3_DEPENDENCY', 'curator', 'meta')}
    decode_overrides = {
        'P1_DEDUP': {'reasoning_effort': 'max', 'max_output_tokens': 4096},
        'P2_BUDGET': {'reasoning_effort': 'max', 'max_output_tokens': 4096},
        'P3_DEPENDENCY': {'reasoning_effort': 'max', 'max_output_tokens': 4096},
        'diagnose': {'max_output_tokens': 1024},
        'curator': {'max_output_tokens': 1024},
        'meta': {'max_output_tokens': 1024},
    }
    client = OpenAIRoles(None, tmp_path / 'roles', tmp_path / 'unused-key', None,
                         models=models, decode_overrides=decode_overrides, max_attempts=1,
                         service_tier='default',
                         role_profile=M1_SPECIALIZED_V1, seam_profile='query-autonomous-v1')

    proposer_decode = client.runtime_identity['roles']['P1_DEDUP']['decode']
    diagnose_decode = client.runtime_identity['roles']['diagnose']['decode']
    assert proposer_decode == {'reasoning': {'effort': 'max'}, 'max_output_tokens': 4096}
    assert diagnose_decode == {'reasoning': {'effort': 'none'}, 'max_output_tokens': 1024, 'temperature': 0}
    assert client.runtime_identity['service_tier'] == 'default'
    assert decode_settings('P1_DEDUP', 'gpt-6-luna') == {
        'reasoning': {'effort': 'none'}, 'max_output_tokens': 3072, 'temperature': 0}


def test_dev_decode_overrides_cannot_be_used_with_formal_launch_or_unbounded_values(tmp_path):
    with pytest.raises(ValueError, match='DEV_DECODE_OVERRIDES_NOT_ALLOWED_WITH_FORMAL_LAUNCH'):
        OpenAIRoles(None, tmp_path / 'formal', tmp_path / 'unused-key', None,
                    decode_overrides={'P1_DEDUP': {'reasoning_effort': 'max'}},
                    launch=object())

    with pytest.raises(ValueError, match='MODEL_ATTEMPT_LIMIT_NOT_ALLOWED'):
        OpenAIRoles(None, tmp_path / 'attempts', tmp_path / 'unused-key', None, max_attempts=3)

    with pytest.raises(ValueError, match='MODEL_DECODE_OVERRIDE_NOT_ALLOWED'):
        OpenAIRoles(None, tmp_path / 'unknown', tmp_path / 'unused-key', None,
                    decode_overrides={'P1_DEDUP': {'max_output_tokens': 8192}})


def test_default_service_tier_and_pre_dispatch_guard_are_in_the_hashed_request(tmp_path):
    seen = []
    guarded = []

    def transport(body, *_):
        seen.append(body)
        return response({'status': 'ABSTAIN', 'result': None, 'reason': 'SYNTHETIC'})

    def guard(body, role):
        guarded.append((body, role))
        assert role == 'meta'
        assert body['model'] == 'gpt-6-luna'
        assert body['service_tier'] == 'default'
        assert body['reasoning'] == {'effort': 'none'}
        assert body['temperature'] == 0

    client = OpenAIRoles(Budget(tmp_path / 'budget.sqlite'), tmp_path / 'roles', tmp_path / 'unused-key', LeakScanner(),
                         transport=transport, models={'meta': 'gpt-6-luna'},
                         decode_overrides={'meta': {'max_output_tokens': 1024}},
                         max_attempts=1, service_tier='default', request_guard=guard,
                         role_profile=M1_SPECIALIZED_V1)

    assert client.call('meta', public())['status'] == 'ABSTAIN'
    assert len(guarded) == len(seen) == 1
    assert seen[0]['service_tier'] == 'default'
    assert seen[0]['max_output_tokens'] == 1024
    request_files = list((tmp_path / 'roles').glob('*.request.json'))
    assert len(request_files) == 1
    import json
    request = json.loads(request_files[0].read_bytes())
    assert request['service_tier'] == 'default'
    assert request['max_output_tokens'] == 1024


def test_pre_dispatch_guard_mutation_is_rejected_before_any_charge_or_send(tmp_path):
    sent = []

    def guard(body, role):
        body['model'] = 'gpt-6-astra'

    client = OpenAIRoles(Budget(tmp_path / 'budget.sqlite'), tmp_path / 'roles', tmp_path / 'unused-key', LeakScanner(),
                         transport=lambda body, *_: sent.append(body), request_guard=guard,
                         role_profile=M1_SPECIALIZED_V1)

    with pytest.raises(ContractError, match='REQUEST_GUARD_MUTATED_BODY'):
        client.call('meta', public())
    assert not sent
    assert client.budget.snapshot()['attempts'] == 0


def test_single_attempt_limit_does_not_issue_a_format_repair(tmp_path):
    seen = []

    def transport(body, *_):
        seen.append(body)
        return response({'wrong': 'schema'})

    client = OpenAIRoles(Budget(tmp_path / 'budget.sqlite'), tmp_path / 'roles', tmp_path / 'unused-key', LeakScanner(),
                         transport=transport, max_attempts=1, role_profile=M1_SPECIALIZED_V1)

    result = client.call('meta', public())
    assert result['reason'] == 'CONTRACT_ERROR_NO_REPAIR'
    assert len(seen) == client.budget.snapshot()['attempts'] == 1


def test_autonomous_sol_escalation_uses_6_2_only_after_two_coding_failures():
    assert proposer_model([]) == 'gpt-6-luna'
    assert proposer_model([
        {'outcome': 'PATCH_INVALID'},
        {'outcome': 'UNIT_FAILED'},
    ]) == 'gpt-6.2-sol'
    assert proposer_model([
        {'outcome': 'PATCH_INVALID'},
        {'outcome': 'NO_GAIN'},
    ]) == 'gpt-6-luna'


def test_budget_ledger_prices_6_2_usage_without_changing_old_model_rate(tmp_path):
    budget = Budget(tmp_path / 'task-model-budget.sqlite')
    request = {'model': 'gpt-6.2-sol', 'max_output_tokens': 100, 'input': 'offline'}
    budget.start('sol-62', 'P2_BUDGET', request['model'], digest(request))

    snapshot = budget.complete('sol-62', {'input_tokens': 100, 'output_tokens': 25}, status='RECEIVED')

    event = budget.log.read()[-1]
    assert snapshot['recorded_cost_nano_upper'] == 500_000
    assert event['payload']['model'] == 'gpt-6.2-sol'
    assert event['payload']['price_version'] == PRICE_VERSION

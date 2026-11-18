"""Offline controls for provider stops, total-output budgets and strategy isolation."""
import json
from copy import deepcopy

import pytest

from rsik.adjudicate import SLOTS
from rsik.autonomous import remember_round
from rsik.budgets import Budget, PRICES
from rsik.errors import StageFailure
from rsik.openai_roles import OpenAIRoles, decode_settings, runtime_profile_identity, validate_runtime_profile_identity
from rsik.redact import LeakScanner
from rsik.response_audit import inspect
from rsik.role_profiles import M1_SPECIALIZED_V1
from rsik.state import digest
from rsik.tests.test_openai_roles import public, response
from rsik.tests.test_workflow import FakeRoles, callbacks
from rsik.token_budget import LEGACY, TUNING, plan_truncated_recovery, request_cost_upper, role_limit
from rsik.workflow import execute_round


def truncated(visible_text=None):
    output = [{'type': 'reasoning'}]
    if visible_text is not None:
        output.extend(response({})['response']['output'])
        output[-1]['content'][0]['text'] = visible_text
    receipt = {'transport_status': 'RECEIVED', 'http_status': 200, 'provider_request_id': 'test-truncation',
               'response': {'model': 'gpt-6-luna', 'status': 'incomplete', 'output': output,
                            'incomplete_details': {'reason': 'max_output_tokens'},
                            'usage': {'input_tokens': 100, 'output_tokens': 4096,
                                      'output_tokens_details': {'reasoning_tokens': 4096 if visible_text is None else 4000}}}}
    return receipt


@pytest.mark.parametrize('text', [None, '{"status":"OK","result":{"worker_patch":'])
def test_confirmed_total_output_limit_without_full_envelope(text):
    audit, value = inspect(truncated(text))
    assert audit['output_classification'] == 'OUTPUT_TRUNCATED'
    assert audit['provider_finish_reason'] is None
    assert audit['provider_incomplete_reason'] == 'max_output_tokens'
    assert audit['reasoning_tokens'] == (4096 if text is None else 4000)
    assert (audit['visible_output_tokens'], audit['visible_output_tokens_upper']) == (
        (0, 0) if text is None else (None, 96))
    assert not audit['structured_json_complete'] and value is None


def test_chat_length_is_truncation_even_when_partial_json_is_parseable():
    audit, value = inspect({'transport_status': 'RECEIVED', 'response': {
        'choices': [{'finish_reason': 'length', 'message': {'content': '{"status":"OK"}'}}],
        'usage': {'prompt_tokens': 4, 'completion_tokens': 2}}})
    assert value == {'status': 'OK'}
    assert audit['output_classification'] == 'OUTPUT_TRUNCATED'
    assert audit['reasoning_tokens'] is None and audit['visible_output_tokens'] is None


def test_full_provider_response_without_reasoning_usage_keeps_visible_unknown():
    value = {'status': 'OK', 'result': {'worker_patch': {'old': 'a', 'new': 'b'}}}
    audit, parsed = inspect(response(value))
    assert parsed == value and audit['output_classification'] == 'COMPLETE'
    assert audit['reasoning_tokens'] is None and audit['visible_output_tokens_upper'] is None


@pytest.mark.parametrize(('receipt','expected'), [
    ({'transport_status': 'HTTP_ERROR'}, 'API_ERROR'),
    ({'transport_status': 'TIMEOUT'}, 'TIMEOUT'),
    ({'transport_status': 'UNKNOWN'}, 'UNKNOWN'),
    ({'transport_status': 'RECEIVED', 'response': {'status': 'incomplete'}}, 'UNKNOWN'),
    ({'transport_status': 'RECEIVED', 'response': {'status': 'completed', 'output': []}}, 'MODEL_STOPPED'),
])
def test_other_provider_outcomes_are_not_reported_as_truncations(receipt, expected):
    assert inspect(receipt)[0]['output_classification'] == expected


def client(tmp_path, transport, **kwargs):
    return OpenAIRoles(Budget(tmp_path/'budget.sqlite'), tmp_path/'roles', tmp_path/'unused',
                       LeakScanner(), transport=transport, **kwargs)


def test_billed_truncation_does_not_resend_or_enter_static_review(tmp_path):
    fake, seen = FakeRoles(), []
    ctx = public()
    def transport(body, *_):
        seen.append(body)
        schema = body['text']['format']['schema']
        first_slot = 'P1_DEDUP' in json.dumps(schema)
        if 'worker_patch' not in json.dumps(schema):
            return response(fake.diagnose(ctx))
        return response(fake.propose('P1_DEDUP', ctx, None)) if first_slot else truncated()
    runner = client(tmp_path, transport)
    review, replay, reviewed, replayed = callbacks()
    result = execute_round(ctx, runner, review, replay)
    assert len(seen) == 4 and runner.budget.snapshot()['completed'] == 4
    assert reviewed == replayed == ['P1_DEDUP']
    assert len(result['candidate_decisions']) == len(SLOTS)
    assert result['agent_call_results']['P2_BUDGET'] == result['agent_call_results']['P3_ORDER'] == 'OUTPUT_TRUNCATED'
    assert {x['verdict'] for x in result['candidate_decisions'] if x['angle'] != 'P1_DEDUP'} == {'INCONCLUSIVE'}
    terminal = [e['payload'] for e in runner.observer.log.read() if e['event_type'] == 'INVOCATION_FINISHED']
    truncated_terminal = [e for e in terminal if e['outcome']=='OUTPUT_TRUNCATED']
    assert len(truncated_terminal) == 2
    assert all(e['contract_result']=='NOT_EVALUATED' and e['reasoning_tokens']==4096 for e in truncated_terminal)
    assert all(e['visible_output_tokens']==0 for e in truncated_terminal)
    assert runner.budget.snapshot()['recorded_cost_nano_upper'] == 2*2_060_500 + 2*22_500
    assert remember_round({}, [], {'agent_call_results': {'P1_DEDUP': 'OUTPUT_TRUNCATED'}}, b'', 1) == []


def test_partial_patch_json_is_neutral_terminal_distinct_from_budget_truncation(tmp_path):
    partial = response({'status':'OK','result':{'worker_patch':{'old':'keep'}}})
    runner = client(tmp_path, lambda *_: partial)
    with pytest.raises(StageFailure, match='INCOMPLETE_PATCH'):
        runner.propose('P1_DEDUP', public(), None)
    assert runner.budget.snapshot()['completed']==1
    end = runner.observer.log.read()[-1]['payload']
    assert end['outcome']=='INCOMPLETE_PATCH' and end['structured_json_complete']


def test_advisory_truncation_is_saved_without_masking_independent_meta(tmp_path, monkeypatch):
    from rsik import postround
    monkeypatch.setattr(postround, 'completed_projection', lambda *_: ({}, {}))
    class Advisors:
        scanner = LeakScanner()
        def call(self, role, _):
            if role == 'curator':
                raise StageFailure('AGENT_ERROR', 'OUTPUT_TRUNCATED', global_stop=False)
            return {'status':'ABSTAIN', 'result':None, 'reason':'nothing to review'}
    result = postround.run_advisory(Advisors(), [], tmp_path/'advice')
    assert result['curator_status']=='OUTPUT_TRUNCATED' and result['meta_status']=='ABSTAIN'
    assert result['committed_entries']==[]


def test_known_malformed_json_repair_uses_distinct_profile_limit_and_request(tmp_path):
    seen = []
    malformed = response({})
    malformed['response']['output'][0]['content'][0]['text'] = '{"status":'
    def transport(body, *_):
        seen.append(body)
        return malformed if len(seen)==1 else response({'status':'ABSTAIN','reason':'unavailable','result':None})
    runner = client(tmp_path, transport, token_budget_profile=TUNING, service_tier='default',
                    request_guard=lambda b, _: request_cost_upper(b, PRICES[b['model']]))
    assert runner.diagnose(public())['status']=='ABSTAIN'
    assert [b['max_output_tokens'] for b in seen]==[8192,4096]
    assert all(b['service_tier']=='default' and 'max_completion_tokens' not in b for b in seen)
    assert seen[0]!=seen[1] and runner.budget.snapshot()['completed']==2
    starts = [e['payload'] for e in runner.observer.log.read() if e['event_type']=='INVOCATION_STARTED']
    repair = next(item for item in starts if item['attempt']==1)
    assert repair['repair_of'].endswith('-diagnose-0')
    assert repair['requested_output_tokens']==4096


def test_review_profile_separates_roles_and_binds_decode_to_identity():
    model = 'gpt-6-luna'
    assert [role_limit(LEGACY, role) for role in ('diagnose','P1_DEDUP','curator','meta')] == [1024,3072,2048,2048]
    assert [role_limit(TUNING, role) for role in ('diagnose','P1_DEDUP','curator','meta')] == [8192,16384,8192,8192]
    assert decode_settings('P1_DEDUP',model,token_budget_profile=TUNING) == {
        'reasoning': {'effort':'max'}, 'max_output_tokens': 16384}
    identity = runtime_profile_identity(M1_SPECIALIZED_V1, {}, 'query-autonomous-v1',
                                        token_budget_profile=TUNING)
    new = deepcopy(identity)
    new['token_budget']['requested_limits']['proposer'] = 8192
    with pytest.raises(ValueError, match='M1_RUNTIME_PROFILE_IDENTITY_CHANGED'):
        validate_runtime_profile_identity(new, M1_SPECIALIZED_V1, {}, 'query-autonomous-v1',
                                          token_budget_profile=TUNING)


@pytest.mark.parametrize('unsupported', ['max_completion_tokens','max_tokens','reasoning_budget'])
def test_responses_does_not_accept_foreign_or_unpublished_budget_parameters(unsupported):
    with pytest.raises(ValueError, match='REQUEST_OUTSIDE_TOKEN_BUDGET_CONTRACT'):
        request_cost_upper({'model':'gpt-6-luna','service_tier':'default','max_output_tokens':8192,
                            unsupported:4096}, PRICES['gpt-6-luna'])


def test_recovery_plan_checks_real_usage_binding_increment_and_duplicate_attempt(tmp_path):
    ledger = Budget(tmp_path/'budget.sqlite')
    original = {'model':'gpt-6-luna','service_tier':'default','max_output_tokens':4096,
                'input':[{'role':'user','content':'public fixture'}]}
    receipt = truncated()
    ledger.start('original','P1_DEDUP','gpt-6-luna',digest(original))
    ledger.complete('original',receipt['response']['usage'],status='RECEIVED',
                    provider_request_id=receipt['provider_request_id'])
    with pytest.raises(ValueError, match='RECOVERY_COST_OR_LEDGER_BLOCKED'):
        plan_truncated_recovery(ledger,'original',original,receipt,new_limit=8192,ceiling_nano=1)
    plan, new = plan_truncated_recovery(ledger,'original',original,receipt,new_limit=16384,ceiling_nano=11_000_000)
    assert plan['status']=='AWAITING_EXPLICIT_AUTHORIZATION' and new['input']==original['input']
    assert new['max_output_tokens']==16384 and ledger.snapshot()['attempts']==1
    ledger.start(plan['request_key'],'P1_DEDUP','gpt-6-luna',plan['request_sha256'])
    ledger.complete(plan['request_key'],{'input_tokens':100,'output_tokens':100},status='RECEIVED')
    with pytest.raises(ValueError, match='RECOVERY_ALREADY_DISPATCHED'):
        plan_truncated_recovery(ledger,'original',original,receipt,new_limit=16384,ceiling_nano=11_000_000)

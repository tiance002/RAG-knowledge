"""No-network tests of the exact send boundary and post-usage bookkeeping."""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from rsik.budgets import Budget
from rsik.contracts import ContractError
from rsik.errors import StageFailure
from rsik.openai_roles import OpenAIRoles
from rsik.redact import LeakScanner, make_visible, project_summary


def public():
    return make_visible([project_summary('REPLAY_SUMMARY', {'questions': 20}, include_count_evidence=True)],
                        parent_code='def select_member_indices(members, chunks, files, seed_ids, selection):\n    return []')


def response(value, model='gpt-6-luna'):
    return {'transport_status': 'RECEIVED', 'http_status': 200, 'response': {
        'model': model, 'status': 'completed', 'usage': {'input_tokens': 100, 'output_tokens': 20},
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}}


def roles(tmp_path, transport):
    return OpenAIRoles(Budget(tmp_path / 'budget.sqlite'), tmp_path / 'roles', tmp_path / 'not-read.env',
                       LeakScanner(['PRIVATE_EVALUATOR_SENTINEL_778899']), transport=transport)


def test_real_send_shape_and_abstain_usage(tmp_path):
    seen = []
    def transport(body, path, timeout):
        seen.append(body)
        return response({'status': 'ABSTAIN', 'result': None, 'reason': 'insufficient detail'})
    client = roles(tmp_path, transport)
    assert client.diagnose(public())['status'] == 'ABSTAIN'
    assert seen[0]['model'] == 'gpt-6-luna'
    assert seen[0]['reasoning'] == {'effort': 'none'} and seen[0]['temperature'] == 0
    assert 'service_tier' not in seen[0]
    assert 'tools' not in seen[0] and not seen[0]['store']
    state = client.budget.snapshot()
    assert state['attempts'] == state['completed'] == 1
    assert state['reservation_usd'] == '0' and state['recorded_cost_nano_upper'] > 0


def test_only_known_format_failure_can_repair_once_and_both_billed(tmp_path):
    seen = []
    def transport(body, *_):
        seen.append(body)
        return response({'wrong': 'contract'})
    client = roles(tmp_path, transport)
    assert client.diagnose(public())['reason'] == 'CONTRACT_ERROR_AFTER_ONE_REPAIR'
    assert len(seen) == 2 and client.budget.snapshot()['attempts'] == 2
    assert 'wrong' not in seen[1]['input'][1]['content']


@pytest.mark.parametrize('receipt', [{'transport_status': 'TIMEOUT'}, {'transport_status': 'HTTP_ERROR', 'http_status': 429}])
def test_unknown_billing_never_retries(tmp_path, receipt):
    seen = []
    def transport(*args):
        seen.append(args)
        return receipt
    client = roles(tmp_path, transport)
    with pytest.raises(StageFailure, match='BILLING_OR_REQUEST_STATUS_UNKNOWN'):
        client.diagnose(public())
    assert len(seen) == 1 and client.budget.snapshot()['total_cost_status'] == 'UNKNOWN'


@pytest.mark.parametrize('mutation', ['nested', 'alias', 'quote'])
def test_nested_private_sentinel_never_dispatched(tmp_path, mutation):
    context = public()
    if mutation == 'nested': context['observations'][0]['counts']['questions'] = {'text': 'PRIVATE_EVALUATOR_SENTINEL_778899'}
    if mutation == 'alias': context['parent_alias'] = 'PRIVATE_EVALUATOR_SENTINEL_778899'
    if mutation == 'quote': context['evidence_quotes']['new'] = 'PRIVATE_EVALUATOR_SENTINEL_778899'
    client = roles(tmp_path, lambda *_: pytest.fail('must not dispatch'))
    with pytest.raises(ContractError): client.diagnose(context)
    assert client.budget.snapshot()['attempts'] == 0


def test_provider_cannot_substitute_astra_even_with_valid_envelope(tmp_path):
    client = roles(tmp_path, lambda *_: response({'status': 'ABSTAIN', 'result': None, 'reason': 'unused'}, 'gpt-6-astra'))
    with pytest.raises(StageFailure, match='MODEL_NOT_ALLOWED'): client.diagnose(public())
    assert client.budget.snapshot()['completed'] == 1


def test_same_call_is_not_resent_after_receipt(tmp_path):
    client = roles(tmp_path, lambda *_: response({'status': 'ABSTAIN', 'result': None, 'reason': 'unused'}))
    context = public()
    client.diagnose(context)
    assert client.diagnose(context)['status']=='ABSTAIN'
    assert client.budget.snapshot()['attempts'] == 1


def test_commentary_is_not_mistaken_for_second_structured_answer(tmp_path):
    receipt = response({'status': 'ABSTAIN', 'result': None, 'reason': 'unused'})
    receipt['response']['output'][0]['phase'] = 'final_answer'
    receipt['response']['output'].insert(0, {'type': 'message', 'phase': 'commentary',
        'content': [{'type': 'output_text', 'text': 'unstructured commentary'}]})
    client = roles(tmp_path, lambda *_: receipt)
    assert client.diagnose(public())['status'] == 'ABSTAIN'
    assert client.budget.snapshot()['attempts'] == 1


def test_child_receipt_is_locale_independent_and_persisted_before_pipe(tmp_path):
    import subprocess,os
    path=tmp_path/'transport.json'
    script="from rsik.http_worker import emit_receipt; emit_receipt({'text':'中文诊断','usage':{'output_tokens':3}},"+repr(str(path))+")"
    process=subprocess.run([sys.executable,'-B','-c',script],cwd=ROOT,
        env={**os.environ,'PYTHONIOENCODING':'gbk'},capture_output=True,timeout=10)
    assert process.returncode==0 and not process.stderr
    assert process.stdout.isascii()
    assert json.loads(process.stdout)==json.loads(path.read_bytes())
    assert json.loads(process.stdout)['text']=='中文诊断'


def test_money_limit_has_explicit_stop_reason_and_does_not_dispatch_again(tmp_path):
    seen=[]
    def transport(*args):
        seen.append(args)
        return response({'status':'ABSTAIN','result':None,'reason':'SYNTHETIC'})
    client=OpenAIRoles(Budget(tmp_path/'budget.sqlite',limit_usd='0.000000001'),tmp_path/'roles',
                      tmp_path/'unused.env',LeakScanner(),transport=transport)
    client.diagnose(public())  # permitted final call may overshoot this synthetic tiny limit
    with pytest.raises(StageFailure,match='BUDGET_EXHAUSTED'):client.diagnose(public())
    assert len(seen)==1

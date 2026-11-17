"""Synthetic invocation receipts only: no HTTP worker or paid endpoints."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rsik.budgets import Budget
from rsik.errors import StageFailure
from rsik.observability import Observer, VERSION, observation_for
from rsik.openai_roles import OpenAIRoles
from rsik.redact import LeakScanner, make_visible, project_summary
from rsik.state import digest

PRIVATE = 'PRIVATE_BODY_SENTINEL_187930'
ABSTAIN = {'status': 'ABSTAIN', 'result': None, 'reason': PRIVATE}


def public():
    return make_visible([project_summary('REPLAY_SUMMARY', {'questions': 20}, include_count_evidence=True)],
                        parent_code='def select_member_indices(members, chunks, files, seed_ids, selection):\n    return []')


def receipt(value):
    return {'transport_status': 'RECEIVED', 'http_status': 200, 'response': {
        'model': 'gpt-6-luna', 'status': 'completed',
        'usage': {'input_tokens': 100, 'output_tokens': 20},
        'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': json.dumps(value)}]}]}}


def client(tmp_path, transport, observer=None, **kwargs):
    return OpenAIRoles(Budget(tmp_path/'budget.sqlite'), tmp_path/'responses', tmp_path/'unused.env',
                       LeakScanner(), transport=transport, observer=observer, **kwargs)


def events(c):
    return c.observer.log.read()


def finished(c):
    return [e['payload'] for e in events(c) if e['event_type'] == 'INVOCATION_FINISHED']


def test_success_contract_and_cost_link_not_response_received(tmp_path):
    value = {'status': 'OK', 'reason': None, 'result': {
        'schema_version': 'rsi-v2.2', 'findings': [], 'limitations': []}}
    c = client(tmp_path, lambda *_: receipt(value))
    assert c.call('meta', public()) == value
    log = events(c)
    assert [e['event_type'] for e in log] == ['INVOCATION_STARTED', 'RESPONSE_RECEIVED',
                                            'CONTRACT_VALIDATED', 'INVOCATION_FINISHED']
    assert all(e['schema_version'] == 'rsi-event/v2.2' for e in log)
    assert all(e['payload']['schema_version'] == VERSION for e in log)
    end = finished(c)[0]
    assert (end['outcome'], end['contract_result'], end['failure_origin']) == ('SUCCESS', 'VALID', 'NONE')
    assert end['cost_source'] == digest(str(c.budget.log.path.resolve()))
    assert end['cost_request_key'] == c.budget.log.read()[0]['payload']['request_key']
    assert 'outcome' not in log[1]['payload']
    assert c.budget.log.path != c.observer.path


def test_abstention_cached_resume_and_public_metadata_has_no_body(tmp_path):
    calls = []
    def transport(*args):
        calls.append(args)
        return receipt(ABSTAIN)
    c = client(tmp_path, transport)
    context = public()
    assert c.diagnose(context) == ABSTAIN
    original_events = events(c)
    c.observer.update_context(candidate_id='later-candidate')
    assert c.diagnose(context) == ABSTAIN
    assert events(c) == original_events and len(calls) == 1
    assert finished(c)[0]['outcome'] == 'ABSTAINED'
    serialized = json.dumps(events(c))
    assert PRIVATE not in serialized and 'input_tokens' not in serialized and 'cost_nano' not in serialized
    assert 'output_text' not in serialized and 'content' not in serialized and 'key_file' not in serialized
    assert c.budget.snapshot()['attempts'] == 1


def test_repair_new_attempt_contract_failures_and_fallback_is_not_abstention(tmp_path):
    seen = []
    def transport(body, *_):
        seen.append(body)
        return receipt({'bad': PRIVATE})
    c = client(tmp_path, transport)
    result = c.diagnose(public())
    assert result['reason'] == 'CONTRACT_ERROR_AFTER_ONE_REPAIR'
    ends = finished(c)
    assert [e['outcome'] for e in ends] == ['CONTRACT_ERROR', 'CONTRACT_ERROR']
    assert [e['attempt'] for e in ends] == [0, 1]
    assert len({e['invocation_id'] for e in ends}) == 2
    assert all(e['contract_result'] == 'INVALID' for e in ends)
    assert len(seen) == c.budget.snapshot()['attempts'] == 2
    assert 'repair_instruction' in seen[1]['input'][1]['content']
    assert PRIVATE not in json.dumps(events(c))


@pytest.mark.parametrize('transport_status,outcome,origin', [
    ('TIMEOUT', 'TIMEOUT', 'TRANSPORT'), ('HTTP_ERROR', 'API_ERROR', 'PROVIDER'),
    ('UNKNOWN', 'INFRA_FAILURE', 'INFRASTRUCTURE'), ('CANCELLED', 'CANCELLED', 'CANCELLATION')])
def test_transport_failures_do_not_retry_or_claim_response_success(tmp_path, transport_status, outcome, origin):
    seen = []
    def transport(*args):
        seen.append(args)
        return {'transport_status': transport_status, 'error_message': PRIVATE}
    c = client(tmp_path, transport)
    with pytest.raises(StageFailure, match='BILLING_OR_REQUEST_STATUS_UNKNOWN'):
        c.diagnose(public())
    assert len(seen) == 1
    assert finished(c)[0]['outcome'] == outcome
    assert finished(c)[0]['failure_origin'] == origin
    assert finished(c)[0]['contract_result'] == 'NOT_EVALUATED'
    assert not any(e['event_type'] == 'RESPONSE_RECEIVED' for e in events(c))
    assert PRIVATE not in json.dumps(events(c))


def test_transport_timeout_exception_preserves_original_exception_behavior(tmp_path):
    def transport(*_): raise TimeoutError(PRIVATE)
    c = client(tmp_path, transport)
    with pytest.raises(StageFailure, match='BILLING_OR_REQUEST_STATUS_UNKNOWN'):
        c.diagnose(public())
    assert finished(c)[0]['outcome'] == 'TIMEOUT'
    # Existing private receipt contract is unchanged.
    raw = list(c.directory.glob('*.json'))
    assert any(json.loads(p.read_bytes()) == {'transport_status': 'UNKNOWN'} for p in raw)
    assert PRIVATE not in json.dumps(events(c))


def test_cancellation_after_received_response_and_infrastructure_failure(tmp_path):
    count = 0
    def stop_after_transport():
        nonlocal count
        count += 1
        if count == 2: raise StageFailure('CANCELLED', 'SYNTHETIC_CANCELLED')
    c = client(tmp_path, lambda *_: receipt(ABSTAIN), before_request=stop_after_transport)
    with pytest.raises(StageFailure, match='SYNTHETIC_CANCELLED'):
        c.diagnose(public())
    assert finished(c)[0]['outcome'] == 'CANCELLED'
    assert finished(c)[0]['contract_result'] == 'NOT_EVALUATED'
    assert any(e['event_type'] == 'RESPONSE_RECEIVED' for e in events(c))


def test_observer_closed_metadata_idempotency_and_injection(tmp_path):
    observer = Observer(tmp_path/'injected.sqlite', {'run_id': 'synthetic'})
    c = client(tmp_path, lambda *_: receipt(ABSTAIN), observer=observer)
    assert observation_for(c) is observer
    first = observer.emit('STAGE_STARTED', {'stage_id': 'synthetic'}, key='one')
    assert observer.emit('STAGE_STARTED', {'stage_id': 'synthetic'}, key='one') == first
    with pytest.raises(RuntimeError, match='IDEMPOTENCY_CONFLICT'):
        observer.emit('STAGE_STARTED', {'stage_id': 'changed'}, key='one')
    for payload in ({'body': PRIVATE}, {'request': {'content': PRIVATE}}, {'reason': 'raw failure message'}):
        with pytest.raises(ValueError): observer.emit('STAGE_STARTED', payload)
    c.diagnose(public())
    assert finished(c)[0]['run_id'] == 'synthetic'
    assert not (c.directory/'observations.sqlite').exists()


def test_cached_unobserved_receipt_is_not_historical_backfill(tmp_path):
    c = client(tmp_path, lambda *_: receipt(ABSTAIN))
    context = public()
    c.diagnose(context)
    replacement = Observer(tmp_path/'new-runtime-only.sqlite')
    c.observer = replacement
    assert c.diagnose(context) == ABSTAIN
    assert replacement.log.read() == []


def test_invalid_projection_has_no_attempt(tmp_path):
    c = client(tmp_path, lambda *_: pytest.fail('must never dispatch'))
    bad = public()
    bad['secret'] = PRIVATE
    with pytest.raises(ValueError): c.diagnose(bad)
    assert not events(c) and c.budget.snapshot()['attempts'] == 0


def test_invocation_link_is_thread_local_for_parallel_roles(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    barrier = threading.Barrier(2)
    def transport(*_):
        barrier.wait(timeout=5)
        return receipt(ABSTAIN)
    c = client(tmp_path, transport)
    context = public()
    def invoke(role):
        c.call(role, context)
        link = c.observation_invocation
        link['agent_role'] = 'mutated-copy'
        return c.observation_invocation
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(invoke, 'meta')
        b = pool.submit(invoke, 'diagnose')
        links = [a.result(timeout=10), b.result(timeout=10)]
    assert [v['agent_role'] for v in links] == ['meta', 'diagnose']
    assert len({v['invocation_id'] for v in links}) == 2
    assert c.observation_invocation == {}
    assert len(finished(c)) == 2


def test_non_identifier_public_proposal_identity_is_hashed(tmp_path):
    observer = Observer(tmp_path/'metadata.sqlite')
    event = observer.emit('PROPOSAL_CREATED', {'proposal_id': 'public proposal with spaces'})
    assert event['payload']['proposal_id'] == digest('public proposal with spaces')


@pytest.mark.parametrize('kind', ['INVOCATION_STARTED', 'RESPONSE_RECEIVED', 'INVOCATION_FINISHED'])
@pytest.mark.parametrize('error_type', [OSError, __import__('sqlite3').OperationalError])
def test_required_lifecycle_storage_failures_stop_and_preserve_real_ledger(tmp_path, monkeypatch, kind, error_type):
    calls = []
    c = client(tmp_path, lambda *_: calls.append(1) or receipt(ABSTAIN))
    context = public()
    original = c.observer.log.append
    def fail(key, event_type, *args, **kwargs):
        if event_type == kind:
            raise error_type('private disk failure')
        return original(key, event_type, *args, **kwargs)
    monkeypatch.setattr(c.observer.log, 'append', fail)
    with pytest.raises(StageFailure) as caught:
        c.diagnose(context)
    assert caught.value.category == 'STORAGE_ERROR'
    assert caught.value.global_stop is True
    snapshot = c.budget.snapshot()
    if kind == 'INVOCATION_STARTED':
        assert not calls and snapshot['attempts'] == snapshot['completed'] == 0
        assert not events(c)
    else:
        assert calls == [1] and snapshot['attempts'] == snapshot['completed'] == 1
        assert snapshot['total_cost_status'] == 'USAGE_ESTIMATED'
        assert snapshot['recorded_cost_nano_upper'] > 0
        from rsik.analytics.metrics import compute_metrics
        from rsik.analytics.projections import project_sources
        from rsik.analytics.reader import read_event_log
        records = project_sources([read_event_log(c.observer.path)])
        invocation, = [r for r in records if r['projection_type'] == 'AgentInvocation']
        assert invocation['payload']['evidence_complete'] is False
        group, = compute_metrics(records)['groups']
        r6 = next(m for m in group['metrics'] if m['metric_id'] == 'R6')
        assert (r6['numerator'], r6['denominator']) == (1, 1)
        with pytest.raises(StageFailure, match='OBSERVATION_STORAGE_UNAVAILABLE'):
            c.diagnose(context)
        assert calls == [1] and c.budget.snapshot() == snapshot
        monkeypatch.setattr(c.observer.log, 'append', original)
        assert c.diagnose(context) == ABSTAIN
        assert calls == [1] and c.budget.snapshot() == snapshot
    assert 'private disk failure' not in json.dumps(events(c))


def test_response_storage_failure_keeps_unknown_cost_and_never_resends(tmp_path, monkeypatch):
    calls = []
    c = client(tmp_path, lambda *_: calls.append(1) or receipt(ABSTAIN))
    context = public()
    from rsik import openai_roles
    original = openai_roles.atomic_write
    def fail(path, *args, **kwargs):
        if path.name.endswith('.json') and not any(part in path.name for part in ('.request.', '.strategy.')):
            raise OSError('private response disk failure')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(openai_roles, 'atomic_write', fail)
    with pytest.raises(StageFailure) as caught:
        c.diagnose(context)
    assert caught.value.category == 'STORAGE_ERROR'
    snapshot = c.budget.snapshot()
    assert calls == [1] and snapshot['attempts'] == 1 and snapshot['completed'] == 0
    assert snapshot['pending_request_keys'] and snapshot['total_cost_status'] == 'UNKNOWN'
    assert snapshot['provider_invoiced_charge'] == 'UNKNOWN'
    monkeypatch.setattr(openai_roles, 'atomic_write', original)
    with pytest.raises(StageFailure, match='REQUEST_STATUS_UNKNOWN'):
        c.diagnose(context)
    assert calls == [1] and c.budget.snapshot() == snapshot



def test_shared_observer_does_not_create_unused_private_role_databases(tmp_path):
    clients = [client(tmp_path / str(i), lambda *_: receipt(ABSTAIN)) for i in range(3)]
    assert not list(tmp_path.rglob('observations.sqlite'))
    shared = Observer(tmp_path / 'shared' / 'observations.sqlite')
    for c in clients:
        c.observer = shared
        assert c.diagnose(public()) == ABSTAIN
    assert list(tmp_path.rglob('observations.sqlite')) == [shared.path]
    assert len([e for e in shared.log.read() if e['event_type'] == 'INVOCATION_FINISHED']) == 3



@pytest.mark.parametrize('kind', ['INVOCATION_STARTED', 'RESPONSE_RECEIVED', 'INVOCATION_FINISHED'])
def test_injected_observer_raw_storage_failure_is_not_swallowed(tmp_path, monkeypatch, kind):
    calls = []
    c = client(tmp_path, lambda *_: calls.append(1) or receipt(ABSTAIN))
    original = c.observer.emit
    def fail(event_type, *args, **kwargs):
        if event_type == kind:
            raise OSError('private injected observer failure')
        return original(event_type, *args, **kwargs)
    monkeypatch.setattr(c.observer, 'emit', fail)
    with pytest.raises(StageFailure) as caught:
        c.diagnose(public())
    assert caught.value.category == 'STORAGE_ERROR' and caught.value.global_stop
    assert len(calls) == (0 if kind == 'INVOCATION_STARTED' else 1)
    assert not finished(c)

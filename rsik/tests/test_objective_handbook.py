"""Synthetic objective filtering using real immutable Handbook/EventLog storage."""
from copy import deepcopy
import hashlib

import pytest

from rsik.contracts import ContractError
from rsik.events import canonical
from rsik.handbook.store import Handbook
from rsik.objectives import CONTRACT_VERSION
from rsik.query_learning import _delta_files
from rsik.tests.test_handbook_and_promotion import entry, evidence_context, delta


def profile(primary='context_chars', version=1):
    return {'objective_profile_id': primary, 'version': version, 'description': 'Synthetic explicit config',
            'primary_metric': primary, 'direction': 'min', 'hard_constraints': [],
            'secondary_metrics': [], 'tie_breakers': [],
            'required_metric_contracts': {primary: CONTRACT_VERSION}, 'components': ['context_selection']}


def context(objective=None, **extra):
    result = evidence_context(True)
    if objective is not None:
        result['objective_profile'] = objective
    result.update(extra)
    return result


def retrieve(book, objective=None, **kwargs):
    return book.retrieve('dedup', 'OTHER', {'same_source': True}, 'SYNTHETIC-compatible',
                         objective_profile=objective, component='context_selection', **kwargs)


def test_legacy_hash_and_event_retrieval_are_unchanged(tmp_path):
    book = Handbook(tmp_path)
    body = entry()
    key = book.commit_delta(delta(body), context(), idempotency_key='legacy', created_at='fixed')
    expected = {**body, 'evidence_mode': 'REAL_REPLAY', 'created_at': 'fixed'}
    assert key == hashlib.sha256(canonical(expected)).hexdigest()
    assert len(retrieve(book)) == 1
    assert retrieve(book, profile()) == []
    assert 'objective_profile_id' not in book.get(key)


def test_scoped_success_survives_hard_and_secondary_revision_not_new_primary(tmp_path):
    book = Handbook(tmp_path)
    current = profile()
    key = book.commit_delta(delta(entry()), context(current), idempotency_key='scoped')
    initial = retrieve(book, current)[0]
    assert initial['entry_id'] == key
    assert initial['objective_applicability'] == 'COMPATIBILITY_UNKNOWN'
    assert not retrieve(book, {**current, 'objective_profile_id': 'another-goal'})
    revised = deepcopy(current)
    revised['version'] = 2
    revised['hard_constraints'] = [{'metric_id': 'context_chars', 'operator': '<=', 'value': 900}]
    summary = retrieve(book, revised)[0]
    assert summary['entry_id'] == key
    assert summary['objective_applicability'] == 'COMPATIBILITY_UNKNOWN'
    assert summary['hard_constraint_statuses'] == {'context_chars': 'UNKNOWN'}
    revised['secondary_metrics'] = ['replay_seconds']
    revised['required_metric_contracts']['replay_seconds'] = CONTRACT_VERSION
    assert retrieve(book, revised)[0]['metric_effects']['replay_seconds'] == 'UNKNOWN'
    assert retrieve(book, profile('replay_seconds')) == []
    assert retrieve(book) == []
    assert retrieve(book, current, limit=0) == []
    assert book.view()['states'][key] == 'ACTIVE'
    assert book.view()['counts'].get(key, {}).get('helpful', 0) == 0


@pytest.mark.parametrize('failure,expected', [('CONDITIONAL_REGRESSION', True),
    ('NO_GAIN', False), ('INFRASTRUCTURE', False), ('PARENT_FAILURE', False), ('UNKNOWN', False)])
def test_only_suitable_failures_cross_objectives(tmp_path, failure, expected):
    book = Handbook(tmp_path)
    current = profile()
    ctx = context(current, objective_applicability={'kind': 'FAILURE', 'failure_type': failure})
    key = book.commit_delta(delta(entry()), ctx, idempotency_key='failure')
    result = retrieve(book, profile('replay_seconds'))
    assert bool(result) is expected
    if failure == 'NO_GAIN':
        assert retrieve(book, current)[0]['entry_id'] == key
        assert not retrieve(book, {**current, 'objective_profile_id': 'another-goal'})
    assert not book.retrieve('dedup', 'OTHER', {'same_source': False}, 'SYNTHETIC-compatible',
                             objective_profile=current, component='context_selection')
    assert not book.retrieve('dedup', 'OTHER', {'same_source': True}, 'SYNTHETIC-compatible',
                             objective_profile=current, component='retrieval')


def test_general_constraint_requires_host_authority_and_original_activation(tmp_path):
    book = Handbook(tmp_path)
    ctx = context(profile(), objective_applicability={'kind': 'GENERAL_CONSTRAINT'})
    with pytest.raises(ContractError, match='HOST_CONSTRAINT_AUTHORITY_REQUIRED'):
        book.commit_delta(delta(entry()), ctx, idempotency_key='invalid')
    assert not list((tmp_path / 'entries').glob('*'))
    ctx['objective_applicability']['authority'] = 'EXPLICIT_USER'
    ctx['verified_candidates'] = {}
    key = book.commit_delta(delta(entry()), ctx, idempotency_key='valid')
    assert book.view()['states'][key] == 'DRAFT'
    assert retrieve(book, profile('replay_seconds')) == []
    ctx['verified_candidates'] = {'candidate': 'h' * 64}
    assert book.activate_if_qualified(key, ctx)
    assert retrieve(book, profile('replay_seconds'))[0]['entry_id'] == key


def test_metric_effects_are_host_bound_and_recovery_serializes_identically(tmp_path):
    book = Handbook(tmp_path)
    ctx = context(profile(), metric_effects={'context_chars': {
        'status': 'IMPROVED', 'evidence_refs': ['evidence-one', 'evidence-two']}})
    change = delta(entry())
    key, raw, bundle_hash, bundle = _delta_files(book, change, ctx, 'fixed')
    assert book.commit_delta(change, ctx, idempotency_key='scoped', created_at='fixed') == key
    assert (tmp_path / 'entries' / key).read_bytes() == raw
    assert (tmp_path / 'evidence' / bundle_hash).read_bytes() == bundle
    assert book.get(key)['metric_effects']['context_chars']['status'] == 'IMPROVED'
    assert book.commit_delta(change, ctx, idempotency_key='scoped') == key
    altered = deepcopy(ctx)
    altered['objective_profile']['version'] = 2
    with pytest.raises(ContractError, match='HANDBOOK_OBJECTIVE_BINDING_CHANGED'):
        book.commit_delta(change, altered, idempotency_key='scoped')
    fabricated = entry()
    fabricated['metric_effects'] = ctx['metric_effects']
    with pytest.raises(ContractError):
        book.commit_delta(delta(fabricated), ctx, idempotency_key='model-fabrication')
    ctx['metric_effects']['context_chars']['evidence_refs'] = ['invented']
    with pytest.raises(ContractError, match='UNTRACEABLE_METRIC_EFFECT'):
        book.commit_delta(change, ctx, idempotency_key='bad-evidence')


def test_normal_training_verification_and_learning_bind_frozen_objective(tmp_path, monkeypatch):
    from rsik.tests import test_query_learning_bridge as bridge_tests
    from rsik.query_learning import VerifiedCandidateEvidence
    from rsik.execution_evidence import ExecutionEvidence
    from rsik.experiment import load
    from rsik.redact import LeakScanner

    objective = profile()
    original = bridge_tests.run_query_round
    def configured_round(**kwargs):
        return original(**kwargs, objective_transition={
            'previous': None, 'current': objective, 'reason': 'Synthetic explicit objective',
            'source_reference': 'SYNTHETIC-config'})
    monkeypatch.setattr(bridge_tests, 'run_query_round', configured_round)
    # Reuse the existing complete synthetic archive builder and deterministic
    # non-execution capsule; this never calls a real model or claims real gain.
    case = bridge_tests.completed.__wrapped__(tmp_path)
    assert load(case['training'] / 'round.json')['objective_profile'] == objective
    bound = VerifiedCandidateEvidence.from_archives(
        registry=case['registry'].directory, training=case['training'],
        train_fixtures=case['train_fixtures'], verification=case['verification'],
        evidence=ExecutionEvidence.synthetic_control())
    public, trusted = bound.projection(LeakScanner(), 'SYNTHETIC-compatible')
    assert trusted['objective_profile'] == objective
    assert 'objective' not in public
    assert trusted['metric_effects']['context_chars']['status'] == 'IMPROVED'
    receipt = bridge_tests.bridge(case)
    key = receipt['entries'][0]
    stored = case['handbook'].get(key)
    assert stored['objective_profile_id'] == objective['objective_profile_id']
    assert stored['metric_effects']['context_chars']['status'] == 'IMPROVED'
    assert case['handbook'].retrieve('budget', 'OTHER', {'same_source': True},
        'SYNTHETIC-compatible', objective_profile=objective, component='context_selection')[0]['entry_id'] == key
    assert not case['handbook'].retrieve('budget', 'OTHER', {'same_source': True},
        'SYNTHETIC-compatible', objective_profile=profile('replay_seconds'), component='context_selection')
    before = case['handbook'].log.read()
    assert bridge_tests.bridge(case) == receipt
    assert case['handbook'].log.read() == before


def test_regressed_metric_is_a_tradeoff_or_hard_conflict(tmp_path):
    from rsik.redact import validate_handbook_summaries

    book = Handbook(tmp_path)
    current = profile('candidate_chars')
    refs = ['evidence-one', 'evidence-two']
    ctx = context(current, metric_effects={
        'context_chars': {'status': 'IMPROVED', 'evidence_refs': refs},
        'shorter_cases': {'status': 'REGRESSED', 'evidence_refs': refs},
    })
    key = book.commit_delta(delta(entry()), ctx, idempotency_key='tradeoff')
    before = book.get(key)
    revised = {**current, 'version': 2, 'secondary_metrics': ['shorter_cases'],
               'required_metric_contracts': {**current['required_metric_contracts'], 'shorter_cases': CONTRACT_VERSION}}
    summary = retrieve(book, revised)[0]
    assert summary['objective_applicability'] == 'TRADEOFF'
    assert summary['metric_effects']['shorter_cases'] == 'REGRESSED'
    assert summary['metric_effects']['candidate_chars'] == 'IMPROVED'
    revised['hard_constraints'] = [{'metric_id': 'shorter_cases', 'operator': '>=', 'value': 1}]
    result = retrieve(book, revised)
    assert result[0]['objective_applicability'] == 'HARD_CONSTRAINT_CONFLICT'
    assert result[0]['hard_constraint_statuses'] == {'shorter_cases': 'CONFLICT'}
    validate_handbook_summaries(result, [key])
    result[0]['hard_constraint_statuses']['shorter_cases'] = 'PASS'
    with pytest.raises(ContractError, match='INVALID_HANDBOOK_OBJECTIVE_SUMMARY'):
        validate_handbook_summaries(result, [key])
    assert book.get(key) == before

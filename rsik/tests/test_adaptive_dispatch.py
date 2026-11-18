"""Synthetic public counts only: no model, source corpus, Gold, index or capsule."""
from copy import deepcopy
import json

import pytest

from rsik.adaptive_dispatch import (build_dispatch_plan, catalog_identity, generalist_fallback,
    task_catalog, validate_dispatch_plan, validate_assignment, DispatchUnavailable)
from rsik.adjudicate import SLOTS
from rsik.contracts import ADAPTIVE_DIAGNOSIS_VERSION, ContractError
from rsik.redact import LeakScanner, make_visible, project_summary
from rsik.state import digest


def fixture(counts=None, kinds=(), *, conflicts=()):
    obs = project_summary('NONSEED_EXPANSION', counts or {'questions': 3}, include_count_evidence=True)
    obs['observation_ref'] = 'observation-' + '1' * 32
    obs['safe_evidence'][0]['evidence_ref'] = 'evidence-' + '2' * 32
    public = make_visible([obs], parent_code='SYNTHETIC_PUBLIC_SELECTOR', round_id='synthetic-round')
    public['parent_alias'] = 'synthetic-parent'
    ref = next(iter(public['evidence_quotes']))
    diagnosis = {'schema_version': ADAPTIVE_DIAGNOSIS_VERSION,
        'observation_refs': public['observation_refs'], 'problem_layer': 'SELECTION',
        'first_deviation': 'Synthetic public structural count', 'evidence_refs': [ref],
        'evidence_quote': public['evidence_quotes'][ref], 'hypothesis': 'Synthetic falsifiable hypothesis',
        'fixable_within_seam': True, 'required_data': [], 'confidence': .99,
        'task_signals': [{'task_type': k, 'evidence_refs': [ref], 'disposition': disposition}
                         for disposition, seq in [('SUPPORT', kinds), ('CONFLICT', conflicts)] for k in seq]}
    return public, diagnosis


def kinds(plan):
    return [a['task_type'] for a in plan['assignments']]


@pytest.mark.parametrize('counts,signal,expected', [
    ({'exact_content_duplicate_pairs': 2, 'duplicate_key_eligible_members': 4}, 'DEDUP', 'DEDUP'),
    ({'exact_content_duplicate_pairs': 0, 'duplicate_key_eligible_members': 4}, 'DEDUP', 'GENERALIST'),
    ({'exact_content_duplicate_pairs': 2, 'duplicate_key_eligible_members': 0}, 'DEDUP', 'GENERALIST'),
    ({'nonseed_members': 2}, 'BUDGET', 'BUDGET'),
    ({'nonseed_content_chars': 10}, 'BUDGET', 'BUDGET'),
    ({'seed_content_chars': 10, 'questions': 3}, 'BUDGET', 'GENERALIST'),
    ({'dependency_edges': 2}, 'DEPENDENCY', 'DEPENDENCY'),
    ({'related_member_pairs': 2}, 'DEPENDENCY', 'DEPENDENCY'),
    ({'questions': 20}, 'DEPENDENCY', 'GENERALIST'),
])
def test_each_task_requires_compatible_actual_structural_evidence(counts, signal, expected):
    public, diagnosis = fixture(counts, [signal])
    plan = build_dispatch_plan(public, diagnosis)
    assert kinds(plan) == [expected] * 3
    assert validate_dispatch_plan(plan, public, diagnosis) == plan
    if expected != 'GENERALIST':
        assert all(a['evidence_refs'] == diagnosis['evidence_refs'] for a in plan['assignments'])
        assert [a['assignment_reason'] for a in plan['assignments']][1:] == [
            'REPEATED_SUPPORTED_TASK_INDEPENDENT_SEARCH'] * 2


def test_mixed_support_uses_fixed_slots_and_deterministic_catalog_order():
    public, diagnosis = fixture({'nonseed_members': 2, 'dependency_edges': 1,
        'exact_content_duplicate_pairs': 1, 'duplicate_key_eligible_members': 2},
        ['DEPENDENCY', 'DEDUP', 'BUDGET'])
    original = deepcopy((public, diagnosis))
    plan = build_dispatch_plan(public, diagnosis)
    assert kinds(plan) == ['DEDUP', 'BUDGET', 'DEPENDENCY']
    assert [a['slot'] for a in plan['assignments']] == list(SLOTS)
    assert len({a['task_id'] for a in plan['assignments']}) == 3
    assert (public, diagnosis) == original
    assert build_dispatch_plan(public, diagnosis) == plan
    assert plan['dispatch_sha256'] == digest({k: v for k, v in plan.items() if k != 'dispatch_sha256'})


def test_two_families_repeat_with_explicit_independence_reason():
    public, diagnosis = fixture({'nonseed_members': 2, 'dependency_edges': 1}, ['DEPENDENCY', 'BUDGET'])
    plan = build_dispatch_plan(public, diagnosis)
    assert kinds(plan) == ['BUDGET', 'DEPENDENCY', 'BUDGET']
    assert plan['assignments'][2]['assignment_reason'] == 'REPEATED_SUPPORTED_TASK_INDEPENDENT_SEARCH'


def test_conflicting_family_excluded_without_suppressing_other_supported_family():
    public, diagnosis = fixture({'nonseed_members': 2, 'dependency_edges': 1},
                                ['BUDGET', 'DEPENDENCY'], conflicts=['BUDGET'])
    assert kinds(build_dispatch_plan(public, diagnosis)) == ['DEPENDENCY'] * 3
    diagnosis['task_signals'] += [{'task_type': 'DEPENDENCY', 'evidence_refs': diagnosis['evidence_refs'],
                                  'disposition': 'CONFLICT'}]
    assert kinds(build_dispatch_plan(public, diagnosis)) == ['GENERALIST'] * 3


@pytest.mark.parametrize('change', ['unknown', 'confidence_only', 'nonfixable', 'missing_data', 'abstain', 'none'])
def test_uncertainty_and_non_admissibility_are_explicit(change):
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    expected = 'GENERALIST'
    if change == 'unknown':
        diagnosis['problem_layer'] = 'UNKNOWN'
    if change == 'confidence_only':
        diagnosis['task_signals'] = []
    if change == 'nonfixable':
        diagnosis['fixable_within_seam'] = False
        expected = 'ABSTAIN'
    if change == 'missing_data':
        diagnosis['required_data'] = ['Unavailable public structural signal']
        expected = 'ABSTAIN'
    if change == 'abstain':
        diagnosis = {'status': 'ABSTAIN', 'reason': 'Synthetic insufficient evidence', 'result': None}
        expected = 'ABSTAIN'
    if change == 'none':
        diagnosis = None
        expected = 'ABSTAIN'
    assert kinds(build_dispatch_plan(public, diagnosis)) == [expected] * 3


def test_confidence_never_selects_or_suppresses_a_task():
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    low = build_dispatch_plan(public, {**diagnosis, 'confidence': 0})
    high = build_dispatch_plan(public, {**diagnosis, 'confidence': 1})
    assert kinds(low) == kinds(high) == ['BUDGET'] * 3
    assert [a['assignment_reason'] for a in low['assignments']] == [a['assignment_reason'] for a in high['assignments']]


def test_fallback_is_explicit_frozen_generalist_and_bound_to_diagnosis():
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    fallback = generalist_fallback(public, reason='DISPATCH_UNAVAILABLE', diagnosis=diagnosis)
    assert kinds(fallback) == ['GENERALIST'] * 3
    assert fallback['mode'] == 'FROZEN_GENERALIST_FALLBACK'
    assert fallback['fallback_profile'] == 'M1_ADAPTIVE_FROZEN_GENERALIST_FALLBACK_V1'
    assert fallback['diagnosis_sha256'] == digest(diagnosis)
    assert validate_dispatch_plan(fallback, public, diagnosis) == fallback
    for assignment in fallback['assignments']:
        assert validate_assignment(assignment, public, diagnosis) == assignment
    with pytest.raises(ContractError):
        generalist_fallback(public, reason='PRIVATE_SENTINEL')
    assert str(DispatchUnavailable()) == 'DISPATCH_UNAVAILABLE'


@pytest.mark.parametrize('diagnosis_change', ['none', 'nonfixable', 'missing_data', 'abstain'])
def test_fallback_never_overrides_non_admissible_seam(diagnosis_change):
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    if diagnosis_change == 'none':
        diagnosis = None
    elif diagnosis_change == 'nonfixable':
        diagnosis['fixable_within_seam'] = False
    elif diagnosis_change == 'missing_data':
        diagnosis['required_data'] = ['Unavailable evidence']
    else:
        diagnosis = {'status': 'ABSTAIN', 'reason': 'Synthetic no evidence', 'result': None}
    plan = generalist_fallback(public, diagnosis, reason='DISPATCH_UNAVAILABLE')
    assert kinds(plan) == ['ABSTAIN'] * 3
    assert validate_dispatch_plan(plan, public, diagnosis) == plan
    for assignment in plan['assignments']:
        assert validate_assignment(assignment, public, diagnosis) == assignment


@pytest.mark.parametrize('mutation', ['catalog', 'signal_ref', 'extra_signal', 'extra_diagnosis', 'private_count', 'quote'])
def test_unauthorized_inputs_fail_closed_without_leaking_values(mutation):
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    catalog = None
    secret = 'PRIVATE_SENTINEL_NEVER_SEND'
    if mutation == 'catalog':
        catalog = task_catalog(); catalog['BUDGET']['scope'] = secret
    if mutation == 'signal_ref':
        diagnosis['task_signals'][0]['evidence_refs'] = [secret]
    if mutation == 'extra_signal':
        diagnosis['task_signals'][0]['private_data'] = secret
    if mutation == 'extra_diagnosis':
        diagnosis['private_data'] = secret
    if mutation == 'private_count':
        public['observations'][0]['counts'][secret] = 1
    if mutation == 'quote':
        public['evidence_quotes'][diagnosis['evidence_refs'][0]] = secret
    with pytest.raises(ContractError) as error:
        build_dispatch_plan(public, diagnosis, catalog=catalog)
    assert secret not in str(error.value)


def test_assignment_validation_rejects_cross_slot_and_content_tampering():
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    plan = build_dispatch_plan(public, diagnosis)
    for assignment in plan['assignments']:
        assert validate_assignment(assignment, public, diagnosis) == assignment
    forged = deepcopy(plan)
    forged['assignments'][0]['forbidden_actions'] = []
    forged['dispatch_sha256'] = digest({k: v for k, v in forged.items() if k != 'dispatch_sha256'})
    with pytest.raises(ContractError):
        validate_dispatch_plan(forged, public, diagnosis)
    bad = {**plan['assignments'][0], 'task_type': 'GENERALIST'}
    with pytest.raises(ContractError):
        validate_assignment(bad, public, diagnosis)
    other_public = {**public, 'round_id': 'other-round'}
    with pytest.raises(ContractError):
        validate_dispatch_plan(plan, other_public, diagnosis)


def test_catalog_identity_is_json_capsule_safe_fresh_and_public_only():
    before = catalog_identity()
    catalog = task_catalog()
    assert json.loads(json.dumps(catalog)) == catalog
    assert before == digest({'dispatcher_version': 'M1_ADAPTIVE_DISPATCH_V1', 'catalog': catalog})
    catalog['BUDGET']['forbidden_actions'].append('PRIVATE_SENTINEL')
    assert catalog_identity() == before
    assert 'PRIVATE_SENTINEL' not in json.dumps(task_catalog())


def test_assignment_wire_is_accepted_by_unchanged_secret_scanner():
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    LeakScanner().check(build_dispatch_plan(public, diagnosis))


def test_explicit_fallback_assignment_validation_does_not_call_dispatcher(monkeypatch):
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    plan = generalist_fallback(public, diagnosis, reason='DISPATCH_UNAVAILABLE')
    def unavailable(*args, **kwargs):
        raise DispatchUnavailable()
    monkeypatch.setattr('rsik.adaptive_dispatch.build_dispatch_plan', unavailable)
    for assignment in plan['assignments']:
        assert validate_assignment(assignment, public, diagnosis) == assignment


@pytest.mark.parametrize('key,value', [('category', 'PRIVATE_SENTINEL'), ('format', 'PRIVATE_SENTINEL'),
                                      ('observation_ref', 'PRIVATE_SENTINEL')])
def test_public_projection_aliases_and_vocabularies_are_strict(key, value):
    public, diagnosis = fixture({'nonseed_members': 2}, ['BUDGET'])
    public['observations'][0][key] = value
    with pytest.raises(ContractError):
        build_dispatch_plan(public, diagnosis)

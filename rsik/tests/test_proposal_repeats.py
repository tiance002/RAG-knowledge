import copy
import json

import pytest

from rsik.analytics.proposal_repeats import project_proposal_repeats


SCOPE = {'parent_sha256': 'a' * 64, 'fixtures': {'unit': 'b' * 64},
         'objective_sha256': 'c' * 64, 'compatibility_fingerprint': 'd' * 64}
PROPOSAL = {'proposal_id': 'p1', 'round_id': 'r1',
            'worker_patch': {'old': 'return []\r\n', 'new': 'return [0]\r\n'}}


def outcome(slot='P1', *, result='REJECTED', origin='CANDIDATE', reasons=None):
    return {'slot': slot, 'selector_sha256': 'E' * 64, 'history': {
        'outcome': result, 'quality_verdict': 'REJECTED', 'execution_status': 'COMPLETED',
        'failure_origin': origin, 'reason_codes': reasons if reasons is not None else ['FACT_LOSS']}}


def project(proposals=None, outcomes=None, *, number=1, scope=SCOPE, prior=()):
    return project_proposal_repeats(
        proposals if proposals is not None else {'P1': PROPOSAL},
        outcomes if outcomes is not None else [outcome()],
        round_number=number, scope=scope, prior_rows=prior)


def test_previous_repeats_and_same_round_duplicates_are_separate_without_mutation():
    proposals = {'P1': PROPOSAL, 'P2': PROPOSAL}
    outcomes = [outcome(), outcome('P2')]
    original = copy.deepcopy((proposals, outcomes, SCOPE))
    first = project(proposals, outcomes)
    for row in first['rows']:
        assert row['prior_exact_repeat_count'] == 0
        assert row['same_round_exact_duplicate_count'] == 1
    second = project(number=2, prior=first['rows'])
    row = second['rows'][0]
    assert row['prior_exact_repeat_count'] == 2
    assert row['prior_exact_confirmed_failure_count'] == 2
    assert row['same_round_exact_duplicate_count'] == 0
    assert row['selector_sha256'] == 'e' * 64
    assert (proposals, outcomes, SCOPE) == original
    encoded = json.dumps(second, allow_nan=False)
    assert 'return ' not in encoded and 'worker_patch' not in encoded
    assert 'rejected' not in row


@pytest.mark.parametrize('result,origin,reasons,confirmed', [
    ('REJECTED', 'CANDIDATE', ['FACT_LOSS'], True),
    ('NO_GAIN', 'CANDIDATE', ['NO_GAIN'], False),
    ('REJECTED', 'INFRASTRUCTURE', ['FACT_LOSS'], False),
    ('REJECTED', 'PARENT', ['FACT_LOSS'], False),
    ('REJECTED', 'UNKNOWN', ['FACT_LOSS'], 'UNKNOWN'),
    ('OBSERVED', 'NONE', [], False),
    ('PATCH_INVALID', 'CONTRACT', ['STATIC_FORBIDDEN'], True),
    ('UNIT_FAILED', 'CANDIDATE', ['QUERY_UNIT_FAILED'], True),
])
def test_confirmed_failure_uses_existing_classification(result, origin, reasons, confirmed):
    first = project(outcomes=[outcome(result=result, origin=origin, reasons=reasons)])
    assert first['rows'][0]['confirmed_negative'] == confirmed
    # A forged cached classification cannot override original evidence.
    first['rows'][0]['confirmed_negative'] = True
    row = project(number=2, prior=first['rows'])['rows'][0]
    assert row['prior_exact_repeat_count'] == 1
    assert row['prior_exact_confirmed_failure_count'] == int(confirmed is True)


def test_scope_and_round_boundaries_exclude_incompatible_or_nonprior_rows():
    first = project()
    for scope in ({**SCOPE, 'parent_sha256': '0' * 64},
                  {**SCOPE, 'fixtures': {}}, {**SCOPE, 'objective_sha256': '0' * 64},
                  {**SCOPE, 'compatibility_fingerprint': '0' * 64}):
        row = project(number=2, scope=scope, prior=first['rows'])['rows'][0]
        assert row['prior_exact_repeat_count'] == 0
    assert project(prior=first['rows'])['rows'][0]['prior_exact_repeat_count'] == 0


def test_line_endings_normalize_but_formatting_uses_selector_equivalence():
    first = project()
    lf = copy.deepcopy(PROPOSAL)
    lf['worker_patch'] = {'old': 'return []\n', 'new': 'return [0]\n'}
    row = project({'P1': lf}, number=2, prior=first['rows'])['rows'][0]
    assert row['prior_exact_repeat_count'] == 1
    assert row['prior_ast_equivalent_byte_different_count'] == 0
    lf['worker_patch']['new'] = 'return [ 0 ]\n'
    row = project({'P1': lf}, number=2, prior=first['rows'])['rows'][0]
    assert row['prior_exact_repeat_count'] == 0
    assert row['prior_ast_equivalent_byte_different_count'] == 1
    assert row['prior_ast_equivalent_byte_different_confirmed_failure_count'] == 1


@pytest.mark.parametrize('patch', [None, {}, {'old': '', 'new': 'x'},
    {'old': 'x', 'new': 1}, {'old': 'x', 'new': 'y', 'extra': 'z'}])
def test_malformed_patch_and_missing_evidence_stay_unknown(patch):
    row = project({'P1': {'worker_patch': patch}}, [])['rows'][0]
    assert row['patch_sha256'] == row['selector_sha256'] == 'UNKNOWN'
    assert row['prior_exact_repeat_count'] == row['confirmed_negative'] == 'UNKNOWN'
    assert row['outcome'] == row['reason_codes'] == 'UNKNOWN'


def test_duplicate_outcome_evidence_is_unknown_and_scope_is_order_independent():
    row = project(outcomes=[outcome(), outcome()])['rows'][0]
    assert row['selector_sha256'] == row['confirmed_negative'] == 'UNKNOWN'
    assert project(scope=dict(reversed(list(SCOPE.items()))))['scope_sha256'] == project()['scope_sha256']

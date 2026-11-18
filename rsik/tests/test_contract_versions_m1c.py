"""Response, role identity and statistical protocol are independent versions."""
import pytest

from rsik.adjudicate import SLOTS
from rsik.contracts import (ADAPTIVE_DIAGNOSIS_VERSION, ANGLES, VERSION, ContractError,
                            role_schema, validate_diagnosis)
from rsik.family_decision import POLICY
from rsik.state import digest
from rsik.role_profiles import LEGACY, DEPENDENCY, M1_INDEPENDENT_GENERALIST_V1, roles_for
from rsik.tests.test_workflow import FakeRoles, public_context


@pytest.fixture
def diagnosis():
    public = public_context()
    legacy = FakeRoles().diagnose(public)['result']
    revised = {**legacy, 'schema_version': ADAPTIVE_DIAGNOSIS_VERSION, 'task_signals': [{
        'task_type': 'BUDGET', 'disposition': 'SUPPORT', 'evidence_refs': legacy['evidence_refs']}]}
    return public, legacy, revised


def test_legacy_still_reads_legacy_adaptive_channel_checks_version(diagnosis):
    public, legacy, revised = diagnosis
    assert validate_diagnosis(legacy, public) == legacy
    assert validate_diagnosis(revised, public, adaptive=True) == revised
    with pytest.raises(ContractError):
        validate_diagnosis(legacy, public, adaptive=True)
    with pytest.raises(ContractError):
        validate_diagnosis(revised, public)


@pytest.mark.parametrize('key,value', [('task_type', 'NEW_PERMISSION'),
                                       ('disposition', 'ASSERT_CERTAIN'),
                                       ('evidence_refs', ['private_answer_alias']),
                                       ('evidence_refs', []),
                                       ('gold', ['private_label'])])
def test_adaptive_signal_is_limited_to_validated_public_evidence(diagnosis, key, value):
    public, _, revised = diagnosis
    revised['task_signals'][0][key] = value
    with pytest.raises(ContractError):
        validate_diagnosis(revised, public, adaptive=True)


def test_adaptive_schema_does_not_change_existing_proposal_or_statistical_protocol():
    original = role_schema('diagnose')
    revised = role_schema('diagnose', adaptive=True)
    result = revised['properties']['result']['anyOf'][0]
    assert 'task_signals' in result['required']
    assert result['properties']['schema_version']['enum'] == [ADAPTIVE_DIAGNOSIS_VERSION]
    assert 'task_signals' not in original['properties']['result']['anyOf'][0]['required']
    assert original['properties']['result']['anyOf'][0]['properties']['schema_version']['enum'] == [VERSION]
    for profile in (LEGACY, DEPENDENCY, M1_INDEPENDENT_GENERALIST_V1):
        assert tuple(roles_for(profile)) == SLOTS
        for role in roles_for(profile).values():
            assert role_schema(role, adaptive=True) == role_schema(role)
    assert POLICY['version'] == 'fixed-holm3/v1'
    assert POLICY['family_size'] == 3


def test_all_pre_adaptive_response_schemas_match_pr6_fingerprint():
    old_roles = ('diagnose', 'curator', 'meta', *ANGLES)
    schemas = {role: role_schema(role) for role in old_roles}
    assert digest(schemas) == '287960bb86a19e17cbfc4f8596767f3c50d9594546f9a755e9b63976fc4b9b53'

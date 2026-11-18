"""Request and budget boundaries for the approved DEV_DEBUG run."""
import pytest

from rsik.contracts import ContractError
from rsik.dev_debug_live import MAX_COST_NANO, ROLES, _guard, _upper_bound


def patch_body():
    return {'model': 'gpt-6-luna', 'reasoning': {'effort': 'max'},
            'store': False, 'service_tier': 'default', 'max_output_tokens': 4096}


def test_development_authorization_covers_exact_six_roles():
    assert len(ROLES) == len(set(ROLES)) == 6
    assert _upper_bound() == 19_968_000 < MAX_COST_NANO
    _guard(patch_body(), 'P3_DEPENDENCY')


@pytest.mark.parametrize('change', [
    {'model': 'gpt-6.2-sol'},
    {'reasoning': {'effort': 'medium'}},
    {'service_tier': 'auto'},
    {'temperature': 0},
    {'tools': [{'type': 'web_search'}]},
    {'max_output_tokens': 4097},
    {'input': ['汉' * 6000]},
])
def test_dev_request_rejects_changes_outside_approval(change):
    with pytest.raises(ContractError, match='DEV_DEBUG_REQUEST_OUTSIDE_APPROVAL'):
        _guard({**patch_body(), **change}, 'P1_DEDUP')

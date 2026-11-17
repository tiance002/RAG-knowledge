"""Unique synthetic answer sentinels never become actual/retry model inputs."""
import base64
from copy import deepcopy
import json
from pathlib import Path
import sys
from urllib.parse import quote

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from rsik.contracts import ContractError, validate_diagnosis, validate_proposal, unwrap_response
from rsik.redact import LeakScanner, make_visible, project_summary


SENTINEL = 'RSI_EVAL_ONLY_SECRET_7d38b271'


def visible():
    observation = project_summary('NONSEED_EXPANSION', {'questions': 20, 'nonseed_members': 35}, include_count_evidence=True)
    return make_visible([observation], parent_code='def select_member_indices(members, chunks, files, seed_ids, selection):\n    return []\n',
                        scanner=LeakScanner([SENTINEL]))


@pytest.mark.parametrize('variant', [SENTINEL, ''.join(chr(ord(c)+0xFEE0) if 33<=ord(c)<=126 else c for c in SENTINEL),
    '\u200b'.join(SENTINEL), ''.join('\\u%04x' % ord(c) for c in SENTINEL),
    base64.b64encode(SENTINEL.encode()).decode(), quote(SENTINEL, safe='')])
def test_nested_unicode_and_derived_sentinels_are_blocked_before_model_request(variant):
    scanner = LeakScanner([SENTINEL])
    with pytest.raises(ContractError, match='LEAK_DETECTED') as caught:
        scanner.check({'input': {'system': [{'unknown_details': {'required_fact': variant}}]}})
    assert SENTINEL not in str(caught.value)


@pytest.mark.parametrize('field', ['details', 'answer', 'required_fact', 'forbidden', 'qid', 'source_path'])
def test_unknown_observation_fields_are_rejected_without_echoing_values(field):
    observation = project_summary('CONTEXT_LENGTH', {'questions': 20})
    observation[field] = SENTINEL
    with pytest.raises(ContractError, match='UNAUTHORIZED_PROJECTION_FIELD'):
        make_visible([observation], parent_code='code')


def test_counts_do_not_hide_private_nested_objects_and_unapproved_snippets():
    observation = project_summary('CONTEXT_LENGTH', {'questions': 20})
    observation['counts']['questions'] = {'secret': SENTINEL}
    with pytest.raises(ContractError): make_visible([observation], parent_code='code')
    observation = project_summary('CONTEXT_LENGTH', {'questions': 20}, include_count_evidence=True)
    observation['safe_evidence'][0]['text'] = 'UNAPPROVED SOURCE TEXT'
    with pytest.raises(ContractError): make_visible([observation], parent_code='code')


def test_only_opaque_aliases_counts_and_controlled_code_are_visible():
    public = visible()
    serialized = json.dumps(public)
    assert SENTINEL not in serialized and 'source_path' not in serialized and 'required_fact' not in serialized
    assert public['parent_alias'].startswith('parent-')
    assert public['observation_refs'][0].startswith('observation-')


def test_diagnosis_cannot_invent_a_quote_and_confidence_is_bounded():
    public = visible()
    evidence = next(iter(public['evidence_quotes']))
    diagnosis = {'schema_version': 'rsi-v2.2', 'observation_refs': public['observation_refs'],
        'problem_layer': 'SELECTION', 'first_deviation': 'UNKNOWN', 'evidence_refs': [evidence],
        'evidence_quote': public['evidence_quotes'][evidence], 'hypothesis': 'SYNTHETIC hypothesis',
        'fixable_within_seam': True, 'required_data': [], 'confidence': .5}
    validate_diagnosis(diagnosis, public)
    diagnosis['evidence_quote'] = 'invented quote'
    with pytest.raises(ContractError, match='HALLUCINATED_QUOTE'): validate_diagnosis(diagnosis, public)
    diagnosis['evidence_quote'], diagnosis['confidence'] = None, float('nan')
    with pytest.raises(ContractError): validate_diagnosis(diagnosis, public)


def proposal(public):
    return {'schema_version': 'rsi-v2.2', 'proposal_id': 'synthetic-proposal', 'round_id': public['round_id'],
        'angle': 'P1_DEDUP', 'parent_id': public['parent_alias'], 'observation_ref': public['observation_refs'][0],
        'hypothesis': 'SYNTHETIC hypothesis', 'change': 'SYNTHETIC change', 'expected_impact': 'SYNTHETIC impact',
        'falsification': 'Any necessary fact lost refutes the hypothesis.', 'worker_patch': {'old': 'return []', 'new': 'return list([])'},
        'evidence_refs': [], 'handbook_entry_ids': [], 'risk_assessment': 'SYNTHETIC risk', 'confidence': .5}


@pytest.mark.parametrize('placeholder', ['none', 'N/A', '无', '  n o n e ', 'Ｎ／Ａ'])
def test_proposal_falsification_cannot_be_a_placeholder(placeholder):
    public = visible();value = proposal(public);value['falsification'] = placeholder
    with pytest.raises(ContractError): validate_proposal(value, public, 'P1_DEDUP')


def test_proposal_identity_and_handbook_references_are_input_bound():
    public = visible();value = proposal(public)
    validate_proposal(value, public, 'P1_DEDUP')
    value['parent_id'] = 'invented parent'
    with pytest.raises(ContractError, match='OBSERVATION_INVALID'): validate_proposal(value, public, 'P1_DEDUP')
    value['parent_id'] = public['parent_alias'];value['handbook_entry_ids'] = ['never-visible']
    with pytest.raises(ContractError, match='UNAUTHORIZED_HANDBOOK_REFERENCE'): validate_proposal(value, public, 'P1_DEDUP')


def test_abstention_is_explicit_and_cannot_smuggle_a_patch():
    assert unwrap_response({'status': 'ABSTAIN', 'result': None, 'reason': 'insufficient evidence'}) is None
    with pytest.raises(ContractError): unwrap_response({'status': 'ABSTAIN', 'result': {'worker_patch': {}}, 'reason': 'hidden'})


def test_secret_in_parent_code_is_blocked_before_any_request():
    with pytest.raises(ContractError, match='SECRET_DETECTED'):
        make_visible([], parent_code="TOKEN=" + ("sk-" + "SYNTHETIC_TEST_ONLY_" * 3))

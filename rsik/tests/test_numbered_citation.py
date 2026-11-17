"""CONTROL-only numbered-delivery contract tests; no real qualification claim."""
import copy
import json

import pytest

from rsik.events import canonical
from rsik.source_relations import assess, numbered_citation_support, validate_spec, recompute_coverage, sha
from test_source_relations import _append_citations, _build_fixture, _capture, _review


@pytest.fixture
def numbered(tmp_path):
    directory = _build_fixture(tmp_path/'control', count=1)
    item = json.loads((directory/'cases.json').read_text(encoding='utf-8'))[0]
    spec = json.loads((directory/'relations.restricted.json').read_text(encoding='utf-8'))[item['qid']]
    spec['citation_binding_profile'] = 'numbered-delivery/v1'
    case = item['case']
    files = {f['id']:f for f in case['files']}
    prefix = 'CONTROL SYSTEM INSTRUCTIONS\n\nDocument context:\n'
    context, members, proofs = '', [], []
    for ordinal, chunk in enumerate(case['chunks'], 1):
        if context:
            context += '\n\n'
        file = files[chunk['file_id']]
        # Same formatter shape as the frozen worker, including location text.
        context += f"[{ordinal}] 来源：{file['name']} record {ordinal}\n"
        start = len(context)
        context += chunk['content']
        members.append({**chunk,'kb_id':file['kb_id']})
        proofs.append({'id':chunk['id'],'context_start':start,'context_end':len(context),
            'prompt_start':len(prefix)+start,'prompt_end':len(prefix)+len(context)})
    capture = {'status':'OK','members':members,'proof':proofs,'context':context,
        'messages':[{'role':'system','content':prefix+context},
                    {'role':'user','content':case['question']}], 'request_calls':1}
    sources = {sid:(directory/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
    return case, spec, sources, capture


def test_numbered_source_headers_support_actual_context_and_prompt(numbered):
    case, spec, sources, capture = numbered
    assert 'citations' not in capture
    assert capture['proof'][0]['prompt_start'] > capture['proof'][0]['context_start']
    assert all(numbered_citation_support(case,capture,spec,s)
               for s in spec['atoms'][0]['supports'])
    assert assess(case,capture,spec,sources)['status'] == 'PRESENT'


@pytest.mark.parametrize('old,new', [('s1.md','s9.md'), ('[1] 来源：','[9] 来源：')])
def test_wrong_filename_or_ordinal_rejects_supported_body(numbered, old, new):
    case, spec, sources, capture = numbered
    capture['context'] = capture['context'].replace(old,new,1)
    capture['messages'][0]['content'] = capture['messages'][0]['content'].replace(old,new,1)
    support = spec['atoms'][0]['supports'][0]
    assert not numbered_citation_support(case,capture,spec,support)
    result = assess(case,capture,spec,sources)
    assert result['status'] == 'ABSENT'
    assert result['atoms'][0]['reason'] == 'CITATION_SUPPORT_MISMATCH'


def test_numbered_source_header_cannot_claim_another_source(numbered):
    case, spec, sources, capture = numbered
    support = copy.deepcopy(spec['atoms'][0]['supports'][0])
    support['source_id'] = spec['atoms'][0]['supports'][1]['source_id']
    assert not numbered_citation_support(case,capture,spec,support)
    capture['members'][0]['file_id'] = case['files'][1]['id']
    assert assess(case,capture,spec,sources)['status'] == 'UNKNOWN'


def test_member_kb_ownership_mismatch_is_unknown(numbered):
    case, spec, sources, capture = numbered
    capture['members'][0]['kb_id'] = 'UNAUTHORIZED-CONTROL-KB'
    result = assess(case,capture,spec,sources)
    assert result['status'] == 'UNKNOWN'
    assert result['reason'] == 'RELATIONS_DELIVERY_SOURCE_MISMATCH'


def test_wrong_actual_prompt_span_is_unknown(numbered):
    case, spec, sources, capture = numbered
    capture['proof'][0]['prompt_start'] += 1
    assert not numbered_citation_support(case,capture,spec,spec['atoms'][0]['supports'][0])
    assert assess(case,capture,spec,sources)['status'] == 'UNKNOWN'


@pytest.mark.parametrize('profile', [None, 'explicit-citations/v1'])
def test_explicit_citation_legacy_contract_remains_required(numbered, profile):
    case, spec, sources, capture = numbered
    if profile is None:
        spec.pop('citation_binding_profile')
    else:
        spec['citation_binding_profile'] = profile
    assert assess(case,capture,spec,sources)['status'] == 'ABSENT'
    _append_citations(capture,spec)
    # This legacy mode checks explicit source intervals, irrespective of headers.
    capture['context'] = capture['context'].replace('s1.md','s9.md',1)
    capture['messages'][0]['content'] = capture['messages'][0]['content'].replace('s1.md','s9.md',1)
    assert assess(case,capture,spec,sources)['status'] == 'PRESENT'


def test_unknown_binding_profile_fails_fixture_validation(numbered):
    case, spec, sources, capture = numbered
    spec['citation_binding_profile'] = 'invented-permissive/v99'
    with pytest.raises(ValueError,match='RELATIONS_CITATION_PROFILE_REQUIRED'):
        validate_spec(case,spec,sources)
    assert assess(case,capture,spec,sources)['status'] == 'UNKNOWN'


def test_control_cannot_credit_another_atoms_missing_support(tmp_path):
    directory = _build_fixture(tmp_path/'control',count=1)
    cases = json.loads((directory/'cases.json').read_text(encoding='utf-8'))
    specs = json.loads((directory/'relations.restricted.json').read_text(encoding='utf-8'))
    item = cases[0]; spec = specs[item['qid']]
    target = copy.deepcopy(spec['atoms'][0])
    target['atom_id'] = 'TARGET-STILL-PRESENT'
    target['components'] = {k:target['components'][k] for k in ('entity','subject','value','unit')}
    target['categories'] = ['CITATION_SUPPORT']
    target['supports'] = target['supports'][:1]
    target['support_alternatives'] = [[target['supports'][0]['support_id']]]
    target.pop('review'); target['review'] = _review(target)
    spec['atoms'].append(target)
    (directory/'relations.restricted.json').write_bytes(canonical(specs))
    manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
    manifest['artifact_hashes']['relations.restricted.json'] = sha((directory/'relations.restricted.json').read_bytes())
    (directory/'manifest.json').write_bytes(canonical(manifest))
    positive = _append_citations(_capture(item['case']),spec)
    negative = _append_citations(_capture(item['case'],include=('m1',)),spec)
    sources = {sid:(directory/s['relative_path']).read_bytes() for sid,s in spec['sources'].items()}
    result = assess(item['case'],negative,spec,sources)
    assert result['status'] == 'ABSENT'
    assert {a['atom_id']:a['status'] for a in result['atoms']}[target['atom_id']] == 'PRESENT'
    positive_path = tmp_path/'positive.json'; negative_path = tmp_path/'negative.json'
    positive_path.write_bytes(canonical(positive)); negative_path.write_bytes(canonical(negative))
    record = {'schema':'rsi-source-relations-coverage/v1','base_dir':str(tmp_path),
        'fixtures':['control'],'controls':[{'fixture':'control','qid':item['qid'],
            'atom_id':target['atom_id'],'positive_capture':'positive.json',
            'positive_sha256':sha(positive_path.read_bytes()),'negative_capture':'negative.json',
            'negative_sha256':sha(negative_path.read_bytes()),'removed_member_ids':['m2']}]}
    with pytest.raises(ValueError,match='RELATIONS_NEGATIVE_CONTROL_FAILED'):
        recompute_coverage(record)

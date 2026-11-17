"""MAIN source-neighbor controls use invented text, never private DEV data."""
from copy import deepcopy
import hashlib
import json

import pytest

from test_relations_stress_suites import inputs,rebind
from scripts.build_rsi_relations_main import build,build_matched_suites,prepare_main
from rsik.events import canonical


def main_inputs(tmp_path):
    source,base,review=inputs(tmp_path)
    provenance=json.loads((source/'provenance.restricted.json').read_text(encoding='utf-8-sig'))
    old=json.loads((source/'cases.json').read_text(encoding='utf-8-sig'))
    for row,r,old_row in zip(base['cases'],review['cases'],old,strict=True):
        origin=row['origin_qids'][0];p=provenance[origin];path=source/p['source_relative_path']
        raw=path.read_bytes()+b' Other detail.';path.write_bytes(raw)
        p['source_sha256']=hashlib.sha256(raw).hexdigest()
        end=row['members'][-1]['end'];tail={'key':'unused','origin_qid':origin,'start':end,'end':len(raw.decode()),
                                          'text_sha256':hashlib.sha256(raw.decode()[end:].encode()).hexdigest()}
        p['member_spans']={'original-tail':{'start':end,'end':len(raw.decode())}}
        old_row['case']['chunks']=[{'id':'original-tail','file_id':origin,'original_index':3,'content':raw.decode()[end:]}]
        r['additional_members']=[tail]
        r['original_neighbor_evidence']=[{'member_key':'unused','origin_qid':origin,
            'original_member_id':'original-tail','start':end,'end':len(raw.decode()),'source_sha256':p['source_sha256']}]
        rebind(r)
    (source/'provenance.restricted.json').write_bytes(canonical(provenance))
    (source/'cases.json').write_bytes(canonical(old))
    review.update(schema='rsi-relations-main-review/v1',source_layout_profile='CONTROL-original-neighbors/v1')
    return source,base,review


def test_main_build_uses_real_neighbors_without_changing_atoms(tmp_path):
    source,base,review=main_inputs(tmp_path)
    bp=tmp_path/'base.json';rp=tmp_path/'review.json'
    bp.write_bytes(canonical(base));rp.write_bytes(canonical(review))
    output=tmp_path/'main';result=build(source,bp,rp,output)
    assert result['atoms']==20
    assert result['independent_question_origins']==20
    assert result['additional_independent_samples']==0
    assert result['static_opportunity']['removable_nonseed_total']==20
    assert result['static_opportunity']['all_reachable_content_under_4000']
    assert result['execution_status']=='NOT_EXECUTED'
    spec=json.loads((output/'relations.restricted.json').read_text(encoding='utf-8-sig'))['CONTROL-0']
    assert spec['atoms'][0]['components']==base['cases'][0]['atoms'][0]['components']
    assert spec['atoms'][0]['categories']==base['cases'][0]['atoms'][0]['categories']
    assert [s['text'] for s in spec['atoms'][0]['supports']]==['only when sealed',' may carry water.']
    with pytest.raises(ValueError,match='FRESH_MAIN_DIRECTORY_REQUIRED'):build(source,bp,rp,output)


def test_matched_suites_keep_main_neighbor_proofs_and_one_lineage(tmp_path):
    source,base,review=main_inputs(tmp_path)
    bp=tmp_path/'base.json';rp=tmp_path/'review.json'
    bp.write_bytes(canonical(base));rp.write_bytes(canonical(review))
    output=tmp_path/'matched';result=build_matched_suites(source,bp,rp,output)
    manifests=[json.loads((output/s/'manifest.json').read_text(encoding='utf-8-sig')) for s in result['suites']]
    assert {m['lineage_id'] for m in manifests}=={review['lineage_id']}
    assert {m['source_layout_profile'] for m in manifests}=={review['source_layout_profile']}
    derived=json.loads((tmp_path/'matched.review-derivation.restricted.json').read_text(encoding='utf-8-sig'))
    assert derived['cases']==review['cases']
    assert derived['derived_from_main_review_sha256']==result['main_review_sha256']
    assert result['suites']['question-paraphrase']['changed_questions']==20
    assert result['suites']['forward-seed']['changed_seed_cases']==20
    assert result['suites']['backward-seed']['changed_seed_cases']==20
    bad=deepcopy(review);bad['cases'][0]['original_neighbor_evidence']=[];rebind(bad['cases'][0])
    rp.write_bytes(canonical(bad))
    with pytest.raises(ValueError,match='ALL_ADDED_NEIGHBORS_REQUIRE'):
        build_matched_suites(source,bp,rp,tmp_path/'rejected')
    assert not (tmp_path/'rejected.review-derivation.restricted.json').exists()


@pytest.mark.parametrize('malformed,reason',[
    ('missing','ALL_ADDED_NEIGHBORS_REQUIRE'),
    ('interval','ORIGINAL_NEIGHBOR_IDENTITY_CHANGED'),
    ('not_original','NEIGHBOR_NOT_IN_FROZEN_CASE'),
    ('gap','MUST_BE_CONTIGUOUS'),
    ('cap','FIXED_CHARACTER_CAP_EXCEEDED'),
])
def test_main_rejects_unproven_neighbors_and_character_overflow(tmp_path,malformed,reason):
    source,base,review=main_inputs(tmp_path);r=review['cases'][0]
    if malformed=='missing':r['original_neighbor_evidence']=[]
    elif malformed=='interval':r['original_neighbor_evidence'][0]['end']-=1
    elif malformed=='not_original':
        old=json.loads((source/'cases.json').read_text(encoding='utf-8-sig'));old[0]['case']['chunks']=[]
        (source/'cases.json').write_bytes(canonical(old))
    elif malformed=='gap':
        r['member_replacements']={'conclusion':{**base['cases'][0]['members'][2],'end':base['cases'][0]['members'][2]['end']-1}}
    else:
        # Declared layout lengths alone must be rejected before materialization;
        # source interval/bytes validation remains an additional curator gate.
        original=base['cases'][0]['members'][0]
        r['member_replacements']={'title':{**original,'end':5000}}
    rebind(r)
    with pytest.raises(ValueError,match=reason):prepare_main(source,base,review)

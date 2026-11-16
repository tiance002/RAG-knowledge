"""Offline fixture controls; no paid model or dynamic candidate execution."""
from copy import deepcopy
import hashlib
import json

import pytest

from scripts.build_rsi_relations_stress import build, directionality, prepare_plans, validate_geometry
from rsik.events import canonical, digest
from rsik.source_relations import check_fixture


def inputs(tmp_path, count=20):
    source = tmp_path / 'train'; (source / 'sources').mkdir(parents=True)
    rows, provenance, old = [], {}, []
    for i in range(count):
        qid = f'CONTROL-{i}'; origin = f'origin-{i}'
        texts = ['Valve A', 'only when sealed', ' may carry water.']
        raw = '\n'.join(texts).encode(); h = hashlib.sha256(raw).hexdigest()
        path = 'sources/' + origin + '.txt'; (source / path).write_bytes(raw)
        provenance[origin] = {'source_sha256':h,'source_relative_path':path,'file_id':origin,'kb_id':'CONTROL'}
        old.append({'qid':origin,'case':{'files':[{'id':origin,'name':origin+'.txt','kb_id':'CONTROL'}]}})
        members=[]; start=0
        for key, text in zip(('title','condition','conclusion'),texts):
            members.append({'key':key,'origin_qid':origin,'start':start,'end':start+len(text),
                            'text_sha256':hashlib.sha256(text.encode()).hexdigest()})
            start += len(text)+1
        row={'qid':qid,'origin_qids':[origin],'question':'When may Valve A carry water?',
             'question_origin':'SYNTHETIC_CONTROL','members':members,'seeds':['condition'],
             'atoms':[{'atom_id':'a','categories':['NONSEED_NECESSITY','NEGATION_CONDITION','CITATION_SUPPORT'],
                'citation_required':True,'components':{'entity':'water','condition':'only when sealed'},
                'supports':[{'support_id':'s0','member':'condition','binds':['condition']},
                            {'support_id':'s1','member':'conclusion','binds':['entity']}],
                'support_alternatives':[['s0','s1']],'remove_members':['conclusion']}]}
        rows.append(row)
    base={'partition':'DEV','lineage_id':'CONTROL-base','reviewer':'CONTROL','reviewed_at':'2026-11-16T00:00:00Z','cases':rows}
    reviews=[]
    for row in rows:
        r={'qid':row['qid'],'question_paraphrase':'Under what condition may Valve A carry water?',
           'primary_seed':['title','conclusion'],
           'forward_seed':['condition'],'backward_seed':['conclusion'],'additional_members':[],
           'equivalence_rationale':'SYNTHETIC same condition and object',
           'semantic_review_scope':{'original_question':row['question'],'atoms_digest':digest(row['atoms']),
                                    'source_member_intervals_digest':digest(row['members'])}}
        r['subject_sha256']=digest(r); reviews.append(r)
    review={'schema':'rsi-relations-stress-review/v1','review_status':'AI_REVIEWED',
            'base_curation_digest':digest(base),'lineage_id':'CONTROL-stress','reviewer':'CONTROL',
            'reviewed_at':'2026-11-16T00:00:00Z','cases':reviews}
    for name,value in [('provenance.restricted.json',provenance),('cases.json',old),('manifest.json',{})]:
        (source/name).write_bytes(canonical(value))
    return source, base, review


def rebind(row):
    row['subject_sha256']=digest({k:v for k,v in row.items() if k!='subject_sha256'})


def test_full_offline_builder_preserves_supports_and_denominator(tmp_path):
    source, base, review=inputs(tmp_path)
    bp=tmp_path/'base.json'; rp=tmp_path/'review.json'
    # Accept explicit UTF8 BOM input; all artifacts still use canonical LF bytes.
    bp.write_bytes(b'\xef\xbb\xbf'+canonical(base)); rp.write_bytes(canonical(review))
    output=tmp_path/'output'; result=build(source,bp,rp,output)
    assert result['additional_independent_samples']==0
    assert result['independent_question_origins']==20
    assert result['execution_status']=='NOT_EXECUTED'
    assert result['suites']['question-paraphrase']['changed_questions']==20
    assert result['suites']['backward-seed']['changed_seed_cases']==20
    specs=[]
    for suite in result['suites']:
        check_fixture(output/suite,expected_count=20,review_policy='research-source-family/v1')
        specs.append(json.loads((output/suite/'relations.restricted.json').read_text(encoding='utf-8-sig')))
    assert all(s==specs[0] for s in specs)
    assert all(r['nonseed_supports'][0]['only_preceding_support'] for r in result['suites']['backward-seed']['directionality'])
    with pytest.raises(ValueError,match='FRESH_STRESS_DIRECTORY_REQUIRED'):
        build(source,bp,rp,output)


def test_review_content_and_origin_binding_fail_closed(tmp_path):
    _,base,review=inputs(tmp_path,1)
    bad=deepcopy(review);bad['cases'][0]['question_paraphrase']='tampered'
    with pytest.raises(ValueError,match='REVIEW_BINDING'):prepare_plans(base,bad)
    bad=deepcopy(review);bad['cases'][0]['semantic_review_scope']['atoms_digest']='wrong';rebind(bad['cases'][0])
    with pytest.raises(ValueError,match='SEMANTIC_REVIEW_SCOPE'):prepare_plans(base,bad)
    bad=deepcopy(review);bad['cases'][0]['question_paraphrase']=base['cases'][0]['question'];rebind(bad['cases'][0])
    with pytest.raises(ValueError,match='PARAPHRASE_MUST_CHANGE'):prepare_plans(base,bad)
    bad=deepcopy(review);bad['base_curation_digest']='stale'
    with pytest.raises(ValueError,match='REVIEW_BASE_CHANGED'):prepare_plans(base,bad)
    bad=deepcopy(review);bad['cases'][0]['additional_members']=[{'key':'foreign','origin_qid':'other','start':0,'end':2,'text_sha256':'unused'}];rebind(bad['cases'][0])
    with pytest.raises(ValueError,match='SOURCE_ORIGIN_SET_CHANGED'):prepare_plans(base,bad)
    bad=deepcopy(review);bad['cases'][0]['support_member_remap']={'condition':'conclusion'};rebind(bad['cases'][0])
    with pytest.raises(ValueError,match='EXACT_SUPPORT_IDENTITY_LOST'):prepare_plans(base,bad)


def test_fixed_window_and_nonseed_necessity_cannot_be_relaxed(tmp_path):
    _,base,_=inputs(tmp_path,1);row=base['cases'][0]
    with pytest.raises(ValueError,match='NONSEED_NECESSITY_LOST'):
        validate_geometry(row,['condition','conclusion'])
    with pytest.raises(ValueError,match='OUTSIDE_FIXED_WINDOW'):
        validate_geometry(row,['title'])
    with pytest.raises(ValueError,match='DIRECTION_NOT_ESTABLISHED'):
        validate_geometry(row,['condition'],'backward')


def test_mixed_anchors_are_never_reported_as_pure_backward(tmp_path):
    _,base,_=inputs(tmp_path,1);row=base['cases'][0]
    row['seeds']=['title','conclusion']
    detail=directionality(base)[0]['nonseed_supports'][0]
    assert detail['reaching_seed_offsets']==[1,-1]
    assert not detail['only_preceding_support']
    assert not detail['only_following_support']
    with pytest.raises(ValueError,match='DIRECTION_NOT_ESTABLISHED'):
        validate_geometry(row,row['seeds'],'backward')


@pytest.mark.parametrize('malformed,reason',[
    ('unchanged','SEED_SET_MUST_CHANGE'),
    ('mixed','DIRECTION_NOT_ESTABLISHED'),
    ('opposite','DIRECTION_NOT_ESTABLISHED'),
])
def test_declared_directional_review_cannot_bypass_geometry(tmp_path,malformed,reason):
    _,base,review=inputs(tmp_path,1)
    r=review['cases'][0]
    if malformed=='unchanged':r['forward_seed']=r['primary_seed']
    elif malformed=='opposite':r['forward_seed']=['conclusion']
    else:
        # Distinct primary with a different chunk makes the unchanged-seed check
        # independent of the mixed-anchor direction check.
        r['primary_seed']=['condition'];r['forward_seed']=['title','conclusion']
    rebind(r)
    with pytest.raises(ValueError,match=reason):prepare_plans(base,review)


def test_overlapping_layout_keeps_original_submember_support_bytes(tmp_path):
    source,base,review=inputs(tmp_path)
    provenance=json.loads((source/'provenance.restricted.json').read_text(encoding='utf-8-sig'))
    for row,r in zip(base['cases'],review['cases'],strict=True):
        origin=row['origin_qids'][0]; path=source/provenance[origin]['source_relative_path']
        raw=path.read_bytes()+b' Other detail.'; path.write_bytes(raw)
        provenance[origin]['source_sha256']=hashlib.sha256(raw).hexdigest()
        condition,conclusion=row['members'][1:]
        expanded={**conclusion,'start':condition['start']}
        expanded['text_sha256']=hashlib.sha256(raw.decode()[expanded['start']:expanded['end']].encode()).hexdigest()
        tail={**conclusion,'key':'tail','start':conclusion['end'],'end':len(raw.decode()),
              'text_sha256':hashlib.sha256(raw.decode()[conclusion['end']:].encode()).hexdigest()}
        r.update(member_replacements={'conclusion':expanded},additional_members=[tail],
                 support_member_remap={'condition':'conclusion'},primary_seed=['condition','tail'],
                 forward_seed=['condition'],backward_seed=['tail'])
        rebind(r)
    (source/'provenance.restricted.json').write_bytes(canonical(provenance))
    bp=tmp_path/'base.json';rp=tmp_path/'review.json'
    bp.write_bytes(canonical(base));rp.write_bytes(canonical(review))
    result=build(source,bp,rp,tmp_path/'overlap')
    assert result['suites']['primary']['static_opportunity']['removable_nonseed_total']==20
    assert result['suites']['backward-seed']['changed_seed_cases']==20
    assert result['suites']['forward-seed']['changed_seed_cases']==20
    spec=json.loads((tmp_path/'overlap/backward-seed/relations.restricted.json').read_text(encoding='utf-8-sig'))['CONTROL-0']
    supports=spec['atoms'][0]['supports']
    assert supports[0]['member_id']==supports[1]['member_id']=='CONTROL-0-conclusion'
    assert supports[0]['text']=='only when sealed'
    assert supports[1]['text']==' may carry water.'
    assert supports[0]['start']==base['cases'][0]['members'][1]['start']
    assert supports[1]['start']==base['cases'][0]['members'][2]['start']
    # Expanded containing members do not expand a support's exact interval.
    assert supports[0]['end']<supports[1]['end']

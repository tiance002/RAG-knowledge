"""Synthetic compiler counterexamples; no model or private dataset access."""
import hashlib
import pytest
from scripts.build_rsi_qa300_fixed_history import projection,exact_quote_matches,literal_components,compile_atom,deliverable_seed_ids


def row(mid,index,text):
    return {'id':mid,'original_index':index,'file_id':'f','content':text,
            'content_sha256':hashlib.sha256(text.encode()).hexdigest()}


def test_native_projection_preserves_utf8_index_and_parent_child_strings():
    rows=[row('child',9,'中文 evidence'),row('parent',7,'parent 中文 evidence')]
    raw,spans=projection(rows)
    assert raw.decode()=='parent 中文 evidence\n\n中文 evidence'
    for r in rows:assert raw.decode()[spans[r['id']]['start']:spans[r['id']]['end']]==r['content']
    assert [r['original_index'] for r in rows]==[9,7]


def test_native_tamper_and_positional_collision_refused():
    bad=row('a',1,'a');bad['content']='changed'
    with pytest.raises(ValueError,match='TAMPER'):projection([bad])
    with pytest.raises(ValueError,match='COLLISION'):projection([row('a',1,'a'),row('b',1,'b')])


def test_quote_is_exact_no_normalization_or_invented_join_fragments():
    text='alpha\nbeta';record={'quote_fields':[{'text':text,'text_sha256':hashlib.sha256(text.encode()).hexdigest(),'field_pointer':'excerpt'}]}
    assert not exact_quote_matches(record,[row('a',0,'alpha'),row('b',1,'beta')])
    escaped=exact_quote_matches(record,[row('a',0,'alpha\\nbeta')])
    assert escaped[0]['member_id']=='a'
    assert escaped[0]['literal_transform']=='newline-to-literal-backslash-n/v1'
    assert not exact_quote_matches(record,[row('a',0,'alpha\\r\\nbeta')])
    assert exact_quote_matches(record,[row('a',0,text)])[0]['member_id']=='a'
    record['quote_fields'][0]['text']='tampered'
    with pytest.raises(ValueError,match='TAMPER'):exact_quote_matches(record,[])


def test_null_components_omitted_ownership_retained_and_qualifiers_not_fabricated():
    assert literal_components({'components':{'entity':'e','ownership':'owner','unit':None,'qualifiers':{}}})=={'entity':'e','scope':'owner'}
    with pytest.raises(ValueError,match='QUALIFIERS'):literal_components({'components':{'entity':'e','qualifiers':{'a':'b'}}})


def test_semantic_gold_component_is_not_silently_replaced_by_quote_text():
    atom={'atom_id':'a','components':{'entity':'abstract entity','relation':'date','value':'2020'},
          'coverage_categories':['引用支持'],'support_alternatives':[{'join':'AND','evidence_refs':[{'record_ref':'q:evidence[0]'}]}]}
    native=row('m',1,'publication 2020');raw,positions=projection([native]);loc={'source_id':'s',**positions['m']}
    evidence={'q:evidence[0]':[{'member_id':'m','start':0,'end':16,'text':'publication 2020',
        'native_length':16,'quote_sha256':hashlib.sha256(b'publication 2020').hexdigest()}]}
    compiled,gaps=compile_atom(atom,evidence,{'m':loc},{},{'seed_ids':[]},'fixed-test-time')
    assert compiled['components']==atom['components']
    assert compiled['accepted_gold_claim']==atom
    assert any(g['code']=='TYPED_COMPONENT_NOT_LITERAL_IN_AND_SUPPORT' and g['components']==['entity','relation'] for g in gaps)


def test_each_AND_evidence_member_required_and_missing_evidence_is_explicit():
    atom={'atom_id':'a','components':{'entity':'e'},'coverage_categories':['引用支持'],
          'support_alternatives':[{'join':'AND','evidence_refs':[{'record_ref':'one'},{'record_ref':'two'}]}]}
    compiled,gaps=compile_atom(atom,{}, {},{}, {'seed_ids':[]},'fixed-test-time')
    assert {g['record_ref'] for g in gaps if g['code']=='EXACT_NATIVE_QUOTE_MISSING'}=={'one','two'}
    assert compiled['support_alternatives']==[[]]


def test_deliverable_seed_replaces_oversize_parent_with_real_child_and_keeps_support_nonseed():
    parent=row('parent',0,'p'*4100);child=row('child',1,'fact');child['parent_id']='parent';child['file_id']='f'
    case={'chunks':[parent,child],'seed_ids':['parent']}
    spec={'atoms':[{'supports':[{'member_id':'parent','support_id':'s1'}],
                    'support_alternatives':[['s1']]}]}
    seeds,reason=deliverable_seed_ids(case,spec,['parent'])
    assert seeds==['child'] and reason=='REAL_NATIVE_DELIVERABLE_SEED'


def test_deliverable_seed_covers_second_source_file_and_refuses_impossible_group():
    p1=row('p1',0,'p'*4100);c1=row('c1',1,'a');c1['parent_id']='p1'
    p2=row('p2',0,'q'*4100);p2['file_id']='g';c2=row('c2',1,'b');c2['parent_id']='p2';c2['file_id']='g'
    case={'chunks':[p1,c1,p2,c2],'seed_ids':['p1','p2']}
    spec={'atoms':[{'supports':[{'member_id':'p1','support_id':'s1'},{'member_id':'p2','support_id':'s2'}],
                    'support_alternatives':[['s1','s2']]}]}
    seeds,reason=deliverable_seed_ids(case,spec,['p1','p2'])
    assert set(seeds)=={'c1','c2'} and reason=='REAL_NATIVE_DELIVERABLE_SEED'
    impossible={'chunks':[row('only',0,'x'*4100)],'seed_ids':['only']}
    spec2={'atoms':[{'supports':[{'member_id':'only','support_id':'s1'}],'support_alternatives':[['s1']]}]}
    seeds2,reason2=deliverable_seed_ids(impossible,spec2,['only'])
    assert seeds2==['only'] and reason2=='NO_DELIVERABLE_SEED'

"""Deterministic experiment-only source-scope checks; no external clients."""
import copy
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'scripts'))
from e21_context_selection import grounded_messages, select_members

TARGET = 'a'*64
OTHER = 'b'*64
QUESTION = f'In the transportation report {TARGET[:12]}, what is the cost?'


def member(mid, file=TARGET, text='synthetic body', **extra):
    return dict(chunk_id=mid, fileId='office-'+file, fileName=file+'.docx', content=text,
                source_mapping_status='VERIFIED', content_hash=hashlib.sha256(text.encode()).hexdigest(),
                page_number=None, **extra)


@pytest.mark.parametrize('relation', ['seed','previous_neighbor','next_neighbor','parent_sibling'])
def test_target_gold_and_all_structures_retained(relation):
    rows=[member('seed'), member('evidence',text='REQUIRED FACT; title; headers; units; footnote',
                                evidence_relation=relation)]
    actual,_=select_members(QUESTION,rows,['seed'])
    assert actual==rows


def test_partial_evidence_and_unknown_target_boilerplate_are_kept():
    rows=[member('seed',text='partial fact'), member('neighbor',text='remaining fact'),
          member('unknown',text='unreviewed content'),member('other',OTHER)]
    saved=copy.deepcopy(rows)
    actual,info=select_members(QUESTION,rows,['seed'])
    assert [x['chunk_id'] for x in actual]==['seed','neighbor','unknown']
    assert info['excluded_evidence_ids']==['other'] and rows==saved


def test_all_initial_candidates_kept_even_when_grouped_as_neighbors():
    rows=[member('s'),member('rank2',OTHER),member('new',OTHER)]
    actual,_=select_members(QUESTION,rows,['s','rank2'])
    assert [x['chunk_id'] for x in actual]==['s','rank2']


@pytest.mark.parametrize('question', ['What does table T07 say?',
    QUESTION+' Compare both reports.', 'Not '+QUESTION,
    QUESTION.replace('what is','what is not')])
def test_uncertain_or_multiple_scope_conservative_keep(question):
    rows=[member('s'),member('other',OTHER)]
    assert select_members(question,rows,['s'])[0]==rows


def test_ambiguous_hash_prefix_keeps_all():
    rows=[member('s'),member('same_prefix','a'*12+'c'*52),member('other',OTHER)]
    assert select_members(QUESTION,rows,['s'])[0]==rows


@pytest.mark.parametrize('corrupt', ['missing_mapping','hash','file_identity'])
def test_uncertain_provenance_conservative_keep(corrupt):
    rows=[member('s'),member('other',OTHER)]
    if corrupt=='missing_mapping': rows[1].pop('source_mapping_status')
    elif corrupt=='hash': rows[1]['content_hash']='invalid'
    else: rows[1]['fileName']='unverified.docx'
    assert select_members(QUESTION,rows,['s'])[0]==rows


def test_conflicting_same_id_keeps_all_without_forging_identity():
    rows=[member('s'),member('same',OTHER,text='one'),member('same',OTHER,text='two')]
    actual,info=select_members(QUESTION,rows,['s'])
    assert actual==rows and all(d['selected'] for d in info['decisions'])


def test_same_id_relations_not_silently_changed_by_selector():
    rows=[member('s'),member('s')]
    assert select_members(QUESTION,rows,['s'])[0]==rows


def test_no_gold_or_label_input_and_message_content_is_exact():
    rows=[member('s',text='seed fact'),member('keep',text='header\nunit'),member('excluded',OTHER,text='EXCLUDED')]
    actual,_=select_members(QUESTION,rows,['s'])
    msgs=grounded_messages(actual,[{'role':'user','content':QUESTION}])
    assert msgs[1]['content']==QUESTION and 'header\nunit' in msgs[0]['content']
    assert 'EXCLUDED' not in msgs[0]['content'] and '[2]' in msgs[0]['content']
    with pytest.raises(TypeError): select_members(QUESTION,rows,['s'],gold='forbidden')


def test_empty_context_has_explicit_scope_and_no_fake_answer():
    actual,info=select_members('unknown',[],[])
    assert actual==[] and info['scope_status']=='UNKNOWN'
    assert len(grounded_messages([],[]))==1

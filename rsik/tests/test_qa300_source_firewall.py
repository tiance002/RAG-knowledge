"""Metadata firewall controls use invented source files and question IDs only."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts.plan_rsi_qa300_source_firewall import binding,plan
from rsik.events import canonical

NS='Full source file SHA-256. Compared only to other full source hash fields and local raw file bytes.'


def write(path,value):path.write_bytes(canonical(value));return binding(path)


def inputs(tmp_path,*,quote_hash_collision=False,gold_overlap=False,ancillary_inventory=False):
    ids=[f'CONTROL-DEV-{i:02d}' for i in range(20)];final_ids=['CONTROL-FINAL']
    finalraw=tmp_path/'final-source.txt';finalraw.write_bytes(b'CONTROL FINAL raw source bytes')
    requiredraw=tmp_path/'dev-source.txt';requiredraw.write_bytes(b'CONTROL DEV raw source bytes')
    fh=binding(finalraw)['sha256'];rh=binding(requiredraw)['sha256']
    devcsv=tmp_path/'dev.csv';devcsv.write_bytes(('item_id\n'+'\n'.join(ids)+'\n').encode())
    finalcsv=tmp_path/'final.csv';finalcsv.write_bytes(b'item_id\nCONTROL-FINAL\n')
    content=finalraw.read_text() if quote_hash_collision else 'CONTROL short citation excerpt'
    ch=hashlib.sha256(content.encode()).hexdigest();sourcehash=rh if quote_hash_collision else fh
    file_id='DEV-FILE' if quote_hash_collision else 'FINAL-RUNTIME-FILE'
    citation={'chunk_id':'CONTROL-CHUNK','content':content,'content_hash':ch,'fileId':file_id,
              'kb_id':'CONTROL-KB','source_metadata':{'source_sha256':sourcehash}}
    result=tmp_path/'results.jsonl';raw=canonical({'item_id':ids[0],'events':[{'data':{'citations':[citation]}}]})
    result.write_bytes(raw+b'\n');rr=binding(result);linehash=hashlib.sha256(raw).hexdigest()
    gold=tmp_path/'gold.json'
    write(gold,{'items':[{'item_id':qid,'source_documents':[{'document_id':'FINAL-DOC' if gold_overlap and i==0 else 'DEV-DOC',
        'sha256':fh if gold_overlap and i==0 else rh}],'question':'SYNTHETIC hidden question','ground_truth':'SYNTHETIC hidden Gold'} for i,qid in enumerate(ids)]})
    inventory=tmp_path/'inventory.json';docs=[{'document_id':'DEV-DOC','source_sha256':rh,'items':ids}]
    if ancillary_inventory:docs.append({'document_id':'ANCILLARY-NOT-GOLD','source_sha256':fh,'items':ids})
    write(inventory,{'documents':docs})
    audit=tmp_path/'audit.json';audit_rows=[]
    for i,qid in enumerate(ids):
        h=fh if gold_overlap and i==0 else rh;local=finalraw if gold_overlap and i==0 else requiredraw
        audit_rows.append({'item_id':qid,'sources':[{'expected_source_sha256':h,'document_id':'FINAL-DOC' if gold_overlap and i==0 else 'DEV-DOC',
            'file_id':'FINAL-GOLD-FILE' if gold_overlap and i==0 else 'DEV-FILE','local_binding':binding(local)}],
            'baseline_ref':{'file':rr,'line_number':1,'line_sha256':linehash} if i==0 else None,
            'capture':{'citations':[{'chunk_id':'CONTROL-CHUNK','content_sha256':ch}]} if i==0 else {'citations':[]}})
    write(audit,{'schema':'qa500-selected-dev-passive-audit/v1','status':'AUDIT_ONLY_NOT_ADMITTED','item_ids':ids,'items':audit_rows,
        'inputs':{'gold':binding(gold),'dev_csv':binding(devcsv),'final_csv':binding(finalcsv)}})
    cm={'item_id':ids[0],'result_file':str(result.resolve()),'result_file_sha256':rr['sha256'],'line_number_1based':1,
        'citation_pointer':'/events/0/data/citations/0','raw_source_sha256':sourcehash,'file_id':file_id,'chunk_id':'CONTROL-CHUNK',
        'content_hash':ch,'context_group_ids':['CONTROL-GROUP'],
        'raw_source_hash_namespace':'source_metadata.source_sha256 full source digest'}
    localproof={'local_actual':{'exists':True,**binding(finalraw)}}
    catalog=tmp_path/'catalog.v2.json'
    write(catalog,{'schema':'rsi-qa300-selected-source-catalog/v2','input_bindings':[binding(audit),binding(gold),binding(inventory),binding(finalcsv),rr],
        'selection':{'DEV_item_ids':ids,'FINAL_item_ids':final_ids},
        'source_catalog_bindings':{'reparse_source_inventory':{'file':binding(inventory)}},
        'selected_FINAL_sources':[{'full_source_sha256':fh,'hash_namespace':NS,'selected_FINAL_item_ids':final_ids,
            'gold_document_ids':['FINAL-DOC'],'gold_file_ids':['FINAL-GOLD-FILE'],
            'snapshot_manifest_raw_byte_binding':[localproof],
            'index_validation_file_bindings':[{'file_id':'FINAL-INDEX-FILE','source_sha256':fh}]}],
        'current_DEV_RAG_citations':{'all_citation_metadata':[cm]},'unresolved_FINAL_metadata_family_candidates':[]})
    edge=tmp_path/'edges.json';overlap=not quote_hash_collision
    write(edge,{'schema':'rsi-qa300-dev-final-exact-source-overlap-edges/v1','source_catalog_report':binding(catalog),
        'citation_edges':[{'dev_item_id':ids[0],'result_file_sha256':rr['sha256'],'line_number_1based':1,
            'citation_pointer':cm['citation_pointer'],'raw_source_sha256':fh,'file_id':file_id,
            'chunk_id':cm['chunk_id'],'citation_content_hash':ch}] if overlap else [],
        'source_file_edges':[{'raw_source_sha256':fh,'local_snapshot_raw_hash_bindings':[localproof]}] if overlap else []})
    return {'dev_audit':audit,'source_catalog':catalog,'overlap_edges':edge,'output':tmp_path/'plan'},ids,fh,rh


def test_exact_proposal_preserves_every_id_but_unknown_family_blocks(tmp_path):
    kwargs,ids,fh,_=inputs(tmp_path)
    r=plan(**kwargs)
    assert r['selected_item_ids']==ids and len(r['items'])==20
    assert r['exact_only_status']=='EXACT_EXCLUSION_PROPOSAL_NO_GOLD_LOSS'
    assert r['status']=='BLOCKED_NOT_ADMITTED' and not r['admission_allowed']
    assert r['family_qualified_status']=='BLOCKED_NO_REVIEWED_FAMILY_MAP'
    assert 'CAPTURED_SOURCE_FAMILIES_UNRESOLVED' in r['blocking_reasons']
    assert r['summary']['captured_overlap_occurrences']==1
    assert r['proposed_global_exclusions']['raw_source_sha256']==[fh]
    assert not r['proposed_global_exclusions']['apply_allowed']
    data=(kwargs['output']/'firewall-plan.restricted.json').read_text(encoding='utf-8-sig')
    assert 'SYNTHETIC hidden question' not in data and 'SYNTHETIC hidden Gold' not in data
    with pytest.raises(ValueError,match='FRESH_FIREWALL_PLAN_OUTPUT_REQUIRED'):plan(**kwargs)


def test_citation_quote_hash_never_becomes_raw_source_identity(tmp_path):
    kwargs,_,fh,_=inputs(tmp_path,quote_hash_collision=True)
    r=plan(**kwargs)
    assert r['summary']['captured_overlap_occurrences']==0
    assert r['items'][0]['proposed_excluded_citation_refs']==[]
    assert r['items'][0]['required_Gold_overlap']['raw_source_sha256']==[]
    assert fh in r['proposed_global_exclusions']['raw_source_sha256']


def test_required_gold_overlap_blocks_without_replacing_selected_questions(tmp_path):
    kwargs,ids,_,_=inputs(tmp_path,gold_overlap=True)
    r=plan(**kwargs)
    assert r['summary']['required_Gold_conflict_items']==1
    assert r['items'][0]['exact_protection_status']=='BLOCKED_REQUIRED_GOLD_OVERLAP'
    assert r['selected_item_ids']==ids and all(x['item_preserved'] for x in r['items'])
    assert 'REQUIRED_GOLD_SOURCE_EXCLUSION_CONFLICT' in r['blocking_reasons']


def test_inventory_membership_alone_cannot_invent_required_gold_support(tmp_path):
    kwargs,_,fh,rh=inputs(tmp_path,ancillary_inventory=True)
    r=plan(**kwargs)
    assert r['summary']['required_Gold_conflict_items']==0
    assert r['items'][0]['required_Gold_source_protection']['raw_source_sha256']==[rh]
    assert fh not in r['items'][0]['required_Gold_source_protection']['raw_source_sha256']


def test_changed_bound_catalog_is_rejected_before_any_plan(tmp_path):
    kwargs,*_=inputs(tmp_path)
    kwargs['source_catalog'].write_bytes(kwargs['source_catalog'].read_bytes()+b'\n')
    with pytest.raises(ValueError,match='BOUND_ARTIFACT_CHANGED'):plan(**kwargs)
    assert not kwargs['output'].exists()


def test_malformed_duplicate_capture_cannot_hide_another_occurrence(tmp_path):
    kwargs,*_=inputs(tmp_path)
    catalog=json.loads(kwargs['source_catalog'].read_text(encoding='utf-8-sig'))
    rows=catalog['current_DEV_RAG_citations']['all_citation_metadata'];rows.append(dict(rows[0]))
    write(kwargs['source_catalog'],catalog)
    edges=json.loads(kwargs['overlap_edges'].read_text(encoding='utf-8-sig'))
    edges['source_catalog_report']=binding(kwargs['source_catalog']);write(kwargs['overlap_edges'],edges)
    with pytest.raises(ValueError,match='DUPLICATE_CAPTURE_OCCURRENCE'):plan(**kwargs)
    assert not kwargs['output'].exists()

"""Immutable metadata-only source firewall proposal; never applies exclusions.

Exact raw-source identity is separate from citation hashes and family review.
Disjoint exact identities cannot authorize a run or establish fresh holdout.
"""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rsik.events import canonical
from rsik.locking import restrict_directory


def load(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def binding(path):
    path=Path(path).resolve()
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def verify(ref):
    if not isinstance(ref,dict) or not {'path','sha256'}<=set(ref):raise ValueError('FIREWALL_BOUND_ARTIFACT_REQUIRED')
    actual=binding(ref['path'])
    if actual['sha256']!=ref['sha256']:raise ValueError('FIREWALL_BOUND_ARTIFACT_CHANGED')
    if 'bytes' in ref and Path(ref['path']).stat().st_size!=ref['bytes']:raise ValueError('FIREWALL_BOUND_ARTIFACT_SIZE_CHANGED')
    return actual


def raw_hash(value):
    if value is not None and (not isinstance(value,str) or not re.fullmatch(r'[0-9a-f]{64}',value)):
        raise ValueError('FIREWALL_RAW_SOURCE_HASH_MALFORMED')
    return value


def pointer(value,path):
    for token in path.strip('/').split('/'):
        token=token.replace('~1','/').replace('~0','~')
        value=value[int(token)] if isinstance(value,list) else value[token]
    return value


def required_sources(audit,catalog):
    """Only explicit full-source metadata, never evidence/quote digests."""
    gold=load(audit['inputs']['gold']['path'])
    inventory_ref=catalog['source_catalog_bindings']['reparse_source_inventory']['file'];verify(inventory_ref)
    inventory=load(inventory_ref['path'])['documents']
    ids=audit['item_ids'];by_id={r['item_id']:r for r in audit['items']}
    gold_rows={r['item_id']:(i,r) for i,r in enumerate(gold['items']) if r.get('item_id') in set(ids)}
    if set(gold_rows)!=set(ids):raise ValueError('FIREWALL_SELECTED_GOLD_IDENTITIES_CHANGED')
    protected={}
    for qid in ids:
        gi,row=gold_rows[qid];hashes=set();documents=set();files=set();refs=[];unknown=[]
        strict_hashes=set();declared_hashes=set();declared_documents_without_hash=set()
        for j,s in enumerate(row.get('source_documents',[])):
            if isinstance(s,str):documents.add(s);declared_documents_without_hash.add(s)
            elif isinstance(s,dict):
                if s.get('document_id'):documents.add(s['document_id'])
                if s.get('file_id'):files.add(s['file_id'])
                h=raw_hash(s.get('sha256'))
                if h:hashes.add(h);declared_hashes.add(h)
                elif s.get('document_id'):declared_documents_without_hash.add(s['document_id'])
            else:raise ValueError('FIREWALL_GOLD_SOURCE_METADATA_MALFORMED')
            refs.append({'file':audit['inputs']['gold'],'pointer':f'/items/{gi}/source_documents/{j}'})
        for j,s in enumerate(by_id[qid]['sources']):
            if s.get('local_binding'):verify(s['local_binding'])
            h=raw_hash(s.get('expected_source_sha256') or s.get('manifest_remote_sha256'))
            if h:hashes.add(h);strict_hashes.add(h)
            else:unknown.append({'kind':'AUDIT_REQUIRED_SOURCE_HASH_UNKNOWN','source_index':j})
            if s.get('document_id'):documents.add(s['document_id'])
            if s.get('file_id'):files.add(s['file_id'])
            refs.append({'audit_item_id':qid,'source_index':j,'local_binding':s.get('local_binding')})
        resolved_declared={}
        for j,d in enumerate(inventory):
            # Inventory item membership alone is NOT required-Gold support.
            # It may include ancillary records or historical collection inputs.
            if d.get('document_id') not in declared_documents_without_hash:continue
            h=raw_hash(d.get('source_sha256'))
            if h:
                hashes.add(h);declared_hashes.add(h)
                resolved_declared.setdefault(d['document_id'],set()).add(h)
            else:unknown.append({'kind':'INVENTORY_REQUIRED_SOURCE_HASH_UNKNOWN','pointer':f'/documents/{j}'})
            if d.get('document_id'):documents.add(d['document_id'])
            if d.get('file_id'):files.add(d['file_id'])
            refs.append({'file':inventory_ref,'pointer':f'/documents/{j}'})
        for did in declared_documents_without_hash:
            if not resolved_declared.get(did):unknown.append({'kind':'DECLARED_GOLD_DOCUMENT_HASH_UNRESOLVED','document_id':did})
            elif len(resolved_declared[did])!=1:unknown.append({'kind':'DECLARED_GOLD_DOCUMENT_VERSION_AMBIGUOUS','document_id':did})
        if not hashes:unknown.append({'kind':'NO_BOUND_REQUIRED_RAW_SOURCE_IDENTITY'})
        protected[qid]={'raw_source_sha256':sorted(hashes),'document_ids':sorted(documents),
                         'strict_actual_Gold_reference_raw_sha256':sorted(strict_hashes),
                         'conservative_declared_source_documents_raw_sha256':sorted(declared_hashes),
                         'file_ids':sorted(files),'bindings':refs,'unknown_required_identities':unknown}
    return protected


def plan(*,dev_audit,overlap_edges,source_catalog,output,epoch_id='qa300-selected-dev-firewall-proposal-v1',expected_count=20):
    audit,catalog,edges=load(dev_audit),load(source_catalog),load(overlap_edges)
    if audit.get('schema')!='qa500-selected-dev-passive-audit/v1' or audit.get('status')!='AUDIT_ONLY_NOT_ADMITTED':
        raise ValueError('FIREWALL_DEV_PASSIVE_AUDIT_REQUIRED')
    if catalog.get('schema') not in {'rsi-qa300-selected-source-catalog/v1','rsi-qa300-selected-source-catalog/v2',
                                   'rsi-qa300-selected40-source-catalog/v2'}:
        raise ValueError('FIREWALL_SOURCE_CATALOG_SCHEMA_REQUIRED')
    if edges.get('schema') not in {'rsi-qa300-dev-final-exact-source-overlap-edges/v1',
                                  'rsi-qa300-dev-final-exact-overlap-edges/v2'}:
        raise ValueError('FIREWALL_EDGE_SCHEMA_REQUIRED')
    verify(edges.get('source_catalog_report'))
    current_catalog_ref=edges.get('catalog_v2_binding',edges['source_catalog_report'])
    verify(current_catalog_ref)
    if binding(source_catalog)!=binding(current_catalog_ref['path']):
        raise ValueError('FIREWALL_WRONG_EDGE_SOURCE_CATALOG')
    for ref in audit['inputs'].values():verify(ref)
    for ref in catalog['input_bindings']:verify(ref)
    if not any(binding(ref['path'])==binding(dev_audit) for ref in catalog['input_bindings']):
        raise ValueError('FIREWALL_CATALOG_MUST_BIND_DEV_AUDIT')
    ids=audit['item_ids'];selected=catalog['selection']
    if (len(ids)!=expected_count or len(set(ids))!=len(ids) or ids!=[r['item_id'] for r in audit['items']]
            or set(ids)!=set(selected['DEV_item_ids']) or set(ids)&set(selected['FINAL_item_ids'])):
        raise ValueError('FIREWALL_SELECTED_IDS_CHANGED')
    with Path(audit['inputs']['final_csv']['path']).open(encoding='utf-8-sig',newline='') as f:
        final_ids={r['item_id'] for r in csv.DictReader(f)}
    if final_ids!=set(selected['FINAL_item_ids']):raise ValueError('FIREWALL_FINAL_IDS_CHANGED')
    protection=required_sources(audit,catalog)
    final_sources={};excluded_files=set();final_documents=set();unknown=[]
    for source in catalog['selected_FINAL_sources']:
        h=raw_hash(source.get('full_source_sha256'))
        if not h:unknown.append('FINAL_RAW_SOURCE_IDENTITY_UNKNOWN');continue
        if h in final_sources:raise ValueError('FIREWALL_DUPLICATE_FINAL_SOURCE_HASH')
        if source.get('hash_namespace')!='Full source file SHA-256. Compared only to other full source hash fields and local raw file bytes.':
            raise ValueError('FIREWALL_RAW_VS_CITATION_HASH_NAMESPACE_REQUIRED')
        if not set(source['selected_FINAL_item_ids'])<=final_ids:raise ValueError('FIREWALL_FINAL_SOURCE_SELECTION_MISMATCH')
        final_sources[h]=source;final_documents.update(source.get('gold_document_ids',[]))
        local_proof=False
        for row in source.get('snapshot_manifest_raw_byte_binding',[]):
            local=row.get('local_actual',{})
            if local.get('exists') and local.get('sha256')==h:
                verify(local);local_proof=True
        if not local_proof:unknown.append('FINAL_RAW_SOURCE_BYTES_NOT_BOUND:'+h)
        excluded_files.update(source.get('gold_file_ids',[]))
        for key in ('index_validation_file_bindings','ecs_source_db_audit_exact_hash_bindings'):
            for r in source.get(key,[]):
                if r.get('source_sha256') not in (None,h):raise ValueError('FIREWALL_FINAL_FILE_ALIAS_HASH_CONFLICT')
                if r.get('file_id'):excluded_files.add(r['file_id'])
    citations=catalog['current_DEV_RAG_citations']['all_citation_metadata']
    audit_by_id={r['item_id']:r for r in audit['items']};observed=[];unknown_capture=[];capture_keys=set()
    for c in citations:
        qid=c['item_id']
        if qid not in audit_by_id:raise ValueError('FIREWALL_NON_DEV_CAPTURE_FORBIDDEN')
        capture_key=(qid,c['result_file_sha256'],c['line_number_1based'],c['citation_pointer'])
        if capture_key in capture_keys:raise ValueError('FIREWALL_DUPLICATE_CAPTURE_OCCURRENCE')
        capture_keys.add(capture_key)
        ref=audit_by_id[qid]['baseline_ref']
        if (not ref or str(Path(c['result_file']).resolve())!=ref['file']['path']
                or c['result_file_sha256']!=ref['file']['sha256'] or c['line_number_1based']!=ref['line_number']):
            raise ValueError('FIREWALL_CAPTURE_BINDING_MISMATCH')
        verify(ref['file'])
        raw=Path(c['result_file']).read_bytes().splitlines()[c['line_number_1based']-1]
        if hashlib.sha256(raw).hexdigest()!=ref['line_sha256']:raise ValueError('FIREWALL_CAPTURE_LINE_CHANGED')
        row=json.loads(raw.decode('utf-8-sig'))
        if row.get('item_id')!=qid:raise ValueError('FIREWALL_CAPTURE_ROW_IDENTITY_MISMATCH')
        actual=pointer(row,c['citation_pointer']);h=raw_hash(c.get('raw_source_sha256'))
        if h and c.get('raw_source_hash_namespace')!='source_metadata.source_sha256 full source digest':
            raise ValueError('FIREWALL_RAW_VS_CITATION_HASH_NAMESPACE_REQUIRED')
        if (h!=actual.get('source_metadata',{}).get('source_sha256')
                or c['content_hash']!=actual.get('content_hash') or c['chunk_id']!=actual.get('chunk_id')
                or c['file_id']!=actual.get('fileId')):
            raise ValueError('FIREWALL_CAPTURE_SOURCE_IDENTITY_MISMATCH')
        matches=[a for a in audit_by_id[qid]['capture']['citations'] if a['chunk_id']==c['chunk_id'] and a['content_sha256']==c['content_hash']]
        if not matches:raise ValueError('FIREWALL_CAPTURE_NOT_IN_PASSIVE_AUDIT')
        if not h:unknown_capture.append({'item_id':qid,'chunk_id':c['chunk_id']})
        if h in final_sources:observed.append(c)
    expected_capture=Counter((r['item_id'],c['chunk_id'],c['content_sha256']) for r in audit['items'] for c in r['capture']['citations'])
    actual_capture=Counter((c['item_id'],c['chunk_id'],c['content_hash']) for c in citations)
    if actual_capture!=expected_capture:
        raise ValueError('FIREWALL_CAPTURE_DENOMINATOR_CHANGED')
    edge_keys={(e['dev_item_id'],e['result_file_sha256'],e['line_number_1based'],e['citation_pointer'],e['raw_source_sha256'],e['file_id'],e['chunk_id'],e['citation_content_hash']) for e in edges['citation_edges']}
    expected={(c['item_id'],c['result_file_sha256'],c['line_number_1based'],c['citation_pointer'],c['raw_source_sha256'],c['file_id'],c['chunk_id'],c['content_hash']) for c in observed}
    if len(edge_keys)!=len(edges['citation_edges']) or edge_keys!=expected:
        raise ValueError('FIREWALL_EDGE_RAW_SOURCE_JOIN_MISMATCH')
    if {s['raw_source_sha256'] for s in edges['source_file_edges']}!={c['raw_source_sha256'] for c in observed}:
        raise ValueError('FIREWALL_SOURCE_FILE_EDGES_INCOMPLETE')
    for s in edges['source_file_edges']:
        h=s['raw_source_sha256']
        proven=False
        for raw_ref in s['local_snapshot_raw_hash_bindings']:
            local=raw_ref.get('local_actual',{})
            if local.get('exists') and local.get('sha256')==h:
                verify(local);proven=True
        if not proven:raise ValueError('FIREWALL_EXACT_OVERLAP_RAW_BYTES_UNBOUND')
    excluded_files.update(c['file_id'] for c in observed if c.get('file_id'))
    items=[];gold_conflicts=[];identity_unknown=[]
    for qid in ids:
        protected=protection[qid]
        overlap={'raw_source_sha256':sorted(set(protected['raw_source_sha256'])&set(final_sources)),
                 'file_ids':sorted(set(protected['file_ids'])&excluded_files),
                 'document_ids':sorted(set(protected['document_ids'])&final_documents)}
        if any(overlap.values()):gold_conflicts.append(qid)
        if protected['unknown_required_identities']:identity_unknown.append(qid)
        excluded=[c for c in observed if c['item_id']==qid]
        items.append({'item_id':qid,'required_Gold_source_protection':protected,'required_Gold_overlap':overlap,
            'proposed_excluded_citation_refs':[{'file':{'path':c['result_file'],'sha256':c['result_file_sha256']},
               'line_number_1based':c['line_number_1based'],'citation_pointer':c['citation_pointer'],
               'raw_source_sha256':c['raw_source_sha256'],'file_id':c['file_id'],'chunk_id':c['chunk_id'],
               'context_group_ids':c['context_group_ids'],'citation_content_hash':c['content_hash'],
               'reason':'EXACT_FULL_RAW_SOURCE_MATCH_TO_SELECTED_FINAL; proposed only, never applied'} for c in excluded],
            'exact_protection_status':'BLOCKED_REQUIRED_GOLD_OVERLAP' if any(overlap.values()) else
                'BLOCKED_REQUIRED_SOURCE_UNKNOWN' if protected['unknown_required_identities'] else 'EXACT_GOLD_PROTECTION_CHECK_ONLY',
            'item_preserved':True,'family_qualification':'UNKNOWN_NOT_REVIEWED'})
    reasons=['UNKNOWN_SOURCE_FAMILY','CAPTURED_SOURCE_FAMILIES_UNRESOLVED']
    if gold_conflicts:reasons.append('REQUIRED_GOLD_SOURCE_EXCLUSION_CONFLICT')
    if identity_unknown or unknown or unknown_capture:reasons.append('UNKNOWN_REQUIRED_OR_CAPTURE_SOURCE_IDENTITY')
    result={'schema':'rsi-qa300-source-firewall-plan/v1','status':'BLOCKED_NOT_ADMITTED','epoch_id':epoch_id,
        'input_profile':'NEW_DEV_EXACT_SOURCE_FIREWALL_PROPOSAL_ONLY','selected_item_ids':ids,'selected_count':len(ids),
        'inputs':{'dev_audit':binding(dev_audit),'overlap_edges':binding(overlap_edges),'source_catalog':binding(source_catalog)},
        'preserved_predecessor_catalog_binding':edges['source_catalog_report'] if 'catalog_v2_binding' in edges else None,
        'source_catalog_artifact_status':'PREDECESSOR_V1_SUPERSEDED_NOT_QUALIFIED' if '.v1.' in Path(source_catalog).name else 'METADATA_ONLY_NOT_QUALIFIED',
        'items':items,'exact_only_status':'BLOCKED_REQUIRED_GOLD_OR_IDENTITY' if gold_conflicts or identity_unknown or unknown or unknown_capture else 'EXACT_EXCLUSION_PROPOSAL_NO_GOLD_LOSS',
        'family_qualified_status':'BLOCKED_NO_REVIEWED_FAMILY_MAP','blocking_reasons':reasons,
        'proposed_global_exclusions':{'raw_source_sha256':sorted(final_sources),'file_ids':sorted(excluded_files),
            'matching_rule':'Full raw source SHA OR verified source-file identity; never citation/quote hash equality.',
            'apply_allowed':False},
        'summary':{'selected_ids_preserved':len(items),'FINAL_raw_source_hashes':len(final_sources),'excluded_file_id_aliases':len(excluded_files),
            'captured_overlap_occurrences':len(observed),'captured_overlap_DEV_items':len({c['item_id'] for c in observed}),
            'required_Gold_conflict_items':len(gold_conflicts),'required_identity_unknown_items':len(identity_unknown),
            'captured_identity_unknown_occurrences':len(unknown_capture),
            'strict_Gold_reference_raw_hashes':len(set().union(*(set(p['strict_actual_Gold_reference_raw_sha256']) for p in protection.values()))),
            'declared_source_documents_raw_hashes':len(set().union(*(set(p['conservative_declared_source_documents_raw_sha256']) for p in protection.values()))),
            'captured_family_status':'UNRESOLVED_WITHOUT_REVIEWED_FAMILY_MAP',
            'FINAL_metadata_family_candidates_unresolved':len(catalog.get('unresolved_FINAL_metadata_family_candidates',[]))},
        'full_source_and_capture_bindings':{'catalog_inputs':catalog['input_bindings'],
            'catalog_source_bindings':catalog['source_catalog_bindings'],'audit_inputs':audit['inputs']},
        'admission_allowed':False,'production_qualified':False,'fixture_index_capture_modified':False,
        'model_or_remote_calls':0,'text_exported':False,'execution_status':'PLAN_ONLY_NOT_EXECUTED',
        'limits':['Exact source exclusion is not family independence, freshness, semantic correctness or admission.',
                  'No source/citation is removed by this planner; every selected ID remains, including blocked cases.',
                  'Any required Gold intersection or unknown source/family blocks use; resolve evidence rather than weakening gates.']}
    output=Path(output).resolve()
    if output.exists():raise ValueError('FRESH_FIREWALL_PLAN_OUTPUT_REQUIRED')
    output.mkdir(parents=True);restrict_directory(output)
    (output/'firewall-plan.restricted.json').write_bytes(canonical(result))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('dev-audit','overlap-edges','source-catalog','output'):p.add_argument('--'+name,required=True)
    p.add_argument('--epoch-id',default='qa300-selected-dev-firewall-proposal-v1');a=p.parse_args()
    r=plan(**vars(a));print(json.dumps({'status':r['status'],'exact_only_status':r['exact_only_status'],'summary':r['summary']}))

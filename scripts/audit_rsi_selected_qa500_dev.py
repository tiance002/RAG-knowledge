"""Passive, DEV-allowlisted QA500 inventory. No execution or admission proof.

FINAL CSV content is limited to identifiers. Gold is used only for selected DEV
metadata and source references; Gold answers never become conversation history.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rsik.events import canonical,digest
from rsik.locking import restrict_directory

PROFILE='qa500-selected-dev-passive-audit/v1'


def binding(path):
    path=Path(path).resolve()
    return {'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def verify_binding(record):
    if binding(record['path'])!=record:raise ValueError('QA500_BOUND_INPUT_CHANGED')


def selections(dev_csv,final_csv):
    with Path(dev_csv).open(encoding='utf-8-sig',newline='') as f:dev=list(csv.DictReader(f))
    with Path(final_csv).open(encoding='utf-8-sig',newline='') as f:
        final=[r['item_id'] for r in csv.DictReader(f)]
    ids=[r['item_id'] for r in dev]
    if not ids or len(ids)!=len(set(ids)) or len(final)!=len(set(final)) or set(ids)&set(final):
        raise ValueError('QA500_SELECTION_IDENTITIES_INVALID')
    return dev,set(final)


def partition_selections(dev_csv, final_csv, partition='DEV'):
    dev, final = selections(dev_csv, final_csv)
    if partition == 'DEV': return dev, final
    if partition != 'FINAL': raise ValueError('QA500_SELECTION_PARTITION_INVALID')
    with Path(final_csv).open(encoding='utf-8-sig',newline='') as stream:
        selected = list(csv.DictReader(stream))
    return selected, {r['item_id'] for r in dev}


def jsonl_rows(path):
    """Retain physical row identity; callers gate IDs before content projection."""
    for number,raw in enumerate(Path(path).read_bytes().splitlines(),1):
        if not raw.strip():continue
        yield number,hashlib.sha256(raw).hexdigest(),json.loads(raw.decode('utf-8-sig'))


def input_metadata(path):
    rows=[];seen=set()
    for number,h,row in jsonl_rows(path):
        qid=row['item_id']
        if qid in seen:raise ValueError('QA500_DUPLICATE_RUNTIME_ITEM')
        seen.add(qid)
        rows.append({k:row[k] for k in ('item_id','session_id','index','turn_index')}|{'line_number':number,'line_sha256':h})
    return rows


def selected_results(root,ids):
    result={}
    for path in sorted(Path(root).rglob('results.jsonl')):
        if 'web50' in path.parts:continue
        for number,h,row in jsonl_rows(path):
            if row.get('item_id') not in ids:continue
            if row['item_id'] in result:raise ValueError('QA500_AMBIGUOUS_BASELINE_RESULT')
            result[row['item_id']]=(row,{'file':binding(path),'line_number':number,'line_sha256':h})
    return result


def snapshot_paths(manifest):
    if manifest is None:return {},None
    data=load(manifest)
    if data.get('schema')!='qa500-ecs-source-snapshot/v2':raise ValueError('QA500_SOURCE_SNAPSHOT_SCHEMA_REQUIRED')
    index={}
    for row in data.get('files',[])+data.get('additional_files',[]):
        remote=row['path'];h=row.get('remote_sha256')
        if remote in index and index[remote].get('remote_sha256')!=h:
            raise ValueError('QA500_SNAPSHOT_REMOTE_IDENTITY_CONFLICT')
        index.setdefault(remote,row)
    return index,binding(manifest)


def source_references(item,snapshot,manifest_index=None,manifest_binding=None):
    rows=[]
    for index,e in enumerate(item.get('evidence',[]),1):
        source=e.get('source_reference',e.get('source',e))
        remote=source.get('source_path',source.get('local_path',source.get('path','')))
        expected=source.get('source_sha256')
        match=(manifest_index or {}).get(remote)
        local=Path(match['local_path']) if match and match.get('local_path') else (
            (Path(snapshot)/remote.lstrip('/')) if remote.startswith('/') else Path(remote))
        exists=bool(remote) and local.is_file()
        actual=binding(local) if exists else None
        manifest_hash=match.get('remote_sha256') if match else None
        if actual and manifest_hash and actual['sha256']!=manifest_hash:
            raise ValueError('QA500_SOURCE_MANIFEST_BYTES_CHANGED')
        rows.append({'evidence_id':e.get('evidence_id',f"{item['item_id']}#source-{index}"),
            'document_id':source.get('document_id',source.get('source_document_id',source.get('file_id'))),
            'file_id':source.get('file_id'),
            'remote_path':remote,'expected_source_sha256':expected,'local_binding':actual,
            'source_byte_status':('HASH_MATCH' if expected and actual['sha256']==expected else
                'HASH_MISMATCH' if expected else 'MANIFEST_HASH_MATCH_GOLD_HASH_UNBOUND' if manifest_hash else
                'PRESENT_HASH_UNBOUND') if exists else 'NOT_IN_DECLARED_LOCAL_SNAPSHOTS',
            'snapshot_lookup':'BOUND_MANIFEST_EXACT_REMOTE_PATH' if match else 'PRIMARY_MIRROR_ONLY',
            'snapshot_manifest':manifest_binding if match else None,'manifest_remote_sha256':manifest_hash,
            'chunk_id':source.get('chunk_id'),'kb_id':source.get('kb_id'),
            'location_kind':'PARSED_PDF_INDEX_TEXT' if 'exact_indexed_text' in source else 'SOURCE_TEXT_REFERENCE'})
    return rows


def capture_metadata(result):
    rag=[e['data'] for e in result.get('events',[]) if e.get('event')=='rag_result']
    if len(rag)>1:raise ValueError('QA500_AMBIGUOUS_RAG_CAPTURE')
    if not rag:return {'control_class':'CONTROL_NO_RETRIEVAL','citations':[],
                       'capture_scope':'No current retrieval capture; history-only is not an invalid question.'}
    data=rag[0];rows=[]
    for c in data.get('citations',[]):
        content=c.get('content')
        if not isinstance(content,str):raise ValueError('QA500_CAPTURE_TEXT_REQUIRED')
        h=hashlib.sha256(content.encode('utf-8')).hexdigest()
        if h!=c.get('content_hash'):raise ValueError('QA500_CAPTURE_CONTENT_HASH_MISMATCH')
        rows.append({k:c.get(k) for k in ('chunk_id','content_hash','context_group_id','context_group_ids',
            'seed_evidence_ids','seed_child_ids','parent_chunk_id','fileId','kb_id','evidence_relation','source_mapping_status')}
            |{'content_chars':len(content),'content_sha256':h})
    return {'control_class':'SOURCE_CONTEXT_CAPTURE','citations':rows,
        'expansion_delivery_status':data.get('expansion_delivery_status'),
        'source_mapping_status':data.get('source_mapping_status'),
        'capture_scope':'SSE citation/group projection only; not full prepared pool or prompt evidence.',
        'over_4000_group_spec_mismatch_rows':sum(r['content_chars']>4000 for r in rows),
        'length_rule':'4000 is protected max_parent_length PER expanded group, not total context/prompt capacity.'}


def audit(*,dev_csv,final_csv,gold,audit_reviews,runtime_input,baseline_root,source_snapshot,output,expected_count=20,
          index_validation_report=None,source_snapshot_manifest=None,partition='DEV'):
    dev,final=partition_selections(dev_csv,final_csv,partition);ids={r['item_id'] for r in dev}
    if len(dev)!=expected_count:raise ValueError('QA500_SELECTED_DEV_COUNT_CHANGED')
    gold_items=[r for r in load(gold)['items'] if r.get('item_id') in ids]
    manifest_index,manifest_binding=snapshot_paths(source_snapshot_manifest)
    by_id={r['item_id']:r for r in gold_items}
    if len(gold_items)!=len(by_id) or set(by_id)!=ids:raise ValueError('QA500_SELECTED_GOLD_IDENTITY_MISMATCH')
    if partition == 'FINAL' and any(r.get('item_kind') != 'standalone' for r in gold_items):
        raise ValueError('QA500_FINAL_STANDALONE_REQUIRED')
    metadata=input_metadata(runtime_input);runtime={r['item_id']:r for r in metadata}
    for _,_,row in jsonl_rows(runtime_input):
        if row.get('item_id') in ids and row.get('question')!=by_id[row['item_id']]['question']:
            raise ValueError('QA500_SELECTED_RUNTIME_QUESTION_CHANGED')
    results=selected_results(baseline_root,ids)
    reviews={}
    for number,h,row in jsonl_rows(audit_reviews):
        if row.get('item_id') not in ids:continue
        if row['item_id'] in reviews:raise ValueError('QA500_AMBIGUOUS_SELECTED_REVIEW')
        reviews[row['item_id']]={'line_number':number,'line_sha256':h,
            **{k:row.get(k) for k in ('answer_grade','citation_grade','gold_source_defect','status')}}
    if set(reviews)!=ids:raise ValueError('QA500_SELECTED_REVIEW_IDENTITY_MISMATCH')
    rows=[]
    for selection in dev:
        qid=selection['item_id'];item=by_id[qid]
        if qid not in runtime:raise ValueError('QA500_SELECTED_RUNTIME_ITEM_MISSING')
        m=runtime[qid];result,ref=results[qid] if qid in results else (None,None)
        prior=[r for r in metadata if r['session_id']==m['session_id'] and r['index']<m['index']]
        history_final=sorted({r['item_id'] for r in prior}&final)
        semantic=item['item_kind']=='session_turn'
        declared_turns=item.get('depends_on_turns',[]) if semantic else []
        ancestor_turns=item.get('transitive_ancestor_turns',[]) if semantic else []
        dependency_rows=[r for r in prior if r['turn_index']+1 in set(declared_turns+ancestor_turns)]
        if semantic and (item['session_id']!=m['session_id'] or item['turn_index']-1!=m['turn_index']):
            raise ValueError('QA500_SEMANTIC_TURN_IDENTITY_MISMATCH')
        if result and any(result.get(k)!=m[k] for k in ('index','session_id','turn_index')):
            raise ValueError('QA500_BASELINE_TURN_IDENTITY_MISMATCH')
        capture=capture_metadata(result) if result else {'control_class':'CAPTURE_MISSING','citations':[]}
        rows.append({'item_id':qid,'item_kind':item['item_kind'],'language':item['language'],
            'declared_dataset':item.get('source_record',{}).get('dataset'),
            'question_sha256':hashlib.sha256(item['question'].encode()).hexdigest(),
            'selection_stratum':{k:selection.get(k) for k in ('existing_answer_label','existing_citation_label','dev_categories')},
            'runtime_identity':m,'baseline_ref':ref,'baseline_run_id':result.get('run_id') if result else None,
            'baseline_complete':result.get('complete') if result else None,'review':reviews[qid],
            'history_policy':'FIXED_ACTUAL_SEMANTIC_PREFIX' if semantic else 'EMPTY_HISTORY_NEW_ISOLATED_RUN',
            'baseline_reproduction_claim':False,
            'prior_input_identities':prior,'prior_selected_FINAL_ids_metadata_only':history_final,
            'declared_dependency_turns':declared_turns,'declared_ancestor_turns':ancestor_turns,
            'declared_dependency_ids':[r['item_id'] for r in dependency_rows],
            'declared_dependency_metadata_complete':len({r['turn_index']+1 for r in dependency_rows})==len(set(declared_turns+ancestor_turns)),
            'baseline_history_leak_risk':bool(history_final),
            'semantic_history_blocked_by_FINAL_id':semantic and bool(history_final),
            'upstream_continuity_status':result.get('context_continuity_status') if result else 'BASELINE_MISSING',
            'upstream_gap_ids':result.get('context_gap_qids',[]) if result else [],
            'sources':source_references(item,source_snapshot,manifest_index,manifest_binding),'capture':capture})
    output=Path(output).resolve()
    if output.exists():raise ValueError('FRESH_QA500_DEV_AUDIT_REQUIRED')
    report={'schema':PROFILE,'status':'AUDIT_ONLY_NOT_ADMITTED','selected_dev_count':len(rows),
        'item_ids':[r['item_id'] for r in rows],'items':rows,
        'inputs':{k:binding(v) for k,v in {'dev_csv':dev_csv,'final_csv':final_csv,'gold':gold,
            'audit_reviews':audit_reviews,'runtime_input':runtime_input}.items()},
        'baseline_root':str(Path(baseline_root).resolve()),'source_snapshot':str(Path(source_snapshot).resolve()),
        'summary':{'semantic_followups':sum(r['item_kind']=='session_turn' for r in rows),
            'standalone_stateless':sum(r['item_kind']=='standalone' for r in rows),
            'history_only_controls':sum(r['capture']['control_class']=='CONTROL_NO_RETRIEVAL' for r in rows),
            'citation_rows':sum(len(r['capture']['citations']) for r in rows),
            'distinct_context_groups_per_request_sum':sum(len({c['context_group_id'] for c in r['capture']['citations']}) for r in rows),
            'over4000_group_spec_mismatch_rows':sum(r['capture'].get('over_4000_group_spec_mismatch_rows',0) for r in rows),
            'baseline_history_FINAL_overlap_DEV_items':sum(r['baseline_history_leak_risk'] for r in rows)},
        'candidate_edit_seam_changed':False,'execution_status':'NOT_EXECUTED','paid_calls':0,
        'production_qualified':False,'additional_independent_samples':0,
        'limitations':['Metadata/captured-context audit only; no source fact scoring or baseline qualification.',
            'Standalone history=[] is a new isolated epoch policy, not reproduction of the historical conversation.',
            'Missing source mirrors or partial captures are reported; selected questions are never dropped or replaced.']}
    if manifest_binding:report['inputs']['source_snapshot_manifest']=manifest_binding
    if partition == 'FINAL':
        report['schema']='qa500-selected-final-passive-audit/v1'
        report['partition']='FINAL_HOLDOUT'
        report['selected_final_count']=report.pop('selected_dev_count')
    output.mkdir(parents=True);restrict_directory(output)
    (output/'audit.restricted.json').write_bytes(canonical(report))
    kb_ids={c['kb_id'] for r in rows for c in r['capture']['citations'] if c.get('kb_id')}
    kb_ids.update(s['kb_id'] for r in rows for s in r['sources'] if s.get('kb_id'))
    remote_paths={}
    historical_path_hints={}
    if index_validation_report:
        index=load(index_validation_report)
        remote_paths={k:v for k,v in index.get('isolated_candidate_paths',{}).items() if v.get('kb_id') in kb_ids}
        datasets={r['declared_dataset'] for r in rows if r['declared_dataset']}
        historical_path_hints={k:v for k,v in index.get('isolated_candidate_paths',{}).items() if k in datasets}
    request={'schema':'qa500-selected-dev-readonly-fetch-request/v1','status':'PROPOSED_NOT_AUTHORIZED_NOT_EXECUTED',
        'dev_allowlist_binding':binding(dev_csv),'audit':binding(output/'audit.restricted.json'),
        'selected_dev_ids':[r['item_id'] for r in rows],
        'lookup_order':['EXISTING_ACTUAL_REQUEST_PREPARE_CAPTURE_SNAPSHOT_FIRST',
                        'IF_MISSING_TARGETED_INDEX_ROWS_AND_SOURCE_PARSED_BINDING_ONLY'],
        'requested_fields':['kb_id','file_id','chunk_id','original_index','parent_chunk_id','seed_child_ids',
            'exact_parsed_content','content_sha256','source_sha256','source_locator','parser_profile_identity',
            'group_order','actual_prepare_request_identity','actual_current_user_query_identity'],
        'items':[{'item_id':r['item_id'],'baseline_ref':r['baseline_ref'],'runtime_identity':r['runtime_identity'],
                  'capture_members':r['capture']['citations'],'gold_source_references_metadata_only':r['sources'],
                  'control_class':r['capture']['control_class']} for r in rows],
        'known_matching_isolated_index_paths':remote_paths,
        'historical_dataset_index_path_hints':historical_path_hints,
        'historical_path_hint_limit':'Dataset-name link only; historical candidate KB identity may differ from actual baseline KB. Resolve before index-row interpretation.',
        'index_validation_report':binding(index_validation_report) if index_validation_report else None,
        'unresolved_database_paths':'Resolve from existing capture/manifest identity; no guessed database path.',
        'service_or_index_start_allowed':False,'whole_database_copy_allowed':False,
        'remote_calls_made':0,'final_content_requested':False,'paid_calls':0}
    (output/'readonly-fetch-request.restricted.json').write_bytes(canonical(request))
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('dev-csv','final-csv','gold','audit-reviews','runtime-input','baseline-root','source-snapshot','output'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--index-validation-report')
    p.add_argument('--source-snapshot-manifest')
    p.add_argument('--partition',choices=['DEV','FINAL'],default='DEV')
    a=p.parse_args();r=audit(**vars(a));print(json.dumps({'status':r['status'],'summary':r['summary']}))

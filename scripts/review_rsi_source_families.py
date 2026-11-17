"""Versioned, proof-bound source metadata refinement; never edits old ledgers.

Original events and anchors remain preserved as complete immutable records.
Known origin is distinct from complete exposure history or fresh holdout data.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rsik.events import EventLog,canonical,digest
from rsik.exposure import ExposureLedger
from rsik.launch import file_binding
from rsik.locking import restrict_directory


def load(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def bound(record):
    if not isinstance(record,dict) or set(record)!={'path','sha256'}:
        raise ValueError('FAMILY_FILE_BINDING_REQUIRED')
    raw=Path(record['path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=record['sha256']:
        raise ValueError('FAMILY_FILE_BINDING_CHANGED')
    return raw


def reviewed(review,subject):
    if (not isinstance(review,dict) or review.get('status') not in {'AI_REVIEWED','HUMAN_REVIEWED'}
            or review.get('subject_sha256')!=digest(subject)
            or not review.get('reviewer') or not review.get('reviewed_at')):
        raise ValueError('FAMILY_REVIEW_BINDING_REQUIRED')


def read_ledger(path):
    ledger=object.__new__(ExposureLedger);ledger.log=object.__new__(EventLog);ledger.log.path=Path(path)
    return ledger


def build(historical_ledger,refinement,output):
    output=Path(output).resolve()
    if output.exists():raise ValueError('FRESH_SOURCE_LEDGER_DIRECTORY_REQUIRED')
    binding=file_binding(historical_ledger);old=read_ledger(historical_ledger)
    events=old.log.read();sources=old.sources();proposal=load(refinement)
    parents,mappings=validate_refinement(proposal,sources,events)
    output.mkdir(parents=True);restrict_directory(output)
    current=ExposureLedger(output/'exposure-events.sqlite')
    # Parents precede derived raw identities; old source IDs remain unchanged.
    for record,review in parents:current.register(record,review=review)
    for sid,source in sources.items():
        record={k:v for k,v in source.items() if k!='review'}
        review=source['review']
        mapping=mappings.get(record['content_sha256'])
        if mapping:
            record.update({k:mapping[k] for k in ('source_family_id','relation','related_source_ids','exposure_status')})
            r=proposal['review'];review={'status':mapping['review_status'],'subject_sha256':digest(record),
                'reviewer':r['reviewer'],'reviewed_at':r['reviewed_at']}
        current.register(record,review=review)
    # Retain every original event, including exact prior family/UNKNOWN fields,
    # as a complete hash-verifiable record rather than rewriting old history.
    (output/'historical-events.restricted.json').write_bytes(canonical(events))
    for event in events:
        current.log.append('preserved:'+event['event_hash'],'HISTORICAL_EVENT_PRESERVED',{'original_event':event})
        if event['event_type']=='SOURCE_EXPOSED':
            p=event['payload']
            current.expose(p['source_ids'],partition=p['partition'],experiment_id=p['experiment_id'],
                actor=p['actor'],evidence_ref='historical-event:'+event['event_hash'],occurred_at=p['occurred_at'],
                event_key='historical:'+event['event_hash'])
    receipt={'schema':'rsi-versioned-source-family-refinement/v1','original_ledger':binding,
        'original_frontier':{'sequence':len(events),'event_hash':events[-1]['event_hash'] if events else '0'*64},
        'refinement':file_binding(refinement),'historical_events':file_binding(output/'historical-events.restricted.json'),
        'raw_source_ids_preserved':sorted(sources),'canonical_parent_ids':[s['source_id'] for s,_ in parents],
        'refined_source_hashes':sorted(mappings),'historical_exposure_events':sum(e['event_type']=='SOURCE_EXPOSED' for e in events),
        'history_completeness':'INCOMPLETE_UNKNOWN','freshness_inferred':False,'production_qualified':False,
        'original_ledger_modified':False,'execution_status':'METADATA_ENGINEERING_ONLY'}
    if file_binding(historical_ledger)!=binding:raise ValueError('HISTORICAL_LEDGER_CHANGED_DURING_REFINEMENT')
    current.log.append('source-refinement-anchor','HISTORICAL_IMPORT_ANCHOR',receipt)
    (output/'refinement-receipt.restricted.json').write_bytes(canonical(receipt))
    return receipt


def validate_refinement(proposal,sources,events):
    """Validate complete source joins before any output or ledger mutation."""
    if proposal.get('schema')!='rsi-source-family-refinement/v1':
        raise ValueError('FAMILY_REFINEMENT_SCHEMA_REQUIRED')
    reviewed(proposal.get('review'),{k:v for k,v in proposal.items() if k!='review'})
    if proposal.get('history_completeness')!='INCOMPLETE_UNKNOWN':
        raise ValueError('FAMILY_EXPOSURE_COMPLETENESS_MUST_REMAIN_UNKNOWN')
    mappings=proposal['mappings'];parent_rows=proposal['parents']
    if not isinstance(mappings,dict) or not mappings or not isinstance(parent_rows,list):
        raise ValueError('FAMILY_REFINEMENT_INCOMPLETE')
    if not isinstance(proposal.get('proofs'),dict):
        raise ValueError('FAMILY_CORPUS_PROOF_REQUIRED')
    if set(proposal['proofs'])-set(mappings):
        raise ValueError('FAMILY_UNPLANNED_CORPUS_PROOF')
    parents=[];by_id={}
    for row in parent_rows:
        source=row['source'];review=row['review'];reviewed(review,source)
        if (source['source_id'] in by_id or source['source_id'] in sources or source['relation']!='ORIGINAL'
                or source['related_source_ids']):raise ValueError('FAMILY_CANONICAL_PARENT_COLLISION')
        by_id[source['source_id']]=source;parents.append((source,review))
    raw_hashes={s['content_sha256'] for s in sources.values()}
    if set(mappings)-raw_hashes:raise ValueError('FAMILY_RAW_SOURCE_UNKNOWN')
    referenced=set()
    for raw_hash,mapping in mappings.items():
        fields={'source_family_id','relation','related_source_ids','exposure_status','review_status'}
        if (set(mapping)!=fields or mapping['relation']!='DERIVED_FROM'
                or len(mapping['related_source_ids'])!=1 or mapping['review_status'] not in {'AI_REVIEWED','HUMAN_REVIEWED'}
                or mapping['review_status']!=proposal['review']['status']):
            raise ValueError('FAMILY_REFINEMENT_MAPPING_INCOMPLETE')
        pid=mapping['related_source_ids'][0];parent=by_id.get(pid)
        if not parent or parent['source_family_id']!=mapping['source_family_id']:
            raise ValueError('FAMILY_CROSS_FAMILY_PARENT')
        if mapping['exposure_status'] not in {'EXPOSURE_UNKNOWN','KNOWN_HISTORY'}:
            raise ValueError('FAMILY_EXPOSURE_STATUS_INVALID')
        matching=[s for s in sources.values() if s['content_sha256']==raw_hash]
        if mapping['exposure_status']=='KNOWN_HISTORY' and any(not any(e['event_type']=='SOURCE_EXPOSED'
                and s['source_id'] in e['payload']['source_ids'] for e in events) for s in matching):
            raise ValueError('FAMILY_KNOWN_HISTORY_REQUIRES_RECORDED_EXPOSURE')
        proof=proposal.get('proofs',{}).get(raw_hash)
        if not isinstance(proof,dict):raise ValueError('FAMILY_CORPUS_PROOF_REQUIRED')
        validate_proof(raw_hash,mapping,parent,proof,proposal)
        referenced.add(pid)
    if referenced!=set(by_id):raise ValueError('FAMILY_UNREFERENCED_CANONICAL_PARENT')
    seen_hashes={};seen_documents={}
    records=[s for s,_ in parents]
    for source in sources.values():
        mapping=mappings.get(source['content_sha256'],{})
        records.append({**source,**{k:mapping[k] for k in ('source_family_id','relation','related_source_ids','exposure_status') if k in mapping}})
    for source in records:
        for index,key in ((seen_hashes,'content_sha256'),(seen_documents,'document_id')):
            if source[key] in index and index[source[key]]!=source['source_family_id']:
                raise ValueError('FAMILY_CONTENT_OR_DOCUMENT_COLLISION')
            index[source[key]]=source['source_family_id']
    return parents,mappings


def validate_proof(raw_hash,mapping,parent,proof,proposal):
    required={'local_source_path','local_source_sha256','canonical_parent_id','dataset','doc_id',
              'dataset_version','source_revisions','manifest_path','manifest_sha256',
              'corpus_path','corpus_sha256','manifest_corpus_sha256','local_canonical_sha256',
              'corpus_canonical_sha256','corpus_record_sha256','corpus_line','canonicalization',
              'version_ref','corpus_join_evidence'}
    if required-set(proof):raise ValueError('FAMILY_CORPUS_PROOF_INCOMPLETE')
    if (any(not isinstance(proof[k],str) or not re.fullmatch(r'[0-9a-f]{64}',proof[k]) for k in
            ('local_source_sha256','manifest_sha256','corpus_sha256','manifest_corpus_sha256',
             'local_canonical_sha256','corpus_canonical_sha256','corpus_record_sha256'))
            or not isinstance(proof['source_revisions'],dict) or not proof['source_revisions']
            or any(not isinstance(v,str) or not re.fullmatch(r'[0-9a-f]{40}',v) for v in proof['source_revisions'].values())):
        raise ValueError('FAMILY_PINNED_HASH_OR_REVISION_REQUIRED')
    if proof['local_source_sha256']!=raw_hash or proof['canonical_parent_id']!=parent['source_id']:
        raise ValueError('FAMILY_RAW_CANONICAL_JOIN_MISMATCH')
    if proof['canonicalization']!='markdown-h1-title-text/v1':
        raise ValueError('FAMILY_EXACT_CANONICALIZATION_REQUIRED')
    raw=Path(proof['local_source_path']).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=raw_hash:raise ValueError('FAMILY_RAW_SOURCE_HASH_CHANGED')
    manifest_raw=bound({'path':proof['manifest_path'],'sha256':proof['manifest_sha256']})
    manifest=json.loads(manifest_raw)
    corpus_files=[f for f in manifest.get('files',[]) if f['path']=='corpus.jsonl']
    if (len(corpus_files)!=1 or corpus_files[0]['sha256']!=proof['corpus_sha256']
            or proof['manifest_corpus_sha256']!=proof['corpus_sha256']
            or manifest.get('dataset')!=proof['dataset'] or manifest.get('dataset_version')!=proof['dataset_version']
            or manifest.get('source_revisions')!=proof['source_revisions']
            or not proof['source_revisions'] or type(proof['corpus_line']) is not int or proof['corpus_line']<1):
        raise ValueError('FAMILY_PINNED_CORPUS_MANIFEST_MISMATCH')
    version_ref={k:proof[k] for k in ('dataset_version','source_revisions','corpus_sha256','manifest_sha256')}
    if proof['version_ref']!=version_ref or parent['version']!='sha256:'+digest(version_ref):
        raise ValueError('FAMILY_CANONICAL_VERSION_BINDING_MISMATCH')
    joins=json.loads(bound(proof['corpus_join_evidence']))
    if not isinstance(joins,list):raise ValueError('FAMILY_CORPUS_SCAN_EVIDENCE_REQUIRED')
    joined=[j for j in joins if j.get('dataset')==proof['dataset']
            and all(j.get(k)==proof[k] for k in ('dataset_version','source_revisions','manifest_sha256','corpus_path','corpus_sha256','manifest_corpus_sha256'))]
    matches=[m for j in joined for m in j.get('exact_content_matches',[])
             if m.get('source_sha256')==raw_hash and str(m.get('doc_id'))==str(proof['doc_id'])
             and all(m.get(k)==proof[k] for k in ('local_canonical_sha256','corpus_canonical_sha256','corpus_record_sha256','corpus_line'))
             and m.get('match')=='CANONICAL_EXACT']
    if len(joined)!=1 or len(matches)!=1:raise ValueError('FAMILY_CORPUS_SCAN_EXACT_JOIN_REQUIRED')
    record=proposal.get('canonical_records',{}).get(parent['source_id'])
    if not isinstance(record,dict) or not isinstance(record.get('raw_record_json'),str):
        raise ValueError('FAMILY_EXACT_CANONICAL_RECORD_REQUIRED')
    record_raw=record['raw_record_json'].encode('utf-8')
    if hashlib.sha256(record_raw).hexdigest()!=proof['corpus_record_sha256']:
        raise ValueError('FAMILY_CANONICAL_RECORD_HASH_CHANGED')
    doc=json.loads(record_raw)
    doc_id=str(doc.get('_id',doc.get('id',doc.get('doc_id',''))))
    if doc_id!=str(proof['doc_id']) or not isinstance(doc.get('title'),str) or not isinstance(doc.get('text'),str):
        raise ValueError('FAMILY_CANONICAL_DOCUMENT_ID_MISMATCH')
    canonical_text=(doc['title']+'\n\n'+doc['text']).encode('utf-8')
    h=hashlib.sha256(canonical_text).hexdigest()
    if (not raw.startswith(b'# ') or raw[2:]!=canonical_text
            or any(v!=h for v in (proof['local_canonical_sha256'],proof['corpus_canonical_sha256'],parent['content_sha256']))):
        raise ValueError('FAMILY_CANONICAL_CONTENT_EXACT_JOIN_FAILED')
    if parent['source_id']!='upstream:'+proof['dataset']+':doc:'+doc_id:
        raise ValueError('FAMILY_CANONICAL_PARENT_ID_MISMATCH')
    # Family granularity is predeclared for the two supported refinements;
    # LongBench record identities remain auxiliary until article-family review.
    if proof['dataset']=='miracl-zh':family='miracl-zh:article-component:'+doc_id.split('#')[0]
    elif proof['dataset']=='scifact':family='scifact:paper:'+doc_id
    else:raise ValueError('FAMILY_GRANULARITY_NOT_REVIEWED')
    if mapping['source_family_id']!=family:raise ValueError('FAMILY_CANONICAL_GRANULARITY_MISMATCH')
    if not isinstance(parent['version'],str) or not parent['version']:
        raise ValueError('FAMILY_CANONICAL_VERSION_REQUIRED')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('historical-ledger','refinement','output'):p.add_argument('--'+name,required=True)
    a=p.parse_args();print(json.dumps(build(a.historical_ledger,a.refinement,a.output)))

"""Import historical identities/exposure conservatively, never certify freshness.

Only source manifests and archived sealed-use metadata are read. No Gold or
source text is exported. Unresolved family identity remains EXPOSURE_UNKNOWN.
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sqlite3
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.events import digest
from rsik.exposure import ExposureLedger


def bootstrap(old_root, output):
    old_root,output=Path(old_root),Path(output)
    output.mkdir(parents=True,exist_ok=True)
    ledger=ExposureLedger(output/'exposure-events.sqlite')
    records={};uses={};controls=set();unreadable=[]
    for manifest in sorted(old_root.rglob('sources.manifest.json')):
        try:items=json.loads(manifest.read_text(encoding='utf-8-sig'))
        except (PermissionError,OSError):
            unreadable.append(manifest.relative_to(old_root).as_posix());continue
        if not isinstance(items,list):continue
        relative=manifest.relative_to(old_root).as_posix()
        for item in items:
            sha=item.get('source_sha256')
            if not isinstance(sha,str) or len(sha)!=64:continue
            original=str(item.get('original_path',''))
            control=any(p.startswith(('test_','release-pytest')) for p in manifest.parts) or 'SYNTHETIC' in original
            if sha not in records:
                records[sha]={'source_id':sha,'document_id':original or 'UNKNOWN:'+sha,
                    'version':sha,'content_sha256':sha,'source_family_id':'UNRESOLVED_HISTORICAL_FAMILY',
                    'origin':'HISTORICAL_METADATA_ONLY','relation':'UNKNOWN','related_source_ids':[],
                    'exposure_status':'EXPOSURE_UNKNOWN'}
            uses.setdefault(sha,[]).append({'manifest':relative,'partition':'CONTROL' if control else 'DEV'})
            if control:controls.add(sha)
    # The same artifact cannot be re-labeled fresh just because current files
    # are under a different folder. Sealed starts consume listed source hashes.
    sealed=old_root/'query-aware-v1/registry/sealed.sqlite';consumed=set()
    if sealed.is_file():
        db=sqlite3.connect(sealed.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            for (raw,) in db.execute('SELECT event_json FROM events ORDER BY sequence'):
                event=json.loads(raw)
                if event.get('event_type')=='QUERY_SEALED_BUNDLE_STARTED':
                    consumed.update(event['payload'].get('source_hashes',[]))
        finally:db.close()
    stamp_path=output/'import-start.json'
    if stamp_path.exists():
        stamp=json.loads(stamp_path.read_text())
        if stamp['input_root']!=str(old_root.resolve()) or stamp['source_identity_hash']!=digest(records):raise ValueError('IMPORT_INPUT_IDENTITY_CHANGED')
        now=stamp['created_at']
    else:
        now=datetime.now(timezone.utc).isoformat()
        stamp_path.write_text(json.dumps({'input_root':str(old_root.resolve()),'source_identity_hash':digest(records),'created_at':now}),encoding='utf-8')
    for sha,source in records.items():
        ledger.register(source,review={'status':'UNREVIEWED','subject_sha256':digest(source),
            'reviewer':'metadata-import; source-family relations not reviewed','reviewed_at':now})
        for usage in uses[sha]:
            ledger.expose([sha],partition=usage['partition'],experiment_id='historical-v22-import',
                actor='trusted-metadata-import',evidence_ref=usage['manifest'],occurred_at=now,
                event_key=digest({'sha':sha,**usage}))
        if sha in consumed:
            ledger.expose([sha],partition='DEV',experiment_id='historical-consumed-holdout',
                actor='trusted-metadata-import',evidence_ref='archived sealed-start event; never fresh again',
                occurred_at=now,event_key='consumed:'+sha)
    families=ledger.sources()
    (output/'source-families.restricted.json').write_text(json.dumps(families,indent=2),encoding='utf-8')
    summary={'schema':'rsi-historical-exposure-import/v1','registered_sources':len(records),
        'manifest_occurrences':sum(map(len,uses.values())),'control_source_ids':len(controls),
        'consumed_holdout_sources':len(consumed),'unresolved_family_sources':len(records),
        'source_text_or_gold_read':False,'new_final_eligible_sources':0,
        'status':'BLOCKED_DATA','reason':'Unreviewed derivation/reprint/family relations retained as UNKNOWN',
        'unreadable_manifests':unreadable,'history_completeness':'INCOMPLETE_UNKNOWN' if unreadable else 'METADATA_IMPORTED_FAMILY_UNKNOWN',
        'events':len(ledger.log.read()),'ledger_hash_chain_only_not_third_party_proof':True}
    (output/'import-receipt.json').write_text(json.dumps(summary,indent=2),encoding='utf-8')
    return summary


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--old-root',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    a=p.parse_args();print(json.dumps(bootstrap(a.old_root,a.output)))

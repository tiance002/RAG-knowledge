"""Prepare an isolated prequalification collection epoch, without executing it.

This imports the complete old exposure chain by reference and projects all its
exposures onto the current document aliases. UNKNOWN remains UNKNOWN. It never
creates fresh holdout identities, a baseline qualification or a search result.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rsik.budgets import Budget, PRICE_VERSION
from rsik.events import EventLog, canonical, digest
from rsik.execution_evidence import ExecutionEvidence
from rsik.exposure import ExposureLedger
from rsik.foundation import verify_boundary
from rsik.handbook.store import Handbook
from rsik.launch import file_binding, fixture_identity
from rsik.query_aware import QueryRegistry
from rsik.query_learning import ExperienceSession
from rsik.query_workflow import implementation_hashes


def prepare(*, fixture, historical_ledger, registry, candidate_id, budget_ledger,
            collection_plan, price_review, business_root, output, run_output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    fixture = Path(fixture).resolve()
    specs = json.loads((fixture/'relations.restricted.json').read_bytes())
    aliases = {sid:s for spec in specs.values() for sid,s in spec['sources'].items()}
    original_binding = file_binding(historical_ledger)
    old = object.__new__(ExposureLedger)
    old.log = object.__new__(EventLog)
    old.log.path = Path(historical_ledger)
    events = old.log.read()
    sources = old.sources()
    current = ExposureLedger(output/'exposure-events.sqlite')
    mapping = {}
    fields = ('document_id','version','content_sha256','source_family_id','relation',
              'related_source_ids','exposure_status')
    now = datetime.now(timezone.utc).isoformat()
    by_hash = {s['content_sha256']:sid for sid,s in aliases.items()}
    for sid, source in sources.items():
        if source['content_sha256'] in by_hash:
            alias_id = by_hash[source['content_sha256']]
            alias = aliases[alias_id]
            # New alias labels are provisional; they must not resolve UNKNOWN.
            if alias['relation'] != 'UNKNOWN' or alias['exposure_status'] != 'EXPOSURE_UNKNOWN':
                raise ValueError('UNKNOWN_HISTORY_MUST_NOT_BE_RELABELLED_KNOWN')
            record = {'source_id':alias_id,'origin':'VERSIONED_DOCUMENT_ALIAS_UNKNOWN_FAMILY',
                      **{k:alias[k] for k in fields}}
            review = {'status':alias['review_status'],'subject_sha256':digest(record),
                      'reviewer':'Codex exact DEV document identity only; upstream relation unresolved',
                      'reviewed_at':now}
            mapping[sid] = alias_id
        else:
            record = {k:v for k,v in source.items() if k != 'review'}
            review = source['review']
            mapping[sid] = sid
        current.register(record, review=review)
    if set(aliases) - set(mapping.values()):
        raise ValueError('UNACCOUNTED_ALIAS_HISTORY')
    for event in events:
        if event['event_type'] != 'SOURCE_EXPOSED':
            continue
        p = event['payload']
        current.expose(sorted({mapping[s] for s in p['source_ids']}),
            partition=p['partition'],experiment_id=p['experiment_id'],actor=p['actor'],
            evidence_ref='historical-event:'+event['event_hash'],occurred_at=p['occurred_at'],
            event_key='historical:'+event['event_hash'])
    imported = {'schema':'rsi-exposure-alias-import/v1',
        'original_ledger':original_binding,
        'original_frontier':{'sequence':len(events),'event_hash':events[-1]['event_hash']},
        'source_id_mapping':mapping,'historical_exposure_events':sum(e['event_type']=='SOURCE_EXPOSED' for e in events),
        'claim':'Document aliases only; UNKNOWN cluster and every prior exposure preserved; no new independence or freshness',
        'original_ledger_modified':False}
    if file_binding(historical_ledger) != original_binding:
        raise ValueError('HISTORICAL_LEDGER_CHANGED_DURING_IMPORT')
    (output/'import-receipt.restricted.json').write_bytes(canonical(imported))
    current.log.append('import-anchor','HISTORICAL_IMPORT_ANCHOR',imported)
    current.expose(sorted(aliases), partition='DEV',experiment_id='rsi-s3-coverage-collection-20261115',
        actor='ENGINEERING_CURATION',evidence_ref=str(fixture/'build-receipt.json'),occurred_at=now,
        event_key='current-curation')
    snapshot = current.snapshot(sorted(aliases),experiment_id='rsi-s3-coverage-collection-20261115')
    (output/'exposure-snapshot.restricted.json').write_bytes(canonical(snapshot))
    (output/'source-families.restricted.json').write_bytes(canonical(snapshot['sources']))
    session = ExperienceSession(output/'learning',Handbook(output/'handbook'))
    candidate = QueryRegistry(registry).get(candidate_id)
    budget = Budget(budget_ledger)
    log = budget.log.read()
    plan = {'schema':'rsi-launch/v1','run_id':'rsi-s3-coverage-collection-20261115',
        'allowed_phases':['COVERAGE_COLLECTION'],'run_directory':str(Path(run_output).resolve()),
        'implementation':implementation_hashes(),'protected':verify_boundary(),
        'execution_policy':ExecutionEvidence.real(business_root).to_dict(),
        'registry':str(Path(registry).resolve()),
        'candidate':{'candidate_id':candidate_id, 'worker_sha256':digest_bytes(candidate['worker']),
                     'config_sha256':digest_bytes(candidate['config'])},
        'budget':{'path':str(Path(budget_ledger).resolve()),'price_version':PRICE_VERSION,
                  'limit_usd':'5','reserve_usd':'0',
                  'event_prefix':{'sequence':len(log),'event_hash':log[-1]['event_hash'] if log else '0'*64}},
        'price_review':file_binding(price_review),'models':{'diagnose':'gpt-6-luna'},
        'fixtures':{str(fixture):fixture_identity(fixture)},'evaluator_profile':'source-relations/v1',
        'seam_profile':'rsi-query-aware-selector/v1',
        'source_families':file_binding(output/'source-families.restricted.json'),
        'exposure_snapshot':file_binding(output/'exposure-snapshot.restricted.json'),
        'coverage_collection':file_binding(collection_plan),'initial_memory':file_binding(session.initial),
        'experiment':{'epoch_id':'s3-engineering-coverage-v1','group':'ENGINEERING_CONTROL_ONLY','repetition':1,
            'final_nomination_limit':1,'final_holdout_uses':1,
            'analysis_points':['COMPLETE_20_CASES_AND_EVERY_DECLARED_ATOM_CONTROL'],
            'stop_rules':['INFRA_FAILURE','EVIDENCE_UNKNOWN','UNPLANNED_ARTIFACT'],
            'autonomous_gain_credit':False}}
    (output/'launch-plan.restricted.json').write_bytes(canonical(plan))
    return {'plan':file_binding(output/'launch-plan.restricted.json'),'source_count':len(aliases),
            'exposure_snapshot_eligible':snapshot['eligible'],'independence_claim':False,
            'paid_requests':0,'execution_status':'NOT_EXECUTED'}


def digest_bytes(data):
    import hashlib
    return hashlib.sha256(data).hexdigest()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('fixture','historical-ledger','registry','candidate-id','budget-ledger',
                 'collection-plan','price-review','business-root','output','run-output'):
        parser.add_argument('--'+name,required=True)
    args = vars(parser.parse_args())
    print(json.dumps(prepare(**{k.replace('-','_'):v for k,v in args.items()})))

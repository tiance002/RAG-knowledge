"""Synthetic history-import controls; no archive, RPC, model, or admission proof."""
from pathlib import Path
import hashlib
import json

import pytest

from scripts import prepare_rsi_coverage_launch as builder
from rsik.budgets import Budget
from rsik.events import canonical, digest
from rsik.execution_evidence import ExecutionEvidence
from rsik.exposure import ExposureLedger, validate_snapshot
from rsik.launch import bind_fixture_sources, fixture_identity
from rsik.query_aware import QueryRegistry, adapt_legacy
from rsik.source_relations import check_fixture
from test_source_relations import _build_fixture
from portable_fixtures import ROOT


def read(path):
    return json.loads(Path(path).read_bytes())


def review(subject):
    return {'status':'AI_REVIEWED','subject_sha256':digest(subject),
            'reviewer':'SYNTHETIC_IMPORT_CONTROL','reviewed_at':'2026-11-15T00:00:00+00:00'}


@pytest.fixture
def import_control(tmp_path,monkeypatch):
    fixture=_build_fixture(tmp_path/'fixture',partition='DEV',source_status='EXPOSURE_UNKNOWN',count=1)
    specs=read(fixture/'relations.restricted.json')
    aliases={sid:s for spec in specs.values() for sid,s in spec['sources'].items()}
    for index,source in enumerate(aliases.values()):
        source['relation']='UNKNOWN'
        source['source_family_id']=f'PROVISIONAL-DOCUMENT-LABEL-{index}'
    (fixture/'relations.restricted.json').write_bytes(canonical(specs))
    manifest=read(fixture/'manifest.json')
    manifest['artifact_hashes']['relations.restricted.json']=hashlib.sha256((fixture/'relations.restricted.json').read_bytes()).hexdigest()
    (fixture/'manifest.json').write_bytes(canonical(manifest))
    assert check_fixture(fixture)['exposure_statuses']==['EXPOSURE_UNKNOWN']
    historical=ExposureLedger(tmp_path/'historical.sqlite')
    mapping={}
    for index,(alias_id,alias) in enumerate(aliases.items()):
        sid=f'old-hash-id-{index}';mapping[sid]=alias_id
        source={'source_id':sid,'document_id':f'old-document-{index}','version':'legacy',
            'content_sha256':alias['content_sha256'],'source_family_id':'OLD-UNRESOLVED-FAMILY',
            'origin':'SYNTHETIC_HISTORY','relation':'UNKNOWN','related_source_ids':[],
            'exposure_status':'EXPOSURE_UNKNOWN'}
        historical.register(source,review=review(source))
    untouched={'source_id':'historical-unaliased','document_id':'unrelated','version':'1',
        'content_sha256':'f'*64,'source_family_id':'OTHER-UNRESOLVED-FAMILY','origin':'SYNTHETIC_HISTORY',
        'relation':'UNKNOWN','related_source_ids':[],'exposure_status':'EXPOSURE_UNKNOWN'}
    historical.register(untouched,review=review(untouched))
    mapping[untouched['source_id']]=untouched['source_id']
    for index,sids in enumerate(([list(mapping)[0]],[list(mapping)[1]],[list(mapping)[0],untouched['source_id']])):
        historical.expose(sids,partition='DEV',experiment_id=f'historical-exp-{index}',
            actor=f'historical-actor-{index}',evidence_ref=f'synthetic-evidence-{index}',
            occurred_at=f'2026-11-0{index+1}T00:00:00+00:00',event_key=f'historical-{index}')
    registry=QueryRegistry(tmp_path/'query'/'registry')
    cid=registry.put(adapt_legacy((ROOT/'scripts/rsi_delivery_worker.py').read_bytes()),
        b'{"selection":"all"}',parent_id='OWNER_ROOT',origin='OWNER_SPECIFIED',evidence={})
    budget=Budget(tmp_path/'task-model-budget.sqlite')
    # These two files are deliberately only synthetic binding inputs. This test
    # proves preparation/import, never a reviewed launch or model-price claim.
    collection=tmp_path/'synthetic-collection.json';collection.write_bytes(canonical({'SYNTHETIC':'NOT_ADMISSION_PROOF'}))
    prices=tmp_path/'synthetic-prices.json';prices.write_bytes(canonical({'SYNTHETIC':'NOT_PRICE_PROOF'}))
    monkeypatch.setattr(builder,'implementation_hashes',lambda:{'SYNTHETIC':'NO_REAL_EXECUTION'})
    monkeypatch.setattr(ExecutionEvidence,'real',classmethod(lambda cls,root:ExecutionEvidence.synthetic_control()))
    kwargs={'fixture':fixture,'historical_ledger':historical.log.path,'registry':registry.directory,
        'candidate_id':cid,'budget_ledger':budget.log.path,
        'collection_plan':collection,'price_review':prices,'business_root':tmp_path/'unused-business',
        'output':tmp_path/'prepared','run_output':tmp_path/'not-executed'}
    return kwargs,historical,budget,mapping,aliases


def test_prepare_preserves_complete_history_and_unknown_freshness(import_control):
    kwargs,historical,budget,mapping,aliases=import_control
    original=Path(historical.log.path).read_bytes();events=historical.log.read()
    original_budget=budget.log.read()
    before=historical.final_eligibility(list(mapping),experiment_id='unseen-future-experiment')
    assert not before['eligible'] and 'SOURCE_FAMILY_ALREADY_EXPOSED' in before['blocking_reasons']
    result=builder.prepare(**kwargs)
    out=kwargs['output'];receipt=read(out/'import-receipt.restricted.json')
    assert Path(historical.log.path).read_bytes()==original
    assert historical.log.read()==events
    assert receipt['original_ledger']['sha256']==hashlib.sha256(original).hexdigest()
    assert receipt['original_frontier']=={'sequence':len(events),'event_hash':events[-1]['event_hash']}
    assert receipt['original_ledger_modified'] is False
    assert receipt['source_id_mapping']==mapping
    imported=ExposureLedger(out/'exposure-events.sqlite')
    assert imported.sources()['historical-unaliased']==historical.sources()['historical-unaliased']
    imported_events=imported.log.read()
    anchor=[e for e in imported_events if e['event_type']=='HISTORICAL_IMPORT_ANCHOR']
    assert len(anchor)==1 and anchor[0]['payload']==receipt
    old_exposures=[e for e in events if e['event_type']=='SOURCE_EXPOSED']
    projected=[e for e in imported_events if e['event_type']=='SOURCE_EXPOSED'
               and e['payload']['evidence_ref'].startswith('historical-event:')]
    assert len(projected)==len(old_exposures)==receipt['historical_exposure_events']
    projected_by_ref={e['payload']['evidence_ref']:e['payload'] for e in projected}
    for event in old_exposures:
        old=event['payload'];new=projected_by_ref['historical-event:'+event['event_hash']]
        assert new['source_ids']==sorted({mapping[sid] for sid in old['source_ids']})
        for field in ('partition','experiment_id','actor','occurred_at'):
            assert new[field]==old[field]
        assert '__UNKNOWN_SOURCE_FAMILY__' in old['dependency_cluster_ids']
        assert '__UNKNOWN_SOURCE_FAMILY__' in new['dependency_cluster_ids']
    snapshot=read(out/'exposure-snapshot.restricted.json')
    assert validate_snapshot(snapshot)==snapshot
    plan=read(out/'launch-plan.restricted.json')
    assert plan['fixtures']=={str(kwargs['fixture'].resolve()):fixture_identity(kwargs['fixture'])}
    bind_fixture_sources(plan,snapshot)
    assert read(out/'source-families.restricted.json')==snapshot['sources']
    assert {s['source_id'] for s in snapshot['sources']}==set(aliases)
    for source in snapshot['sources']:
        assert source['source_family_id']==aliases[source['source_id']]['source_family_id']
        assert source['relation']=='UNKNOWN' and source['exposure_status']=='EXPOSURE_UNKNOWN'
    after=imported.final_eligibility(sorted(aliases),experiment_id='unseen-future-experiment')
    assert not after['eligible'] and not after['production_review_eligible']
    assert {'EXPOSURE_UNKNOWN','SOURCE_FAMILY_UNKNOWN','SOURCE_FAMILY_ALREADY_EXPOSED'}<=set(after['blocking_reasons'])
    assert '__UNKNOWN_SOURCE_FAMILY__' in after['dependency_cluster_ids']
    assert result['paid_requests']==0 and result['execution_status']=='NOT_EXECUTED'
    assert result['independence_claim'] is False and result['exposure_snapshot_eligible'] is False
    assert plan['allowed_phases']==['COVERAGE_COLLECTION']
    assert 'qualification' not in plan and 'coverage' not in plan
    assert not kwargs['run_output'].exists()
    assert budget.log.read()==original_budget
    assert budget.snapshot()['attempts']==0
    assert not list(out.rglob('*qualification*')) and not list(out.rglob('*capture*'))


def test_import_rejects_alias_that_would_resolve_unknown(import_control):
    kwargs,historical,budget,mapping,aliases=import_control
    original=Path(historical.log.path).read_bytes()
    specs=read(kwargs['fixture']/'relations.restricted.json')
    next(iter(next(iter(specs.values()))['sources'].values()))['relation']='ORIGINAL'
    (kwargs['fixture']/'relations.restricted.json').write_bytes(canonical(specs))
    with pytest.raises(ValueError,match='UNKNOWN_HISTORY_MUST_NOT_BE_RELABELLED_KNOWN'):
        builder.prepare(**kwargs)
    assert Path(historical.log.path).read_bytes()==original
    assert budget.snapshot()['attempts']==0

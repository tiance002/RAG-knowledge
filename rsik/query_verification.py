"""Receipt-bound query-profile training and one-use sealed-source verification."""
from pathlib import Path
import time
from .query_aware import QueryRegistry,invariant_probe_source
from .experiment import load,bytes_hash,read_decision
from .adjudicate import adjudicate_round,adjudicate_holdout
from .foundation import check_split,source_separation,verify_boundary
from .holdout import replay_artifacts
from .state import canonical,digest
from .events import EventLog
from .locking import atomic_write,WorkspaceLock,restrict_directory


def claim_bundle(log,candidate_id,sources,freezes,output):
    if not 10<=len(sources)<=20 or len(freezes)!=3:raise ValueError('SEALED_BUNDLE_SHAPE_INVALID')
    if any(set(e['payload'].get('source_hashes',[]))&set(sources) for e in log.read() if e['event_type']=='QUERY_SEALED_BUNDLE_STARTED'):
        raise ValueError('SEALED_SOURCES_ALREADY_USED')
    bundle_hash=digest(freezes)
    log.append('sealed-start:'+bundle_hash,'QUERY_SEALED_BUNDLE_STARTED',{'candidate_id':candidate_id,
        'source_hashes':sorted(sources),'bundle_hash':bundle_hash,'freeze_hashes':freezes,
        'output':str(Path(output).resolve()),'statistical_denominator':len(sources),'auxiliary_cases':2*len(sources)})
    return bundle_hash


def verify_training(registry,training,fixtures,cid):
    registry=QueryRegistry(registry);registry.require_not_quarantined(cid)
    training=Path(training);record=load(training/'round.json');candidate=registry.get(cid)
    if record.get('adopted_candidate_id')!=cid or record.get('suite_expected')!=4 or len(record.get('suite_decisions',[]))!=4:
        raise ValueError('QUERY_TRAINING_ADOPTION_REQUIRED')
    if (training/'probes/worker.py').read_bytes()!=invariant_probe_source(candidate['worker']) or load(training/'probes/capture.json').get('status')!='PASS':
        raise ValueError('QUERY_UNIT_PROFILE_STALE_OR_FAILED')
    freezes={str(Path(f).resolve()):check_split(f,expected_count=20) for f in fixtures}
    if load(training/'fixture-freezes.restricted.json')!=freezes:raise ValueError('QUERY_TRAINING_FIXTURE_CHANGED')
    parent=registry.get(candidate['record']['parent_id']);decisions=[]
    for index,f in enumerate(fixtures,1):
        run=training/('suite-'+str(index));manifest=load(run/'manifest.json')
        for arm,artifact in [('A',parent),('B',candidate)]:
            spec=manifest['arms'][arm]
            if (spec['candidate_id']!=artifact['candidate_id'] or spec['worker_sha256']!=bytes_hash(artifact['worker'])
                    or spec['sha256']!=bytes_hash(artifact['config'])):raise ValueError('QUERY_TRAINING_LINEAGE_CHANGED')
        decision=read_decision(run,f,cid,{'angle':'P2_BUDGET','proposal_id':'rsi-query-aware-engineering/v1'},
            candidate['record']['worker_sha256'],candidate['record']['config_sha256'])
        if decision!=record['suite_decisions'][index-1] or not all(decision['gates']['G'+str(i)] is True for i in range(1,5)):
            raise ValueError('QUERY_TRAINING_EVIDENCE_CHANGED')
        decisions.append(decision)
    if adjudicate_round([decisions[0]])['adopted_candidate_id']!=cid:raise ValueError('QUERY_TRAINING_ADOPTION_NOT_REPRODUCIBLE')
    return candidate,parent


def verify_sealed(*,registry,training,train_fixtures,sealed_fixtures,used_holdout,candidate_id,trusted_id,capsule,output):
    registry,output=Path(registry),Path(output);qreg=QueryRegistry(registry)
    with WorkspaceLock(registry/'writer.lock','query-sealed-verification'):
        verify_boundary();candidate,parent=verify_training(registry,training,train_fixtures,candidate_id)
        trusted=qreg.get(trusted_id);qreg.require_not_quarantined(trusted_id)
        primary=Path(sealed_fixtures[0]);count=len(load(primary/'cases.json'))
        if len(sealed_fixtures)!=3 or not 10<=count<=20:raise ValueError('SEALED_BUNDLE_SHAPE_INVALID')
        primary_sources={s['source_sha256'] for s in load(primary/'sources.manifest.json')}
        freezes=[]
        for fixture in sealed_fixtures:
            fixture=Path(fixture);freeze=check_split(fixture,expected_count=count)
            if load(fixture/'freeze.json')!=freeze:raise ValueError('SEALED_FIXTURE_CHANGED')
            if {s['source_sha256'] for s in load(fixture/'sources.manifest.json')}!=primary_sources:raise ValueError('SEALED_AUXILIARY_SOURCE_SET_CHANGED')
            freezes.append(bytes_hash((fixture/'freeze.json').read_bytes()))
        if source_separation(train_fixtures[0],primary)['collisions'] or source_separation(used_holdout,primary)['collisions']:
            raise ValueError('SEALED_SOURCES_NOT_INDEPENDENT')
        log=EventLog(registry/'sealed.sqlite')
        if any(set(e['payload'].get('source_hashes',[]))&primary_sources for e in log.read() if e['event_type']=='QUERY_SEALED_BUNDLE_STARTED'):
            raise ValueError('SEALED_SOURCES_ALREADY_USED')
        if output.exists():raise ValueError('FRESH_SEALED_OUTPUT_REQUIRED')
        output.mkdir(parents=True);restrict_directory(output)
        bundle_hash=claim_bundle(log,candidate_id,primary_sources,freezes,output)
        try:
            capsule.setup();deadline=time.monotonic()+1800
            decisions=[];trusted_primary=None
            for index,fixture in enumerate(sealed_fixtures,1):
                decision=replay_artifacts(capsule,fixture,{'A':parent,'B':candidate},output/('vs-parent-'+str(index)),
                    deadline=deadline,schema='rsi-query-aware-engineering/v1',scope='one-use fresh source sealed bundle; auxiliary cases never enlarge statistical denominator')
                atomic_write(output/('comparison-'+str(index)+'.json'),canonical(decision),exclusive=True);decisions.append(decision)
            if parent['candidate_id']==trusted_id:trusted_primary=decisions[0]
            else:
                trusted_primary=replay_artifacts(capsule,primary,{'A':trusted,'B':candidate},output/'vs-trusted',
                    deadline=deadline,schema='rsi-query-aware-engineering/v1',scope='one-use fresh source sealed comparison vs trusted anchor')
            result=adjudicate_holdout(decisions[0],trusted_primary)
            auxiliary_pass=all(all(d['gates']['G'+str(i)] is True for i in range(1,5)) for d in decisions[1:])
            if not auxiliary_pass:
                confirmed=any('FACT_LOSS' in d['reason_codes'] or 'LOCAL_REGRESSION' in d['reason_codes'] or 'EXEC_FAULT' in d['reason_codes'] for d in decisions[1:])
                result.update(verdict='REGRESSION_SUSPECTED' if confirmed else 'HOLDOUT_INCONCLUSIVE',reason='SEALED_AUXILIARY_HARD_GATE_FAILED')
            result.update(schema='rsi-query-aware-sealed/v1',candidate_id=candidate_id,parent_id=parent['candidate_id'],trusted_id=trusted_id,
                shared_primary_comparison=parent['candidate_id']==trusted_id,auxiliary_pass=auxiliary_pass,
                suite_decisions=decisions,statistical_denominator=count,auxiliary_cases=2*count,
                bundle_hash=bundle_hash,training_receipt_sha256=bytes_hash((Path(training)/'round.json').read_bytes()),
                production_changed=False,answer_quality='NOT_RUN',retrieval_quality='NOT_RUN')
            path=output/'receipt.restricted.json';atomic_write(path,canonical(result),exclusive=True)
            log.append('sealed-finish:'+bundle_hash,'QUERY_SEALED_BUNDLE_FINISHED',{'candidate_id':candidate_id,
                'verdict':result['verdict'],'receipt_path':str(path.resolve()),'receipt_sha256':bytes_hash(path.read_bytes()),'bundle_hash':bundle_hash})
            if result['verdict']=='REGRESSION_SUSPECTED':qreg.quarantine(candidate_id,path,'SEALED_SOURCE_REGRESSION')
            return result
        finally:capsule.close()

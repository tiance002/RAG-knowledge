"""Actual business-delivery diagnostics against archived frozen upstream frames.

The unchanged assessor and source-position checks review actual batch captures.
This does not create ordinary per-case execution receipts or admit a parent.
"""
from pathlib import Path
import time
from .experiment import load,executor,read_decision,bytes_hash
from .foundation import check_split
from .query_batch import diagnostic_worker
from .query_aware import invariant_probe_source
from .locking import atomic_write
from .state import canonical
from .provenance import assess_provenance
from .adjudicate import evaluate_candidate,adjudicate_round


def frames(ordinary_round,fixtures,arm='A'):
    ordinary_round=Path(ordinary_round);entries=[];baseline={};parent_id=None;worker=None;config=None
    for suite,fixture in enumerate(fixtures,1):
        fixture=Path(fixture);check_split(fixture,expected_count=20)
        run=ordinary_round/('suite-'+str(suite));manifest=load(run/'manifest.json')
        spec=manifest['arms']['B']
        audited=read_decision(run,fixture,spec['candidate_id'],{'angle':'P2_BUDGET','proposal_id':manifest['schema']},spec['worker_sha256'],spec['sha256'])
        current=manifest['arms'][arm];raw_worker=(run/(arm+'_worker.py')).read_bytes();raw_config=(run/(arm+'.json')).read_bytes()
        if bytes_hash(raw_worker)!=current['worker_sha256'] or bytes_hash(raw_config)!=current['sha256']:raise ValueError('PREVIEW_PARENT_ARTIFACT_CHANGED')
        if parent_id is not None and (parent_id!=current['candidate_id'] or worker!=raw_worker or config!=raw_config):raise ValueError('MIXED_PREVIEW_PARENT')
        parent_id,worker,config=current['candidate_id'],raw_worker,raw_config
        cases,rubric,proof=load(fixture/'cases.json'),load(fixture/'evaluation.json'),load(fixture/'provenance.restricted.json')
        for index,(item,row) in enumerate(zip(cases,audited['per_question'],strict=True),1):
            key=f'{suite}-{index}';directory=run/f'case-{index:03}';snapshot=load(directory/'arm-input.json');capture=load(directory/arm/'capture.json')
            if row['pair_status']!='PAIRED' or not row['pair_identity']:raise ValueError('UNPAIRED_PREVIEW_BASELINE')
            if capture['upstream_sha256']!=executor.fingerprint(snapshot):raise ValueError('PREVIEW_FRAME_CHANGED')
            entries.append({'key':key,'snapshot':snapshot,'config':load(run/(arm+'.json'))})
            baseline[key]={'case':item['case'],'qid':item['qid'],'capture':capture,'rubric':rubric[item['qid']],
                'proof':proof[item['qid']],'source_bytes':(fixture/proof[item['qid']]['source_relative_path']).read_bytes(),'suite':suite}
    return {'parent_id':parent_id,'worker':worker,'config':config,'entries':entries,'baseline':baseline}


def replay(capsule,frame,worker,output,candidate_id):
    output=Path(output);output.mkdir(parents=True,exist_ok=False)
    unit=output/'unit';unit.mkdir();atomic_write(unit/'worker.py',invariant_probe_source(worker),exclusive=True);atomic_write(unit/'input.json',b'{}',exclusive=True)
    capsule.deadline=time.monotonic()+90
    try:
        receipt=capsule.child('prepare',[unit/'input.json',unit/'capture.json'],unit,worker=unit/'worker.py',business_root=capsule.business_root)
    except ValueError:
        if 'QUERY_' not in (unit/'worker.log').read_text(encoding='utf-8',errors='replace'):raise
        return {'schema':'rsi-query-diagnostic/v1','status':'PRECHECK_REJECTED','reason':'UNIT_FAILED','formal_admission':False}
    if receipt['status']!='PASS':raise ValueError('PREVIEW_UNIT_INCOMPLETE')
    wrapper=diagnostic_worker(worker);atomic_write(output/'worker.py',wrapper,exclusive=True)
    atomic_write(output/'input.json',canonical({'entries':frame['entries']}),exclusive=True)
    capsule.deadline=time.monotonic()+90
    actual=capsule.child('prepare',[output/'input.json',output/'capture.json'],output,worker=output/'worker.py',business_root=capsule.business_root)
    captures={r['key']:r['capture'] for r in actual['captures']}
    if len(captures)!=len(actual['captures']) or set(captures)!=set(frame['baseline']):raise ValueError('PREVIEW_OUTPUT_INCOMPLETE')
    faults=[key for key,capture in captures.items() if capture.get('status')!='OK']
    if faults:
        result={'schema':'rsi-query-diagnostic/v1','status':'PRECHECK_REJECTED','reason':'DELIVERY_EXECUTION_FAULT',
            'failed_keys':faults,'formal_admission':False,'candidate_id':candidate_id,'recommend_ordinary_replay':False}
        atomic_write(output/'diagnostic.json',canonical(result),exclusive=True);return result
    groups={i:[] for i in range(1,5)}
    for key,base in frame['baseline'].items():
        a,b=base['capture'],captures[key];case=base['case'];proof=base['proof']
        pa=assess_provenance(case,a,proof,base['source_bytes']);pb=assess_provenance(case,b,proof,base['source_bytes'])
        sa=executor.assess(case,a,base['rubric'],'A');sb=executor.assess(case,b,base['rubric'],'B')
        groups[base['suite']].append({'qid':base['qid'],'source_group':proof['source_group'],
            'chars_A':len(a.get('context','')),'chars_B':len(b.get('context','')),
            'parent_facts':{f['fact_id']:f['status'] for f in pa['facts']},'candidate_facts':{f['fact_id']:f['status'] for f in pb['facts']},
            'parent_faults':sa['faults'],'candidate_faults':sb['faults'],'provenance_A':pa['status'],'provenance_B':pb['status'],
            'execution':'COMPLETED','pair_status':'PAIRED' if a.get('status')==b.get('status')=='OK' else 'FAILED',
            'pair_identity':a.get('upstream_sha256')==b.get('upstream_sha256'),'fault_origin':'CANDIDATE' if sb['faults'] else 'NONE'})
    decisions=[evaluate_candidate({'angle':'P2_BUDGET','proposal_id':'batch-diagnostic','candidate_id':candidate_id,
        'worker_hash':bytes_hash(worker),'config_hash':bytes_hash(frame['config'])},rows,[r['qid'] for r in rows],total_questions=20)
        for rows in groups.values()]
    primary=adjudicate_round([decisions[0]])
    result={'schema':'rsi-query-diagnostic/v1','status':'PRECHECK_COMPLETE','formal_admission':False,
        'candidate_id':candidate_id,'parent_id':frame['parent_id'],'suite_decisions':decisions,
        'primary_screen':primary,'recommend_ordinary_replay':bool(primary['adopted_candidate_id']) and all(
            all(d['gates']['G'+str(i)] is True for i in range(1,5)) for d in decisions),
        'capture_sha256':bytes_hash((output/'capture.json').read_bytes()),'policy_worker_sha256':bytes_hash(worker),
        'trusted_wrapper_worker_sha256':bytes_hash(wrapper),'fixture_questions':20,'auxiliary_cases':60,
        'independent_holdout':False,'production_changed':False}
    atomic_write(output/'diagnostic.json',canonical(result),exclusive=True)
    return result

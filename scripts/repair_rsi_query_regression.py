"""Engineering safety repair from an actual failing parent; never optimization admission."""
import argparse
import json
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rsik.experiment import Experiment, registry_api, executor, load, bytes_hash
from rsik.foundation import check_split, verify_boundary
from rsik.handbook.store import Handbook
from rsik.lineage import ParentPool
from rsik.locking import WorkspaceLock, atomic_write
from rsik.state import canonical
from run_rsi_loop import selector


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('registry', 'fixture', 'source-run', 'directory', 'business-root', 'identity'):
        p.add_argument('--'+name, required=True, type=Path)
    for name in ('parent-id', 'host'):
        p.add_argument('--'+name, required=True)
    a = p.parse_args()
    verify_boundary(); check_split(a.fixture, expected_count=20)
    if a.directory.exists(): raise ValueError('FRESH_REPAIR_DIRECTORY_REQUIRED')
    source_receipt = a.source_run.parent/'receipt.json'
    receipt = load(source_receipt)
    if (receipt['candidate_id'] != a.parent_id or receipt['passed']
            or receipt['candidate_fact_loss_questions'] <= 0):
        raise ValueError('CONFIRMED_PARENT_LOSS_REQUIRED')
    feedback = load(a.source_run/'feedback.json')
    observations = [o for o in feedback['observations'] if o['category']=='required_fact_absent']
    if not observations: raise ValueError('FACT_LOSS_OBSERVATION_REQUIRED')
    obs = observations[0]['observation_id']
    with WorkspaceLock(a.registry/'writer.lock', 'engineering-safety-repair'):
        ParentPool(a.registry/'policy').quarantine(a.parent_id, source_receipt, 'CONFIRMED_REAL_QUERY_FACT_LOSS')
        book = Handbook(a.registry/'handbook')
        evidence_hash = bytes_hash(source_receipt.read_bytes())
        for event in book.log.read():
            if event['event_type']=='ENTRY_ACTIVATED' and a.parent_id in event['payload'].get('verified_candidate_ids', []):
                entry = event['payload']['entry_id']
                book.log.append('query-regression:'+entry, 'ENTRY_QUARANTINED',
                    {'entry_id':entry,'candidate_id':a.parent_id,'reason':'CONFIRMED_REAL_QUERY_FACT_LOSS','receipt_hash':evidence_hash})
        book.log.append('query-regression-outcome:'+a.parent_id, 'OUTCOME_INVALIDATED',
            {'candidate_id':a.parent_id,'reason':'CONFIRMED_REAL_QUERY_FACT_LOSS','receipt_hash':evidence_hash})
        parent = registry_api.candidate(a.registry,a.parent_id)
        original = registry_api.get(a.registry,parent['worker'])
        old = selector(original.decode())[1]
        new = ('def select_member_indices(members, chunks, files, seed_ids, selection):\n'
               "    require(selection in ('all', 'exact_duplicate'), 'invalid selection')\n"
               '    if not seed_ids:\n        return []\n'
               '    return list(range(len(members)))')
        proposal = {'angle':'P2_BUDGET','hypothesis':'Restore neighbors omitted by the query-blind length threshold.',
            'change':'Conservative restoration of all baseline members when seeds exist.',
            'falsification':'Any candidate fact loss, missing provenance or seed/order violation rejects the repair.',
            'expected_impact':'Restore safety with higher context size; no optimization gain claimed.',
            'worker_patch':{'old':old,'new':new}}
        experiment = Experiment(registry=a.registry,fixture=a.fixture,parent_id=a.parent_id,
            source_run=a.source_run,observation_id=obs,directory=a.directory,business_root=a.business_root,
            host=a.host,identity=a.identity,round_id='repair-'+uuid.uuid4().hex)
        experiment.parent_bytes = original
        atomic_write(a.directory/'proposal.json',canonical(proposal),exclusive=True)
        try:
            artifact = experiment.review(proposal)
            config = a.directory/'config.json'
            atomic_write(config,registry_api.get(a.registry,parent['config']),exclusive=True)
            notes = {'observation':'Confirmed missing source-bound facts on real alternate reviewed questions.',
                'hypothesis':proposal['hypothesis'],'change':proposal['change'],'impact':proposal['expected_impact'],
                'falsification':proposal['falsification']}
            child = registry_api.derive(a.registry,a.parent_id,config,Path(artifact['worker_path']),notes,a.source_run,obs)
            atomic_write(a.directory/'candidate.json',canonical({'candidate_id':child,'parent_id':a.parent_id,
                'scope':'ENGINEERING_SAFETY_REPAIR','model_calls':0}),exclusive=True)
            manifest = registry_api.materialize(a.registry,a.parent_id,child,a.directory/'bundle',
                cases_path=a.fixture/'cases.json',evaluation_path=a.fixture/'evaluation.json')
            experiment.capsule.deadline = time.monotonic()+420
            with experiment.capsule.bind(executor):
                executor.run(manifest,a.directory/'run',business_root=a.business_root)
            results = load(a.directory/'run/results.json')
            summary = {'scope':'ENGINEERING_SAFETY_REPAIR','candidate_id':child,'parent_id':a.parent_id,
                'total':len(results['rows']),'paired':sum(r['status']=='PAIRED' for r in results['rows']),
                'original_summary':results['summary'],'optimization_admitted':False,'production_changed':False,
                'model_calls':0,'results_sha256':bytes_hash((a.directory/'run/results.json').read_bytes())}
            atomic_write(a.directory/'repair-summary.json',canonical(summary),exclusive=True)
            print(json.dumps(summary))
        finally:
            experiment.capsule.close()
    verify_boundary()


if __name__=='__main__': main()

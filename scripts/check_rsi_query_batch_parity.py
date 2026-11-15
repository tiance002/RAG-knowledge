"""Compare actual batched business captures with archived ordinary captures."""
import argparse,json,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.query_batch import diagnostic_worker
from rsik.query_aware import QueryRegistry
from rsik.experiment import load,bytes_hash,executor
from rsik.capsule import Capsule
from rsik.state import canonical
from rsik.locking import atomic_write,restrict_directory


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('registry','ordinary-round','output','identity','business-root'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--host',required=True);a=p.parse_args()
    previous=load(a.ordinary_round/'round.json');candidate=QueryRegistry(a.registry).get(previous['candidate_id'])
    entries=[];expected={}
    for suite in range(1,5):
        run=a.ordinary_round/('suite-'+str(suite));config=load(run/'B.json')
        if load(run/'manifest.json')['arms']['B']['candidate_id']!=candidate['candidate_id']:raise ValueError('ORDINARY_CANDIDATE_CHANGED')
        for index in range(1,21):
            case=run/f'case-{index:03}';key=f'{suite}-{index}'
            snapshot=load(case/'arm-input.json');capture=load(case/'B/capture.json')
            if executor.fingerprint(snapshot)!=capture['upstream_sha256']:raise ValueError('ORDINARY_SNAPSHOT_CHANGED')
            entries.append({'key':key,'snapshot':snapshot,'config':config});expected[key]=capture
    a.output.mkdir(parents=True,exist_ok=False);restrict_directory(a.output)
    worker=diagnostic_worker(candidate['worker']);atomic_write(a.output/'worker.py',worker,exclusive=True)
    atomic_write(a.output/'input.json',canonical({'entries':entries}),exclusive=True)
    capsule=Capsule(a.host,a.identity,a.business_root,a.output/'capsule')
    try:
        capsule.setup();capsule.deadline=time.monotonic()+90;started=time.monotonic()
        result=capsule.child('prepare',[a.output/'input.json',a.output/'capture.json'],a.output,worker=a.output/'worker.py',business_root=a.business_root)
        observed={row['key']:row['capture'] for row in result['captures']}
        mismatches=[key for key in expected if observed.get(key)!=expected[key]]
        receipt={'scope':'diagnostic business replay parity only; not candidate admission',
            'candidate_id':candidate['candidate_id'],'expected':len(expected),'observed':len(observed),
            'mismatches':mismatches,'status':'PASS' if not mismatches and set(expected)==set(observed) else 'FAIL',
            'elapsed_seconds':time.monotonic()-started,'model_calls':0,'new_source_or_index_copies':0,
            'policy_worker_sha256':bytes_hash(candidate['worker']),'trusted_wrapper_worker_sha256':bytes_hash(worker),
            'input_sha256':bytes_hash((a.output/'input.json').read_bytes()),'capture_sha256':bytes_hash((a.output/'capture.json').read_bytes())}
        atomic_write(a.output/'receipt.json',canonical(receipt),exclusive=True);print(json.dumps(receipt))
        if receipt['status']!='PASS':raise ValueError('BATCH_PARITY_FAILED')
    finally:capsule.close()


if __name__=='__main__':main()

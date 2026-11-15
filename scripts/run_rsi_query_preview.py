"""Cheap proposal and actual batch diagnosis; all admissions require ordinary replay."""
import argparse,json,sys,time,uuid
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.query_preview import frames,replay
from rsik.query_aware import apply_patch,interface_contract
from rsik.selector_contract import INTENTS
from rsik.query_aware import invariant_probe_source
from rsik.experiment import load,bytes_hash
from rsik.foundation import check_split,verify_boundary
from rsik.capsule import Capsule
from rsik.contracts import unwrap_response
from rsik.openai_roles import OpenAIRoles
from rsik.budgets import Budget
from rsik.redact import LeakScanner,make_visible,project_summary
from rsik.locking import atomic_write,restrict_directory
from rsik.state import canonical,digest
from run_rsi_loop import selector


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('ordinary-round','directory','primary','query-stress','next-stress','previous-stress','identity','business-root','key-file','budget-ledger'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--arm',choices=('A','B'),default='A');p.add_argument('--host',required=True)
    p.add_argument('--intent',choices=sorted(i for i in INTENTS if i.startswith('QUERY_')),required=True)
    a=p.parse_args();verify_boundary()
    if a.directory.exists():raise ValueError('FRESH_PREVIEW_DIRECTORY_REQUIRED')
    fixtures=[a.primary,a.query_stress,a.next_stress,a.previous_stress]
    frame=frames(a.ordinary_round,fixtures,a.arm)
    a.directory.mkdir(parents=True);restrict_directory(a.directory)
    atomic_write(a.directory/'baseline.json',canonical({'parent_id':frame['parent_id'],
        'ordinary_round':str(a.ordinary_round.resolve()),'ordinary_round_sha256':bytes_hash((a.ordinary_round/'round.json').read_bytes()),
        'arm':a.arm,'parent_worker_sha256':bytes_hash(frame['worker']),'parent_config_sha256':bytes_hash(frame['config'])}),exclusive=True)
    scanner=LeakScanner([frame['parent_id']]+[str(f.resolve()) for f in fixtures])
    public=make_visible([project_summary('REPLAY_SUMMARY',{'questions':20,'source_groups':20,'paired':20,'failed':0},include_count_evidence=True)],
        parent_code=selector(frame['worker'].decode())[1],round_id='round-'+uuid.uuid4().hex,scanner=scanner)
    public.update(selector_contract=interface_contract(),experiment_intent=a.intent,parent_selection=load(a.ordinary_round/'suite-1'/f'{a.arm}.json')['selection'])
    atomic_write(a.directory/'public-context.json',canonical(public),exclusive=True)
    freezes={str(f.resolve()):check_split(f,expected_count=20) for f in fixtures}
    atomic_write(a.directory/'fixture-freezes.restricted.json',canonical(freezes),exclusive=True)
    budget=Budget(a.budget_ledger);roles=OpenAIRoles(budget,a.directory/'roles',a.key_file,scanner,seam_profile='query-aware-v1')
    diagnosis_raw=roles.diagnose(public);atomic_write(a.directory/'diagnosis.json',canonical(diagnosis_raw),exclusive=True)
    diagnosis=unwrap_response(diagnosis_raw)
    if diagnosis is None:
        atomic_write(a.directory/'preview.json',canonical({'status':'ABSTAIN','formal_admission':False,'budget':budget.snapshot()}),exclusive=True);return
    raw=roles.propose('P2_BUDGET',public,diagnosis);atomic_write(a.directory/'proposal.json',canonical(raw),exclusive=True)
    proposal=unwrap_response(raw)
    if proposal is None:
        atomic_write(a.directory/'preview.json',canonical({'status':'ABSTAIN','formal_admission':False,'budget':budget.snapshot()}),exclusive=True);return
    try:worker=apply_patch(frame['worker'],proposal['worker_patch'])
    except (ValueError,SyntaxError):
        atomic_write(a.directory/'preview.json',canonical({'status':'PRECHECK_REJECTED','reason':'AST_PATCH_INVALID','formal_admission':False,'budget':budget.snapshot()}),exclusive=True);return
    atomic_write(a.directory/'policy.py',worker,exclusive=True)
    candidate_id=digest({'parent_id':frame['parent_id'],'worker_sha256':bytes_hash(worker),'proposal_sha256':bytes_hash((a.directory/'proposal.json').read_bytes())})
    capsule=Capsule(a.host,a.identity,a.business_root,a.directory/'capsule')
    try:
        capsule.setup();result=replay(capsule,frame,worker,a.directory/'replay',candidate_id)
        result.update(budget=budget.snapshot(),scope='diagnostic only; no parent pool or production admission')
        atomic_write(a.directory/'preview.json',canonical(result),exclusive=True)
        print(json.dumps({'status':result['status'],'reason':result.get('reason'),'recommend_ordinary_replay':result.get('recommend_ordinary_replay'),
            'primary_delta':result.get('suite_decisions',[{}])[0].get('delta_total'),'budget':budget.snapshot()}))
    finally:capsule.close()
    verify_boundary()


if __name__=='__main__':main()

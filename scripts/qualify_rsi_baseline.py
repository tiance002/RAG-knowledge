"""Qualify an explicitly designated root by isolated A/A replay, never ADOPT."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.baseline_qualification import qualify_baseline
from rsik.capsule import Capsule
from rsik.handbook.store import Handbook
from rsik.query_learning import ExperienceSession
from rsik.launch import LaunchContext,validate_launch


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('registry','business-root','identity','output','budget-ledger','handbook','learning-session','launch-plan'):
        p.add_argument('--'+name,required=True,type=Path)
    for name in ('candidate-id','host','run-id','launch-plan-sha256'):
        p.add_argument('--'+name,required=True)
    p.add_argument('--fixtures',nargs=4,required=True,type=Path)
    p.add_argument('--origin',choices=('ENGINEER_GUIDED','MIGRATED','OWNER_SPECIFIED'),required=True)
    a=p.parse_args()
    session=ExperienceSession(a.learning_session,Handbook(a.handbook))
    context=LaunchContext(str(a.launch_plan),a.launch_plan_sha256,a.run_id,'BASELINE_QUALIFICATION',
        str(a.budget_ledger),str(a.registry),str(a.business_root),session)
    validate_launch(context,fixtures=a.fixtures)
    capsule=Capsule(a.host,a.identity,a.business_root,a.output.parent/(a.output.name+'-capsule'))
    result=qualify_baseline(registry=a.registry,candidate_id=a.candidate_id,fixtures=a.fixtures,capsule=capsule,
        output=a.output,origin=a.origin,launch=context)
    print(json.dumps({'status':result['status'],'candidate_id':result['candidate_id'],
        'improvement_claim':False,'production_changed':False}))


if __name__=='__main__':main()

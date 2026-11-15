"""One-use fresh-source bundle; unchanged assessor, normal statistics + stress gates."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.query_verification import verify_sealed
from rsik.capsule import Capsule


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('registry','training','primary','query-stress','next-stress','previous-stress','sealed','sealed-next','sealed-previous','used-holdout','identity','business-root','output'):
        p.add_argument('--'+name,required=True,type=Path)
    for name in ('candidate-id','trusted-id','host'):p.add_argument('--'+name,required=True)
    p.add_argument('--handbook',type=Path)
    p.add_argument('--handbook-fingerprint')
    p.add_argument('--learning-session',type=Path)
    p.add_argument('--key-file',type=Path)
    p.add_argument('--budget-ledger',type=Path)
    a=p.parse_args()
    handbook=advisory=session=None
    if a.handbook:
        if not all((a.handbook_fingerprint,a.learning_session,a.key_file,a.budget_ledger)):
            p.error('verified learning requires --handbook-fingerprint, --learning-session, --key-file and original --budget-ledger')
        from rsik.query_workflow import bind_budget_path
        from rsik.handbook.store import Handbook
        from rsik.query_learning import ExperienceSession
        from rsik.budgets import Budget
        from rsik.openai_roles import OpenAIRoles
        from rsik.redact import LeakScanner
        bind_budget_path(a.registry,a.budget_ledger)
        handbook=Handbook(a.handbook)
        session=ExperienceSession(a.learning_session,handbook)
        advisory=OpenAIRoles(Budget(a.budget_ledger),a.output.parent/(a.output.name+'-advisory'),a.key_file,
            LeakScanner(),before_request=session.verify,strategy_context=session.verify)
    elif any((a.handbook_fingerprint,a.learning_session,a.key_file,a.budget_ledger)):
        p.error('learning configuration requires --handbook')
    capsule=Capsule(a.host,a.identity,a.business_root,a.output.parent/(a.output.name+'-capsule'))
    result=verify_sealed(registry=a.registry,training=a.training,train_fixtures=[a.primary,a.query_stress,a.next_stress,a.previous_stress],
        sealed_fixtures=[a.sealed,a.sealed_next,a.sealed_previous],used_holdout=a.used_holdout,
        candidate_id=a.candidate_id,trusted_id=a.trusted_id,capsule=capsule,output=a.output,
        handbook=handbook,advisory_roles=advisory,compatibility_fingerprint=a.handbook_fingerprint,experience_session=session)
    print(json.dumps({'verdict':result['verdict'],'statistical_denominator':result['statistical_denominator'],
        'auxiliary_cases':result['auxiliary_cases'],'auxiliary_pass':result['auxiliary_pass'],'production_changed':False}))


if __name__=='__main__':main()

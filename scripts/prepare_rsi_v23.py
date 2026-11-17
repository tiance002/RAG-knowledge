"""Create a frozen S4 preparation manifest; never invoke models or containers."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from rsik.experiment_config import prepare
from rsik.locking import atomic_write
from rsik.state import canonical


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('registry','training','verification','handbook','budget-ledger','output'):
        p.add_argument('--'+name,type=Path,required=True)
    p.add_argument('--fixtures',type=Path,nargs=4,required=True)
    p.add_argument('--s3-readiness',type=Path)
    a=p.parse_args()
    value=prepare(registry=a.registry,training=a.training,verification=a.verification,
        fixtures=a.fixtures,handbook=a.handbook,budget_ledger=a.budget_ledger,s3_readiness=a.s3_readiness)
    atomic_write(a.output,canonical(value),exclusive=True)
    print(json.dumps({'plan_id':value['plan_id'],'ready_for_real_run':value['ready_for_real_run'],
                      'blocking_prerequisites':value['blocking_prerequisites'],'models_called':0}))


if __name__=='__main__': main()

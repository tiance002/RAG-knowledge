"""Recover one declared learning transaction without sending a model request."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from rsik.handbook.store import Handbook
from rsik.query_learning import ExperienceSession


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--session',type=Path,required=True)
    p.add_argument('--handbook',type=Path,required=True)
    p.add_argument('--evidence-mode',choices=('REAL_REPLAY','SYNTHETIC_CONTROL'),default='REAL_REPLAY')
    a=p.parse_args()
    session=ExperienceSession(a.session,Handbook(a.handbook,evidence_mode=a.evidence_mode),recovery=True)
    result=session.recover()
    print(json.dumps({'status':'RECOVERED','new_model_requests':0,'result':result}))


if __name__=='__main__':main()

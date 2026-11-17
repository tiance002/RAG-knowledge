"""Offline control preparation and explicitly admitted real coverage collection."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rsik.coverage_collection import prepare_collection, collect


def main():
    p=argparse.ArgumentParser()
    p.add_argument('mode',choices=('prepare','collect'))
    p.add_argument('--registry',required=True);p.add_argument('--candidate-id',required=True)
    p.add_argument('--fixture',action='append',required=True);p.add_argument('--output',required=True)
    p.add_argument('--launch-context');p.add_argument('--host');p.add_argument('--identity')
    a=p.parse_args()
    kwargs={'registry':a.registry,'candidate_id':a.candidate_id,'fixtures':a.fixture,'output':a.output}
    if a.mode=='prepare':
        result=prepare_collection(**kwargs)
        print(json.dumps({'status':'PREPARED_NOT_EXECUTED','tasks':len(result['tasks'])}))
    else:
        if not all((a.launch_context,a.host,a.identity)):
            p.error('collect requires pinned --launch-context, --host and --identity')
        from rsik.launch import LaunchContext
        from rsik.capsule import Capsule
        from rsik.handbook.store import Handbook
        from rsik.query_learning import ExperienceSession
        config=json.loads(Path(a.launch_context).read_text(encoding='utf-8-sig'))
        config['session']=ExperienceSession(config.pop('learning_session'),Handbook(config.pop('handbook')))
        context=LaunchContext(**config)
        capsule=Capsule(a.host,a.identity,context.business_root,Path(a.output).parent/(Path(a.output).name+'-capsule'))
        result=collect(**kwargs,capsule=capsule,launch=context)
        print(json.dumps({'status':result['status'],'controls':len(result['controls'])}))


if __name__=='__main__':main()

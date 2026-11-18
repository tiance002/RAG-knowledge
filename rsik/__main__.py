"""Run one isolated, fully recorded cost-first RSI training round."""
import argparse
import json
from pathlib import Path
import uuid

from .adjudicate import SLOTS
from .budgets import Budget
from .experiment import Experiment
from .openai_roles import OpenAIRoles
from .selector_contract import INTENTS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('registry', 'fixture', 'source-run', 'directory', 'business-root', 'identity', 'key-file', 'budget-ledger'):
        parser.add_argument('--'+name, required=True, type=Path)
    for name in ('parent-id', 'observation-id', 'host'):
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--profile', choices=('single', 'parallel'), default='single')
    parser.add_argument('--dedup-model', choices=('gpt-6-luna','gpt-6.1-sol','gpt-6.2-sol'), default='gpt-6-luna')
    parser.add_argument('--budget-model', choices=('gpt-6-luna','gpt-6.1-sol','gpt-6.2-sol'), default='gpt-6-luna')
    parser.add_argument('--single-angle',choices=SLOTS,default='P1_DEDUP')
    parser.add_argument('--experiment-intent',choices=sorted(INTENTS),default='STRUCTURAL_POLICY_SEARCH')
    parser.add_argument('--robustness-fixture',type=Path,help='frozen same-source stress hard gate before training adoption')
    parser.add_argument('--resume-completed', action='store_true', help='revalidate a completed round without model/worker calls')
    args = parser.parse_args()
    if args.directory.exists() and not args.resume_completed:
        parser.error('fresh round directory required; inspect retained stages for recovery, never blindly restart calls')
    if args.resume_completed:
        if not (args.directory/'round.json').is_file():parser.error('round is incomplete; unknown side effects require reconciliation, not automatic retries')
        round_id=json.loads((args.directory/'public-context.json').read_text(encoding='utf-8'))['round_id']
    else:
        round_id='round-'+uuid.uuid4().hex
    experiment = Experiment(registry=args.registry, fixture=args.fixture, parent_id=args.parent_id,
        source_run=args.source_run, observation_id=args.observation_id, directory=args.directory,
        business_root=args.business_root, host=args.host, identity=args.identity,
        round_id=round_id,experiment_intent=args.experiment_intent,robustness_fixture=args.robustness_fixture)
    public = experiment.public_context()
    budget = Budget(args.budget_ledger)
    roles = OpenAIRoles(budget, args.directory/'roles', args.key_file, experiment.scanner,
                        models={'P1_DEDUP':args.dedup_model,'P2_BUDGET':args.budget_model})
    result = experiment.run(roles, enabled_slots=(args.single_angle,) if args.profile == 'single' else SLOTS,resume=args.resume_completed)
    print(json.dumps({'execution_state': result['execution_state'], 'verdict': result['verdict'],
        'stop_reason': result.get('stop_reason'), 'qualified': result['qualified'],
        'replayed': result['attempted_replays'], 'budget': budget.snapshot(),
        'production_changed': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()

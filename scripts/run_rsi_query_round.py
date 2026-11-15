"""Run a query-profile K1/K3 round with all six role positions and original ledger."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from rsik.budgets import Budget
from rsik.capsule import Capsule
from rsik.foundation import verify_boundary
from rsik.locking import WorkspaceLock
from rsik.openai_roles import OpenAIRoles
from rsik.query_aware import QueryRegistry, autonomy_interface_contract
from rsik.query_preview import frames
from rsik.query_verification import verify_training
from rsik.query_workflow import run_query_round, resume_query_round, bind_budget_path
from rsik.adjudicate import SLOTS
from rsik.role_profiles import LEGACY, DEPENDENCY, profile_manifest
from rsik.query_learning import ExperienceSession
from rsik.redact import LeakScanner, make_visible, project_summary
from run_rsi_loop import selector


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('query-registry', 'directory', 'baseline-round', 'primary', 'query-stress',
                 'next-stress', 'previous-stress', 'identity', 'business-root', 'key-file', 'budget-ledger'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--launch-plan',type=Path,required=True)
    parser.add_argument('--launch-plan-sha256',required=True)
    parser.add_argument('--run-id',required=True)
    parser.add_argument('--baseline-qualification',type=Path)
    parser.add_argument('--parent-id', required=True)
    parser.add_argument('--host', required=True)
    parser.add_argument('--role-profile',choices=(LEGACY,DEPENDENCY),default=DEPENDENCY)
    parser.add_argument('--learning-session',type=Path)
    parser.add_argument('--profile', choices=('single', 'parallel'), default='parallel')
    parser.add_argument('--single-angle', choices=SLOTS, default='P2_BUDGET')
    parser.add_argument('--resume-completed', action='store_true')
    parser.add_argument('--handbook', type=Path)
    parser.add_argument('--handbook-fingerprint')
    parser.add_argument('--handbook-format', choices=('PDF', 'DOCX', 'XLSX', 'OTHER'), default='OTHER')
    parser.add_argument('--handbook-features', type=json.loads, default={})
    args = parser.parse_args()
    verify_boundary()
    fixtures = [args.primary, args.query_stress, args.next_stress, args.previous_stress]
    registry = QueryRegistry(args.query_registry)
    bind_budget_path(args.query_registry, args.budget_ledger)
    if args.resume_completed:
        result = resume_query_round(directory=args.directory, registry=registry, fixtures=fixtures)
        print(json.dumps({'verdict': result['verdict'], 'completed_recovery': True, 'new_model_calls': 0, 'new_capsules': 0}))
        return
    with WorkspaceLock(args.query_registry / 'writer.lock', 'query-multi-role-round'):
        from rsik.launch import LaunchContext,validate_launch
        from rsik.handbook.store import Handbook
        if not args.handbook or not args.learning_session:raise ValueError('LAUNCH_MEMORY_SESSION_REQUIRED')
        handbook=Handbook(args.handbook)
        session=ExperienceSession(args.learning_session,handbook)
        context=LaunchContext(str(args.launch_plan),args.launch_plan_sha256,args.run_id,'QUERY_ROUND',str(args.budget_ledger),str(args.query_registry),str(args.business_root),session)
        validate_launch(context,fixtures=fixtures)
        if args.baseline_qualification:
            from rsik.baseline_qualification import verify_baseline_qualification
            from rsik.execution_evidence import ExecutionEvidence
            parent,_=verify_baseline_qualification(registry=args.query_registry,receipt=args.baseline_qualification,
                fixtures=fixtures,evidence=ExecutionEvidence.real(args.business_root))
        else:parent, _ = verify_training(args.query_registry, args.baseline_round, fixtures, args.parent_id)
        frame = frames(args.baseline_round, fixtures, arm='B')
        if frame['parent_id'] != parent['candidate_id'] or frame['worker'] != parent['worker'] or frame['config'] != parent['config']:
            raise ValueError('QUERY_PARENT_FRAME_CHANGED')
        scanner = LeakScanner([args.parent_id] + [str(f.resolve()) for f in fixtures])
        primary = [b for b in frame['baseline'].values() if b['suite'] == 1]
        counts = {'questions': 20, 'source_groups': 20, 'paired': 20, 'failed': 0,
                  'context_chars': sum(len(b['capture']['context']) for b in primary),
                  'delivered_members': sum(len(b['capture']['members']) for b in primary),
                  'seed_members': sum(len(b['case']['seed_ids']) for b in primary)}
        counts['nonseed_members'] = counts['delivered_members'] - counts['seed_members']
        public = make_visible([project_summary('REPLAY_SUMMARY', counts, include_count_evidence=True)],
                              parent_code=selector(parent['worker'].decode())[1], scanner=scanner)
        public.update(selector_contract=autonomy_interface_contract(), experiment_intent='STRUCTURAL_POLICY_SEARCH',
                      parent_selection=json.loads(parent['config'])['selection'])
        budget = Budget(args.budget_ledger)
        roles = OpenAIRoles(budget, args.directory.parent / (args.directory.name + '-roles'), args.key_file,
                            scanner, seam_profile='query-autonomous-v1',role_profile=args.role_profile,launch=context)
        advisory = OpenAIRoles(budget, args.directory.parent / (args.directory.name + '-advisory-roles'), args.key_file, scanner,launch=context)
        handbook.scanner=scanner
        if session:
            roles.before_request=session.verify
            roles.strategy_context=session.verify
            advisory.before_request=session.verify
            advisory.strategy_context=session.verify
        result = run_query_round(public=public, roles=roles, advisory_roles=advisory, parent=parent,
            fixtures=fixtures, registry=registry, directory=args.directory, scanner=scanner,
            capsule_factory=lambda output: Capsule(args.host, args.identity, args.business_root, output),
            enabled_slots=SLOTS if args.profile == 'parallel' else (args.single_angle,),
            handbook=handbook, compatibility_fingerprint=args.handbook_fingerprint,
            format=args.handbook_format, features=args.handbook_features,role_profile=args.role_profile,experience_session=session,launch=context)
        print(json.dumps({'verdict': result['verdict'], 'adopted_candidate_id': result.get('adopted_candidate_id'),
                          'budget': budget.snapshot(), 'production_changed': False}))
    verify_boundary()


if __name__ == '__main__':
    main()

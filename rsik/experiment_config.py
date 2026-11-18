"""Offline preparation only: freeze identities without starting paid experiments."""
from pathlib import Path
from .experiment import bytes_hash, load
from .foundation import verify_boundary, check_split
from .query_workflow import implementation_hashes
from .role_profiles import DEPENDENCY, profile_manifest
from .state import digest


METRICS = ['actual_requests', 'cost_known_or_upper_bound', 'contract_validity',
    'static_pass_rate', 'normalized_ast_strategy_count', 'repeat_proposal_rate',
    'real_replay_completion', 'fact_regression', 'NO_GAIN', 'OBSERVED',
    'INCONCLUSIVE', 'infrastructure_failures', 'training_adoptions',
    'independent_verifications', 'cost_per_verified_improvement']


def prepare(*, registry, training, verification, fixtures, handbook, budget_ledger,
            s3_readiness=None):
    """Output may be frozen as a plan while readiness remains explicitly false.

    v2.2 receipts do not become v2.3 evidence by renaming them. A new real
    execution qualification and S3 source-family/exposure freeze are prerequisites.
    """
    protected = verify_boundary()
    budget = Path(budget_ledger).resolve()
    expected = Path(registry).resolve().parent.parent / 'task-model-budget.sqlite'
    if budget != expected or not budget.is_file():
        raise ValueError('ORIGINAL_TASK_BUDGET_LEDGER_REQUIRED')
    freezes = {str(Path(f).resolve()): check_split(f, expected_count=20) for f in fixtures}
    if len(fixtures) != 4:
        raise ValueError('FOUR_QUERY_SUITES_REQUIRED')
    receipt = load(verification)
    readiness = load(s3_readiness) if s3_readiness else None
    missing = []
    if not readiness or readiness.get('schema') != 'rsi-s3-readiness/v1' or readiness.get('approved') is not True:
        missing.append('S3_SOURCE_FAMILY_FACT_COVERAGE_EXPOSURE_FREEZE_REQUIRED')
    if (receipt.get('schema') != 'rsi-query-aware-sealed/v2.3' or receipt.get('verdict') != 'VERIFIED'
            or receipt.get('real_eligible') is not True or not (Path(training)/'family-decision.json').is_file()):
        missing.append('NEW_V23_REAL_EXECUTION_AND_FAMILY_QUALIFICATION_REQUIRED')
    memory = Path(handbook)
    # Read a pre-existing frozen memory snapshot; preparation never creates or
    # migrates the current production/legacy handbook or its database.
    initial = memory/'initial-memory.json'
    if not initial.is_file():
        missing.append('INITIAL_MEMORY_AND_EVENT_FRONTIER_FREEZE_REQUIRED')
    body = {'schema': 'rsi-v23-experiment-plan/v1', 'phase': 'S4_PREPARATION_ONLY',
        'ready_for_real_run': not missing, 'blocking_prerequisites': missing,
        'models_called': 0, 'production_changed': False, 'system_self_modification': False,
        'implementation': implementation_hashes(), 'protected': protected,
        'role_profile': profile_manifest(DEPENDENCY), 'seam': 'rsi-query-autonomous-selector/v1',
        'fixtures': freezes, 'evaluator_policy': 'unchanged frozen evaluator; fixed-holm3/v1 alpha=0.1',
        'parent': {'candidate_id': receipt['candidate_id'], 'training': str(Path(training).resolve()),
                   'verification': str(Path(verification).resolve()),
                   'verification_sha256': bytes_hash(Path(verification).read_bytes()),
                   'historical_origin': 'ENGINEER_GUIDED; no autonomous credit'},
        'memory_initial_sha256': bytes_hash(initial.read_bytes()) if initial.is_file() else None,
        's3_readiness_sha256': bytes_hash(Path(s3_readiness).read_bytes()) if s3_readiness else None,
        'budget': {'ledger': str(budget), 'ledger_sha256_at_preparation': bytes_hash(budget.read_bytes()),
            'cumulative_limit_usd': '5', 'reset': False, 'reserve': '0',
            'call_or_round_cap': None, 'last_single_request_may_cross_limit': True,
            'model_policy': {'default': 'gpt-6-luna', 'coding_failure_escalation': 'gpt-6.2-sol', 'astra': False},
            'include': ['Diagnosis','P1','P2','P3','Curator','Meta','format_repairs','failures','UNKNOWN']},
        'design': {'main': 'parallel/K3', 'comparison': 'matched K1 and frozen-old-role control',
            'planned_independent_repetitions_per_group': 3, 'no_posttrial_memory_sharing': True,
            'same_initial_parent_visible_evidence_models_and_actual_total_cost_budget': True,
            'confirmatory_holdout': 'candidate/search-policy frozen before one-use independent verification',
            'analysis_points': 'predeclare in S3 readiness; do not infer sequential validity from Holm3',
            'engineering_ablation': 'separate feedback/learning-bridge/P3 version; no causal credit without ablation'},
        'metrics': METRICS, 'zero_verified_cost_metric': 'N/A; retain actual total spend',
        'answer_quality': {'status':'NOT_RUN', 'required': ['paired parent/candidate generation',
            'fixed QA model/prompt/retrieval/decoding/concurrency', 'balanced repeated ordering',
            'usage and cost', 'correctness/omission/unsupported statements/citation support',
            'empty/truncated outputs', 'first-token/total latency', 'UNKNOWN kept unresolved']},
        'authorization': 'preparation is not launch; this S0-S2 task starts no S4 experiment'}
    return {**body, 'plan_id': digest(body)}

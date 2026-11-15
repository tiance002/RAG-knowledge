"""Freeze a validated RSI version, then run model-selected hypotheses until a real stop."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from rsik.autonomous import run
from rsik.budgets import Budget
from rsik.experiment import load,bytes_hash
from rsik.foundation import PROTECTED,verify_boundary
from rsik.locking import atomic_write,restrict_directory
from rsik.state import canonical,digest
from rsik.test_phase import freeze


def main():
    p=argparse.ArgumentParser(description=__doc__)
    for name in ('legacy-registry','query-registry','baseline-round','baseline-verification','primary','query-stress','next-stress','previous-stress',
                 'identity','business-root','key-file','budget-ledger','repair-receipt','directory','validation-audit'):
        p.add_argument('--'+name,required=True,type=Path)
    p.add_argument('--repair-id',required=True);p.add_argument('--host',required=True)
    p.add_argument('--profile',choices=('single','parallel'),default='parallel')
    p.add_argument('--role-profile',choices=('legacy/v1','query-k3-dependency/v1'),default='query-k3-dependency/v1')
    p.add_argument('--handbook',type=Path)
    p.add_argument('--handbook-fingerprint')
    p.add_argument('--handbook-format',choices=('PDF','DOCX','XLSX','OTHER'),default='OTHER')
    p.add_argument('--handbook-features',default='{}',help='JSON object of finite structured handbook features')
    a=p.parse_args();verify_boundary()
    features=json.loads(a.handbook_features)
    from rsik.handbook.contracts import DOMAINS
    if not isinstance(features,dict) or any(k not in DOMAINS or v not in DOMAINS[k] for k,v in features.items()):
        raise ValueError('INVALID_HANDBOOK_FEATURES')
    if a.budget_ledger.resolve()!=(a.legacy_registry.parent/'task-model-budget.sqlite').resolve():
        raise ValueError('ORIGINAL_CUMULATIVE_BUDGET_LEDGER_REQUIRED')
    if a.directory.exists():raise ValueError('FRESH_AUTONOMY_TEST_DIRECTORY_REQUIRED; inspect existing state, never blindly restart')
    validation=load(a.validation_audit)
    if bytes_hash(Path(validation['log_path']).read_bytes())!=validation['log_sha256']:raise ValueError('VALIDATION_LOG_CHANGED')
    baseline=load(a.baseline_verification)
    if baseline['verdict']!='VERIFIED':raise ValueError('VERIFIED_BASELINE_REQUIRED')
    a.directory.mkdir(parents=True);restrict_directory(a.directory)
    fixtures=[a.primary,a.query_stress,a.next_stress,a.previous_stress]
    from rsik.query_workflow import implementation_hashes
    code=[ROOT/name for name in implementation_hashes()]+[ROOT/name for name in PROTECTED]
    code.extend(ROOT/'scripts'/name for name in ('run_rsi_loop.py','run_rsi_query_experiment.py','run_rsi_query_round.py','run_rsi_autonomous.py'))
    # Pin inputs and baseline evidence too; generated future policies remain outside this set.
    fixed=[p for f in fixtures for p in f.rglob('*') if p.is_file()]
    fixed.extend(p for p in a.baseline_round.rglob('*') if p.is_file())
    fixed.extend([a.baseline_verification,a.validation_audit])
    from rsik.handbook.store import Handbook
    from rsik.query_learning import ExperienceSession
    learning=ExperienceSession(a.directory/'learning-session',Handbook(a.handbook or a.legacy_registry/'handbook'))
    fixed.append(learning.initial)
    strategy={'profile':a.profile,'role_profile':a.role_profile,'handbook_format':a.handbook_format,
        'handbook_features':features,'handbook_fingerprint_override':a.handbook_fingerprint,
        'models':{'default':'gpt-6-luna','coding_failure_escalation':'gpt-6.1-sol'},
        'initial_memory_sha256':bytes_hash(learning.initial.read_bytes()),'no_external_learning':True}
    atomic_write(a.directory/'strategy-config.json',canonical(strategy),exclusive=True)
    fixed.append(a.directory/'strategy-config.json')
    path=a.directory/'system-freeze.json'
    key=freeze(ROOT,sorted(set(code+fixed)),path,baseline={'candidate_id':baseline['candidate_id'],
        'origin':'ENGINEER_GUIDED_DEVELOPMENT_BASELINE; not an autonomous-test gain',
        'verification_sha256':bytes_hash(a.baseline_verification.read_bytes())},validation=validation,budget_snapshot=Budget(a.budget_ledger).snapshot())
    config={name:str(getattr(a,name).resolve()) for name in ('legacy_registry','query_registry','baseline_round','baseline_verification','identity',
        'business_root','key_file','budget_ledger','repair_receipt','directory')}
    config.update(fixtures=[str(f.resolve()) for f in fixtures],repair_id=a.repair_id,host=a.host,
        baseline_id=baseline['candidate_id'],freeze=str(path.resolve()),freeze_sha256=key,
        strategy_config=str((a.directory/'strategy-config.json').resolve()),strategy_config_sha256=bytes_hash((a.directory/'strategy-config.json').read_bytes()))
    from rsik.query_aware import autonomy_interface_contract
    from rsik.experiment import executor
    fingerprint=a.handbook_fingerprint or digest({'contract':autonomy_interface_contract(),
        'evaluator':executor.EVALUATOR,'fixed':executor.FIXED,'training_freeze_sha256':bytes_hash((a.primary/'freeze.json').read_bytes())})
    config.update(profile=a.profile,role_profile=a.role_profile,handbook=str((a.handbook or a.legacy_registry/'handbook').resolve()),
        handbook_compatibility_fingerprint=fingerprint,handbook_format=a.handbook_format,handbook_features=features)
    atomic_write(a.directory/'launch.json',canonical(config),exclusive=True)
    result=run(config)
    print(json.dumps({'phase':result['phase'],'execution_state':result['execution_state'],
        'stop_reason':result.get('stop_reason'),'rounds':result['rounds'],'current_parent_id':result['current_parent_id'],'budget':result['budget']}))


if __name__=='__main__':main()

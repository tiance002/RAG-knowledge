"""Pre-registered matched search plan and one original cumulative fee ledger."""
from decimal import Decimal
import random
from .budgets import Budget, NANO
from .state import digest


def matched_plan(*, epoch_id, baseline_id, input_manifest_hash, initial_memory_hash,
                 qa_input_token_bound=None, qa_output_token_bound=1024,
                 qa_price=None, seed=20261115):
    runs=[]
    for repeat in range(1,4):
        group=[('A_NEW_K3','parallel','query-k3-dependency/v1'),
               ('B_K1_P2','single','query-k3-dependency/v1'),('C_OLD_K3','parallel','legacy/v1')]
        random.Random(seed+repeat).shuffle(group)
        for name,profile,role in group:
            runs.append({'run_id':f'{epoch_id}-{name}-{repeat}','group':name,'repeat':repeat,
                'profile':profile,'role_profile':role,'single_angle':'P2_BUDGET',
                'nominal_usd':'0.20','baseline_id':baseline_id,
                'input_manifest_hash':input_manifest_hash,'initial_memory_hash':initial_memory_hash,
                'learning_namespace':f'{name}-{repeat}','temperature_zero_is_deterministic':False})
    return {'schema':'rsi-nine-search-plan/v1','epoch_id':epoch_id,'runs':runs,'order_seed':seed,
        'same_kernel':True,'cross_run_learning':False,'search_nominal_usd':'1.80',
        'max_final_nominations':1,'final_holdout_uses':1,'qa_repeats':3,'qa_questions_target':20,
        'qa_generations_planned':120,'qa_input_token_bound':qa_input_token_bound,
        'qa_output_token_bound':qa_output_token_bound,'qa_price':qa_price,
        'analysis':'all nine complete/budget-blocked outcomes; source clusters distinct from cases',
        'nomination_order':['hard_gates','complete_family','delta_total','worst_relative_delta','ast_nodes','candidate_id'],
        'fixed_holm_family':3,'alpha':'0.1','sequential_inference':'exploratory only',
        'stop_rules':['evidence/data/parent/billing/identity failure','run nominal fee crossed after one settled request','owner stop'],
        'no_gain_round_stop':False,'models':'existing OpenAI allowlist, no Astra','production_changed':False}


def budget_feasibility(plan, budget):
    s=budget.snapshot();remaining=Decimal(s['limit_usd'])-Decimal(s['recorded_usd_upper'])
    reasons=[];qa=None
    if s['pending_request_keys'] or s['unbounded_usage_requests']:reasons.append('BILLING_OR_REQUEST_STATUS_UNKNOWN')
    if remaining<Decimal(plan['search_nominal_usd']):reasons.append('NINE_SEARCH_NOMINAL_BUDGET_INSUFFICIENT')
    rates=plan.get('qa_price');tokens=plan.get('qa_input_token_bound')
    if not rates or type(tokens)is not int or tokens<=0:reasons.append('QA_BUDGET_BOUND_UNKNOWN')
    else:
        qa=Decimal(plan['qa_generations_planned'])*(Decimal(tokens)*Decimal(str(rates['write']))+
            Decimal(plan['qa_output_token_bound'])*Decimal(str(rates['output'])))/NANO
        if remaining<Decimal(plan['search_nominal_usd'])+qa:reasons.append('SEARCH_PLUS_QA_PLAN_BUDGET_INSUFFICIENT')
    return {'status':'PASS' if not reasons else 'BLOCKED','blocking_reasons':reasons,
        'remaining_usd_upper_basis':str(remaining),'nominal_search_usd':plan['search_nominal_usd'],
        'qa_upper_usd':str(qa) if qa is not None else 'UNKNOWN','money_reserved':False,
        'last_single_request_may_cross_original_limit':True,'ledger_snapshot':s}


class SearchBudget(Budget):
    """Serial fee admission, independent role contexts, no money reservations.

    Start balance is frozen in the run receipt. Resumption must reuse it; the
    caller supplies a validated launch identity, never a freshly reset balance.
    """
    def __init__(self,path,*,start_nano,nominal_usd='0.20'):
        super().__init__(path)
        if type(start_nano)is not int or start_nano<0:raise ValueError('INVALID_SEARCH_START_BALANCE')
        self.search_start_nano=start_nano
        self.search_allowance_nano=int(Decimal(nominal_usd)*NANO)
        if self.search_allowance_nano<=0:raise ValueError('INVALID_SEARCH_ALLOWANCE')

    def start(self,*args,**kwargs):
        with self.mutex:
            state=self.snapshot()
            if state['recorded_cost_nano_upper']-self.search_start_nano>=self.search_allowance_nano:
                raise RuntimeError('BUDGET_EXHAUSTED')
            if self.inflight:raise RuntimeError('BUDGET_SERIALIZE_REQUIRED')
            return super().start(*args,**kwargs)


def nominate(results, *, plan=None):
    """At most one frozen training-qualified artifact; no holdout input allowed."""
    if results:
        expected={r['run_id'] for r in (plan or {}).get('runs',[])}
        actual=[r.get('run_id') for r in results]
        terminal={'COMPLETED','BUDGET_EXHAUSTED','BLOCKED','FAILED','INVALIDATED'}
        if (len(expected)!=9 or len(actual)!=len(set(actual)) or set(actual)!=expected
                or any(r.get('execution_state') not in terminal for r in results)):
            return {'status':'BLOCKED_SEARCH_INCOMPLETE','candidate_id':None,'final_holdout_seen':False}
    candidates=[]
    for run in results:
        for item in run.get('candidate_decisions',[]):
            if (run.get('execution_state')=='COMPLETED' and run.get('family_size')==3
                    and item.get('verdict')=='VALIDATED' and item.get('gates_passed')):
                candidates.append((run,item))
    def key(pair):
        _,v=pair;worst=v['worst_relative_delta']
        return (v['delta_total'],Decimal(worst['numerator'])/Decimal(worst['denominator']),
                v.get('ast_nodes',10**9),v['candidate_id'])
    selected=min(candidates,key=key) if candidates else None
    return {'status':'NOMINATED' if selected else 'NO_QUALIFIED_CANDIDATE',
        'candidate_id':selected[1]['candidate_id'] if selected else None,
        'run_id':selected[0]['round_id'] if selected else None,'final_holdout_seen':False,
        'evidence_hash':digest(results),'production_changed':False}

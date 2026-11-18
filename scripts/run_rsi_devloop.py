"""Isolated DEV_DEBUG loop for archival replay or an explicitly approved live round."""
from collections import Counter
from decimal import Decimal
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'scripts')]

from rsik.adjudicate import SLOTS
from rsik.analytics.reader import read_event_log
from rsik.budgets import PRICES, PRICE_VERSION, NANO
from rsik.openai_roles import decode_settings, effective_role_prompt, role_schema
from rsik.role_profiles import M1_SPECIALIZED_V1, roles_for
from rsik.state import canonical, digest

MODE = 'DEV_DEBUG'
MODEL = 'gpt-6-luna'
SEAM = 'query-autonomous-v1'
OUTPUT_ROOT = ROOT / 'var' / 'devloop'
BUDGET_PATH = Path('E:/codex_workspace/2026-11-03/task/rsi-work/var/rsi-v22-20261115/task-model-budget.sqlite')


def money(nano):
    return str(Decimal(nano) / NANO)


def read_budget(path):
    """Read the original ledger's validated chain, without invoking the writer."""
    source = read_event_log(path)
    starts, completed, bounds = {}, {}, {}
    maps = {'MODEL_REQUEST_STARTED': starts, 'MODEL_REQUEST_COMPLETED': completed,
            'MODEL_REQUEST_POSTHOC_COST_BOUND': bounds}
    for event in source['events']:
        target = maps.get(event['event_type'])
        if target is not None:
            payload = event['payload']
            target[payload['request_key']] = payload
    if not set(completed) <= set(starts) or not set(bounds) <= set(completed):
        raise ValueError('DEV_DEBUG_LEDGER_INCOMPLETE')
    unknown = {k for k, p in completed.items() if p.get('usage_status') != 'KNOWN'}
    pending = sorted(set(starts) - set(completed))
    unbounded = sorted(unknown - set(bounds))
    recorded = sum(p.get('cost_nano_upper') or 0 for p in completed.values())
    recorded += sum(bounds[key]['cost_nano_upper'] for key in unknown if key in bounds)
    return {'sha256': source['sha256'], 'events': len(source['events']),
            'requests_started': len(starts), 'requests_completed': len(completed),
            'recorded_usd_upper': money(recorded), 'remaining_usd_at_recorded_upper': money(5 * NANO - recorded),
            'unknown_usage_count': len(unknown), 'pending_count': len(pending),
            'unbounded_count': len(unbounded), 'paid_requests_this_run': 0,
            'new_cost_usd': '0', 'provider_invoiced_charge': 'UNKNOWN'}


def specialized_plan(ledger):
    """Freeze six proposed roles without creating a request in the shared ledger."""
    model = MODEL
    price = PRICES[model]
    roles = []
    names = ('diagnose', *roles_for(M1_SPECIALIZED_V1).values(), 'curator', 'meta')
    total = 0
    for name in names:
        policy, task = effective_role_prompt(M1_SPECIALIZED_V1, name, SEAM)
        decode = decode_settings(name, model)
        upper = 16_384 * price['write'] + decode['max_output_tokens'] * price['output']
        total += upper
        roles.append({'role': name, 'model': model, 'prompt_sha256': digest(policy + '\n' + task),
                      'response_schema_sha256': digest(role_schema(name)), 'decode': decode,
                      'estimated_usd_upper_at_repo_rates': money(upper)})
    return {'status': 'BLOCKED_PAYMENT_AUTHORIZATION_REQUIRED',
            'profile': M1_SPECIALIZED_V1, 'seam': SEAM, 'request_limit': len(roles),
            'retries': 0, 'per_request_input_byte_ceiling': 16384,
            'estimated_total_usd_upper_at_repo_rates': money(total),
            'price_version': PRICE_VERSION, 'dev_ceiling_proposal_usd': '0.30', 'roles': roles,
            'cumulative_budget_ledger_sha256': ledger['sha256'],
            'pricing_status': 'REPO_RATE_ESTIMATE_NEEDS_CURRENT_PROVIDER_CONFIRMATION',
            'provider_dispatches': 0}


def write(path, value):
    path.write_bytes(canonical(value) + b'\n')


def safe_output(path):
    resolved = path.resolve()
    if resolved == OUTPUT_ROOT.resolve() or not resolved.is_relative_to(OUTPUT_ROOT.resolve()):
        raise ValueError('DEV_DEBUG_OUTPUT_MUST_BE_CHILD_OF_VAR_DEVLOOP')
    resolved.mkdir(parents=True, exist_ok=False)
    return resolved


def _args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Run one owner-authorized live DEV_DEBUG round (six calls max)')
    parser.add_argument('--cases', type=Path, required=True, help='Frozen DEV-only case bundle')
    parser.add_argument('--archive', type=Path, required=True, help='Bound historical role receipts')
    parser.add_argument('--ledger', type=Path, default=BUDGET_PATH, help='Original cumulative ledger')
    parser.add_argument('--out', type=Path, required=True, help='Fresh directory below var/devloop')
    parser.add_argument('--key-file', type=Path, help='OPENAI_API_KEY file; required only with --live')
    parser.add_argument('--approval-id', help='Explicit one-round approval identifier for --live')
    return parser.parse_args(argv)


def _fixed_cases(path):
    from rsik.dev_debug_cases import load_cases
    value = load_cases(path)
    cases = value['cases'] if isinstance(value, dict) else value
    cohorts = Counter(case['cohort'] for case in cases)
    if cohorts != {'failed': 8, 'correct': 6} or len({c['case_id'] for c in cases}) != 14:
        raise ValueError('DEV_DEBUG_EXPECTED_FROZEN_8_PLUS_6')
    if any(c.get('partition', 'DEV') != 'DEV' for c in cases):
        raise ValueError('DEV_DEBUG_PARTITION_REQUIRED')
    return cases


def compare(pair, cases):
    """Compare only explicit reviewed member predicates. Do not score answers."""
    index = {case['case_id']: case for case in cases}
    counts = Counter()
    for row in pair['per_case']:
        sample = index[row['case_id']]
        cohort = row['historical_cohort'] = sample['cohort']
        row['historical_answer_label'] = sample['historical_label']
        left, right = row['baseline'], row['candidate']
        paired = row['pair_status'] == 'DEV_PAIRED'
        row['removed_member_ids'] = sorted(set(left['member_ids']) - set(right['member_ids'])) if paired else []
        row['added_member_ids'] = sorted(set(right['member_ids']) - set(left['member_ids'])) if paired else []
        row['answer_quality'] = 'UNKNOWN_NO_NEW_ANSWER_EVALUATION'
        a, b = (arm.get('retention', {}).get('status', 'UNKNOWN') for arm in (left, right))
        if not paired or 'UNKNOWN' in (a, b):
            row['reviewed_fact_transition'] = 'UNKNOWN'
        else:
            row['reviewed_fact_transition'] = a + '_TO_' + b
            counts['reviewed_' + cohort] += 1
            if a == 'MISSING' and b == 'RETAINED' and cohort == 'failed':
                counts['reviewed_failed_case_repairs'] += 1
            if a == 'RETAINED' and b == 'MISSING' and cohort == 'correct':
                counts['reviewed_correct_case_regressions'] += 1
        counts['changed_member_selection'] += bool(row['added_member_ids'] or row['removed_member_ids'])
        counts['removed_member_count'] += len(row['removed_member_ids'])
    return {**counts, 'reviewed_failed_denominator': counts['reviewed_failed'],
            'reviewed_correct_denominator': counts['reviewed_correct'],
            'reviewed_failed_case_repairs': counts['reviewed_failed_case_repairs'] if counts['reviewed_failed'] else 'UNKNOWN',
            'reviewed_correct_case_regressions': counts['reviewed_correct_case_regressions'] if counts['reviewed_correct'] else 'UNKNOWN',
            'unknown_review_count': sum(r['reviewed_fact_transition']=='UNKNOWN' for r in pair['per_case'])}


def run(args):
    from rsik.contracts import ContractError, unwrap_response, validate_diagnosis, validate_proposal, validate_readonly
    from rsik.dev_debug_receipts import load_archive, role_receipt
    from rsik.dev_debug_replay import replay_candidate
    from rsik.query_aware import apply_patch
    from rsik.workflow import full_worker_identity

    start = time.monotonic()
    cases = _fixed_cases(args.cases)
    archive = load_archive(args.archive)
    if Path(archive['ledger_path']).resolve() != args.ledger.resolve():
        raise ValueError('DEV_DEBUG_ORIGINAL_LEDGER_REQUIRED')
    snapshot = read_budget(args.ledger)
    parent = archive['parent']
    worker = Path(parent['worker_path']).read_bytes()
    config = Path(parent['config_path']).read_bytes()
    if (hashlib.sha256(worker).hexdigest() != parent['worker_sha256']
            or hashlib.sha256(config).hexdigest() != parent['config_sha256']):
        raise ValueError('DEV_DEBUG_PARENT_CHANGED')
    directory = safe_output(args.out)
    baseline = replay_candidate(worker, config, worker, cases)
    write(directory/'baseline.json', baseline)
    planning = specialized_plan(snapshot)
    write(directory/'specialized-plan.json', planning)

    public, envelope = role_receipt(archive, 'diagnose')
    diagnosis = unwrap_response(envelope)
    if diagnosis is None:
        raise ValueError('DEV_DEBUG_ARCHIVED_DIAGNOSIS_ABSTAINED')
    validate_diagnosis(diagnosis, public)
    write(directory/'diagnosis.json', diagnosis)

    candidates = []
    for slot in SLOTS:
        attempt = {'slot': slot, 'source_profile': archive['source_role_profile'],
                   'proposal_instance_id': digest([archive['source_round_id'], slot])}
        candidates.append(attempt)
        visible, envelope = role_receipt(archive, slot)
        try:
            proposal = unwrap_response(envelope)
            if proposal is None:
                attempt.update(status='DEV_ABSTAINED', reason=envelope['reason'])
                continue
            validate_proposal(proposal, visible, slot)
        except ContractError as error:
            attempt.update(status='DEV_CONTRACT_FAILED', reason=error.code)
            continue
        slot_dir = directory/slot
        slot_dir.mkdir()
        patch = proposal['worker_patch']
        write(slot_dir/'patch.json', patch)
        attempt.update(proposal_id=proposal['proposal_id'], patch_sha256=digest(patch), contract='VALID')
        try:
            candidate = apply_patch(worker, patch)
        except (ValueError, SyntaxError) as error:
            attempt.update(status='DEV_STATIC_REJECTED', reason=str(error), ast='REJECTED')
            continue
        attempt.update(ast='VALID', **full_worker_identity({'worker': candidate}))
        (slot_dir/'worker.py').write_bytes(candidate)
        began = time.monotonic()
        try:
            replay = replay_candidate(worker, config, candidate, cases)
        except ValueError as error:
            attempt.update(status='DEV_REPLAY_INPUT_REJECTED', reason=str(error))
            continue
        replay['comparison'] = compare(replay, cases)
        baseline_status = {r['case_id']: r['baseline'] for r in baseline['per_case']}
        comparable = ('status','indices','member_ids','chars','retention')
        if any(any(row['baseline'].get(k) != baseline_status[row['case_id']].get(k)
                   for k in comparable) for row in replay['per_case']):
            replay['status'] = 'DEV_BASELINE_REPRODUCTION_FAILED'
        write(slot_dir/'paired.json', replay)
        attempt.update(status=replay['status'], paired_cases=replay['paired_cases'],
                       planned_cases=replay['planned_cases'], failures=replay['failed_cases'],
                       baseline_chars=replay['baseline_chars'], candidate_chars=replay['candidate_chars'],
                       replay_seconds=round(time.monotonic()-began, 3), comparison=replay['comparison'])
    advisory = {}
    for role in ('curator','meta'):
        visible, envelope = role_receipt(archive, role)
        try:
            value = unwrap_response(envelope)
            if value is not None:
                validate_readonly(value, visible, role)
            advisory[role] = {'status': envelope['status'], 'scope': 'ORIGINAL_ROUND_ONLY_NOT_CURRENT_EVALUATION'}
        except ContractError as error:
            advisory[role] = {'status': 'DEV_ARCHIVE_CONTRACT_FAILED', 'reason': error.code}
    incurred = archive['roles']
    costs = {role: {'original_usd_upper': money(val['historical_cost_nano_upper']),
                    'historical_usage': val['usage'], 'new_cost_usd':'0'}
             for role,val in incurred.items()}
    feedback = {
        'classification': dict(Counter(c['status'] for c in candidates)),
        'quality_boundary': 'Selector-only, no fresh answer evaluation; unreviewed fact predicates remain UNKNOWN.',
        'archived_advisory': advisory,
        'next_specialized_round': planning,
    }
    complete = sum(c['status'] == 'DEV_REPLAY_COMPLETE' for c in candidates)
    result = {
        'schema':'rsi-dev-debug-round/v1','mode': MODE,
        'status':'DEV_ARCHIVAL_ROUND_COMPLETE' if complete else 'DEV_ARCHIVAL_NO_COMPLETE_REPLAY',
        'source_role_profile': archive['source_role_profile'], 'target_live_profile': M1_SPECIALIZED_V1,
        'source_round_id': archive['source_round_id'], 'case_bundle_sha256': hashlib.sha256(args.cases.read_bytes()).hexdigest(),
        'planned_cases':len(cases), 'cohorts':dict(Counter(c['cohort'] for c in cases)), 'candidates':candidates,
        'proposal_count':sum(c.get('contract')=='VALID' for c in candidates),
        'static_valid_count':sum(c.get('ast')=='VALID' for c in candidates),
        'complete_replay_count':complete,
        'archived_roles': costs, 'archived_usd_upper':money(sum(v['historical_cost_nano_upper'] for v in incurred.values())),
        'ledger': snapshot, 'latency_seconds':round(time.monotonic()-start, 3), 'feedback':feedback,
    }
    if read_budget(args.ledger) != snapshot:
        raise ValueError('DEV_DEBUG_LEDGER_CHANGED_DURING_RUN')
    write(directory/'report.json', result)
    return result


def main(argv=None):
    args = _args(argv)
    if args.live:
        if args.key_file is None:
            raise SystemExit('--key-file is required with --live')
        from rsik.dev_debug_live import run_live
        result = run_live(args)
        print(json.dumps({k:result.get(k) for k in (
            'status','planned_cases','completed_replays','request_count','new_cost_usd_upper','stop_reason')},
            ensure_ascii=False))
        return 0 if result.get('status') == 'DEV_DEBUG_LIVE_COMPLETE' else 2
    result = run(args)
    print(json.dumps({k:result[k] for k in (
        'status','planned_cases','proposal_count','static_valid_count','complete_replay_count','latency_seconds')},
        ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""One bounded, explicitly approved live DEV_DEBUG round."""
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import time

from .budgets import Budget, NANO, PRICES, PRICE_VERSION
from .contracts import ContractError, unwrap_response
from .dev_debug_receipts import load_archive
from .dev_debug_replay import replay_candidate
from .errors import StageFailure
from .openai_roles import OpenAIRoles, runtime_profile_identity, single_request
from .query_aware import apply_patch, autonomy_interface_contract
from .redact import LeakScanner, make_visible, project_summary
from .role_profiles import M1_SPECIALIZED_V1, roles_for
from .state import canonical, digest
from .workflow import full_worker_identity
from run_rsi_loop import selector

APPROVAL = 'rsi-dev-v0-20261118-luna-01'
MODEL = 'gpt-6-luna'
PROFILE = M1_SPECIALIZED_V1
SEAM = 'query-autonomous-v1'
MAX_BODY_BYTES = 16_384
MAX_COST_NANO = 20_000_000
SLOTS = roles_for(PROFILE)
ROLES = ('diagnose', *SLOTS.values(), 'curator', 'meta')
OUTPUT_LIMITS = {role: 4096 if role in SLOTS.values() else 1024 for role in ROLES}
DECODE = {role: {'max_output_tokens': limit, **({'reasoning_effort': 'max'} if role in SLOTS.values() else {})}
          for role, limit in OUTPUT_LIMITS.items()}


def _upper_bound():
    rates = PRICES[MODEL]
    if rates != {'input': 100, 'cached': 10, 'write': 125, 'output': 500}:
        raise ValueError('DEV_DEBUG_APPROVED_PRICE_CHANGED')
    return MAX_BODY_BYTES * rates['write'] * len(ROLES) + sum(OUTPUT_LIMITS.values()) * rates['output']


def _guard(body, role):
    wire = json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8')
    proposer = role in SLOTS.values()
    if (body.get('model') != MODEL or body.get('service_tier') != 'default'
            or body.get('store') is not False or body.get('tools') or body.get('background')
            or body.get('max_output_tokens') != OUTPUT_LIMITS[role]
            or body.get('reasoning') != {'effort': 'max' if proposer else 'none'}
            or (proposer and 'temperature' in body)
            or len(wire) > MAX_BODY_BYTES or len(canonical(body)) > MAX_BODY_BYTES):
        raise ContractError('DEV_DEBUG_REQUEST_OUTSIDE_APPROVAL')


def _public(worker, config, snapshot, cases, round_id, scanner, *, results=None):
    counts = {'questions': len(cases), 'paired': snapshot['paired_cases'],
              'failed': snapshot['failed_cases'], 'unknown': len(cases)}
    if results is None:
        selected = {row['case_id']: row['baseline'] for row in snapshot['per_case']}
        counts['delivered_members'] = sum(len(value['member_ids']) for value in selected.values())
        counts['seed_members'] = sum(len(case['seed_ids']) for case in cases)
        counts['nonseed_members'] = counts['delivered_members'] - counts['seed_members']
        counts['context_chars'] = snapshot['baseline_chars']
    else:
        counts.update(proposal_instances=len(results),
                      qualified_proposals=sum(item.get('ast')=='VALID' for item in results),
                      paired=sum(item.get('paired_cases', 0) for item in results))
    observation = project_summary('CONTEXT_LENGTH' if results is None else 'REPLAY_SUMMARY', counts,
                                  include_count_evidence=True)
    public = make_visible([observation], parent_code=selector(worker.decode('utf-8'))[1],
                          round_id=round_id, scanner=scanner)
    public['parent_alias'] = 'parent-' + hashlib.sha256(worker).hexdigest()[:32]
    if results is None:
        public.update(parent_selection=json.loads(config)['selection'],
                      experiment_intent='STRUCTURAL_POLICY_SEARCH',
                      selector_contract=autonomy_interface_contract())
    return public


def _read_key_ready(path):
    for line in path.read_text(encoding='utf-8-sig').splitlines():
        name, sep, value = line.strip().removeprefix('export ').partition('=')
        if sep and name.strip() == 'OPENAI_API_KEY':
            return value.strip().strip('"\'').startswith('sk-')
    return False


def _artifacts(archive_path, ledger_path):
    archive = load_archive(archive_path)
    if Path(archive['ledger_path']).resolve() != ledger_path.resolve():
        raise ValueError('DEV_DEBUG_ORIGINAL_LEDGER_REQUIRED')
    parent = archive['parent']
    worker = Path(parent['worker_path']).read_bytes()
    config = Path(parent['config_path']).read_bytes()
    if (hashlib.sha256(worker).hexdigest() != parent['worker_sha256']
            or hashlib.sha256(config).hexdigest() != parent['config_sha256']):
        raise ValueError('DEV_DEBUG_PARENT_CHANGED')
    return worker, config


def run_live(args):
    from run_rsi_devloop import _fixed_cases, compare, read_budget, safe_output, write
    from .foundation import verify_boundary
    from .http_worker import transport_identity

    if args.approval_id != APPROVAL:
        raise ValueError('DEV_DEBUG_APPROVAL_ID_REQUIRED')
    if not _read_key_ready(args.key_file):
        raise ValueError('DEV_DEBUG_CREDENTIAL_UNAVAILABLE')
    rates_upper = _upper_bound()
    if rates_upper > MAX_COST_NANO:
        raise ValueError('DEV_DEBUG_COST_PLAN_EXCEEDS_APPROVAL')
    verify_boundary()
    route = transport_identity()
    snapshot = read_budget(args.ledger)
    if snapshot['pending_count'] or snapshot['unbounded_count']:
        raise ValueError('DEV_DEBUG_ORIGINAL_LEDGER_UNRESOLVED')
    before = Decimal(snapshot['recorded_usd_upper'])
    if before + Decimal(rates_upper) / NANO > 5:
        raise ValueError('DEV_DEBUG_CUMULATIVE_BUDGET_EXCEEDED')
    cases = _fixed_cases(args.cases)
    worker, config = _artifacts(args.archive, args.ledger)
    round_id = 'round-' + digest(APPROVAL)[:32]
    budget = Budget(args.ledger)
    if any(event['event_type']=='MODEL_REQUEST_STARTED'
           and event['payload']['request_key'].startswith(round_id + '-') for event in budget.log.read()):
        raise ValueError('DEV_DEBUG_APPROVAL_ALREADY_USED')
    directory = safe_output(args.out)
    began = time.monotonic()
    baseline = replay_candidate(worker, config, worker, cases)
    write(directory/'baseline.json', baseline)
    if baseline['paired_cases'] != len(cases) or any(
            row['baseline']['member_ids'] != row['candidate']['member_ids'] for row in baseline['per_case']):
        raise ValueError('DEV_DEBUG_BASELINE_NOT_REPRODUCED')

    roles_dir = directory/'roles'
    def dev_transport(body, key_file, timeout):
        path = roles_dir/f'{round_id}-{role}-0.transport.json'
        return single_request(body, key_file, timeout, response_path=path)

    # Private inputs are retained for local replay. Only an explicit public
    # projection reaches the role contract and then the provider.
    scanner = LeakScanner(case['query'] for case in cases)
    models = dict.fromkeys(ROLES, MODEL)
    identity = runtime_profile_identity(PROFILE, models, SEAM, DECODE, 'default')
    runner = OpenAIRoles(budget, roles_dir, args.key_file, scanner, transport=dev_transport,
                         models=models, decode_overrides=DECODE, max_attempts=1, service_tier='default',
                         request_guard=_guard, seam_profile=SEAM, role_profile=PROFILE)
    write(directory/'plan.json', {'approval_id': APPROVAL, 'round_id': round_id, 'profile': identity,
                                  'request_limit': len(ROLES), 'retries': 0, 'cost_nano_upper': rates_upper,
                                  'price_version': PRICE_VERSION, 'transport': route, 'ledger_before': snapshot})
    primary = _public(worker, config, baseline, cases, round_id, scanner)
    diagnosis, stopped = None, None
    entries, candidates = [], []
    for role in ROLES:
        if stopped:
            entries.append({'role': role, 'status': 'DEV_NOT_DISPATCHED'})
            continue
        entry = {'role': role, 'request_key': f'{round_id}-{role}-0'}
        entries.append(entry)
        is_proposer = role in SLOTS.values()
        start = time.monotonic()
        try:
            public = (_public(worker, config, baseline, cases, round_id, scanner, results=candidates)
                      if role in {'curator','meta'} else primary)
            envelope = runner.call(role, public,
                                   diagnosis if is_proposer else None)
            if envelope.get('reason') == 'CONTRACT_ERROR_NO_REPAIR':
                receipt_path = roles_dir/(entry['request_key'] + '.json')
                receipt = json.loads(receipt_path.read_bytes())
                response = receipt.get('response', {})
                details = response.get('incomplete_details') or {}
                if response.get('status') == 'incomplete':
                    entry.update(status='DEV_OUTPUT_INCOMPLETE', reason=details.get('reason', 'UNKNOWN'))
                else:
                    entry.update(status='DEV_CONTRACT_FAILED')
                continue
            payload = unwrap_response(envelope)
            entry.update(status='DEV_ABSTAINED' if payload is None else 'DEV_CONTRACT_VALID')
            if payload is None:
                continue
            if role == 'diagnose':
                diagnosis = payload
            if is_proposer:
                slot = next(slot for slot, agent in SLOTS.items() if agent == role)
                item = {'slot': slot, 'role': role, 'proposal_id': payload['proposal_id'],
                        'proposal_instance_id': digest([round_id, slot]), 'status': 'DEV_STATIC_REJECTED'}
                candidates.append(item)
                slot_dir = directory/slot
                slot_dir.mkdir()
                patch = payload['worker_patch']
                write(slot_dir/'patch.json', patch)
                item['patch_sha256'] = digest(patch)
                try:
                    candidate = apply_patch(worker, patch)
                except (ValueError, SyntaxError) as error:
                    item.update(reason=str(error), ast='REJECTED')
                    continue
                item.update(ast='VALID', **full_worker_identity({'worker': candidate}))
                (slot_dir/'worker.py').write_bytes(candidate)
                paired = replay_candidate(worker, config, candidate, cases)
                paired['comparison'] = compare(paired, cases)
                base_rows = {row['case_id']: row['baseline'] for row in baseline['per_case']}
                if any(row['baseline']['member_ids'] != base_rows[row['case_id']]['member_ids']
                       for row in paired['per_case'] if row['baseline']['status']=='DEV_EXECUTED'):
                    paired['status'] = 'DEV_BASELINE_REPRODUCTION_FAILED'
                write(slot_dir/'paired.json', paired)
                item.update(status=paired['status'], paired_cases=paired['paired_cases'],
                            baseline_chars=paired['baseline_chars'], candidate_chars=paired['candidate_chars'],
                            comparison=paired['comparison'])
        except ContractError as error:
            entry.update(status='DEV_CONTRACT_FAILED', reason=error.code)
        except (StageFailure, OSError, ValueError, TimeoutError, RuntimeError) as error:
            stopped = str(error)
            entry.update(status='DEV_STOPPED', reason=stopped)
        finally:
            entry['elapsed_seconds'] = round(time.monotonic()-start, 3)
    after = read_budget(args.ledger)
    increase = Decimal(after['recorded_usd_upper']) - before
    started = [event['payload'] for event in budget.log.read() if event['event_type']=='MODEL_REQUEST_STARTED'
               and event['payload']['request_key'].startswith(round_id + '-')]
    if after['pending_count'] or after['unbounded_count']:
        cost = 'UNKNOWN'
    else:
        cost = str(increase)
    if len(started) != len(ROLES) and not stopped:
        stopped = 'DEV_DEBUG_DISPATCH_INCOMPLETE'
    for ledger in (snapshot, after):
        ledger.pop('new_cost_usd')
        ledger.pop('paid_requests_this_run')
    result = {'schema':'rsi-dev-debug-live/v1', 'status':'DEV_DEBUG_LIVE_STOPPED' if stopped else 'DEV_DEBUG_LIVE_COMPLETE',
              'mode':'DEV_DEBUG', 'round_id':round_id, 'planned_cases':len(cases), 'stop_reason':stopped,
              'request_count':len(started), 'new_cost_usd_upper':cost,
              'completed_replays':sum(c['status']=='DEV_REPLAY_COMPLETE' for c in candidates),
              'roles':entries, 'candidates':candidates, 'ledger_before':snapshot, 'ledger_after':after,
              'latency_seconds':round(time.monotonic()-began, 3)}
    write(directory/'report.json', result)
    return result

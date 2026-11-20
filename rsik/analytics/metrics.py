"""Read-only deterministic metrics over already validated projections."""
from collections import Counter, defaultdict
from decimal import Decimal
import json

from .schemas import GROUP_DIMENSIONS, METRICS, MEMORY_METRIC_DEFINITIONS, SELECTION_METRIC_DEFINITIONS
from .portfolio import analyze_portfolio
from .selection import analyze_selection, SELECTION_CONTRACT_VERSION


def _kind(record):
    return record.get('kind', record.get('projection_type', record.get('type')))


def _refs(records):
    refs = []
    for record in records:
        value = record.get('evidence_refs', record.get('source_refs', []))
        if not value:
            value = record.get('event_ids') or [record.get('event_id') or record.get('record_id') or record.get('id')]
        if not isinstance(value, list):
            value = [value]
        refs.extend(item for item in value if item is not None)
    return [json.loads(value) for value in sorted({json.dumps(item, sort_keys=True, ensure_ascii=False) for item in refs})]


def _result(mid, records, numerator=None, denominator=None, *, reason=None, status=None, count=False):
    if status is None:
        status = 'UNKNOWN' if numerator is None or (denominator is None and not count) else 'N/A' if denominator == 0 and not count else 'OK'
    value = None
    if status == 'OK':
        value = numerator if count else float(Decimal(str(numerator)) / Decimal(str(denominator)))
    return {'metric_id': mid, 'status': status, 'numerator': numerator,
            'denominator': denominator, 'value': value, 'unit': METRICS[mid]['unit'],
            'evidence_refs': _refs(records), 'reason': reason or ('observed denominator is empty' if status == 'N/A' else 'missing required observations' if status == 'UNKNOWN' else 'explicit observed evidence')}


def _flag_rate(mid, records, numerator, eligible=None):
    eligible = records if eligible is None else eligible
    if not records:
        return _result(mid, records)
    values = [numerator(record['payload']) for record in eligible]
    return _result(mid, records, sum(value is True for value in values) if all(type(value) is bool for value in values) else None, len(eligible))


def _unique(records, field):
    result = {}
    for record in records:
        key = record['payload'].get(field)
        if key is not None:
            if _kind(record) == 'CostEvent':
                refs = record.get('source_refs', [])
                source = record.get('history_source_id') or record['payload'].get('history_source_id')
                if source is None and refs:
                    source = refs[0].get('source_id') if isinstance(refs[0], dict) else None
                # Without a log identity, do not assume equal local keys denote
                # the same request. Projection identities are already scoped.
                source = source or record.get('id') or record.get('record_id')
                key = (source, key)
            if _kind(record) == 'CostEvent' and key in result and result[key]['payload'] != record['payload']:
                raise ValueError('CONFLICTING_COST_REQUEST_IDENTITY')
            result[key] = record
    return list(result.values())


def token_statistics(records):
    """Aggregate provider totals once per ledger and request key."""
    requests = _unique([r for r in records if _kind(r) == 'CostEvent'], 'request_key')
    round_experiments, roles, candidate_experiments = defaultdict(set), defaultdict(set), defaultdict(set)
    for record in records:
        payload, dim = record['payload'], record['dimensions']
        round_id, exp = payload.get('round_id'), dim.get('experiment_id')
        if not round_id:
            continue
        if _kind(record) == 'Round' and exp not in (None, 'UNKNOWN'):
            round_experiments[round_id].add(exp)
        if _kind(record) == 'Candidate':
            candidate_id = dim.get('candidate_id')
            if candidate_id not in (None, 'UNKNOWN'):
                roles[round_id, dim.get('agent_role')].add(candidate_id)
                if exp not in (None, 'UNKNOWN'):
                    candidate_experiments[candidate_id].add(exp)

    def totals(items):
        known = [r for r in items if r.get('usage_status') == 'KNOWN'
                 and all(type(r.get(key)) is int and r[key] >= 0 for key in ('input_tokens', 'output_tokens'))]
        inputs = sum(r['input_tokens'] for r in known) if known else None
        outputs = sum(r['output_tokens'] for r in known) if known else None
        unknown = len(items) - len(known)
        return {'requests': len(items), 'known_requests': len(known), 'unknown_requests': unknown,
                'usage_status': 'UNKNOWN' if not known else 'PARTIAL' if unknown else 'KNOWN',
                'input_tokens': inputs, 'output_tokens': outputs,
                'total_tokens': inputs + outputs if known else None}

    def only(values):
        return next(iter(values)) if len(values) == 1 else 'UNKNOWN'

    buckets = {axis: defaultdict(list) for axis in ('model', 'stage', 'round', 'experiment', 'candidate')}
    for ids in roles.values():
        for candidate_id in ids:
            buckets['candidate'][candidate_id]
    request_rows = []
    for record in requests:
        p, dim = record['payload'], record['dimensions']
        round_id = p.get('round_id') or 'UNKNOWN'
        role = dim.get('agent_role') or 'UNKNOWN'
        experiment = dim.get('experiment_id')
        candidate = dim.get('candidate_id')
        model = p.get('model') or 'UNKNOWN'
        stage = p.get('stage') or role
        experiment = only(round_experiments[round_id]) if experiment in (None, 'UNKNOWN') else experiment
        candidate = only(roles[round_id, role]) if candidate in (None, 'UNKNOWN') else candidate
        for axis, key in zip(buckets, (model, stage, round_id, experiment, candidate)):
            buckets[axis][key].append(p)
        measured = totals([p])
        request_rows.append({'request_key': p['request_key'], 'model_id': model, 'stage': stage,
                             'round_id': round_id, 'experiment_id': experiment, 'candidate_id': candidate,
                             'source_id': record.get('source_refs', [{}])[0].get('source_id')
                             if record.get('source_refs') else None,
                             **{key: measured[key] for key in ('usage_status', 'input_tokens', 'output_tokens', 'total_tokens')}})
    breakdown = {axis: {key: totals(items) for key, items in sorted(values.items())}
                 for axis, values in buckets.items()}
    completed = {r['dimensions'].get('experiment_id') for r in records if _kind(r) == 'Experiment'
                 and r['payload'].get('event_type') in {'EXPERIMENT_COMPLETED', 'EXPERIMENT_FINISHED', 'AUTONOMY_TEST_ENDED'}
                 and r['payload'].get('execution_state', r['payload'].get('status', 'COMPLETED')) == 'COMPLETED'}
    by_experiment = breakdown['experiment']
    completed_experiments = {key: by_experiment.get(key, totals([]))
                             for key in sorted(completed - {None, 'UNKNOWN'})}
    efficiency = {}
    for key, items in by_experiment.items():
        if key == 'UNKNOWN':
            continue
        scoped = [r for r in records if _kind(r) == 'Holdout' and (
            r['dimensions'].get('experiment_id') == key
            or (r['dimensions'].get('experiment_id') in (None, 'UNKNOWN')
                and candidate_experiments.get(r['payload'].get('candidate_id')) == {key}))]
        c4 = next(m for m in _group_metrics(scoped)['metrics'] if m['metric_id'] == 'C4')
        n = c4['denominator'] if any(r['payload'].get('completed') is True
                                    and r['payload'].get('independent') is True for r in scoped) else None
        efficiency[key] = {'verified_improvements': n,
            'status': 'N/A' if n == 0 else 'OK' if n and items['usage_status'] == 'KNOWN' else 'UNKNOWN',
            'tokens_per_verified_improvement': items['total_tokens'] / n if n and items['usage_status'] == 'KNOWN' else None}
    return {'totals': totals([r['payload'] for r in requests]), 'by': breakdown,
            'completed_experiments': completed_experiments, 'efficiency_by_experiment': efficiency,
            'requests': request_rows,
            'scope': 'Reported usage in supplied ledgers only; candidate attribution requires a unique round/role binding.'}


def _group_metrics(records):
    kinds = defaultdict(list)
    for record in records:
        kinds[_kind(record)].append(record)
    output = {mid: _result(mid, []) for mid in METRICS}
    agents = kinds['AgentInvocation']
    attempts = [record for record in agents if record['payload'].get('attempted') is True]
    output['A1'] = _flag_rate('A1', agents, lambda p: p.get('success'), attempts)
    received = [record for record in agents if record['payload'].get('received') is True]
    if agents and all(type(record['payload'].get('received')) is bool for record in agents):
        output['A2'] = _flag_rate('A2', agents, lambda p: p.get('schema_valid'), received)
    output['A3'] = _flag_rate('A3', agents, lambda p: p.get('abstained'), attempts)
    output['A4'] = _flag_rate('A4', agents, lambda p: p.get('failed'), attempts)
    if agents and any(record['payload'].get('attempted') is not True for record in agents):
        for mid in ('A1', 'A3', 'A4'):
            output[mid] = _result(mid, agents, reason='attempt eligibility unknown')
    output['R4'] = _flag_rate('R4', agents, lambda p: p.get('failure_origin') == 'INFRASTRUCTURE' if p.get('failure_origin') not in (None, 'UNKNOWN') else None, attempts)
    if agents and any(type(record['payload'].get('attempted')) is not bool for record in agents):
        output['R4'] = _result('R4', agents, reason='attempt eligibility unknown')

    proposals = kinds['Proposal']
    output['P1'] = _flag_rate('P1', proposals, lambda p: p.get('schema_valid'))
    valid = [record for record in proposals if record['payload'].get('schema_valid') is True]
    if proposals and all(type(record['payload'].get('schema_valid')) is bool for record in proposals):
        output['P2'] = _flag_rate('P2', proposals, lambda p: p.get('static_pass'), valid)
    qualified = [record for record in valid if record['payload'].get('static_pass') is True]
    portfolio = analyze_portfolio(qualified)
    hashes = [item['ast_hash'] for item in portfolio['instances']]
    if proposals and all(type(r['payload'].get('static_pass')) is bool for r in valid) and all(type(r['payload'].get('schema_valid')) is bool for r in proposals):
        if all(hashes):
            output['P3'] = _result('P3', qualified, len(set(hashes)), len(hashes))
            output['P4'] = _result('P4', qualified, len(hashes)-len(set(hashes)), len(hashes))

    families = kinds['FamilyDecision']
    complete_rounds = set()
    adjudicated = set()
    adopted = set()
    for record in families:
        p = record['payload']
        decisions = p.get('candidate_decisions', p.get('decisions', []))
        slots = [item.get('angle', item.get('slot')) for item in decisions if isinstance(item, dict)]
        enabled = p.get('enabled_slots')
        enabled_valid = enabled is None or (isinstance(enabled, list) and len(enabled) == len(set(enabled))
            and set(enabled) <= {'P1_DEDUP', 'P2_BUDGET', 'P3_ORDER'})
        complete = (p.get('complete') is True and p.get('family_size') == 3 and len(decisions) == 3
            and len(slots) == 3 and set(slots) == {'P1_DEDUP', 'P2_BUDGET', 'P3_ORDER'}
            and enabled_valid and p.get('round_id') not in (None, '', 'UNKNOWN', 'N/A')
            and p.get('verdict') != 'ROUND_ABORTED')
        if not complete:
            continue
        complete_rounds.add(p.get('round_id'))
        for decision in decisions:
            cid = decision.get('candidate_id')
            infra = decision.get('failure_origin') == 'INFRASTRUCTURE' or bool(set(decision.get('reason_codes') or []) & {'INFRA_FAILURE', 'PARTIAL_REPLAY', 'QUERY_REPLAY_FAILURE', 'EXECUTION_EVIDENCE_MISSING', 'EXECUTION_EVIDENCE_INVALID'})
            if cid and not infra and decision.get('verdict') not in (None, 'UNKNOWN', 'INCONCLUSIVE'):
                adjudicated.add(cid)
        if p.get('adopted_candidate_id'):
            adopted.add(p['adopted_candidate_id'])
    replays = _unique([record for record in kinds['Replay'] if record['payload'].get('round_id') in complete_rounds], 'candidate_id')
    entered = [record for record in replays if record['payload'].get('entered') is True]
    completed = [record for record in entered if record['payload'].get('completed') is True and record['payload'].get('paired') is True]
    if complete_rounds:
        entered_observed = all(type(record['payload'].get('entered')) is bool for record in replays)
        replay_observed = all(type(record['payload'].get('completed')) is bool and type(record['payload'].get('paired')) is bool for record in entered)
        output['P5'] = _result('P5', replays + families,
            len(completed) if entered_observed and replay_observed else None,
            len(entered) if entered_observed else None)
        output['S1'] = _result('S1', families, len(adopted & adjudicated), len(adjudicated))

    holdout_records = [record for record in kinds['Holdout'] if record['payload'].get('completed') is True and record['payload'].get('independent') is True]
    holdout_verdicts = defaultdict(set)
    for record in holdout_records:
        holdout_verdicts[record['payload'].get('candidate_id')].add(record['payload'].get('verdict'))
    ambiguous_holdout = any(key is None or len(values) != 1 or None in values for key, values in holdout_verdicts.items())
    holdouts = _unique([record for record in holdout_records if len(holdout_verdicts[record['payload'].get('candidate_id')]) == 1], 'candidate_id')
    verified = {record['payload']['candidate_id'] for record in holdouts if record['payload'].get('verdict') == 'VERIFIED'}
    verified_improvements = {record['payload']['candidate_id'] for record in holdouts if record['payload'].get('verdict') == 'VERIFIED' and record['payload'].get('improvement') is True}
    improvement_unknown = any(record['payload'].get('verdict') == 'VERIFIED'
        and type(record['payload'].get('improvement')) is not bool for record in holdouts)
    output['S2'] = _result('S2', kinds['Holdout'], len(verified), len(holdouts), status='OK' if holdouts else 'N/A', reason='only independent completed verification; training adoption is excluded')
    if ambiguous_holdout:
        output['S2'] = _result('S2', holdout_records, reason='conflicting or incomplete candidate verification identity')
        verified.clear()
        verified_improvements.clear()

    operations = [r for r in kinds['Selection'] if r['payload'].get('metric_contract_version') == 'rsi-metrics/m1a1']
    if operations:
        complete = [r for r in operations if r['payload'].get('completed') is True
                    and r['payload'].get('valid') is True]
        counts = Counter(r['payload']['selected_parent_id'] for r in complete
                         if r['payload'].get('selected_parent_id') is not None)
        for mid, numerator, denominator in (
                ('S3', len(counts), len(complete)),
                ('S6', sum(n*n for n in counts.values()), len(complete)**2)):
            output[mid] = _result(mid, operations, numerator if complete else None, denominator if complete else None,
                reason='completed valid selection operations including empty results; incomplete operations excluded')
            output[mid].update(completed_operations=len(complete),
                               unknown_operations=len(operations)-len(complete))
    else:
        # Frozen M0 report semantics; v1 selected-only fragments cannot be
        # upgraded into completed operations by this compatibility branch.
        selections = [r for r in kinds['Parent'] if r['payload'].get('selected') is True
                      and r['payload'].get('metric_contract_version') != 'rsi-metrics/m1a1']
        if selections:
            counts = Counter(record['payload'].get('parent_id') for record in selections)
            if None not in counts:
                output['S3'] = _result('S3', selections, len(counts), len(selections))
                output['S6'] = _result('S6', selections, sum(n*n for n in counts.values()), len(selections)**2)
    branches = [record for record in kinds['Parent'] if record['payload'].get('predeclared_window') is True and record['payload'].get('eligible_at_start') is True]
    if branches:
        output['S4'] = _flag_rate('S4', branches, lambda p: p.get('eligible_at_end'))
    depths = [record for record in kinds['Candidate'] if record['payload'].get('lineage_complete') is True and record['payload'].get('lineage_valid') is True]
    if depths and all(type(record['payload'].get('generation')) is int for record in depths):
        output['S5'] = _result('S5', depths, max(record['payload']['generation'] for record in depths), count=True)

    memory = kinds['MemoryEvent']
    def event(record):
        p = record['payload']
        return p.get('event_type', p.get('kind', record.get('event_type')))
    retrieved = [record for record in memory if event(record) in ('MEMORY_RETRIEVED', 'EXPERIENCE_RETRIEVED') and record['payload'].get('raw_retrieval') is True]
    injected = [record for record in memory if event(record) in ('MEMORY_INJECTED', 'EXPERIENCE_INJECTED', 'EXPERIENCE_MATCHED', 'HANDBOOK_MATCHED')]
    referenced = [record for record in memory if event(record) == 'PROPOSAL_REFERENCED']
    used = [record for record in memory if event(record) == 'EXPERIENCE_USED']
    def use_key(record):
        p = record['payload']; return (p.get('round_id'), p.get('entry_id'))
    current_memory = any(r['payload'].get('metric_contract_version') == 'rsi-metrics/m1a1' for r in memory)
    if current_memory:
        operations = [r for r in memory if event(r) in {'MEMORY_RETRIEVAL_STARTED', 'MEMORY_RETRIEVAL_COMPLETED', 'MEMORY_RETRIEVED', 'EXPERIENCE_RETRIEVED'}]
        explicit_injected = [r for r in injected if event(r) in {'MEMORY_INJECTED', 'EXPERIENCE_INJECTED'}]
        def explicit_key(record, axis):
            p = record['payload']
            values = (p.get(axis), p.get('entry_id'))
            return values if all(isinstance(v, str) and v and v != 'UNKNOWN' for v in values) else None
        retrieval_ids = {r['payload']['retrieval_id'] for r in operations if isinstance(r['payload'].get('retrieval_id'), str) and r['payload']['retrieval_id'] not in {'', 'UNKNOWN'}}
        retrieved_keys = {key for r in retrieved if (key := explicit_key(r, 'retrieval_id')) is not None}
        injected_keys = {key for r in explicit_injected if (key := explicit_key(r, 'retrieval_id')) is not None}
        if retrieval_ids:
            identified = all(r['payload'].get('retrieval_id') in retrieval_ids for r in operations)
            output['M1'] = _result('M1', operations, len(retrieval_ids) if identified else None, count=True)
            completed_ids = {r['payload'].get('retrieval_id') for r in operations if event(r) != 'MEMORY_RETRIEVAL_STARTED'}
            unbound = any(explicit_key(r, 'retrieval_id') is None for r in explicit_injected)
            results_known = all(explicit_key(r, 'retrieval_id') is not None or r['payload'].get('returned_count') == 0
                                for r in operations if event(r) != 'MEMORY_RETRIEVAL_STARTED')
            if identified and retrieval_ids <= completed_ids and not unbound and results_known:
                output['M2'] = _result('M2', operations+explicit_injected, len(retrieved_keys & injected_keys), len(retrieved_keys))
            else:
                output['M2'] = _result('M2', operations+explicit_injected,
                    reason='incomplete retrieval result or injection missing explicit operation identity')
        if explicit_injected:
            injection_keys = {key for r in explicit_injected if (key := explicit_key(r, 'round_id')) is not None}
            reference_keys = {key for r in referenced if (key := explicit_key(r, 'round_id')) is not None}
            if all(explicit_key(r, 'round_id') is not None for r in explicit_injected+referenced):
                output['M3'] = _result('M3', explicit_injected+referenced, len(injection_keys & reference_keys), len(injection_keys))
    if retrieved and not current_memory:
        output['M1'] = _result('M1', retrieved, len(retrieved), count=True)
        output['M2'] = _result('M2', retrieved+injected, len({use_key(r) for r in injected}), len({use_key(r) for r in retrieved}))
    if not current_memory and not retrieved and any(event(r) == 'MEMORY_RETRIEVAL_COMPLETED' and r['payload'].get('returned_count') == 0
                             and r['payload'].get('observation_source') == 'rsi-observation/v1' for r in memory):
        output['M1'] = _result('M1', memory, 0, count=True)
        output['M2'] = _result('M2', memory, 0, 0)
    if injected and not current_memory:
        injection_keys = {use_key(r) for r in injected}
        output['M3'] = _result('M3', injected+referenced, len(injection_keys & {use_key(r) for r in referenced}), len(injection_keys))
    completed_ids = {record['payload']['candidate_id'] for record in holdouts}
    checked_uses = {(r['payload'].get('round_id'), r['payload'].get('equivalence_hash'), r['payload'].get('entry_id'), r['payload'].get('candidate_id')) for r in used if r['payload'].get('candidate_id') in completed_ids}
    if used:
        output['M4'] = _result('M4', used+holdouts, sum(key[-1] in verified for key in checked_uses), len(checked_uses))
    pairs = [record for record in memory if event(record) == 'MEMORY_CO_RETRIEVED_PAIR']
    if pairs:
        output['M5'] = _flag_rate('M5', pairs, lambda p: p.get('conflicting'))
    scoped = [record for record in memory if record['payload'].get('entry_fingerprint') and record['payload'].get('current_fingerprint')]
    if scoped:
        output['M6'] = _result('M6', scoped, sum(r['payload']['entry_fingerprint'] != r['payload']['current_fingerprint'] for r in scoped), len(scoped))

    costs = _unique(kinds['CostEvent'], 'request_key')
    summary = {'requests': len(costs) if costs else None, 'observed_request_lower_bound': len(costs),
               'actual_usage': {'input_tokens': None, 'output_tokens': None, 'cached_tokens': None},
               'provider_invoiced_nano': None, 'ledger_upper_nano': None, 'unknown_usage_requests': [],
               'pending_requests': [], 'failed_request_upper_nano': None, 'status': 'UNKNOWN'}
    if costs:
        def sum_known(field, selected=costs):
            values = [record['payload'].get(field) for record in selected]
            return sum(values) if values and all(type(value) is int and value >= 0 for value in values) else None
        for field in summary['actual_usage']:
            summary['actual_usage'][field] = sum_known(field)
        summary['known_usage_subtotal'] = {
            field: {'reported_requests': len(values), 'tokens': sum(values) if values else None}
            for field in summary['actual_usage']
            for values in [[r['payload'][field] for r in costs
                            if type(r['payload'].get(field)) is int and r['payload'][field] >= 0]]}
        summary['known_usage_request_count'] = sum(
            type(r['payload'].get('input_tokens')) is int and type(r['payload'].get('output_tokens')) is int for r in costs)
        summary['ledger_upper_nano'] = sum_known('cost_nano_upper')
        summary['known_ledger_upper_nano'] = sum(r['payload']['cost_nano_upper'] for r in costs if type(r['payload'].get('cost_nano_upper')) is int)
        summary['provider_invoiced_nano'] = sum_known('provider_invoiced_nano')
        summary['unknown_usage_requests'] = sorted(r['payload']['request_key'] for r in costs if r['payload'].get('usage_status') != 'KNOWN')
        unknown_usage = [r for r in costs if r['payload'].get('usage_status') != 'KNOWN']
        summary['unknown_usage_upper_nano'] = sum_known('cost_nano_upper', unknown_usage) if unknown_usage else 0
        summary['pending_requests'] = sorted(r['payload']['request_key'] for r in costs if r['payload'].get('pending') is True)
        failed = [r for r in costs if r['payload'].get('transport_status', r['payload'].get('status'))
            in {'HTTP_ERROR', 'CREDENTIAL_UNAVAILABLE', 'LOCAL_FAILURE', 'TIMEOUT'}
            and r['payload'].get('pending') is not True]
        uncertain = [r for r in costs if r['payload'].get('pending') is True or
            r['payload'].get('transport_status', r['payload'].get('status')) not in
            {'RECEIVED', 'HTTP_ERROR', 'CREDENTIAL_UNAVAILABLE', 'LOCAL_FAILURE', 'TIMEOUT'}]
        summary['failed_request_upper_nano'] = sum_known('cost_nano_upper', failed) if failed else 0
        summary['failed_request_count'] = len(failed)
        summary['failure_count_scope'] = 'CONFIRMED_TRANSPORT_FAILURES_ONLY; uncertain outcomes excluded and separately retained'
        summary['total_failure_count_status'] = 'UNKNOWN' if uncertain else 'KNOWN'
        summary['uncertain_request_keys'] = sorted(r['payload']['request_key'] for r in uncertain)
        summary['uncertain_request_upper_nano'] = sum_known('cost_nano_upper', uncertain) if uncertain else 0
        invoice = summary['provider_invoiced_nano']
        summary['status'] = 'INVOICED' if invoice is not None else 'UNKNOWN' if summary['ledger_upper_nano'] is None else 'BOUNDED_USAGE_ESTIMATE' if summary['unknown_usage_requests'] else 'USAGE_ESTIMATED'
        if invoice is not None:
            output['C1'] = _result('C1', costs, invoice, count=True)
            output['C1']['value'] = str(Decimal(invoice) / Decimal(1_000_000_000))
        valid_denominator = len(valid) if proposals and all(type(r['payload'].get('schema_valid')) is bool for r in proposals) else None
        improvement_denominator = None if improvement_unknown or ambiguous_holdout else len(verified_improvements)
        for mid, denominator in [('C2', valid_denominator), ('C3', output['P5']['numerator'] if complete_rounds else None), ('C4', improvement_denominator)]:
            output[mid] = _result(mid, costs, invoice, denominator,
                status='N/A' if mid == 'C4' and denominator == 0 else None,
                reason='provider invoice required; ledger upper is not actual charge')
            if output[mid]['status'] == 'OK':
                output[mid]['value'] = str(Decimal(invoice) / Decimal(1_000_000_000) / Decimal(denominator))
    else:
        if improvement_unknown or ambiguous_holdout:
            output['C4'] = _result('C4', holdout_records, reason='verified improvement eligibility unknown')
        elif verified_improvements:
            output['C4'] = _result('C4', holdout_records, denominator=len(verified_improvements), reason='provider invoice required')
        else:
            output['C4'] = _result('C4', [], denominator=0, status='N/A', reason='no independently verified improvement')

    stages = kinds['Stage']
    output['R1'] = _flag_rate('R1', stages, lambda p: p.get('failed'))
    recoveries = kinds['Recovery']
    output['R2'] = _flag_rate('R2', recoveries, lambda p: p.get('success'))
    executions = kinds['Execution']
    if executions and all(r['payload'].get('action_id') for r in executions):
        output['R3'] = _result('R3', executions, len(executions)-len({r['payload']['action_id'] for r in executions}), len(executions))
    switches = kinds['Promotion']
    promotions = {r['payload'].get('operation_id') for r in switches if r['payload'].get('status') == 'PROMOTED' and r['payload'].get('operation_id')}
    rollbacks = {r['payload'].get('operation_id') for r in switches if r['payload'].get('status') == 'ROLLED_BACK' and r['payload'].get('operation_id')}
    if switches and all(r['payload'].get('operation_id') for r in switches if r['payload'].get('status') in ('PROMOTED', 'ROLLED_BACK')) and (promotions or rollbacks):
        output['R5'] = _result('R5', switches, len(rollbacks), len(promotions))
    assessed = [r for r in records if r['payload'].get('evidence_contract') and type(r['payload'].get('evidence_complete')) is bool]
    output['R6'] = _flag_rate('R6', assessed, lambda p: not p['evidence_complete'])
    # Current promotion status follows historical terminals, never a registry tip.
    current_by_history = {}
    historically_promoted = defaultdict(set)
    for record in sorted(switches, key=lambda r: (r.get('sequence', r['payload'].get('sequence', 0)), r.get('record_id', r.get('id', '')))):
        p = record['payload']
        # Sequence is meaningful within one history, never across sandboxes.
        history = (p.get('history_source_id', 'UNSPECIFIED'), p.get('history_environment', 'UNSPECIFIED'))
        if p.get('status') == 'PROMOTED' and p.get('candidate_id'):
            historically_promoted[history].add(p['candidate_id'])
            current_by_history[history] = p['candidate_id']
        elif p.get('status') == 'ROLLED_BACK':
            current_by_history.pop(history, None)
            target = p.get('candidate_id')
            if target and target != p.get('rolled_back_candidate_id') and target in historically_promoted[history]:
                current_by_history[history] = target
    current = set(current_by_history.values())
    result = {'metrics': [output[mid] for mid in METRICS], 'cost_breakdown': summary,
            'current_promoted_candidates': sorted(current),
            'current_promoted_histories': [{'source_id': h[0], 'environment': h[1], 'candidate_id': c}
                for h, c in sorted(current_by_history.items())], 'portfolio': analyze_portfolio(records)}
    observed = [r for r in records if r['payload'].get('observation_source') == 'rsi-observation/v1']
    if observed:
        result['observation_coverage'] = {
            'schema_version': 'rsi-observation/v1',
            'source_record_counts': dict(sorted(Counter(r['payload'].get('observation_source', 'LEGACY_UNINSTRUMENTED') for r in records).items())),
            'memory_event_counts': dict(sorted(Counter(event(r) for r in memory).items())),
            'condition_mismatch_count': sum(r['payload'].get('reason') == 'CONDITIONS_MISMATCH' for r in memory),
            'conflicting_pair_count': sum(r['payload'].get('conflicting') is True for r in pairs),
            'observed_pair_count': len(pairs),
            'stale_entry_count': sum(r['payload']['entry_fingerprint'] != r['payload']['current_fingerprint'] for r in scoped),
            'fingerprint_assessed_count': len(scoped),
            'completeness_contracts': sorted({r['payload']['evidence_contract'] for r in assessed}),
            'expected_evidence_count': sum(r['payload'].get('expected_count', 0) for r in assessed),
            'known_evidence_count': sum(r['payload'].get('known_count', 0) for r in assessed),
            'missing_evidence_count': sum(r['payload'].get('missing_count', 0) for r in assessed),
            'interpretation': 'Explicit lifecycle and association observations; no causal or verified credit is inferred.'}
    return result


def compute_metrics(records, *, selection_contract_version=None):
    """Partition by all compatibility axes; missing dimensions remain UNKNOWN."""
    if selection_contract_version not in (None, SELECTION_CONTRACT_VERSION):
        raise ValueError('ANALYTICS_UNKNOWN_SELECTION_CONTRACT')
    records = tuple(records)
    groups = defaultdict(list)
    seen = {}
    for record in records:
        identity = record.get('record_id') or record.get('id') or record.get('event_id')
        key = str(_kind(record)) + ':' + str(identity) if identity is not None else None
        if key is not None:
            serialized = json.dumps(record, sort_keys=True, ensure_ascii=False)
            if key in seen:
                if seen[key] != serialized:
                    raise ValueError('CONFLICTING_PROJECTION_IDENTITY')
                continue
            seen[key] = serialized
        dimensions = record.get('dimensions', {})
        partition = tuple(json.dumps(dimensions.get(name, 'UNKNOWN'), sort_keys=True, ensure_ascii=False) for name in GROUP_DIMENSIONS)
        partition += (json.dumps(record['payload'].get('metric_contract_version', 'LEGACY')),)
        groups[partition].append(record)
    result = []
    for partition in sorted(groups):
        observations = groups[partition]
        group = {'dimensions': dict(zip(GROUP_DIMENSIONS, map(json.loads, partition[:-1]))), **_group_metrics(observations)}
        if selection_contract_version:
            group['selection_metrics'] = analyze_selection(observations)
        contract = json.loads(partition[-1])
        if contract != 'LEGACY':
            group['metric_contract_version'] = contract
            group['metric_definitions'] = {**MEMORY_METRIC_DEFINITIONS, **SELECTION_METRIC_DEFINITIONS}
        group['compatibility_status'] = 'UNKNOWN' if any(value in ('UNKNOWN', None) for value in group['dimensions'].values() if isinstance(value, (str, type(None)))) else 'KNOWN'
        role_groups = defaultdict(list)
        for record in observations:
            if _kind(record) == 'AgentInvocation':
                role_groups[str(record.get('dimensions', {}).get('agent_role', 'UNKNOWN'))].append(record)
        group['agent_roles'] = [{'agent_role': role, 'metrics': [metric for metric in _group_metrics(role_groups[role])['metrics'] if metric['metric_id'].startswith('A')]} for role in sorted(role_groups)]
        result.append(group)
    return {'groups': result, 'portfolio': {'status': 'PARTITIONED',
            'reason': 'portfolio comparisons are restricted to compatibility groups',
            'groups': [{'dimensions': group['dimensions'], 'portfolio': group['portfolio'],
                        **({'metric_contract_version': group['metric_contract_version']} if group.get('metric_contract_version') else {})} for group in result]}}

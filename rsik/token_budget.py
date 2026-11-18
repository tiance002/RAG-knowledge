"""Versioned total-output limits for the Responses API."""
from .contracts import ANGLES
from .state import canonical, digest

LEGACY = 'rsi-token-budget/v1-legacy'
TUNING = 'rsi-token-budget/v2-luna-max'
APPROVED_DEV_V0 = 'rsi-token-budget/v1-dev-luna-max-4096'
PROFILES = {
    LEGACY: {'diagnose': 1024, 'proposer': 3072, 'curator': 2048, 'meta': 2048,
             'format_repair': {'diagnose': 1024, 'proposer': 3072, 'curator': 2048, 'meta': 2048}},
    TUNING: {'diagnose': 8192, 'proposer': 16384, 'curator': 8192, 'meta': 8192,
             'format_repair': {'diagnose': 4096, 'proposer': 8192, 'curator': 4096, 'meta': 4096}},
    APPROVED_DEV_V0: {'diagnose': 1024, 'proposer': 4096, 'curator': 1024, 'meta': 1024,
                      'format_repair': None},
}
PARAMETER = 'max_output_tokens'
MAX_MODEL_OUTPUT = 128_000  # Published Luna and Sol model cap.
CONTEXT_WINDOW = 1_050_000


def validate_output_limit(value):
    if type(value) is not int or not 1 <= value <= MAX_MODEL_OUTPUT:
        raise ValueError('MODEL_OUTPUT_LIMIT_NOT_ALLOWED')
    return value


def role_limit(version, role, *, repair=False):
    if version not in PROFILES:
        raise ValueError('TOKEN_BUDGET_PROFILE_UNKNOWN')
    group = 'proposer' if role in ANGLES else role
    data = PROFILES[version]
    limits = data['format_repair'] if repair else data
    if limits is None or group not in limits or group == 'format_repair':
        raise ValueError('TOKEN_BUDGET_ROLE_UNKNOWN')
    return validate_output_limit(limits[group])


def profile_identity(version):
    if version not in PROFILES:
        raise ValueError('TOKEN_BUDGET_PROFILE_UNKNOWN')
    from copy import deepcopy
    record = {'schema': 'rsi-token-budget/v1', 'version': version, 'endpoint': 'responses',
              'parameter': PARAMETER, 'reasoning_in_total_output': True,
              'requested_limits': deepcopy(PROFILES[version]),
              'luna_proposer_reasoning': 'max' if version != LEGACY else 'none',
              'luna_repair_reasoning': None if version == APPROVED_DEV_V0 else 'none'}
    return {**record, 'sha256': digest(record)}


def request_cost_upper(body, rates, *, max_input_bytes=16_384):
    """Bound Standard short-context, tools-free text requests using wire bytes."""
    import json
    wire_bytes = len(json.dumps(body, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    input_upper = max(wire_bytes, len(canonical(body)))
    limit = validate_output_limit(body.get(PARAMETER))
    if (set(body) & {'max_tokens', 'max_completion_tokens', 'reasoning_budget'}
            or body.get('tools') or body.get('background')
            or body.get('service_tier') != 'default'
            or input_upper > max_input_bytes or input_upper + limit > CONTEXT_WINDOW):
        raise ValueError('REQUEST_OUTSIDE_TOKEN_BUDGET_CONTRACT')
    return input_upper * rates['write'] + limit * rates['output']


def plan_truncated_recovery(budget, request_key, body, receipt, *, new_limit, ceiling_nano):
    """Prepare a fresh, larger request from a known billed truncation.

    Neither the retained partial output nor reasoning is put into the new
    request. The caller must separately authorize the returned frozen plan.
    """
    from .response_audit import OUTPUT_TRUNCATED, inspect
    from .budgets import PRICES

    if type(ceiling_nano) is not int or ceiling_nano <= 0:
        raise ValueError('RECOVERY_CEILING_REQUIRED')
    validate_output_limit(new_limit)
    output, _ = inspect(receipt)
    if output['output_classification'] != OUTPUT_TRUNCATED or new_limit <= body[PARAMETER]:
        raise ValueError('RECOVERY_REQUIRES_CONFIRMED_TRUNCATION_AND_LARGER_LIMIT')
    events = budget.log.read()
    starts = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_STARTED'
              and e['payload'].get('request_key') == request_key]
    ends = [e['payload'] for e in events if e['event_type'] == 'MODEL_REQUEST_COMPLETED'
            and e['payload'].get('request_key') == request_key]
    if (len(starts) != 1 or len(ends) != 1 or starts[0]['request_hash'] != digest(body)
            or ends[0].get('usage_status') != 'KNOWN'
            or ends[0].get('usage') != receipt['response'].get('usage')
            or ends[0].get('provider_request_id') != receipt.get('provider_request_id')):
        raise ValueError('RECOVERY_REQUIRES_BOUND_KNOWN_BILLED_RESPONSE')
    body_copy = {**body, PARAMETER: new_limit}
    upper = request_cost_upper(body_copy, PRICES[body['model']])
    snapshot = budget.snapshot()
    if (upper > ceiling_nano or snapshot['pending_request_keys'] or snapshot['unbounded_usage_requests']
            or snapshot['recorded_cost_nano_upper'] + upper > budget.limit_nano):
        raise ValueError('RECOVERY_COST_OR_LEDGER_BLOCKED')
    id_suffix = digest([request_key, digest(body_copy)])[:32]
    new_request_key = 'recovery-' + id_suffix
    if any(e['payload'].get('request_key') == new_request_key for e in events):
        raise ValueError('RECOVERY_ALREADY_DISPATCHED')
    record = {'schema': 'rsi-token-recovery/v1', 'status': 'AWAITING_EXPLICIT_AUTHORIZATION',
              'strategy': 'FRESH_COMPLETE_ENVELOPE_WITH_LARGER_TOTAL_OUTPUT',
              'original_request_key': request_key, 'request_key': new_request_key,
              'original_request_sha256': digest(body), 'original_receipt_sha256': digest(receipt),
              'request_sha256': digest(body_copy), 'max_output_tokens': new_limit,
              'cost_nano_upper': upper, 'increment_ceiling_nano': ceiling_nano, 'retries': 0,
              'ledger_prefix_sha256': digest(events)}
    return {**record, 'sha256': digest(record)}, body_copy

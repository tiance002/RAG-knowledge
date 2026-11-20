"""Scoped, informational proposal repeat receipts; never changes admission."""
import hashlib
import json
import re

from ..autonomy_contract import _confirmed_negative


UNKNOWN = 'UNKNOWN'
SCHEMA = 'rsi-proposal-repeats/v1'


def _sha(value):
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(',', ':'), allow_nan=False).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _identity(value):
    if isinstance(value, str) and re.fullmatch('[0-9a-fA-F]{64}', value):
        return value.lower()
    return UNKNOWN


def _annotation(value):
    return value if isinstance(value, str) or type(value) is int else UNKNOWN


def _patch_sha(proposal):
    patch = proposal.get('worker_patch')
    if (not isinstance(patch, dict) or set(patch) != {'old', 'new'}
            or any(not isinstance(patch[key], str) or not patch[key] for key in ('old', 'new'))):
        return UNKNOWN
    # Preserve whitespace and anchors: only line-ending differences disappear.
    return _sha({key: patch[key].replace('\r\n', '\n').replace('\r', '\n')
                 for key in ('old', 'new')})


def _feedback(history):
    history = history if isinstance(history, dict) else {}
    result = {key: _annotation(history.get(key)) for key in
              ('outcome', 'quality_verdict', 'execution_status', 'failure_origin')}
    reasons = history.get('reason_codes')
    valid_reasons = (isinstance(reasons, list)
                     and all(isinstance(code, str) and re.fullmatch('[A-Z0-9_]{1,64}', code)
                             for code in reasons))
    result['reason_codes'] = list(reasons) if valid_reasons else UNKNOWN
    required = ('outcome', 'quality_verdict', 'execution_status', 'failure_origin')
    result['confirmed_negative'] = (
        _confirmed_negative(result)
        if valid_reasons and all(result[key] != UNKNOWN for key in required) else UNKNOWN)
    return result


def project_proposal_repeats(proposal_instances, outcomes, *, round_number, scope, prior_rows=()):
    """Project per-instance evidence, comparing only earlier rounds in this scope.

    ``prior_rows`` contains rows from earlier receipts, with their scope hash.
    Counts describe supplied evidence only; they do not reject new proposals.
    A missing fingerprint produces UNKNOWN rather than a zero-repeat claim.
    """
    if type(round_number) is not int or round_number < 1:
        raise ValueError('INVALID_REPEAT_ROUND')
    if not isinstance(scope, dict) or not scope:
        raise ValueError('INVALID_REPEAT_SCOPE')
    scope_sha256 = _sha(scope)
    outcome_slots = {}
    for outcome in outcomes:
        if isinstance(outcome, dict):
            outcome_slots.setdefault(outcome.get('slot'), []).append(outcome)
    rows = []
    for slot, proposal in proposal_instances.items():
        proposal = proposal if isinstance(proposal, dict) else {}
        matches = outcome_slots.get(slot, [])
        outcome = matches[0] if len(matches) == 1 else {}
        rows.append({
            'scope_sha256': scope_sha256, 'round_number': round_number,
            'slot': _annotation(slot), 'proposal_id': _annotation(proposal.get('proposal_id')),
            'round_id': _annotation(proposal.get('round_id')),
            'patch_sha256': _patch_sha(proposal),
            'selector_sha256': _identity(outcome.get('selector_sha256')),
            **_feedback(outcome.get('history')),
        })
    earlier = [row for row in prior_rows if isinstance(row, dict)
               and row.get('scope_sha256') == scope_sha256
               and type(row.get('round_number')) is int
               and 0 < row['round_number'] < round_number]
    for row in rows:
        patch = row['patch_sha256']
        selector = row['selector_sha256']
        exact = [other for other in earlier if patch != UNKNOWN and other.get('patch_sha256') == patch]
        equivalent = [other for other in earlier
                      if patch != UNKNOWN and selector != UNKNOWN
                      and _identity(other.get('selector_sha256')) == selector
                      and _identity(other.get('patch_sha256')) != UNKNOWN
                      and other['patch_sha256'] != patch]
        row['prior_exact_repeat_count'] = len(exact) if patch != UNKNOWN else UNKNOWN
        row['prior_exact_confirmed_failure_count'] = (
            sum(_feedback(other)['confirmed_negative'] is True for other in exact) if patch != UNKNOWN else UNKNOWN)
        ast_known = patch != UNKNOWN and selector != UNKNOWN
        row['prior_ast_equivalent_byte_different_count'] = len(equivalent) if ast_known else UNKNOWN
        row['prior_ast_equivalent_byte_different_confirmed_failure_count'] = (
            sum(_feedback(other)['confirmed_negative'] is True for other in equivalent) if ast_known else UNKNOWN)
        row['same_round_exact_duplicate_count'] = (
            sum(other is not row and other['patch_sha256'] == patch for other in rows)
            if patch != UNKNOWN else UNKNOWN)
        row['same_round_ast_equivalent_byte_different_count'] = (
            sum(other['selector_sha256'] == selector and other['patch_sha256'] != UNKNOWN
                and other['patch_sha256'] != patch for other in rows) if ast_known else UNKNOWN)
    return {'schema': SCHEMA, 'scope_sha256': scope_sha256, 'rows': rows,
            'summary': {
                'proposal_instances': len(rows),
                'known_patch_instances': sum(row['patch_sha256'] != UNKNOWN for row in rows),
                'unknown_patch_instances': sum(row['patch_sha256'] == UNKNOWN for row in rows),
                'instances_with_prior_exact_repeat': sum(
                    isinstance(row['prior_exact_repeat_count'], int) and row['prior_exact_repeat_count'] > 0
                    for row in rows),
                'instances_with_prior_confirmed_failure': sum(
                    isinstance(row['prior_exact_confirmed_failure_count'], int)
                    and row['prior_exact_confirmed_failure_count'] > 0 for row in rows),
                'instances_with_same_round_exact_duplicate': sum(
                    isinstance(row['same_round_exact_duplicate_count'], int)
                    and row['same_round_exact_duplicate_count'] > 0 for row in rows),
            }}

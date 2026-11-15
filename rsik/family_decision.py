"""Immutable fixed-three-slot training decisions, independent of filesystem IO.

The family is the unit of statistical interpretation. A selected candidate is
only a reference to one item; it is never re-tested with the other p values lost.
"""
from copy import deepcopy

from .adjudicate import SLOTS, ALPHA, adjudicate_round
from .state import digest
from .role_profiles import LEGACY,profile_manifest


POLICY = {'version': 'fixed-holm3/v1', 'family_size': 3,
          'alpha': {'numerator': ALPHA.numerator, 'denominator': ALPHA.denominator},
          'scope': 'adaptive exploratory training; not confirmatory significance'}


def make_family(decisions, *, enabled_slots=SLOTS, all_qualified_terminal=True, role_profile=LEGACY, strategy_binding=None):
    """Require explicit missing/disabled/abstaining placeholders for all slots."""
    slots = [d.get('angle') for d in decisions]
    if len(slots) != 3 or set(slots) != set(SLOTS):
        raise ValueError('FAMILY_EXACT_THREE_SLOTS_REQUIRED')
    if len(set(enabled_slots)) != len(enabled_slots) or set(enabled_slots) - set(SLOTS):
        raise ValueError('FAMILY_INVALID_ENABLED_SLOTS')
    ordered = [deepcopy(next(d for d in decisions if d['angle'] == s)) for s in SLOTS]
    ids = [d['candidate_id'] for d in ordered if d.get('candidate_id')]
    if len(ids) != len(set(ids)):
        raise ValueError('FAMILY_DUPLICATE_CANDIDATE_ID')
    for item in ordered:
        if item['angle'] not in enabled_slots and (item.get('candidate_id') is not None
                or item.get('reason_codes') != ['SLOT_DISABLED']):
            raise ValueError('FAMILY_DISABLED_SLOT_NOT_PLACEHOLDER')
    body = {'schema': 'rsi-family-decision/v1', 'policy': deepcopy(POLICY),
            'slots': list(SLOTS), 'enabled_slots': list(enabled_slots), 'role_profile': profile_manifest(role_profile),
            'all_qualified_terminal': bool(all_qualified_terminal), 'strategy_binding': deepcopy(strategy_binding),
            'input_decisions': ordered, 'input_evidence_hashes': {d['angle']: digest(d) for d in ordered},
            'outcome': adjudicate_round(ordered, all_qualified_terminal=all_qualified_terminal)}
    return {**body, 'family_id': digest(body)}


def validate_family(family):
    expected = make_family(family['input_decisions'], enabled_slots=family['enabled_slots'],
                           all_qualified_terminal=family['all_qualified_terminal'],role_profile=family['role_profile']['version'],strategy_binding=family.get('strategy_binding'))
    if expected != family:
        raise ValueError('FAMILY_DECISION_CHANGED')
    return deepcopy(expected['outcome'])


def selected_receipt(family, *, candidate_id, selected_slot):
    outcome = validate_family(family)
    chosen = next((d for d in outcome['candidate_decisions'] if d['angle'] == selected_slot), None)
    if (outcome['adopted_candidate_id'] != candidate_id or chosen is None
            or chosen.get('candidate_id') != candidate_id or chosen['verdict'] != 'VALIDATED'):
        raise ValueError('FAMILY_SELECTED_ITEM_MISMATCH')
    return {**outcome, 'family_id': family['family_id'], 'family_selected_slot': selected_slot}

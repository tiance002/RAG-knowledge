"""Pure deterministic gates, exact clustered sign tests, Holm and selection.

Standard library only. No clock, environment, files, network, LLM or callbacks.
All identities and execution evidence are supplied by the trusted replay reader.
"""
from copy import deepcopy
from fractions import Fraction
from math import comb


SLOTS = ('P1_DEDUP', 'P2_BUDGET', 'P3_ORDER')
ALPHA = Fraction(1, 10)


def sign_test(favorable, unfavorable, ties=0):
    if any(type(n) is not int or n < 0 for n in (favorable, unfavorable, ties)):
        raise ValueError('sign counts must be nonnegative integers')
    n = favorable + unfavorable
    if not n:
        return Fraction(1)
    return min(Fraction(1), Fraction(2 * sum(comb(n, i) for i in range(min(favorable, unfavorable) + 1)), 2**n))


def holm(p_values):
    """Adjusted p in original order. Missing comparisons must be supplied as 1."""
    values = [Fraction(p) for p in p_values]
    if any(p < 0 or p > 1 for p in values):
        raise ValueError('invalid p value')
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    adjusted, previous = [Fraction(1)] * len(values), Fraction(0)
    for rank, i in enumerate(order):
        previous = max(previous, min(Fraction(1), values[i] * (len(values) - rank)))
        adjusted[i] = previous
    return adjusted


def _p_fields(value):
    return {'numerator': value.numerator, 'denominator': value.denominator, 'value': float(value)}


def _counts(deltas):
    return {'favorable': sum(d < 0 for d in deltas), 'unfavorable': sum(d > 0 for d in deltas),
            'ties': sum(d == 0 for d in deltas)}


def evaluate_candidate(identity, rows, eligible_qids, *, total_questions, pre_blocked=0, min_groups=8):
    """G1--G6 only. Statistical acceptance requires the complete fixed family.

    Required row fields: qid, source_group, chars_A/B, parent_facts,
    candidate_facts (nonempty fact_id->status maps), provenance_A/B,
    pair_status, pair_identity, parent_faults, candidate_faults, execution.
    Confirmed defects use fault_origin=CANDIDATE; ambiguous failures stay infra.
    """
    result = {'schema_version': 'rsi-candidate-decision/v2.2', **deepcopy(identity),
              'verdict': 'INCONCLUSIVE', 'reason_codes': [], 'round_stop': False,
              'gates': {f'G{i}': None for i in range(1, 7)}, 'per_question': deepcopy(rows),
              'gates_passed': False, 'sign_p_raw': _p_fields(Fraction(1)),
              'sign_p_adjusted': None, 'delta_total': None, 'reduction_rate': None,
              'eligible_count': len(eligible_qids), 'pre_blocked': pre_blocked,
              'total_questions': total_questions}
    def stop(code, *, reject=False, global_stop=False, gate=None):
        result['reason_codes'].append(code)
        result['verdict'] = 'REJECTED' if reject else 'INCONCLUSIVE'
        result['round_stop'] = global_stop
        if gate:
            result['gates'][gate] = False
        return result
    if (type(total_questions) is not int or type(pre_blocked) is not int or total_questions <= 0
            or pre_blocked < 0 or pre_blocked + len(eligible_qids) != total_questions
            or len(set(eligible_qids)) != len(eligible_qids)):
        return stop('INVALID_FROZEN_PLAN', global_stop=True, gate='G3')
    if 100 * pre_blocked > 10 * total_questions:
        return stop('PRE_BLOCKED_LIMIT', global_stop=True, gate='G3')
    qids = [r.get('qid') for r in rows]
    if len(qids) != len(set(qids)) or set(qids) - set(eligible_qids):
        return stop('INVALID_PAIR', gate='G3')
    # A single failed/unknown parent invalidates every candidate in the round.
    for row in rows:
        parent = row.get('parent_facts', {})
        if (row.get('parent_faults') or row.get('provenance_A') != 'PRESENT'
                or not parent or any(status != 'PRESENT' for status in parent.values())):
            return stop('PARENT_FAULT', global_stop=True, gate='G2')
    # A confirmed local fact loss can reject early, without fabricating full stats.
    for row in rows:
        a, b = row.get('parent_facts', {}), row.get('candidate_facts', {})
        if set(a) == set(b) and any(status == 'ABSENT' for status in b.values()):
            return stop('FACT_LOSS', reject=True, gate='G2')
        if row.get('candidate_faults') and row.get('fault_origin') == 'CANDIDATE':
            return stop('EXEC_FAULT', reject=True, gate='G1')
    if set(qids) != set(eligible_qids):
        return stop('PARTIAL_REPLAY', gate='G3')
    if len({r.get('source_group') for r in rows if r.get('source_group')}) < min_groups:
        return stop('INSUFFICIENT_SOURCE_GROUPS', global_stop=True, gate='G3')
    for row in rows:
        if row.get('execution') != 'COMPLETED' or row.get('pair_status') != 'PAIRED':
            return stop('INFRA_FAILURE' if row.get('fault_origin') != 'CANDIDATE' else 'EXEC_FAULT',
                        reject=row.get('fault_origin') == 'CANDIDATE', gate='G1')
        if row.get('candidate_faults'):
            return stop('UNCLASSIFIED_EXECUTION_FAULT', gate='G1')
        if not row.get('pair_identity'):
            return stop('INVALID_PAIR', gate='G3')
        a, b = row['parent_facts'], row.get('candidate_facts', {})
        if row.get('provenance_B') != 'PRESENT' or set(a) != set(b) or any(v != 'PRESENT' for v in b.values()):
            return stop('FACT_OR_PROVENANCE_UNKNOWN', gate='G2')
        if any(type(row.get(k)) is not int or row[k] < 0 for k in ('chars_A', 'chars_B')):
            return stop('INVALID_CHARACTER_COUNT', gate='G3')
    result['gates'].update(G1=True, G2=True, G3=True)
    if any(100 * row['chars_B'] > 102 * row['chars_A'] for row in rows):
        return stop('LOCAL_REGRESSION', reject=True, gate='G4')
    result['gates']['G4'] = True
    deltas = [row['chars_B'] - row['chars_A'] for row in rows]
    total_a, delta = sum(row['chars_A'] for row in rows), sum(deltas)
    result.update(delta_total=delta, reduction_rate=float(Fraction(-delta, total_a)) if total_a else None,
                  question_counts=_counts(deltas))
    if not total_a:
        return stop('INCONCLUSIVE_BASE_ZERO', gate='G6')
    if not any(d < 0 for d in deltas):
        return stop('NO_GAIN', reject=True, gate='G5')
    result['gates']['G5'] = True
    if delta >= 0:
        return stop('NO_NET_GAIN', reject=True, gate='G6')
    result['gates']['G6'] = True
    groups = {}
    for row in rows:
        group = row['source_group']
        groups[group] = groups.get(group, 0) + row['chars_B'] - row['chars_A']
    counts = _counts(groups.values())
    p = sign_test(counts['favorable'], counts['unfavorable'], counts['ties'])
    worst = max(Fraction(row['chars_B'] - row['chars_A'], row['chars_A']) if row['chars_A'] else Fraction(0)
                for row in rows)
    result.update(verdict='OBSERVED', gates_passed=True, reason_codes=['STATISTICS_PENDING_FAMILY'],
                  source_group_count=len(groups), group_counts=counts, group_deltas=groups,
                  sign_p_raw=_p_fields(p), worst_relative_delta={'numerator': worst.numerator,
                                                                'denominator': worst.denominator})
    return result


def _fraction(fields):
    return Fraction(fields['numerator'], fields['denominator'])


def adjudicate_round(decisions, *, all_qualified_terminal=True, parent_consistent=True):
    """Always use P1/P2/P3 as the three-comparison family, including duplicates."""
    items = deepcopy(decisions)
    if len({item['angle'] for item in items}) != len(items) or any(item['angle'] not in SLOTS for item in items):
        raise ValueError('one independent decision per proposer slot required')
    family = {item['angle']: item for item in items}
    observed_parents, observed_candidates = {}, {}
    candidate_consistent = True
    for item in items:
        for row in item.get('per_question', []):
            if row.get('context_A_sha256'):
                value = (row.get('chars_A'), row['context_A_sha256'], row.get('provenance_A'),row.get('execution_environment_hash'))
                if row['qid'] in observed_parents and observed_parents[row['qid']] != value:
                    parent_consistent = False
                observed_parents[row['qid']] = value
            if row.get('context_B_sha256') and item.get('worker_hash') and row.get('upstream_hash'):
                key = (item['worker_hash'], item.get('config_hash'), row['qid'], row['upstream_hash'])
                value = (row.get('chars_B'), row['context_B_sha256'], row.get('provenance_B'),row.get('execution_environment_hash'))
                if key in observed_candidates and observed_candidates[key] != value:
                    candidate_consistent = False
                observed_candidates[key] = value
    if (not all_qualified_terminal or not parent_consistent or not candidate_consistent
            or any(item.get('round_stop') for item in items)):
        return {'verdict': 'ROUND_ABORTED', 'adopted_proposal_id': None, 'adopted_candidate_id': None,
                'candidate_decisions': items, 'family_size': 3,
                'reason': 'INCOMPLETE_CANDIDATES_OR_UNTRUSTED_PARENT_OR_NONDETERMINISM'}
    p_values = [_fraction(family[s]['sign_p_raw']) if s in family and family[s].get('gates_passed') else Fraction(1)
                for s in SLOTS]
    corrected = holm(p_values)
    for index, slot in enumerate(SLOTS):
        if slot not in family:
            continue
        item = family[slot]
        item['sign_p_adjusted'] = _p_fields(corrected[index])
        if item.get('gates_passed'):
            item['verdict'] = 'VALIDATED' if corrected[index] <= ALPHA else 'OBSERVED'
            item['reason_codes'] = ['TRAINING_GATES_AND_STATISTICS_PASSED' if item['verdict'] == 'VALIDATED'
                                    else 'STATISTICAL_EVIDENCE_INSUFFICIENT']
    eligible = [item for item in items if item['verdict'] == 'VALIDATED']
    chosen = min(eligible, key=lambda item: (item['delta_total'], _fraction(item['worst_relative_delta']),
                                           item['candidate_id'], SLOTS.index(item['angle']))) if eligible else None
    return {'verdict': 'ADOPT_CANDIDATE' if chosen else 'NO_ADOPTION',
            'adopted_proposal_id': chosen['proposal_id'] if chosen else None,
            'adopted_candidate_id': chosen['candidate_id'] if chosen else None,
            'candidate_decisions': items, 'family_size': 3, 'alpha': _p_fields(ALPHA),
            'statistical_scope': 'adaptive exploratory screen; not population significance or answer quality'}


def adjudicate_holdout(parent_comparison, trusted_comparison):
    """Two comparisons even if their A is shared; never shrink the Holm family."""
    items = deepcopy([parent_comparison, trusted_comparison])
    corrected = holm([_fraction(item['sign_p_raw']) if item.get('gates_passed') else Fraction(1) for item in items])
    for item, p in zip(items, corrected, strict=True):
        item['sign_p_adjusted'] = _p_fields(p)
        if item.get('gates_passed'):
            item['verdict'] = 'VALIDATED' if p <= ALPHA else 'OBSERVED'
    regression = any(item['verdict'] == 'REJECTED' and set(item['reason_codes']) &
                     {'FACT_LOSS', 'LOCAL_REGRESSION', 'EXEC_FAULT'} for item in items)
    passed = all(item['verdict'] == 'VALIDATED' and not item.get('round_stop') for item in items)
    return {'verdict': 'VERIFIED' if passed else 'REGRESSION_SUSPECTED' if regression else 'HOLDOUT_INCONCLUSIVE',
            'comparisons': items, 'family_size': 2, 'production_approved': False}

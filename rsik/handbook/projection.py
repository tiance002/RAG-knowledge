"""Rebuild states and single-count outcome episodes from immutable events."""
from collections import defaultdict


CLASSES = ('pending', 'helpful', 'miss', 'harmful', 'excluded')


def project(events, *, evidence_mode='REAL_REPLAY'):
    states, episodes, outcomes = {}, {}, defaultdict(dict)
    for event in events:
        kind, payload = event['event_type'], event['payload']
        if payload.get('evidence_mode',evidence_mode)!=evidence_mode:continue
        if payload.get('mode')=='SIMULATION_ONLY':
            continue  # sandbox switches never create real helpful/harmful credit
        entry = payload.get('entry_id')
        if kind in {'ENTRY_CREATED', 'ENTRY_REVISED'}:
            states[entry] = 'DRAFT'
        elif kind == 'ENTRY_QUARANTINED': states[entry] = 'QUARANTINED'
        elif kind == 'ENTRY_ACTIVATED': states[entry] = 'ACTIVE'
        elif kind == 'ENTRY_DEPRECATED': states[entry] = 'DEPRECATED'
        if kind == 'PROPOSAL_REFERENCED':
            key = (payload['round_id'], payload['equivalence_hash'], entry)
            episode = episodes.setdefault(key, {'candidate_ids': set(), 'proposal_ids': set()})
            episode['candidate_ids'].add(payload['candidate_id'])
            episode['proposal_ids'].add(payload['proposal_id'])
        candidate = payload.get('candidate_id')
        if not candidate:
            continue
        outcome = outcomes[candidate]
        if kind == 'CANDIDATE_ADOPTED': outcome['adopted'] = True
        elif kind == 'HOLDOUT_PASSED': outcome['verified'] = True
        elif kind in {'CANDIDATE_REJECTED', 'CANDIDATE_NOT_SELECTED'}: outcome['miss'] = True
        elif kind == 'HOLDOUT_FAILED':
            if payload.get('reason') in {'INFRA_FAILURE', 'UNKNOWN', 'PARTIAL_REPLAY'}: outcome['excluded'] = True
            else: outcome['miss'] = True
        elif kind in {'OUTCOME_INVALIDATED', 'CANDIDATE_INCONCLUSIVE'}: outcome['excluded'] = True
        elif kind == 'PROMOTED': outcome.update(promoted=True, stable=False)
        elif kind == 'STABLE': outcome['stable'] = True
        elif kind == 'ROLLED_BACK': outcome.update(rollback=True, attribution=payload.get('attribution', 'UNKNOWN'))
    counts = {entry: {name: 0 for name in CLASSES} for entry in states}
    details = []
    for (round_id, equivalent, entry), episode in episodes.items():
        counts.setdefault(entry, {name: 0 for name in CLASSES})
        adopted = [cid for cid in episode['candidate_ids'] if outcomes[cid].get('adopted')]
        if len(adopted) > 1:
            raise ValueError('AMBIGUOUS_EPISODE_ADOPTION')
        if not adopted:
            label = 'miss' if any(outcomes[cid].get('miss') for cid in episode['candidate_ids']) else 'excluded'
        else:
            outcome = outcomes[adopted[0]]
            if outcome.get('excluded'): label = 'excluded'
            elif outcome.get('promoted') and outcome.get('rollback'):
                label = 'harmful' if outcome.get('attribution') == 'RELATED' else 'excluded'
            elif outcome.get('miss'): label = 'miss'
            elif outcome.get('promoted'): label = 'helpful' if outcome.get('stable') else 'pending'
            elif outcome.get('verified'): label = 'helpful'
            else: label = 'pending'
        counts[entry][label] += 1
        details.append({'round_id': round_id, 'equivalence_hash': equivalent, 'entry_id': entry,
                        'classification': label, 'proposal_count': len(episode['proposal_ids'])})
    return {'states': states, 'counts': counts, 'episodes': details,
            'interpretation': 'verified association counts only, not causal effectiveness or success rate'}

"""Persistent parent eligibility and sealed-use state; owner USD budget controls calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .events import EventLog
from .state import digest


class LineagePolicy:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.log = EventLog(self.directory / 'lineage.sqlite')

    def state(self, lineage_id):
        starts, outcomes, frozen, holdout = {}, {}, False, {}
        for event in self.log.read():
            p = event['payload']
            if p.get('lineage_id') != lineage_id: continue
            kind = event['event_type']
            if kind == 'ROUND_STARTED': starts[p['round_id']] = p
            elif kind == 'ROUND_FINISHED': outcomes[p['round_id']] = p
            elif kind == 'LINEAGE_FROZEN': frozen = True
            elif kind == 'HOLDOUT_STARTED': holdout[p['candidate_id']] = 'PENDING'
            elif kind in {'HOLDOUT_PASSED', 'HOLDOUT_FAILED', 'HOLDOUT_UNKNOWN'}:
                holdout[p['candidate_id']] = kind
                if kind == 'HOLDOUT_FAILED': frozen = True
        streak = 0
        for round_id in starts:
            if round_id not in outcomes: break
            outcome = outcomes[round_id]
            if outcome.get('complete_trusted_no_gain'):
                streak += 1
            elif outcome['observed_effective_gain']:
                streak = 0
        return {'rounds': len(starts), 'pending_rounds': sorted(set(starts)-set(outcomes)),
                'no_gain_streak': streak, 'frozen': frozen, 'holdout': holdout,
                'round_limit': None, 'no_gain_action': 'DIAGNOSE_AND_ADJUST' if streak>=3 else None,
                'statistical_scope': 'unlimited adaptive exploration; no population-significance claim',
                'stop_reason': 'LINEAGE_FROZEN' if frozen else None}

    def start_round(self, lineage_id, round_id, *, input_hash):
        state = self.state(lineage_id)
        if state['stop_reason'] or state['pending_rounds']:
            raise RuntimeError(state['stop_reason'] or 'ROUND_STATUS_UNKNOWN')
        self.log.append('start:'+round_id, 'ROUND_STARTED', {'lineage_id': lineage_id,
            'round_id': round_id, 'input_hash': input_hash})

    def finish_round(self, lineage_id, round_id, result, *, result_hash):
        if not any(e['event_type']=='ROUND_STARTED' and e['payload']['round_id']==round_id
                   and e['payload']['lineage_id']==lineage_id for e in self.log.read()):
            raise ValueError('UNBOUND_ROUND_RESULT')
        gain = any(d.get('gates_passed') and (d.get('delta_total') or 0)<0
                   for d in result.get('candidate_decisions', []))
        evaluated = [d for d in result.get('candidate_decisions', []) if d.get('per_question')]
        complete_no_gain = (bool(evaluated) and len(evaluated)==len(result.get('qualified',[]))
            and set(result.get('qualified',[]))==set(result.get('attempted_replays',[]))) and all(
            set(d.get('reason_codes', [])) <= {'NO_GAIN','NO_NET_GAIN'}
            and bool(d.get('reason_codes')) and len(d['per_question']) == d.get('eligible_count')
            and all(row.get('pair_status')=='PAIRED' and row.get('pair_identity')
                    and row.get('provenance_A')==row.get('provenance_B')=='PRESENT'
                    for row in d['per_question']) for d in evaluated)
        self.log.append('finish:'+round_id, 'ROUND_FINISHED', {'lineage_id':lineage_id,
            'round_id':round_id, 'result_hash':result_hash, 'observed_effective_gain':bool(gain),
            'complete_trusted_no_gain':complete_no_gain, 'verdict':result['verdict']})

    def claim_holdout(self, lineage_id, candidate_id, *, holdout_freeze_hash):
        state = self.state(lineage_id)
        used=any(e['event_type']=='HOLDOUT_STARTED' and e['payload'].get('holdout_freeze_hash')==holdout_freeze_hash
                 for e in self.log.read())
        if state['frozen'] or candidate_id in state['holdout'] or used:
            raise RuntimeError('SEALED_HOLDOUT_ALREADY_USED_OR_LINEAGE_FROZEN')
        self.log.append('holdout:'+candidate_id, 'HOLDOUT_STARTED', {'lineage_id':lineage_id,
            'candidate_id':candidate_id, 'holdout_freeze_hash':holdout_freeze_hash})

    def finish_holdout(self, lineage_id, candidate_id, verdict, *, receipt_hash):
        if self.state(lineage_id)['holdout'].get(candidate_id) != 'PENDING':
            raise RuntimeError('HOLDOUT_NOT_PENDING')
        kind = 'HOLDOUT_PASSED' if verdict=='VERIFIED' else 'HOLDOUT_FAILED' if verdict in {'REJECTED','REGRESSION_SUSPECTED'} else 'HOLDOUT_UNKNOWN'
        self.log.append('holdout-finish:'+candidate_id, kind, {'lineage_id':lineage_id,
            'candidate_id':candidate_id, 'receipt_hash':receipt_hash})


class ParentPool:
    """Trusted receipt-bound admission, deterministic Pareto front and tie seed."""
    def __init__(self, directory):
        self.log = EventLog(Path(directory) / 'parents.sqlite')

    def admit(self, candidate_id, lineage_id, receipt_path, *, kind, chars, replay_seconds):
        if kind not in {'VERIFIED', 'EXPLORATION', 'BASELINE_ANCHOR'}:
            raise ValueError('INVALID_POOL')
        data = Path(receipt_path).read_bytes();receipt = json.loads(data)
        if kind=='VERIFIED' and receipt.get('verdict')!='VERIFIED': raise ValueError('UNVERIFIED_PARENT')
        if kind=='EXPLORATION' and receipt.get('verdict') not in {'VALIDATED','ADOPT_CANDIDATE'}: raise ValueError('UNVALIDATED_PARENT')
        if kind=='BASELINE_ANCHOR' and not (receipt.get('status')=='PASS' and receipt.get('paired')==20 and receipt.get('failed')==0):
            raise ValueError('BASELINE_NOT_PROVEN')
        if type(chars) is not int or chars<=0 or type(replay_seconds) not in (float,int) or replay_seconds<=0:
            raise ValueError('INVALID_PARENT_METRICS')
        self.log.append('parent:'+candidate_id+':'+kind, 'PARENT_ADMITTED', {'candidate_id':candidate_id,
            'lineage_id':lineage_id, 'kind':kind, 'receipt_path':str(Path(receipt_path).resolve()),
            'receipt_hash':hashlib.sha256(data).hexdigest(), 'chars':chars, 'replay_seconds':replay_seconds})

    def select(self, policy, *, exploration=False, seed=0, allow_bootstrap=False):
        candidates = {}
        quarantined={e['payload']['candidate_id'] for e in self.log.read() if e['event_type']=='PARENT_QUARANTINED'}
        for event in self.log.read():
            if event['event_type'] != 'PARENT_ADMITTED': continue
            p=event['payload']
            allowed = p['kind']=='VERIFIED' or exploration and p['kind']=='EXPLORATION' or allow_bootstrap and p['kind']=='BASELINE_ANCHOR'
            if not allowed or p['candidate_id'] in quarantined or policy.state(p['lineage_id'])['stop_reason']: continue
            data=Path(p['receipt_path']).read_bytes()
            if hashlib.sha256(data).hexdigest()!=p['receipt_hash']: raise RuntimeError('PARENT_RECEIPT_CHANGED')
            candidates[p['candidate_id']]=p
        values=list(candidates.values())
        front=[p for p in values if not any(q['chars']<=p['chars'] and q['replay_seconds']<=p['replay_seconds']
               and (q['chars'],q['replay_seconds'])!=(p['chars'],p['replay_seconds']) for q in values)]
        if not front: return None
        return min(front,key=lambda p:digest({'seed':seed,'candidate_id':p['candidate_id']}))

    def require_verified(self, candidate_id, policy):
        if any(e['event_type']=='PARENT_QUARANTINED' and e['payload']['candidate_id']==candidate_id for e in self.log.read()):
            raise ValueError('PARENT_QUARANTINED')
        records=[e['payload'] for e in self.log.read() if e['event_type']=='PARENT_ADMITTED'
                 and e['payload']['candidate_id']==candidate_id and e['payload']['kind']=='VERIFIED']
        if len(records)!=1:raise ValueError('PARENT_NOT_IN_VERIFIED_POOL')
        p=records[0];data=Path(p['receipt_path']).read_bytes();receipt=json.loads(data)
        if (hashlib.sha256(data).hexdigest()!=p['receipt_hash'] or receipt.get('verdict')!='VERIFIED'
                or receipt.get('candidate_id')!=candidate_id or policy.state(p['lineage_id'])['frozen']):
            raise ValueError('PARENT_VERIFICATION_INVALID')
        return p

    def quarantine(self,candidate_id,receipt_path,reason):
        data=Path(receipt_path).read_bytes()
        self.log.append('quarantine:'+candidate_id,'PARENT_QUARANTINED',{'candidate_id':candidate_id,
            'reason':reason,'receipt_path':str(Path(receipt_path).resolve()),'receipt_hash':hashlib.sha256(data).hexdigest()})

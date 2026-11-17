"""Persistent parent eligibility and sealed-use state; owner USD budget controls calls."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .events import EventLog
from .observability import observation_for
from .state import digest, canonical
from .locking import atomic_write


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
    def __init__(self, directory, *, evidence_mode='REAL_REPLAY'):
        if evidence_mode not in {'REAL_REPLAY','SYNTHETIC_CONTROL'}:
            raise ValueError('INVALID_POOL_EVIDENCE_MODE')
        self.directory, self.evidence_mode = Path(directory), evidence_mode
        self.scope_path = self.directory/'parent-pool-scope.json'
        if self.scope_path.exists() and json.loads(self.scope_path.read_bytes())['evidence_mode'] != evidence_mode:
            raise ValueError('PARENT_POOL_SCOPE_MISMATCH')
        self.log = EventLog(self.directory / 'parents.sqlite')
        if evidence_mode=='SYNTHETIC_CONTROL' and not self.scope_path.exists() and any(
                event.get('payload',{}).get('evidence_mode')!='SYNTHETIC_CONTROL' for event in self.log.read()):
            raise ValueError('PARENT_POOL_SCOPE_MISMATCH')

    def _real_admission(self, candidate_id, receipt_path, receipt, kind, *, registry, fixture, evidence):
        from .execution_evidence import resolve_policy
        from .experiment import read_decision
        from .adjudicate import adjudicate_holdout
        from .holdout import verify_training_adoption
        policy=resolve_policy(evidence)
        if (not policy.real_eligible or receipt.get('evidence_mode')!='REAL_REPLAY'
                or receipt.get('real_eligible') is not True or receipt.get('execution_policy')!=policy.to_dict()):
            raise ValueError('REAL_PARENT_EVIDENCE_REQUIRED')
        if kind=='BASELINE_ANCHOR':
            raise ValueError('BASELINE_EVIDENCE_MIGRATION_REQUIRED')
        if registry is None or fixture is None:
            raise ValueError('PARENT_REPLAY_RECOMPUTATION_REQUIRED')
        if kind=='EXPLORATION':
            return verify_training_adoption(Path(receipt_path).parent, fixture, registry, candidate_id)
        if receipt.get('candidate_id')!=candidate_id:
            raise ValueError('PARENT_CANDIDATE_MISMATCH')
        from .experiment import registry_api
        candidate=registry_api.candidate(Path(registry),candidate_id)
        if receipt.get('parent_id')!=candidate.get('parent'):
            raise ValueError('PARENT_CANDIDATE_MISMATCH')
        if receipt.get('shared_physical_comparison') and receipt.get('trusted_id')!=receipt.get('parent_id'):
            raise ValueError('PARENT_COMPARISON_IDENTITY_MISMATCH')
        decisions=[]
        for name in ('vs-parent','vs-trusted'):
            run=Path(receipt_path).parent/name
            if name=='vs-trusted' and receipt.get('shared_physical_comparison'):
                decisions.append(decisions[0]);continue
            manifest=json.loads((run/'manifest.json').read_bytes());spec=manifest['arms']['B']
            anchor_id=receipt['parent_id'] if name=='vs-parent' else receipt['trusted_id']
            anchor=registry_api.candidate(Path(registry),anchor_id)
            arm_a=manifest['arms']['A']
            if (spec['candidate_id']!=candidate_id or spec['worker_sha256']!=candidate['worker']
                    or spec['sha256']!=candidate['config'] or arm_a['candidate_id']!=anchor_id
                    or arm_a['worker_sha256']!=anchor['worker'] or arm_a['sha256']!=anchor['config']):
                raise ValueError('PARENT_CANDIDATE_MISMATCH')
            decisions.append(read_decision(run,fixture,candidate_id,
                {'angle':'P2_BUDGET','proposal_id':manifest['schema']},spec['worker_sha256'],spec['sha256'],evidence=policy))
        if adjudicate_holdout(*decisions)['verdict']!='VERIFIED':
            raise ValueError('PARENT_VERIFICATION_NOT_REPRODUCIBLE')
        return digest([d['execution_evidence_hash'] for d in decisions])

    def _check_candidate(self, record):
        data=Path(record['receipt_path']).read_bytes()
        if hashlib.sha256(data).hexdigest()!=record['receipt_hash']:
            raise RuntimeError('PARENT_RECEIPT_CHANGED')
        if self.evidence_mode=='REAL_REPLAY':
            proof=self._real_admission(record['candidate_id'],record['receipt_path'],json.loads(data),
                record['kind'],registry=record.get('registry_path'),fixture=record.get('fixture_path'),evidence=None)
            if proof!=record.get('recomputed_evidence_hash'):
                raise ValueError('PARENT_EXECUTION_EVIDENCE_CHANGED')
        return json.loads(data)

    def admit(self, candidate_id, lineage_id, receipt_path, *, kind, chars, replay_seconds,
              registry=None, fixture=None, evidence=None):
        if kind not in {'VERIFIED', 'EXPLORATION', 'BASELINE_ANCHOR'}:
            raise ValueError('INVALID_POOL')
        data = Path(receipt_path).read_bytes();receipt = json.loads(data)
        proof=None
        if self.evidence_mode=='REAL_REPLAY':
            proof=self._real_admission(candidate_id,receipt_path,receipt,kind,
                registry=registry,fixture=fixture,evidence=evidence)
        if not self.scope_path.exists():
            atomic_write(self.scope_path,canonical({'schema':'rsi-parent-pool-scope/v2.3',
                'evidence_mode':self.evidence_mode}),exclusive=True)
        if kind=='VERIFIED' and receipt.get('verdict')!='VERIFIED': raise ValueError('UNVERIFIED_PARENT')
        if kind=='EXPLORATION' and receipt.get('verdict') not in {'VALIDATED','ADOPT_CANDIDATE'}: raise ValueError('UNVALIDATED_PARENT')
        if kind=='BASELINE_ANCHOR' and not (receipt.get('status')=='PASS' and receipt.get('paired')==20 and receipt.get('failed')==0):
            raise ValueError('BASELINE_NOT_PROVEN')
        if type(chars) is not int or chars<=0 or type(replay_seconds) not in (float,int) or replay_seconds<=0:
            raise ValueError('INVALID_PARENT_METRICS')
        self.log.append('parent:'+candidate_id+':'+kind, 'PARENT_ADMITTED', {'candidate_id':candidate_id,
            'lineage_id':lineage_id, 'kind':kind, 'evidence_mode':self.evidence_mode,
            'admission_version':'rsi-parent-admission/v2.3', 'recomputed_evidence_hash':proof,
            'registry_path':str(Path(registry).resolve()) if registry else None,
            'fixture_path':str(Path(fixture).resolve()) if fixture else None, 'receipt_path':str(Path(receipt_path).resolve()),
            'receipt_hash':hashlib.sha256(data).hexdigest(), 'chars':chars, 'replay_seconds':replay_seconds})

    def select(self, policy, *, exploration=False, seed=0, allow_bootstrap=False, round_id=None, observer=None):
        observer = observer or observation_for(self)
        attempt = 1 + sum(e['event_type'] == 'PARENT_SELECTION_COMPLETED' for e in observer.log.read())
        selection_id = digest({'round_id': round_id or 'UNKNOWN', 'seed': seed,
            'exploration': exploration, 'allow_bootstrap': allow_bootstrap, 'attempt': attempt})
        common = {'selection_id': selection_id, 'round_id': round_id or 'UNKNOWN', 'attempt': attempt,
            'policy_version': 'rsi-parent-pareto-seeded/v2.3', 'seed': seed,
            'exploration': exploration, 'allow_bootstrap': allow_bootstrap, 'evidence_mode': self.evidence_mode}
        candidates = {}
        quarantined={e['payload']['candidate_id'] for e in self.log.read() if e['event_type']=='PARENT_QUARANTINED'}
        for event in self.log.read():
            if event['event_type'] != 'PARENT_ADMITTED': continue
            p=event['payload']
            payload = {**common, 'candidate_id': p['candidate_id'], 'lineage_id': p['lineage_id'], 'admitted': True}
            reason = None
            if p.get('evidence_mode')!=self.evidence_mode or p.get('admission_version')!='rsi-parent-admission/v2.3':
                reason = 'HISTORICAL_PROOF_UNQUALIFIED'
            else:
                allowed = p['kind']=='VERIFIED' or exploration and p['kind']=='EXPLORATION' or allow_bootstrap and p['kind']=='BASELINE_ANCHOR'
                if not allowed: reason = 'KIND_NOT_ENABLED'
                elif p['candidate_id'] in quarantined: reason = 'PARENT_QUARANTINED'
                elif policy.state(p['lineage_id'])['stop_reason']: reason = 'LINEAGE_STOPPED'
            if reason is not None:
                observer.emit('PARENT_ELIGIBILITY_CHECKED', {**payload, 'eligible': False})
                observer.emit('PARENT_REJECTED', {**payload, 'eligible': False, 'reason': reason})
                continue
            try:
                self._check_candidate(p)
            except (ValueError, RuntimeError, OSError):
                observer.emit('PARENT_ELIGIBILITY_CHECKED', {**payload, 'eligible': False, 'reason': 'RECEIPT_INVALID'})
                observer.emit('PARENT_REJECTED', {**payload, 'eligible': False, 'reason': 'RECEIPT_INVALID'})
                raise
            observer.emit('PARENT_ELIGIBILITY_CHECKED', {**payload, 'eligible': True})
            candidates[p['candidate_id']]=p
        values=list(candidates.values())
        front=[p for p in values if not any(q['chars']<=p['chars'] and q['replay_seconds']<=p['replay_seconds']
               and (q['chars'],q['replay_seconds'])!=(p['chars'],p['replay_seconds']) for q in values)]
        ranked = sorted(front,key=lambda p:digest({'seed':seed,'candidate_id':p['candidate_id']}))
        for p in values:
            if p not in front:
                observer.emit('PARENT_REJECTED', {**common, 'candidate_id': p['candidate_id'], 'admitted': True,
                    'eligible': True, 'reason': 'PARETO_DOMINATED'})
        for rank, p in enumerate(ranked, 1):
            observer.emit('PARENT_RANKED', {**common, 'candidate_id': p['candidate_id'], 'admitted': True,
                'eligible': True, 'rank': rank, 'chars': p['chars'], 'replay_seconds': p['replay_seconds']})
        if not front:
            observer.emit('PARENT_SELECTION_COMPLETED', {**common, 'population_count': len(values), 'returned_count': 0})
            return None
        selected = min(front,key=lambda p:digest({'seed':seed,'candidate_id':p['candidate_id']}))
        observer.emit('PARENT_SELECTED', {**common, 'candidate_id': selected['candidate_id'], 'parent_id': selected['candidate_id'],
            'selected': True, 'admitted': True, 'eligible': True, 'rank': 1})
        observer.emit('PARENT_SELECTION_COMPLETED', {**common, 'population_count': len(values), 'returned_count': 1})
        return selected

    def require_verified(self, candidate_id, policy):
        if any(e['event_type']=='PARENT_QUARANTINED' and e['payload']['candidate_id']==candidate_id for e in self.log.read()):
            raise ValueError('PARENT_QUARANTINED')
        records=[e['payload'] for e in self.log.read() if e['event_type']=='PARENT_ADMITTED'
                 and e['payload']['candidate_id']==candidate_id and e['payload']['kind']=='VERIFIED'
                 and e['payload'].get('evidence_mode')==self.evidence_mode
                 and e['payload'].get('admission_version')=='rsi-parent-admission/v2.3']
        if len(records)!=1:raise ValueError('PARENT_NOT_IN_VERIFIED_POOL')
        p=records[0];receipt=self._check_candidate(p)
        if (receipt.get('verdict')!='VERIFIED'
                or receipt.get('candidate_id')!=candidate_id or policy.state(p['lineage_id'])['frozen']):
            raise ValueError('PARENT_VERIFICATION_INVALID')
        return p

    def quarantine(self,candidate_id,receipt_path,reason):
        data=Path(receipt_path).read_bytes()
        self.log.append('quarantine:'+candidate_id,'PARENT_QUARANTINED',{'candidate_id':candidate_id,
            'reason':reason,'receipt_path':str(Path(receipt_path).resolve()),'receipt_hash':hashlib.sha256(data).hexdigest()})

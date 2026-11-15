"""Authenticated *simulation-only* approval/CAS/rollback exercise.

There is deliberately no production adapter. Production needs a real identity
provider, deployment interface and separate owner approval; this local HMAC
authority must never be presented as human-authenticated production approval.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import secrets
import time
import uuid

from .events import EventLog, canonical
from .locking import WorkspaceLock, atomic_write


class PromotionSandbox:
    def __init__(self, directory, *, allowed_root, clock=time.time):
        self.directory, base = Path(directory).resolve(), Path(allowed_root).resolve()
        if not self.directory.is_relative_to(base) or self.directory == base:
            raise ValueError('sandbox must be a dedicated child of the allowed experiment root')
        self.directory.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        owner = self.directory / 'simulation-authority.json'
        if owner.exists():
            record = json.loads(owner.read_text())
            if record['mode'] != 'SIMULATION_ONLY':
                raise ValueError('not a promotion simulation directory')
        else:
            record = {'mode': 'SIMULATION_ONLY', 'environment': 'isolated:' + uuid.uuid4().hex,
                      'hmac_key': secrets.token_hex(32)}
            atomic_write(owner, canonical(record), exclusive=True)
        self.environment = record['environment']
        self.key = bytes.fromhex(record['hmac_key'])
        self.events = EventLog(self.directory / 'events.sqlite')

    def _artifact(self, data):
        digest = hashlib.sha256(data).hexdigest()
        path = self.directory / 'artifacts' / digest
        if path.exists():
            if path.read_bytes() != data:
                raise ValueError('ARTIFACT_HASH_MISMATCH')
        else:
            atomic_write(path, data, exclusive=True)
        return digest

    def initialize(self, version_id, artifact):
        with WorkspaceLock(self.directory / 'deployment.lock', 'simulation-initialize'):
            value = {'version_id': version_id, 'artifact_hash': self._artifact(artifact),
                     'environment': self.environment, 'mode': 'SIMULATION_ONLY',
                     'deployment_revision':0,'operation_id':'initialize:'+uuid.uuid4().hex}
            atomic_write(self.directory / 'current.json', canonical(value), exclusive=True)
            return value

    def current(self):
        value = json.loads((self.directory / 'current.json').read_text())
        if (value.get('mode') != 'SIMULATION_ONLY' or value.get('environment') != self.environment
                or not self._digest_valid(value.get('artifact_hash'))
                or type(value.get('deployment_revision')) is not int or value['deployment_revision']<0
                or not isinstance(value.get('operation_id'),str) or not value['operation_id']):
            raise ValueError('CURRENT_VERSION_IDENTITY_INVALID')
        return value

    @staticmethod
    def _digest_valid(value):
        return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)

    def approval(self, candidate_id, artifact_hash, expected_current_version, *, operation='PROMOTE', ttl=60):
        if operation not in {'PROMOTE', 'ROLLBACK'} or ttl <= 0 or ttl > 3600:
            raise ValueError('invalid simulation approval scope')
        if not self._digest_valid(artifact_hash):
            raise ValueError('invalid approved artifact digest')
        current=self.current()
        payload = {'schema':'rsi-approval-operation/v2.3','operation_id':uuid.uuid4().hex,
                   'expected_deployment_revision':current['deployment_revision'],
                   'candidate_id': candidate_id, 'artifact_hash': artifact_hash,
                   'expected_current_version': expected_current_version, 'target_environment': self.environment,
                   'expected_current_artifact_hash': current['artifact_hash'],
                   'operation': operation, 'expiry': self.clock() + ttl, 'mode': 'SIMULATION_ONLY',
                   'identity_kind': 'SIMULATED_LOCAL_OPERATOR', 'human_authenticated': False}
        return {'payload': payload, 'signature': hmac.new(self.key, canonical(payload), hashlib.sha256).hexdigest()}

    def _authorize(self, approval, operation, *,check_current=True):
        payload = approval['payload']
        signature = hmac.new(self.key, canonical(payload), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, approval['signature']):
            raise ValueError('APPROVAL_SIGNATURE_INVALID')
        if (payload['mode'] != 'SIMULATION_ONLY' or payload['target_environment'] != self.environment
                or payload['operation'] != operation or (check_current and payload['expiry'] <= self.clock())
                or payload.get('schema')!='rsi-approval-operation/v2.3'
                or type(payload.get('expected_deployment_revision')) is not int
                or not isinstance(payload.get('operation_id'),str) or not payload['operation_id']):
            raise ValueError('APPROVAL_EXPIRED_OR_WRONG_SCOPE')
        current = self.current()
        if check_current and (current['version_id'] != payload['expected_current_version']
                or current['artifact_hash'] != payload['expected_current_artifact_hash']
                or current['deployment_revision']!=payload['expected_deployment_revision']):
            raise ValueError('EXPECTED_CURRENT_VERSION_CHANGED')
        return payload

    def promote(self, approval, artifact, *, quality, rollback_hash):
        with WorkspaceLock(self.directory / 'deployment.lock', 'simulation-promote'):
            payload=self._authorize(approval,'PROMOTE',check_current=False)
            if quality != 'VERIFIED':
                raise ValueError('CANDIDATE_NOT_VERIFIED')
            if hashlib.sha256(artifact).hexdigest() != payload['artifact_hash']:
                raise ValueError('APPROVED_ARTIFACT_CHANGED')
            if rollback_hash!=payload['expected_current_artifact_hash']:raise ValueError('ROLLBACK_TARGET_UNAVAILABLE')
            if self._has_intent(approval['signature']):return self._reconcile_switch(approval['signature'])
            payload = self._authorize(approval, 'PROMOTE')
            previous = self.current()
            rollback = self.directory / 'artifacts' / rollback_hash
            if (rollback_hash != previous['artifact_hash'] or not rollback.is_file()
                    or hashlib.sha256(rollback.read_bytes()).hexdigest() != rollback_hash):
                raise ValueError('ROLLBACK_TARGET_UNAVAILABLE')
            self._artifact(artifact)
            value = {'version_id': payload['candidate_id'], 'artifact_hash': payload['artifact_hash'],
                     'environment': self.environment, 'mode': 'SIMULATION_ONLY',
                     'deployment_revision':previous['deployment_revision']+1,'operation_id':payload['operation_id']}
            self.events.append('promote-intent:' + approval['signature'], 'PROMOTION_INTENT',
                               {'mode': 'SIMULATION_ONLY', 'approval': approval, 'previous': previous, 'next': value})
            atomic_write(self.directory / 'current.json', canonical(value))
            if self.current() != value:
                raise RuntimeError('DEPLOYMENT_RESULT_UNKNOWN')
            self.events.append('promoted:' + approval['signature'], 'PROMOTED',
                               {'mode': 'SIMULATION_ONLY', 'previous': previous, 'current': value})
            return {'status': 'PROMOTED', 'mode': 'SIMULATION_ONLY', 'production_changed': False, 'current': value}

    def rollback(self, approval):
        with WorkspaceLock(self.directory / 'deployment.lock', 'simulation-rollback'):
            self._authorize(approval,'ROLLBACK',check_current=False)
            if self._has_intent(approval['signature']):return self._reconcile_switch(approval['signature'])
            payload = self._authorize(approval, 'ROLLBACK')
            path = self.directory / 'artifacts' / payload['artifact_hash']
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != payload['artifact_hash']:
                raise ValueError('ROLLBACK_TARGET_UNAVAILABLE')
            previous = self.current()
            value = {'version_id': payload['candidate_id'], 'artifact_hash': payload['artifact_hash'],
                     'environment': self.environment, 'mode': 'SIMULATION_ONLY',
                     'deployment_revision':previous['deployment_revision']+1,'operation_id':payload['operation_id']}
            self.events.append('rollback-intent:' + approval['signature'], 'ROLLBACK_INTENT',
                               {'mode': 'SIMULATION_ONLY', 'approval': approval, 'previous': previous, 'next': value})
            atomic_write(self.directory / 'current.json', canonical(value))
            if self.current() != value:
                raise RuntimeError('ROLLBACK_RESULT_UNKNOWN')
            self.events.append('rolled-back:' + approval['signature'], 'ROLLED_BACK',
                               {'mode': 'SIMULATION_ONLY', 'previous': previous, 'current': value})
            return {'status': 'ROLLED_BACK', 'mode': 'SIMULATION_ONLY', 'production_changed': False, 'current': value}

    def _has_intent(self, signature):
        return any(e['event_type'] in {'PROMOTION_INTENT','ROLLBACK_INTENT'}
                   and e['payload']['approval']['signature']==signature for e in self.events.read())

    def reconcile_switch(self, signature):
        """Reconcile historical operation identity, never repeat a pointer write."""
        with WorkspaceLock(self.directory/'deployment.lock','simulation-reconcile'):
            return self._reconcile_switch(signature)

    def _reconcile_switch(self, signature):
        events=self.events.read()
        intents=[e for e in events if e['event_type'] in {'PROMOTION_INTENT','ROLLBACK_INTENT'}
                 and e['payload']['approval']['signature']==signature]
        if len(intents)!=1:raise RuntimeError('SWITCH_INTENT_UNKNOWN')
        event=intents[0];record=event['payload'];approval=record['approval'];payload=approval['payload']
        self._authorize(approval,payload['operation'],check_current=False)
        if (record['next']['artifact_hash']!=payload['artifact_hash']
                or record['next']['version_id']!=payload['candidate_id']
                or record['previous']['version_id']!=payload['expected_current_version']
                or record['previous']['artifact_hash']!=payload['expected_current_artifact_hash']
                or record['previous']['deployment_revision']!=payload['expected_deployment_revision']
                or record['next']['deployment_revision']!=record['previous']['deployment_revision']+1
                or record['next']['operation_id']!=payload['operation_id']):
            raise RuntimeError('SWITCH_INTENT_INVALID')
        for value in (record['previous'],record['next']):
            artifact=self.directory/'artifacts'/value['artifact_hash']
            if not artifact.is_file() or hashlib.sha256(artifact.read_bytes()).hexdigest()!=value['artifact_hash']:
                raise RuntimeError('SWITCH_ARTIFACT_UNKNOWN')
        current=self.current()
        rollback=payload['operation']=='ROLLBACK';status='ROLLED_BACK' if rollback else 'PROMOTED'
        completed=any(e['event_type']==status and e['idempotency_key']==('rolled-back:' if rollback else 'promoted:')+signature for e in events)
        if current==record['previous'] and not completed:
            return {'status':'INTENT_NOT_APPLIED','mode':'SIMULATION_ONLY','production_changed':False,
                    'operation_id':payload['operation_id']}
        # A later trusted intent can prove a preceding pointer write even if its
        # terminal event was interrupted. Revisions prevent A->B->A ambiguity.
        observed=record['next']
        for later in events:
            if later['sequence']<=event['sequence'] or later['event_type'] not in {'PROMOTION_INTENT','ROLLBACK_INTENT'}:continue
            step=later['payload']
            if step['previous']==observed:
                if step['next']['deployment_revision']!=observed['deployment_revision']+1:
                    raise RuntimeError('SWITCH_REVISION_CHAIN_INVALID')
                observed=step['next']
        if current!=record['next'] and (current!=observed or current['deployment_revision']<=record['next']['deployment_revision']):
            raise RuntimeError('SWITCH_RESULT_UNKNOWN')
        # Terminal receipt always binds the original switch, not the present tip.
        self.events.append(('rolled-back:' if rollback else 'promoted:')+signature,status,
            {'mode':'SIMULATION_ONLY','previous':record['previous'],'current':record['next']})
        checkpoint={'status':status,'mode':'SIMULATION_ONLY','production_changed':False,'current':record['next']}
        target=self.directory/'switch-checkpoints'/(signature+'.json')
        if target.exists():
            if json.loads(target.read_text())!=checkpoint:raise RuntimeError('SWITCH_CHECKPOINT_CHANGED')
        else:atomic_write(target,canonical(checkpoint),exclusive=True)
        return {**checkpoint,'status':status if current==record['next'] else 'APPLIED_SUPERSEDED',
                'operation_status':status,'operation_id':payload['operation_id'],'observed_current':current}


def production_promote(*args, **kwargs):
    raise PermissionError('PRODUCTION_ADAPTER_UNAVAILABLE: real identity, candidate-specific owner approval and rollback evidence required')

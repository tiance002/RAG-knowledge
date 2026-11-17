"""Hash-bound checkpoints and intent/receipt reconciliation; no blind retries."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import time

from .locking import atomic_write


QUALITY = {'PROPOSED': {'READY', 'REJECTED'}, 'READY': {'REJECTED', 'OBSERVED', 'VALIDATED'},
           'VALIDATED': {'HOLDOUT_PENDING'}, 'HOLDOUT_PENDING': {'VERIFIED', 'REGRESSION_SUSPECTED', 'HOLDOUT_INCONCLUSIVE'},
           'HOLDOUT_INCONCLUSIVE': {'HOLDOUT_PENDING'}}
EXECUTION = {'PENDING': {'RUNNING', 'CANCELLED'}, 'RUNNING': {'COMPLETED', 'FAILED', 'CANCELLED', 'RECOVERABLE'},
             'RECOVERABLE': {'RUNNING', 'FAILED', 'CANCELLED'}}
DEPLOYMENT = {'NOT_PROMOTED': {'PROMOTION_PENDING'}, 'PROMOTION_PENDING': {'PROMOTED', 'NOT_PROMOTED'},
              'PROMOTED': {'STABLE', 'ROLLED_BACK'}, 'STABLE': {'ROLLED_BACK'}}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def transition(dimension, old, new, *, evidence, quality=None, approval_valid=False):
    if not evidence:
        raise ValueError('state changes require evidence')
    table = {'quality': QUALITY, 'execution': EXECUTION, 'deployment': DEPLOYMENT}[dimension]
    if dimension == 'quality' and new == 'INVALIDATED':
        return new
    if new not in table.get(old, set()):
        raise ValueError('illegal state transition')
    if dimension == 'deployment' and new == 'PROMOTION_PENDING' and (quality != 'VERIFIED' or not approval_valid):
        raise ValueError('verified quality and specific human approval required')
    return new


class StageJournal:
    def __init__(self, directory, *, observer=None, run_id=None):
        self.directory = Path(directory)
        self.observer = observer
        self.run_id = run_id or digest({"stage_directory": str(self.directory.resolve())})

    def _path(self, stage, kind):
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', stage):
            raise ValueError('invalid stage identifier')
        return self.directory / stage / (kind + '.json')

    @staticmethod
    def _read(path):
        return json.loads(path.read_text(encoding='utf-8'))

    def execute(self, stage, inputs, action, reconcile, *, crash_at=None):
        """Observe actual attempts without changing reconciliation or callback policy."""
        from .observability import observation_for
        observer = observation_for(self)
        operation_id = digest({'stage': stage, 'input_hash': digest(inputs)})
        prior = observer.log.read()
        attempt = 1 + sum(e['event_type'] == 'STAGE_STARTED' and
                          e['payload'].get('operation_id') == operation_id for e in prior)
        attempt_id = digest({'operation_id': operation_id, 'attempt': attempt})
        base = {'stage_id': stage, 'stage_attempt_id': attempt_id, 'run_id': self.run_id,
                'operation_id': operation_id, 'attempt': attempt, 'input_hash': digest(inputs),
                'checkpoint_id': digest({'operation_id': operation_id, 'kind': 'checkpoint'}),
                'output_hash': None, 'failure_category': 'NONE'}
        recovering = self._path(stage, 'intent').exists() or self._path(stage, 'checkpoint').exists()
        recovery = {**base, 'recovery_attempt_id': attempt_id,
                    'recovery_operation_id': operation_id, 'original_operation_id': operation_id}
        observer.emit('STAGE_STARTED', base, key=attempt_id + ':start')
        if recovering:
            observer.emit('RECOVERY_STARTED', recovery, key=attempt_id + ':recovery-start')
        clock = time.monotonic()
        def observed_action(key):
            observer.emit('EXECUTION_STARTED', {**base, 'action_id': key,
                'actual_execution_id': digest({'stage_attempt_id': attempt_id, 'action_id': key})},
                key=attempt_id + ':execution')
            return action(key)
        try:
            result = self._execute(stage, inputs, observed_action, reconcile, crash_at=crash_at)
        except BaseException as error:
            cancelled = isinstance(error, (KeyboardInterrupt, SystemExit))
            category, recoverable = observation_failure(error)
            terminal = {**base, 'duration_ms': (time.monotonic() - clock) * 1000,
                        'failure_category': category, 'recoverable': recoverable,
                        'error_type': type(error).__name__, 'outcome': 'CANCELLED' if cancelled else 'FAILED'}
            observer.emit('STAGE_CANCELLED' if cancelled else 'STAGE_FAILED', terminal,
                          key=attempt_id + ':terminal')
            if recovering:
                observer.emit('RECOVERY_BLOCKED' if category == 'EVIDENCE_INTEGRITY' else 'RECOVERY_FAILED',
                              {**recovery, **terminal}, key=attempt_id + ':recovery-terminal')
            raise
        terminal = {**base, 'output_hash': digest(result[0]), 'outcome': 'COMPLETED',
                    'duration_ms': (time.monotonic() - clock) * 1000}
        observer.emit('STAGE_COMPLETED', terminal, key=attempt_id + ':terminal')
        if recovering:
            observer.emit('RECOVERY_COMPLETED', {**recovery, **terminal}, key=attempt_id + ':recovery-terminal')
        return result

    def _execute(self, stage, inputs, action, reconcile, *, crash_at=None):
        """Caller holds workspace/run locks. action/reconcile are trusted adapters.

        Reconcile returns (ABSENT, safe_retry=True), (MATCHED, output=value), or
        UNKNOWN/CONFLICT. A model's proposed status is never passed here.
        """
        input_hash = digest(inputs)
        intent_path, receipt_path, checkpoint_path = (self._path(stage, k) for k in ('intent', 'receipt', 'checkpoint'))
        action_key = digest({'stage': stage, 'input_hash': input_hash})
        if checkpoint_path.exists():
            checkpoint, receipt = self._read(checkpoint_path), self._read(receipt_path)
            if (checkpoint['input_hash'] != input_hash or receipt['input_hash'] != input_hash
                    or checkpoint['output_hash'] != digest(receipt['output'])
                    or checkpoint['receipt_hash'] != digest(receipt)):
                raise RuntimeError('RECOVERY_ERROR: completed stage differs from evidence')
            target = reconcile(action_key)
            if target.get('status') != 'MATCHED' or digest(target['output']) != checkpoint['output_hash']:
                raise RuntimeError('RECOVERY_ERROR: committed side effect no longer matches')
            return receipt['output'], True
        if intent_path.exists():
            intent = self._read(intent_path)
            if intent['input_hash'] != input_hash or intent['idempotency_key'] != action_key:
                raise RuntimeError('RECOVERY_ERROR: intent input changed')
        else:
            intent = {'stage_id': stage, 'input_hash': input_hash, 'idempotency_key': action_key,
                      'status': 'RUNNING', 'attempt': 1, 'started_at': datetime.now(timezone.utc).isoformat()}
            atomic_write(intent_path, canonical(intent), exclusive=True)
            if crash_at == 'after_intent':
                raise RuntimeError('INJECTED_CRASH_AFTER_INTENT')
        target = reconcile(action_key)
        if receipt_path.exists():
            receipt = self._read(receipt_path)
            if (receipt['input_hash'] != input_hash or target.get('status') != 'MATCHED'
                    or digest(target['output']) != receipt['output_hash']
                    or digest(receipt['output']) != receipt['output_hash']):
                raise RuntimeError('RECOVERY_ERROR: receipt/target mismatch')
            output = receipt['output']
        else:
            if target.get('status') == 'MATCHED':
                output = target['output']
            elif target.get('status') == 'ABSENT' and target.get('safe_retry') is True:
                output = action(action_key)
            else:
                raise RuntimeError('RECOVERY_ERROR: side effect is unknown or conflicting')
            if crash_at == 'after_action':
                raise RuntimeError('INJECTED_CRASH_AFTER_ACTION')
            receipt = {'stage_id': stage, 'input_hash': input_hash, 'idempotency_key': action_key,
                       'output': output, 'output_hash': digest(output), 'status': 'COMPLETED'}
            atomic_write(receipt_path, canonical(receipt), exclusive=True)
        if crash_at == 'after_receipt':
            raise RuntimeError('INJECTED_CRASH_AFTER_RECEIPT')
        checkpoint = {**intent, 'status': 'COMPLETED', 'output_hash': digest(output),
                      'receipt_hash': digest(receipt),
                      'completed_at': datetime.now(timezone.utc).isoformat(), 'error': None}
        atomic_write(checkpoint_path, canonical(checkpoint), exclusive=True)
        if crash_at == 'after_checkpoint':
            raise RuntimeError('INJECTED_CRASH_AFTER_CHECKPOINT')
        return output, target.get('status') == 'MATCHED'


def observation_failure(error):
    if isinstance(error, (KeyboardInterrupt, SystemExit)):
        return 'CANCELLED', False
    category = getattr(error, 'category', None)
    if category in {'PATCH_ERROR', 'SEAM_ERROR'}:
        return 'CANDIDATE_QUALITY', False
    if category in {'STORAGE_ERROR', 'REPLAY_ERROR', 'TIMEOUT'} or isinstance(error, (OSError, TimeoutError)):
        return 'INFRASTRUCTURE', True
    if category == 'RECOVERY_ERROR' or str(error).startswith(('RECOVERY_ERROR', 'RECOVERY_BLOCKED', 'EXTERNAL_EXPERIENCE')):
        return 'EVIDENCE_INTEGRITY', False
    if str(error).startswith('INJECTED_CRASH'):
        return 'RECOVERABLE', True
    return 'UNRECOVERABLE', False

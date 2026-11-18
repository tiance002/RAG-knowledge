"""Public runtime metadata, separate from billing and model-visible inputs.

Payload v1 lives inside the unchanged rsi-event/v2.2 envelope. Only new runtime
observations belong here; this module never imports or copies historical facts.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from pathlib import Path
import re
import sqlite3

from .events import EventLog, digest

VERSION = 'rsi-observation/v1'
METRIC_CONTRACT_VERSION = 'rsi-metrics/m1a1'
FIELDS = frozenset('''experiment_id epoch evaluator_version profile role_profile_sha256 run_id round_id parent_id candidate_id agent_role role role_profile
role_profile_hash model execution_mode evidence_mode seam_profile prompt_hash request_hash
response_hash request_key cost_request_key cost_source invocation_id attempt_id attempt status outcome contract_result
failure_origin proposal_instance_id proposal_id slot family_id hypothesis_hash expected_impact_hash
patch_hash worker_hash worker_sha256 normalized_ast_sha256 entry_id entry_ids retrieval_id section format rank reason reason_codes
compatibility_fingerprint selection_id policy_version seed exploration allow_bootstrap lineage_id
admitted eligible selected chars replay_seconds population_count returned_count filtered_count
entry_fingerprint current_fingerprint static_pass pair_entry_ids scope_hash scope used conflicting stage stage_id stage_key stage_status source_id source_hash owner_id
observation_id evidence_id evidence_refs support_refs parent_alias entry_state previous_state
new_state from_state to_state source_event_id source_event_type event_key action decision
selected_entry_ids retrieved_entry_ids learning_frontier schema_version metric_contract_version transport_status
fallback_profile fallback_version dispatch_sha256 catalog_sha256 dispatcher_version mode task_type task_id task_contract_hash
stage_attempt_id operation_id input_hash output_hash checkpoint_id failure_category recoverable error_type recovery_attempt_id recovery_operation_id original_operation_id actual_execution_id action_id error_hash http_status repair_of request_attempt relation lineage_state count total duration_ms
token_budget_profile token_budget_sha256 requested_output_tokens output_classification provider_response_status provider_finish_reason provider_incomplete_reason reasoning_tokens visible_output_tokens visible_output_tokens_upper visible_message_received structured_json_complete
'''.split())
_IDENTIFIER = re.compile(r'[A-Za-z0-9_.:/+@=-]{1,256}\Z')


def _public(payload):
    if not isinstance(payload, dict) or set(payload) - FIELDS:
        raise ValueError('OBSERVATION_FIELD_NOT_ALLOWED')
    def scalar(value, field):
        if value is None or type(value) in (bool, int): return value
        if type(value) is float:
            import math
            if math.isfinite(value): return value
        if isinstance(value, str):
            if _IDENTIFIER.fullmatch(value): return value
            if field.endswith(('_id', '_ids')): return digest(value)
        if isinstance(value, (list, tuple)) and len(value) <= 256:
            return [scalar(v, field) for v in value]
        raise ValueError('OBSERVATION_VALUE_NOT_PUBLIC_METADATA')
    return {k: scalar(v, k) for k, v in payload.items()}


class Observer:
    def __init__(self, path, context=None):
        self.path = Path(path)
        try:
            self.log = EventLog(self.path)
        except (OSError, sqlite3.Error) as error:
            from .errors import StageFailure
            raise StageFailure('STORAGE_ERROR', 'OBSERVATION_STORAGE_UNAVAILABLE') from error
        self.context = {}
        self.update_context(context or {})

    def update_context(self, context=None, **kwargs):
        self.context.update(_public({**(context or {}), **kwargs}))
        return self

    def emit(self, event_type, payload, key=None):
        if not isinstance(event_type, str) or not re.fullmatch('[A-Z][A-Z0-9_]*', event_type):
            raise ValueError('OBSERVATION_EVENT_TYPE_INVALID')
        value = _public({**self.context, **payload, 'schema_version': VERSION,
                         'metric_contract_version': METRIC_CONTRACT_VERSION})
        identity = key if key is not None else digest({'event_type': event_type, 'payload': value})
        # Keys may be private paths; publish only their stable digest.
        try:
            return self.log.append('observation:' + digest(str(identity)), event_type, value, actor='observer')
        except (OSError, sqlite3.Error) as error:
            from .errors import StageFailure
            raise StageFailure('STORAGE_ERROR', 'OBSERVATION_STORAGE_UNAVAILABLE') from error

    def invocation(self, payload):
        return InvocationObservation(self, payload)


def observation_for(owner):
    if isinstance(owner, Observer): return owner
    existing = getattr(owner, 'observer', None)
    if existing is not None: return existing
    path = Path(owner) if isinstance(owner, (str, Path)) else Path(owner.directory)
    observer = Observer(path / 'observations.sqlite')
    if not isinstance(owner, (str, Path)): owner.observer = observer
    return observer


class InvocationObservation(AbstractContextManager):
    """Observe an actual dispatch/recovery without influencing its policy."""
    def __init__(self, observer, payload):
        self.observer = observer
        self.payload = dict(payload)
        self.started = False
        self.response_observed = False
        self.outcome = 'INFRA_FAILURE'
        self.failure_origin = 'INFRASTRUCTURE'
        self.contract_result = 'NOT_EVALUATED'

    def _emit(self, kind, **extra):
        # This lifecycle is required evidence: storage uncertainty stops dispatch.
        try:
            return self.observer.emit(kind, {**self.payload, **extra},
                                      key=self.payload['invocation_id'] + ':' + kind)
        except (OSError, sqlite3.Error) as error:
            from .errors import StageFailure
            raise StageFailure('STORAGE_ERROR', 'OBSERVATION_STORAGE_UNAVAILABLE') from error

    def start(self):
        self._emit('INVOCATION_STARTED')
        self.started = True

    def resume(self, receipt):
        # A cached historical receipt is not a new runtime invocation. Resume
        # only a lifecycle this sidecar observed at its original dispatch.
        try:
            events = [e for e in self.observer.log.read() if
                      e['payload'].get('invocation_id') == self.payload['invocation_id']]
        except (OSError, sqlite3.Error) as error:
            from .errors import StageFailure
            raise StageFailure('STORAGE_ERROR', 'OBSERVATION_STORAGE_UNAVAILABLE') from error
        kinds = {e['event_type'] for e in events}
        self.response_observed = 'RESPONSE_RECEIVED' in kinds
        if 'INVOCATION_FINISHED' in kinds and receipt.get('transport_status') == 'RECEIVED':
            response_hash = digest(receipt.get('response', {}))
            if not any(e['event_type'] == 'RESPONSE_RECEIVED' and
                       e['payload'].get('response_hash') == response_hash for e in events):
                from .errors import StageFailure
                raise StageFailure('STORAGE_ERROR', 'OBSERVATION_RESPONSE_EVIDENCE_MISSING')
        self.started = 'INVOCATION_STARTED' in kinds and 'INVOCATION_FINISHED' not in kinds

    def received(self, receipt, output=None):
        if not self.started: return
        status = receipt.get('transport_status')
        if output is not None:
            # Billed input/output totals remain in the cost ledger.
            self.payload.update({k: v for k, v in output.items() if k not in {'input_tokens', 'output_tokens'}})
        if status == 'RECEIVED':
            self.payload['response_hash'] = digest(receipt.get('response', {}))
            if not self.response_observed:
                self._emit('RESPONSE_RECEIVED')
                self.response_observed = True
        elif status == 'TIMEOUT': self.outcome, self.failure_origin = 'TIMEOUT', 'TRANSPORT'
        elif status == 'HTTP_ERROR': self.outcome, self.failure_origin = 'API_ERROR', 'PROVIDER'
        elif status == 'CANCELLED': self.outcome, self.failure_origin = 'CANCELLED', 'CANCELLATION'

    def output_failure(self, reason, *, origin='PROVIDER'):
        if self.started:
            self.outcome, self.failure_origin = reason, origin
            self.payload['output_classification'] = reason

    def validated(self, valid, *, abstained=False):
        if not self.started: return
        self.contract_result = 'VALID' if valid else 'INVALID'
        self.outcome = ('ABSTAINED' if abstained else 'SUCCESS') if valid else 'CONTRACT_ERROR'
        self.failure_origin = 'NONE' if valid else 'CONTRACT'
        self._emit('CONTRACT_VALIDATED', contract_result=self.contract_result)

    def __exit__(self, exc_type, exc, traceback):
        if self.started:
            if exc_type is not None:
                if isinstance(exc, (OSError, sqlite3.Error)) and not isinstance(exc, TimeoutError) or getattr(exc, "category", None) == "STORAGE_ERROR":
                    # Leave the lifecycle incomplete. A saved receipt can restore
                    # missing observations without sending the model request again.
                    return False
                elif issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
                    self.outcome, self.failure_origin = 'CANCELLED', 'CANCELLATION'
                elif getattr(exc, "category", None) == "CANCELLED":
                    self.outcome, self.failure_origin = "CANCELLED", "CANCELLATION"
                elif issubclass(exc_type, TimeoutError) or getattr(exc, "category", None) == "TIMEOUT":
                    self.outcome, self.failure_origin = 'TIMEOUT', 'TRANSPORT'
            self._emit('INVOCATION_FINISHED', outcome=self.outcome,
                       contract_result=self.contract_result, failure_origin=self.failure_origin)
        return False

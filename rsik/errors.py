"""Safe normalized errors; raw stacks stay in separately restricted evidence."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import uuid


CATEGORIES = {'INPUT_ERROR', 'CONTRACT_ERROR', 'AGENT_ERROR', 'PATCH_ERROR', 'SEAM_ERROR',
              'REGISTRY_ERROR', 'MATERIALIZE_ERROR', 'REPLAY_ERROR', 'EVALUATION_ERROR',
              'JUDGE_ERROR', 'STORAGE_ERROR', 'TIMEOUT', 'CANCELLED', 'RECOVERY_ERROR'}
GLOBAL_CATEGORIES = {'REGISTRY_ERROR', 'JUDGE_ERROR', 'STORAGE_ERROR', 'RECOVERY_ERROR'}


@dataclass(frozen=True)
class RSIError:
    error_id: str
    round_id: str
    stage: str
    category: str
    reason_code: str
    retryable: bool
    attempt: int
    scope: str
    timestamp: str
    message_safe: str
    evidence_refs: tuple = ()
    original_error_ref: str | None = None
    proposal_id: str | None = None
    candidate_id: str | None = None
    execution_id: str | None = None

    def to_dict(self):
        return asdict(self)


def normalized_error(round_id, stage, category, reason_code, *, scope='CANDIDATE', retryable=False,
                     attempt=1, evidence_refs=(), original_error_ref=None):
    if category not in CATEGORIES or scope not in {'AGENT', 'CANDIDATE', 'ROUND', 'SYSTEM'}:
        raise ValueError('unsupported normalized error category/scope')
    if not reason_code or any(c not in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ_0123456789' for c in reason_code):
        raise ValueError('reason code must be an enum, never raw error text')
    return RSIError('error-' + uuid.uuid4().hex, round_id, stage, category, reason_code, retryable,
                    attempt, scope, datetime.now(timezone.utc).isoformat(), category + ':' + reason_code,
                    tuple(evidence_refs), original_error_ref)


class StageFailure(RuntimeError):
    def __init__(self, category, reason_code, *, global_stop=None):
        if category not in CATEGORIES:
            raise ValueError('invalid failure category')
        self.category, self.reason_code = category, reason_code
        self.global_stop = category in GLOBAL_CATEGORIES if global_stop is None else global_stop
        super().__init__(reason_code)

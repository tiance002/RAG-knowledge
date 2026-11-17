"""Host-selected execution evidence policy, separate from untrusted captures.

Synthetic controls can exercise mathematical decisions but confer no real replay
or parent-pool eligibility.  A capture/manifest's self-reported mode is ignored.
"""
from dataclasses import dataclass
import hashlib
import json
import subprocess
from pathlib import Path

from .errors import StageFailure


# Existing archived runtime identity; no claim that a new remote run occurred.
# A runtime update requires a new reviewed policy version, not TOFU from output.
APPROVED_RUNTIME = {
    "python": "3.12.3", "uid": 65534, "packages": {
        "httpx": "0.28.1", "langgraph": "1.0.5", "openai": "1.109.1",
        "pandas": "2.3.3", "pydantic": "2.12.5", "sqlalchemy": "2.0.45",
        "sqlmodel": "0.0.27"}}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


@dataclass(frozen=True)
class ExecutionEvidence:
    mode: str
    adapter_sha256: str | None = None
    archive_sha256: str | None = None
    image: str | None = None
    base_commit: str | None = None
    runtime_sha256: str | None = None

    @classmethod
    def synthetic_control(cls):
        return cls('SYNTHETIC_CONTROL')

    @classmethod
    def real(cls, business_root=None):
        # Trusted code derives the archive independently of capture metadata.
        from .capsule import ROOT, BASE_COMMIT, IMAGE, source_archive
        from .foundation import verify_boundary
        verify_boundary()
        data = source_archive(Path(business_root) if business_root else ROOT)
        return cls('REAL_REPLAY', sha((ROOT/'rsik/capsule.py').read_bytes()), sha(data), IMAGE, BASE_COMMIT, sha(canonical(APPROVED_RUNTIME)))

    def __post_init__(self):
        if self.mode not in {'REAL_REPLAY', 'SYNTHETIC_CONTROL'}:
            raise ValueError('UNKNOWN_EXECUTION_EVIDENCE_MODE')
        if self.mode == 'REAL_REPLAY' and (not all(self.__dict__.values())
                or any(len(value) != 64 for value in (self.adapter_sha256, self.archive_sha256, self.runtime_sha256))):
            raise ValueError('UNPINNED_EXECUTION_EVIDENCE_POLICY')

    @property
    def real_eligible(self):
        return self.mode == 'REAL_REPLAY'

    def projection(self):
        return dict(self.__dict__)

    def to_dict(self):
        return self.projection()


def resolve_policy(evidence):
    if evidence is not None:
        if not isinstance(evidence, ExecutionEvidence):
            raise StageFailure('EVALUATION_ERROR', 'UNTRUSTED_EXECUTION_POLICY', global_stop=True)
        return evidence
    try:
        return ExecutionEvidence.real()
    except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        raise StageFailure('EVALUATION_ERROR', 'EXECUTION_POLICY_UNAVAILABLE', global_stop=True) from error


def check_receipt(policy, path, *, stage, worker_hash, input_path, capture_path,
                  config_path=None, canonical_config_hash=None, runtime_identity=None):
    """Require each planned stage; never treat absent proof as candidate failure."""
    path = Path(path)
    if not path.is_file():
        if policy.real_eligible:
            raise StageFailure('EVALUATION_ERROR', 'EXECUTION_EVIDENCE_MISSING', global_stop=True)
        return runtime_identity
    try:
        receipt = json.loads(path.read_text(encoding='utf-8-sig'))
        raw_config_hash = sha(Path(config_path).read_bytes()) if config_path else None
        if (receipt.get('worker_sha256') != worker_hash
                or receipt.get('input_sha256') != sha(Path(input_path).read_bytes())
                or receipt.get('config_sha256') != raw_config_hash):
            raise ValueError('identity')
        if config_path and sha(canonical(json.loads(Path(config_path).read_bytes()))) != canonical_config_hash:
            raise ValueError('normalized configuration')
        runtime = {'runtime': receipt.get('worker_runtime'), 'image': receipt.get('image'),
                   'code_archive_sha256': receipt.get('code_archive_sha256')}
        if not runtime['runtime'] or receipt.get('container_removed') is not True:
            raise ValueError('execution unavailable')
        if policy.real_eligible:
            worker_runtime = receipt.get('worker_runtime')
            if (receipt.get('schema') != 'rsi-execution-evidence/v2.3'
                    or receipt.get('stage') != stage or receipt.get('exit_code') != 0
                    or receipt.get('reason') is not None
                    or receipt.get('execution_adapter_sha256') != policy.adapter_sha256
                    or receipt.get('code_archive_sha256') != policy.archive_sha256
                    or receipt.get('source_base_commit') != policy.base_commit
                    or receipt.get('image') != policy.image
                    or receipt.get('capture_sha256') != sha(Path(capture_path).read_bytes())
                    or sha(canonical(worker_runtime)) != policy.runtime_sha256
                    or not isinstance(worker_runtime, dict)
                    or worker_runtime.get('uid') != 65534
                    or not str(worker_runtime.get('python', '')).startswith('3.12.')
                    or not isinstance(worker_runtime.get('packages'), dict)
                    or not worker_runtime['packages']):
                raise ValueError('untrusted execution')
        if runtime_identity is not None and runtime != runtime_identity:
            raise StageFailure('EVALUATION_ERROR', 'EXECUTION_ENVIRONMENT_CHANGED', global_stop=True)
        return runtime
    except StageFailure:
        raise
    except (ValueError, KeyError, OSError, TypeError) as error:
        raise StageFailure('EVALUATION_ERROR', 'EXECUTION_IDENTITY_UNKNOWN', global_stop=True) from error

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("rsi_playbook_verify", ROOT / "scripts" / "rsi_playbook_verify.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


@pytest.fixture
def candidate():
    return {"schema_version": "ace-playbook/v1", "id": "PB-001", "revision": 1, "previous_sha256": None, "problem": "示例问题", "conditions": ["示例条件"], "observation": "示例观察", "strategy": "示例策略", "evidence": ["experiments/EXP-001/runs/RUN-001/report.md"], "counterexamples": ["条件不成立时未验证"], "confidence": "low", "status": "candidate", "usage_count": 0, "success_count": 0}


def save(path, *records):
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def test_public_example_and_format(candidate, tmp_path):
    path = tmp_path / "entries.jsonl"
    save(path, candidate)
    assert checker.verify(path) == []
    assert checker.verify(ROOT / "docs" / "examples" / "rsi-playbook-v1.jsonl") == []
    del candidate["strategy"]
    save(path, candidate)
    assert "missing fields" in checker.verify(path)[0]


def test_id_revision_history_and_evidence(candidate, tmp_path):
    path = tmp_path / "entries.jsonl"
    second = dict(candidate, revision=2, previous_sha256=checker.digest(candidate))
    save(path, candidate, second)
    assert checker.verify(path) == []
    save(path, candidate, candidate)
    assert any("revision sequence" in e for e in checker.verify(path))
    altered = dict(candidate, problem="silent replacement")
    save(path, altered, second)
    assert any("predecessor hash" in e for e in checker.verify(path))
    altered["evidence"] = []
    save(path, altered)
    assert any("evidence" in e for e in checker.verify(path))


def test_validation_and_counters(candidate, tmp_path):
    path = tmp_path / "entries.jsonl"
    validated = dict(candidate, revision=2, previous_sha256=checker.digest(candidate), status="validated")
    save(path, validated)
    assert any("first revision" in e for e in checker.verify(path))
    save(path, candidate, validated)
    assert any("independent PASS" in e for e in checker.verify(path))
    validated["independent_validation"] = {"reviewer": "reviewer-2", "result": "PASS", "evidence_ref": "experiments/EXP-002/report.md"}
    validated["usage_count"] = 2
    validated["success_count"] = 1
    save(path, candidate, validated)
    assert checker.verify(path) == []
    bad = copy.deepcopy(validated)
    bad.update(revision=3, previous_sha256=checker.digest(validated), usage_count=1)
    save(path, candidate, validated, bad)
    assert any("counters" in e for e in checker.verify(path))


def test_recovery_reports_corrupt_and_incomplete_lines(candidate, tmp_path):
    path = tmp_path / "entries.jsonl"
    encoded = json.dumps(candidate).encode()
    path.write_bytes(encoded + b"\n{bad}\n" + encoded + b"\n{truncated")
    records, errors = checker.read_jsonl(path)
    assert [line for line, _ in records] == [1, 3]
    assert len(errors) == 2 and "line 2" in errors[0] and "line 4" in errors[1]
    assert path.read_bytes().endswith(b"{truncated")


def test_trusted_checkpoint_detects_tail_deletion_and_edit(candidate, tmp_path):
    path = tmp_path / "entries.jsonl"
    second = dict(candidate, revision=2, previous_sha256=checker.digest(candidate))
    save(path, candidate, second)
    pinned = hashlib.sha256(path.read_bytes()).hexdigest()
    assert checker.verify(path, pinned) == []
    save(path, candidate)
    assert checker.verify(path) == []
    assert "checkpoint" in checker.verify(path, pinned)[0]
    save(path, candidate, dict(second, problem="edited tail"))
    assert checker.verify(path) == []
    assert "checkpoint" in checker.verify(path, pinned)[0]


def test_invalid_revision_cannot_authenticate_next(candidate, tmp_path):
    path = tmp_path / "entries.jsonl"
    invalid = dict(candidate, revision=2, previous_sha256=checker.digest(candidate), evidence=[])
    following = dict(candidate, revision=3, previous_sha256=checker.digest(invalid))
    save(path, candidate, invalid, following)
    errors = checker.verify(path)
    assert any("line 2" in e and "evidence" in e for e in errors)
    assert any("line 3" in e and "predecessor" in e for e in errors)


def test_public_evidence_exists():
    example = json.loads((ROOT / "docs/examples/rsi-playbook-v1.jsonl").read_text(encoding="utf-8"))
    assert all((ROOT / ref).is_file() for ref in example["evidence"])

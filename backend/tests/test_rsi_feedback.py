"""Small engineered feedback archives; no worker, models, corpus or network."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/rsi_feedback.py"
spec = importlib.util.spec_from_file_location("rsi_feedback_under_test", SCRIPT)
feedback = importlib.util.module_from_spec(spec)
spec.loader.exec_module(feedback)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def archive(tmp_path, *, facts=True, lost=False, blocked=False):
    case = {"seed_ids": ["seed"], "allowed_kb_ids": ["kb"],
            "files": [{"id": "f", "kb_id": "kb"}],
            "chunks": [{"id": "seed", "file_id": "f", "content": "answer"}]}
    snapshot = {"status": "OK", "synthetic": True}
    arms = {}
    for arm in ("A", "B"):
        members = [] if arm == "B" and lost else [dict(case["chunks"][0], kb_id="kb")]
        context = "answer" if members else ""
        messages = [{"content": context}]
        capture = {"status": "OK", "context": context, "members": members, "messages": messages,
                   "request_calls": 1, "upstream_sha256": feedback._hash(snapshot),
                   "proof": [{"id": "seed", "context_start": 0, "context_end": 6,
                              "prompt_start": 0, "prompt_end": 6}] if members else []}
        score = {"qid": "q", "upstream_sha256": feedback._hash(snapshot),
                 "fixed_config_sha256": "config", "evaluator_version": "small-v1",
                 "member_ids": [m["id"] for m in members], "context_chars": len(context),
                 "context_utf8_bytes": len(context.encode()),
                 "context_sha256": feedback.hashlib.sha256(context.encode()).hexdigest(),
                 "messages_sha256": feedback._hash(messages), "request_calls": 1,
                 "facts": [{"kind": "field", "file_id": "f", "text": "answer",
                            "status": "PRESENT" if members else "ABSENT",
                            "member_ids": [m["id"] for m in members]}] if facts else [],
                 "fact_assessment": "KNOWN" if facts else "UNKNOWN"}
        arms[arm] = score
        if not blocked:
            save(tmp_path / f"case-001/{arm}/capture.json", capture)
    row = {"qid": "q", "input_sha256": feedback._hash(case), "seed_ids": ["seed"],
           "status": "BLOCKED" if blocked else "PAIRED", "meets_expectation": True,
           "arms": {} if blocked else arms}
    if blocked:
        row["reason"] = "missing FileDocument ownership: f"
    else:
        row.update(delta_chars=arms["B"]["context_chars"] - arms["A"]["context_chars"],
                   delta_bytes=arms["B"]["context_utf8_bytes"] - arms["A"]["context_utf8_bytes"])
    save(tmp_path / "case-001/input.json", case)
    save(tmp_path / "case-001/snapshot.json", snapshot)
    save(tmp_path / "case-001/result.json", row)
    save(tmp_path / "results.json", {"summary": {"total": 1, "paired": int(not blocked),
                                               "blocked": int(blocked)}, "rows": [row]})
    return row


def rewrite_row(tmp_path, row):
    save(tmp_path / "case-001/result.json", row)
    data = json.loads((tmp_path / "results.json").read_text())
    data["rows"] = [row]
    save(tmp_path / "results.json", data)


def categories(result):
    return {o["category"] for o in result["observations"]}


def test_verified_required_fact_and_seed_losses_link_parent_receipts(tmp_path):
    archive(tmp_path, lost=True)
    result = feedback.build_feedback(tmp_path, "run-engineered")
    assert result["summary"]["valid_pairs"] == 1
    assert {"seed_loss", "required_fact_absent", "required_fact_loss_vs_parent"} <= categories(result)
    for item in result["observations"]:
        assert item["run_id"] == "run-engineered" and item["qid"] == "q"
        assert item["label"] == "FACT" and item["observation_id"].startswith("obs-")
        assert {r["path"] for r in item["receipts"]} == {"case-001/A/capture.json", "case-001/B/capture.json"}
        assert all(len(r["sha256"]) == 64 and r["receipt_id"] is None for r in item["receipts"])
    assert result["hypotheses"] == []


def test_unknown_fact_evaluation_is_length_only_not_quality_preservation(tmp_path):
    archive(tmp_path, facts=False)
    result = feedback.build_feedback(tmp_path, "legacy")
    assert {"length_only", "fact_evaluation_unknown"} <= categories(result)
    assert "required_fact_loss_vs_parent" not in categories(result)
    observation = next(o for o in result["observations"] if o["category"] == "length_only")
    assert observation["details"]["required_fact_evaluation"] == "UNKNOWN"
    assert result["summary"]["answer_quality"] == "NOT_RUN"


def test_unpaired_execution_is_reported_as_invalid_pair(tmp_path):
    row = archive(tmp_path)
    row.update(status="FAILED", failure_stage="pairing", error="upstream identities differ")
    rewrite_row(tmp_path, row)
    result = feedback.build_feedback(tmp_path, "legacy")
    assert "invalid_pair" in categories(result)
    assert result["summary"]["valid_pairs"] == result["summary"]["execution_failures"] == 0


def test_blocked_is_not_quality_failure_or_pair(tmp_path):
    archive(tmp_path, blocked=True)
    result = feedback.build_feedback(tmp_path, "legacy")
    assert categories(result) == {"input_blocked"}
    assert result["summary"]["valid_pairs"] == 0
    assert result["summary"]["execution_failures"] == 0
    assert result["observations"][0]["details"]["quality_assessment"] == "NOT_RUN"


@pytest.mark.parametrize("mutation,expected", [
    ("pair_id", "pair_identity_mismatch:fixed_config_sha256"),
    ("aggregate", "aggregate_and_case_result_differ"),
    ("capture_fact", "B:stored_fact_capture_mismatch"),
    ("capture_missing", "B:capture_missing"),
    ("snapshot", "B:snapshot_fingerprint_mismatch_or_missing"),
    ("proof", "B:prompt_proof_mismatch"),
    ("receipt_path", "B:receipt_path_mismatch"),
    ("receipt_sha256", "B:receipt_sha256_mismatch"),
    ("proof_missing", "B:proof_member_sequence_mismatch"),
])
def test_inconsistent_archive_excluded_from_pair_comparison(tmp_path, mutation, expected):
    row = archive(tmp_path)
    if mutation == "pair_id":
        row["arms"]["B"]["fixed_config_sha256"] = "different"
        rewrite_row(tmp_path, row)
    elif mutation == "aggregate":
        changed = copy.deepcopy(row)
        changed["meets_expectation"] = False
        save(tmp_path / "case-001/result.json", changed)
    elif mutation == "capture_fact":
        row["arms"]["B"]["facts"][0]["status"] = "ABSENT"
        rewrite_row(tmp_path, row)
    elif mutation == "capture_missing":
        (tmp_path / "case-001/B/capture.json").unlink()
    elif mutation == "snapshot":
        save(tmp_path / "case-001/snapshot.json", {"mutated": True})
    elif mutation in {"proof", "proof_missing"}:
        path = tmp_path / "case-001/B/capture.json"
        value = json.loads(path.read_text())
        if mutation == "proof":
            value["proof"][0]["prompt_start"] = 1
        else:
            value["proof"] = []
        save(path, value)
    elif mutation == "receipt_path":
        row["arms"]["B"]["receipt_path"] = "some-other-run/capture.json"
        rewrite_row(tmp_path, row)
    else:
        row["arms"]["B"]["receipt_sha256"] = "wrong-receipt"
        rewrite_row(tmp_path, row)
    result = feedback.build_feedback(tmp_path, "legacy")
    assert result["summary"]["valid_pairs"] == 0
    obs = next(o for o in result["observations"] if o["category"] == "invalid_pair")
    assert expected in obs["details"]["issues"]
    assert "context_length_change" not in categories(result)


def test_source_conflict_uses_actual_input_not_semantic_guess(tmp_path):
    row = archive(tmp_path)
    case_path = tmp_path / "case-001/input.json"
    case = json.loads(case_path.read_text())
    case["files"][0]["kb_id"] = "other-kb"
    save(case_path, case)
    row["input_sha256"] = feedback._hash(case)
    rewrite_row(tmp_path, row)
    result = feedback.build_feedback(tmp_path, "legacy")
    assert "source_conflict" in categories(result)
    assert all(o["details"]["member_ids"] == ["seed"] for o in result["observations"] if o["category"] == "source_conflict")


def test_missing_input_and_failed_records_preserved(tmp_path):
    row = archive(tmp_path)
    (tmp_path / "case-001/input.json").unlink()
    row.update(status="FAILED", error="engineered preparation failure")
    rewrite_row(tmp_path, row)
    result = feedback.build_feedback(tmp_path, "legacy")
    assert {"input_missing", "execution_failure", "evidence_inconsistency"} <= categories(result)
    assert result["summary"]["execution_failures"] == 1


def test_explicit_legacy_identity_required_and_output_immutable(tmp_path):
    archive(tmp_path)
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*.json")}
    with pytest.raises(ValueError, match="explicit"):
        feedback.build_feedback(tmp_path)
    result = feedback.write_feedback(tmp_path, "legacy")
    assert result["run_id_source"] == "explicit_legacy"
    content = (tmp_path / "feedback.json").read_bytes()
    assert feedback.build_feedback(tmp_path, "legacy") == result
    with pytest.raises(FileExistsError):
        feedback.write_feedback(tmp_path, "legacy")
    assert (tmp_path / "feedback.json").read_bytes() == content
    assert all((tmp_path / path).read_bytes() == value for path, value in before.items())


def test_new_run_identity_is_reused_and_conflicting_explicit_identity_rejected(tmp_path):
    row = archive(tmp_path)
    row["run_id"] = "new-run"
    for arm in ("A", "B"):
        row["arms"][arm].update(run_id="new-run", receipt_path=f"case-001/{arm}/capture.json")
    rewrite_row(tmp_path, row)
    data = json.loads((tmp_path / "results.json").read_text())
    data["summary"]["run_id"] = "new-run"
    save(tmp_path / "results.json", data)
    result = feedback.build_feedback(tmp_path)
    assert result["run_id"] == "new-run" and result["run_id_source"] == "archive"
    assert result["summary"]["valid_pairs"] == 1
    with pytest.raises(ValueError, match="differs"):
        feedback.build_feedback(tmp_path, "unrelated")


def test_registered_run_uses_projected_arm_input_and_raw_receipt_hash(tmp_path):
    row = archive(tmp_path)
    snapshot = json.loads((tmp_path / "case-001/snapshot.json").read_text())
    save(tmp_path / "case-001/arm-input.json", snapshot)
    save(tmp_path / "case-001/snapshot.json", {"preflight_only": "not the delivery input"})
    save(tmp_path / "manifest.json", {"r1": {"registered": True}})
    for arm in ("A", "B"):
        capture_path = tmp_path / f"case-001/{arm}/capture.json"
        row["arms"][arm]["receipt_sha256"] = feedback.hashlib.sha256(capture_path.read_bytes()).hexdigest()
    rewrite_row(tmp_path, row)
    result = feedback.build_feedback(tmp_path, "registered")
    assert result["summary"]["valid_pairs"] == 1
    assert "case-001/arm-input.json" in {r["path"] for r in result["evidence_inventory"]}

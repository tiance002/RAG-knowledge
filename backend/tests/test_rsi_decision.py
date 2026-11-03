"""Engineering policies only; these tests do not execute a delivery."""
import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import rsi_decision as decision


def score(present, faults=()):
    return {"faults": list(faults), "facts": [{"file_id": "f", "kind": kind, "text": kind,
            "status": "PRESENT" if kind in present else "ABSENT", "member_ids": []}
            for kind in ("value", "header", "unit")]}


def review(a, b, health="UNKNOWN"):
    return {"scope": "registered_offline", "exceptions": [], "frozen_score": "FAIL", "cases": [
        {"status": "PAIRED", "facts": decision.fact_delta(a, b), "arms": {"A": a, "B": b},
         "health": health, "delta": {"context_chars": 40}}]}


def test_partial_repair_keeps_remaining_obligation():
    data = review(score(["value"]), score(["value", "header"], ["required_unit_absent"]))
    facts = data["cases"][0]["facts"]
    assert [f["kind"] for f in facts["restored"]] == ["header"]
    assert [f["kind"] for f in facts["still_missing"]] == ["unit"]
    # Parent already failed the unit obligation too.
    data["cases"][0]["arms"]["A"]["faults"] = ["required_unit_absent", "required_header_absent"]
    assert decision.decide(data) == ("CONTINUE_DEVELOPMENT", True)
    assert data["frozen_score"] == "FAIL"


def test_losing_one_fact_while_recovering_another():
    data = review(score(["value", "unit"]), score(["value", "header"]))
    assert decision.decide(data) == ("REGRESSION", False)


def test_no_compression_or_restoration_promotion_by_default():
    data = review(score(["value"]), score(["value", "header", "unit"]), "MATCHED_HEALTHY_CAPTURE")
    assert decision.decide(data) == ("RECOVERED_WAIT_FOR_NEW_OBSERVATION", True)
    assert decision.promotion(None, data) == "NOT_EVALUATED_NO_FROZEN_GATE"
    assert decision.promotion(decision.ENGINEERING_GATE, data) == "FAIL"
    data["cases"][0]["health"] = "UNKNOWN"
    assert decision.promotion(decision.ENGINEERING_GATE, data) == "UNKNOWN"
    data["cases"][0]["health"] = "ADDITIONAL_MEASURED_GAIN"
    assert decision.promotion(decision.ENGINEERING_GATE, data) == "PASS_ENGINEERING_ONLY"


def test_unknown_or_incomplete_receipts_cannot_select_parent():
    data = review({"facts": [], "faults": []}, {"facts": [], "faults": []})
    assert decision.decide(data) == ("BLOCKED_INSUFFICIENT_RECORDS", False)
    data = review(score(["value"]), score(["value", "header"]))
    data["exceptions"].append("aborted")
    assert not decision.decide(data)[1]


def test_decision_requires_child_observation_and_matching_receipts(monkeypatch):
    record = {"child": "B", "eligible_development_parent": True, "bundle": "b", "cases": [
        {"status": "PAIRED", "child_observations": ["child-event"]}]}
    monkeypatch.setattr(decision, "review_run", lambda *args: record)
    decision.authorize_derivation(record, "run", "B", "child-event")
    with pytest.raises(ValueError, match="observation"):
        decision.authorize_derivation(record, "run", "B", "parent-only-event")
    with pytest.raises(ValueError, match="differs"):
        decision.authorize_derivation({**record, "child": "forged"}, "run", "forged", "child-event")

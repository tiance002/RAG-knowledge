"""Pure offline aggregation contracts: exact scope links and retained uncertainty."""
from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

PATH = Path(__file__).resolve().parents[2] / "scripts/m2_current_aggregate.py"
SPEC = importlib.util.spec_from_file_location("m2_current_aggregate_test", PATH)
agg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agg)


def trial():
    return {"run_id": "run", "qid": "q1", "mode": "chat", "arm": "A", "repetition": 1,
            "dataset_version": "frozen-v1", "kb_scope": ["kb1"], "provider_call_ids": [1]}


@pytest.mark.parametrize("field,value", [("run_id", "other"), ("qid", "q1-suffix"), ("mode", "agent"),
    ("arm", "B"), ("repetition", 2), ("dataset_version", "frozen-v2"), ("kb_scope", ["kb2"])])
def test_call_matching_requires_every_exact_scope_component(field, value):
    expected = trial()
    assert agg.scoped_match(expected, expected)
    wrong = {**expected, field: value}
    assert not agg.scoped_match(wrong, expected)


@pytest.mark.parametrize("raw,expected", [(None, "UNKNOWN"), ("UNKNOWN", "UNKNOWN"),
    ("NOT_APPLICABLE", "N/A"), ("PASS_WITH_SOURCE_ALIGNMENT_LIMIT", "PASS_WITH_SOURCE_ALIGNMENT_LIMIT"),
    ("PARTIAL", "PARTIAL"), ("ABSENT", "ABSENT"), (0, "FAIL"), (1.0, "PASS"), (.5, "PARTIAL")])
def test_unknown_and_qualified_statuses_are_not_collapsed(raw, expected):
    value = agg.normalize_metric({"core": raw}, "core")
    assert value["status"] == expected
    assert value["raw"] == raw
    if raw is None or isinstance(raw, str):
        assert value["value"] is None


def test_negative_null_recall_is_na_only_with_final_answer():
    assert agg.normalize_metric({}, "core_inline_citation_recall", negative=True)["status"] == "N/A"
    assert agg.normalize_metric({}, "core_inline_citation_recall", negative=True, final_answer=False)["status"] == "UNKNOWN"
    assert agg.normalize_metric({}, "core_inline_citation_recall")["status"] == "UNKNOWN"


def test_literal_or_and_historical_strict_three_stay_distinct():
    review = {"technical_gold_literal_or": 1, "technical_historical_strict_all_three": .5}
    assert agg.normalize_metric(review, "technical_gold_literal_or", technical=True)["status"] == "PASS"
    assert agg.normalize_metric(review, "technical_historical_strict_all_three", technical=True)["status"] == "PARTIAL"
    assert agg.normalize_metric({}, "technical_gold_literal_or")["status"] == "N/A"


def test_categorical_delivery_never_invents_numeric_denominator():
    metric = agg.normalize_metric({"core_evidence_delivery": "PASS"}, "core_evidence_delivery")
    assert metric == {"status": "PASS", "value": None, "raw": "PASS", "source_field": "core_evidence_delivery"}
    # Whole answer judgments do not become core request faithfulness.
    assert agg.normalize_metric({"faithfulness": "PARTIAL"}, "core_request_faithfulness")["status"] == "UNKNOWN"


def test_observation_matching_excludes_other_arms_runs_and_qids():
    t = trial()
    attempt = "chat-A-r1-" + hashlib.sha256(b"q1").hexdigest()[:12]
    row = {**t, "attempt_id": attempt}
    records = [(1, {**row, "stage": "qa.request", "event": "start"}),
               (2, {**row, "stage": "qa.request", "event": "end"}),
               (3, {**row, "stage": "agent.tool_search"}),
               (4, {**row, "stage": "agent.retry_decision", "metadata": {"action": "SCHEDULED"}})]
    for field, value in [("run_id", "other"), ("qid", "q1-other"), ("dataset_version", "other"), ("attempt_id", "chat-B-r1-"+attempt.split("-")[-1])]:
        records.append((10, {**records[2][1], field: value}))
    result = agg.observed_metrics_from_rows(records, t)
    assert result["tool_search_stage_count"] == 1
    assert result["scheduled_retry_stage_count"] == 1
    assert result["matched_rows"] == 4
    assert agg.observed_metrics_from_rows(records[1:], t)["tool_search_stage_count"] is None


def test_unknown_ledger_attempt_preserves_reservation_and_missing_usage(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    t = trial()
    request = run / "request-0001.json"
    request.write_text('{"messages":[]}', encoding="utf-8")
    pending = {**t, "call_id": 1, "state": "UNKNOWN_PENDING", "reservation_cny": "0.75",
               "request_sha256": agg.sha(request)}
    unknown = {**pending, "state": "UNKNOWN"}
    (run / "provider-ledger.jsonl").write_text("\n".join(map(json.dumps, [pending, unknown])), encoding="utf-8")
    result = agg.audit_ledger(run, {agg.identity(t): (1, t)})[1]
    assert result["state"] == "UNKNOWN"
    assert result["reservation_cny"] == "0.75"
    assert result["usage"] is None and result["price_based_cost_cny"] is None
    wrong = deepcopy(t)
    wrong["arm"] = "B"
    with pytest.raises(ValueError, match="Scoped call identity mismatch"):
        agg.audit_ledger(run, {agg.identity(wrong): (1, wrong)})


def test_duplicate_review_and_mismatched_call_rejected(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr(agg, "ROOT", tmp_path)
    monkeypatch.setattr(agg, "REVIEWS", {"run": ("review.jsonl",)})
    t = trial()
    review = {**t, "core": "UNKNOWN"}
    path = run / "review.jsonl"
    path.write_text("\n".join(map(json.dumps, [review, review])), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate review identity"):
        agg.review_index(run, {agg.identity(t): (1, t)})
    review["provider_call_ids"] = [2]
    path.write_text(json.dumps(review), encoding="utf-8")
    with pytest.raises(ValueError, match="Review call IDs mismatch"):
        agg.review_index(run, {agg.identity(t): (1, t)})


def test_source_review_headers_are_not_trials(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setattr(agg, "ROOT", tmp_path)
    monkeypatch.setattr(agg, "REVIEWS", {"run": ("review.jsonl",)})
    t = trial()
    header = {"record_type": "source_review", "reviewed_trial_count": 1}
    review = {**t, "core": "UNKNOWN", "trial_line": 3}
    (run / "review.jsonl").write_text("\n".join(map(json.dumps, [header, review])), encoding="utf-8")
    indexed, headers, _ = agg.review_index(run, {agg.identity(t): (3, t)})
    assert len(indexed) == len(headers) == 1
    assert indexed[agg.identity(t)][0]["line"] == 2


def test_summary_separates_no_final_from_answered_unknown_and_na():
    samples = []
    for final, core in [(False, "UNKNOWN"), (True, "UNKNOWN"), (True, "PASS")]:
        review = {"core": core}
        samples.append({"final_answer": final, "state": "RETURNED" if final else "GUARD_STOP",
                        "metrics": {m: agg.normalize_metric(review, m) for m in agg.METRICS},
                        "latency_ms": 200 if final else 1000, "provider_calls": [], "review_raw": review,
                        "observed": {"tool_search_stage_count": None, "scheduled_retry_stage_count": None}})
    all_stats = agg.summarize(samples)
    answered = agg.summarize([x for x in samples if x["final_answer"]])
    assert all_stats["recorded"] == 3 and all_stats["no_final_answer"] == 1
    assert all_stats["metrics"]["core"]["status_counts"] == {"UNKNOWN": 2, "PASS": 1}
    assert answered["metrics"]["core"]["status_counts"] == {"UNKNOWN": 1, "PASS": 1}
    assert all_stats["metrics"]["technical_gold_literal_or"]["status_counts"] == {"N/A": 3}
    assert all_stats["metrics"]["core_evidence_delivery"]["numeric_only"]["known_count"] == 0
    assert all_stats["trial_latency_ms"]["known_count"] == 3
    assert answered["trial_latency_ms"]["known_count"] == 2

"""Read captured requests and frozen assertions. Never invoke candidate or model code."""
import argparse
import json
import sys
from pathlib import Path

from rsi_candidates import canonical, read_json, sha, verify_arm_sources, put
from rsi_feedback import build_feedback
from run_rsi_offline import assess, checked_case, EVALUATOR, fingerprint


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_hash(path):
    return sha(path.read_bytes())


def fact_delta(before, after):
    """Only compare an identical, nonempty set of assertions."""
    def facts(score):
        return {(f["file_id"], f["kind"], f["text"]): f for f in score["facts"]}
    a, b = facts(before), facts(after)
    if not a or a.keys() != b.keys():
        return {"status": "UNKNOWN"}
    groups = {"restored": [], "lost": [], "retained": [], "still_missing": []}
    labels = {(False, True): "restored", (True, False): "lost",
              (True, True): "retained", (False, False): "still_missing"}
    for key, item in b.items():
        label = labels[a[key]["status"] == "PRESENT", item["status"] == "PRESENT"]
        groups[label].append(item)
    return {"status": "KNOWN", **groups}


def pair_receipts(case_dir, case, expected, row, manifest, native):
    up = fingerprint(read_json(case_dir / "arm-input.json"))
    scores = {}
    for arm in ("A", "B"):
        path = case_dir / arm / "capture.json"
        cap = read_json(path)
        spec = manifest["arms"][arm]
        require(cap["status"] == "OK" and cap["upstream_sha256"] == up, "capture input mismatch")
        score = assess(case, cap, expected, arm)
        stored = row["arms"][arm]
        require(all(stored.get(k) == v for k, v in score.items()), "frozen assertions differ from receipt")
        if native:
            require(stored["candidate_id"] == spec["candidate_id"] and stored["upstream_sha256"] == up
                    and stored["receipt_sha256"] == file_hash(path)
                    and stored["evaluator_version"] == manifest["evaluator_version"]
                    and stored["fixed_config_sha256"] == fingerprint(manifest["fixed"]), "arm identity mismatch")
        # 'assess' checks selection and scope; verify actual offsets too, including the legacy engineering fixture.
        for m, proof in zip(cap["members"], cap["proof"], strict=True):
            require(m["id"] == proof["id"] and sha(m["content"].encode()) == m["content_hash"]
                    and cap["messages"][0]["content"][proof["prompt_start"]:proof["prompt_end"]] == m["content"]
                    and cap["context"][proof["context_start"]:proof["context_end"]] == m["content"],
                    "delivered content proof mismatch")
        lengths = [m["content"] for m in cap["messages"]]
        scores[arm] = {**score, "capture_sha256": file_hash(path),
                       "capture_path": str(path), "message_content_chars": sum(map(len, lengths)),
                       "message_content_bytes": sum(len(s.encode()) for s in lengths)}
    return up, scores


def inspect(run, bundle):
    run, bundle = Path(run).resolve(), Path(bundle).resolve()
    manifest = read_json(bundle / "manifest.json")
    verify_arm_sources(manifest, bundle)
    require(manifest["evaluator_version"] == EVALUATOR, "unsupported evaluator version")
    result = read_json(run / "results.json")
    native = (run / "manifest.json").is_file()
    observations = []
    if native:
        require(manifest == read_json(run / "manifest.json"), "bundle differs from archived manifest")
        summary = result["summary"]
        run_id = summary["run_id"]
        failed = sum(not r["meets_expectation"] for r in result["rows"])
        require(summary["failed"] == failed and summary["status"] == ("FAIL" if failed else "PASS")
                and summary["experiment_id"] == manifest["experiment_id"], "aggregate score inconsistent")
        versions = read_json(run / "versions.json")
        require(versions["run_id"] == run_id and versions["evaluator"] == EVALUATOR
                and versions["base_commit"] == manifest["base_commit"], "version identity mismatch")
        impl = run / "implementation"
        for arm, spec in manifest["arms"].items():
            require(file_hash(impl / (arm + "_candidate.json")) == spec["candidate_id"] and
                    file_hash(impl / (arm + "_worker.py")) == spec["worker_sha256"] and
                    fingerprint(read_json(impl / (arm + ".json"))) == spec["sha256"], "archived arm identity mismatch")
        feedback = build_feedback(run)
        require(feedback == read_json(run / "feedback.json"), "archived feedback differs from receipts")
        require(all(x["matches"] for x in feedback["summary"]["archive_count_consistency"].values()),
                "aggregate counts inconsistent")
        observations = feedback["observations"]
        invalid = {c["qid"] for c in feedback["cases"] if c["status"] == "PAIRED" and not c["valid_pair"]}
        cases, rubric = (read_json(bundle / manifest[k]["path"]) for k in ("cases", "evaluation"))
    else:
        fixture = read_json(run / "fixture.json")
        run_id = result["run_id"]
        require(fixture["run_id"] == run_id and all(fixture[k] == manifest["r1"][k] for k in ("parent", "child")),
                "engineering fixture identity mismatch")
        cases, rubric, invalid = fixture["cases"], fixture["evaluation"], set()
        summary = {}
    ids = [x["qid"] for x in cases]
    require(len(ids) == len(set(ids)) and set(ids) == set(rubric), "ambiguous frozen plan")
    rows = result["rows"]
    require([r["qid"] for r in rows] == ids[:len(rows)], "rows differ from ordered plan")
    decisions = []
    for i, item in enumerate(cases):
        qid, case = item["qid"], checked_case(item["case"])
        directory = run / f"case-{i+1:03}"
        info = {"qid": qid, "record": str(directory / "result.json"), "health": "UNKNOWN",
                "observations": [o["observation_id"] for o in observations if o["qid"] == qid],
                "child_observations": [o["observation_id"] for o in observations if o["qid"] == qid
                                       and o["details"].get("arm") != "A"]}
        decisions.append(info)
        if i >= len(rows):
            info.update(status="INCOMPLETE")
            continue
        row = rows[i]
        info.update(status=row["status"], frozen_expectation=row.get("meets_expectation"))
        require(row == read_json(directory / "result.json") and case == read_json(directory / "input.json"),
                "row or case differs from frozen input")
        if row["status"] == "BLOCKED":
            reason = read_json(directory / "snapshot.json").get("reason")
            expected = rubric[qid]
            ok = (reason == row["reason"] and expected["status"] == "BLOCKED" and
                  expected.get("missing_file_id") and expected["missing_file_id"] in reason and
                  not any((directory / arm).exists() for arm in ("A", "B")))
            info.update(status="EXPECTED_BLOCK" if ok else "UNEXPECTED_BLOCK", reason=reason)
        elif row["status"] == "PAIRED" and qid not in invalid:
            up, scores = pair_receipts(directory, case, rubric[qid], row, manifest, native)
            a, b = scores["A"], scores["B"]
            length_rule = rubric[qid].get("length_change")
            delta = b["context_chars"] - a["context_chars"]
            length_ok = {"equal": delta == 0, "shorter": delta < 0}.get(length_rule, True)
            if native:
                require(row["meets_expectation"] == (not a["faults"] and not b["faults"] and length_ok
                        and rubric[qid]["status"] == "PAIRED"), "frozen paired verdict inconsistent")
            info.update(upstream=up, arms=scores, facts=fact_delta(a, b),
                        length_rule=length_rule, length_assertion_met=length_ok if length_rule else None,
                        delta={k: b[k]-a[k] for k in ("context_chars", "context_utf8_bytes",
                                                       "message_content_chars", "message_content_bytes")})
        else:
            info.update(status="INVALID_PAIR" if qid in invalid else row["status"], error=row.get("error"))
    return {"run_id": run_id, "run_path": str(run), "results_sha256": file_hash(run / "results.json"),
            "scope": "registered_offline" if native else "independent_engineering_fixture",
            "frozen_score": summary.get("status", "NOT_APPLICABLE"),
            "runner_exit_by_contract": {"PASS": 0, "FAIL": 1}.get(summary.get("status"), "NOT_APPLICABLE"),
            "planned": len(cases), "paired": sum(x["status"] == "PAIRED" for x in decisions),
            "expected_blocks": [x["qid"] for x in decisions if x["status"] == "EXPECTED_BLOCK"],
            "exceptions": [x["qid"] for x in decisions if x["status"] not in ("PAIRED", "EXPECTED_BLOCK")],
            "cases": decisions, "manifest": manifest,
            "proposal": read_json(bundle / manifest["arms"]["B"]["record_path"]),
            "budget": manifest["budget"] if native else fixture["budget"]}


def compare_reference(current, reference):
    """Compare against A in the observation run where today's parent was B."""
    src = current["proposal"]["observation_source"]
    cm, rm = current["manifest"], reference["manifest"]
    if (current["scope"] != "registered_offline" or reference["scope"] != "registered_offline"
            or src.get("arm", "B") != "B" or src["run_id"] != reference["run_id"]
            or src["results_sha256"] != reference["results_sha256"]
            or src["archive_manifest"] != fingerprint(rm)
            or rm["r1"]["child"] != cm["r1"]["parent"]
            or any(cm[k] != rm[k] for k in ("base_commit", "fixed", "evaluator_version", "cases", "evaluation"))):
        return
    refs = {r["qid"]: r for r in reference["cases"] if r["status"] == "PAIRED"}
    for row in current["cases"]:
        ref = refs.get(row["qid"])
        if not ref or row["status"] != "PAIRED" or ref["upstream"] != row["upstream"]:
            continue
        old, b = ref["arms"]["A"], row["arms"]["B"]
        if ref["arms"]["B"]["capture_sha256"] != row["arms"]["A"]["capture_sha256"]:
            continue
        row["reference"] = {"run_id": reference["run_id"], "candidate_id": rm["r1"]["parent"],
                            "capture_path": old["capture_path"], "sha256": old["capture_sha256"]}
        change = fact_delta(old, b)
        row["reference_facts"] = change
        if change["status"] != "KNOWN":
            continue
        if change["lost"] or set(b["faults"]) - set(old["faults"]):
            row["health"] = "REGRESSION"
        elif change["restored"] and not b["faults"]:
            row["health"] = "ADDITIONAL_MEASURED_GAIN"
        elif not old["faults"] and b["capture_sha256"] == old["capture_sha256"]:
            row["health"] = "MATCHED_HEALTHY_CAPTURE"


def decide(review):
    paired = [x for x in review["cases"] if x["status"] == "PAIRED"]
    known = [x for x in paired if x["facts"]["status"] == "KNOWN"]
    if any(x["facts"]["lost"] or x["health"] == "REGRESSION" for x in known) or any(
            set(x["arms"]["B"]["faults"]) - set(x["arms"]["A"]["faults"]) for x in paired):
        return "REGRESSION", False
    if not paired or review["exceptions"] or len(known) != len(paired):
        return "BLOCKED_INSUFFICIENT_RECORDS", False
    critical = {"invalid_source_or_content", "forbidden_request_content", "seed_missing",
                "invalid_selection_order_or_membership", "request_evidence_missing", "unexpected_no_results_delivery"}
    if any(critical.intersection(x["arms"]["B"]["faults"]) for x in paired):
        return "UNRESOLVED_DELIVERY_FAILURE", False
    if any(x["arms"]["B"]["faults"] or x["facts"]["still_missing"] for x in known):
        return "CONTINUE_DEVELOPMENT", True
    if all(x["health"] == "MATCHED_HEALTHY_CAPTURE" for x in paired) and any(x["facts"]["restored"] for x in known):
        return "RECOVERED_WAIT_FOR_NEW_OBSERVATION", True
    return "CONTINUE_DEVELOPMENT", True


ENGINEERING_GATE = "all-paired-safe-with-measured-gain-over-reference-v1"


def promotion(gate, report):
    if not gate:
        return "NOT_EVALUATED_NO_FROZEN_GATE"
    rows = report["cases"]
    if gate != ENGINEERING_GATE or report["scope"] != "registered_offline":
        return "UNKNOWN"
    if not all(r["status"] == "PAIRED" for r in rows) or not rows:
        return "BLOCKED"
    if any(r["arms"]["B"]["faults"] or r["health"] == "REGRESSION" or
           r["facts"].get("lost") for r in rows):
        return "FAIL"
    if any(r["health"] == "UNKNOWN" or r["facts"]["status"] != "KNOWN" for r in rows):
        return "UNKNOWN"
    return "PASS_ENGINEERING_ONLY" if any(r["health"] == "ADDITIONAL_MEASURED_GAIN" for r in rows) else "FAIL"


def review_run(run, bundle, reference=None, reference_bundle=None):
    report = inspect(run, bundle)
    if reference:
        try:
            compare_reference(report, inspect(reference, reference_bundle or Path(reference).parent / "bundle"))
        except (ValueError, KeyError, OSError, IndexError, TypeError) as exc:
            report["reference_unavailable"] = str(exc)
    state, eligible = decide(report)
    proposal = report.pop("proposal")
    manifest = report.pop("manifest")
    report.update(schema="rsi-receipt-decision-v1", parent=manifest["r1"]["parent"], child=manifest["r1"]["child"],
                  bundle=str(Path(bundle).resolve()), reference_run=str(Path(reference).resolve()) if reference else None,
                  reference_bundle=str(Path(reference_bundle).resolve()) if reference_bundle else None,
                  hypothesis=proposal["notes"], origin={k: proposal["observation_source"][k]
                  for k in ("run_id", "qid", "observation_id")}, next_step=state,
                  eligible_development_parent=eligible and report["scope"] == "registered_offline",
                  formal_effect="NOT_RUN", promotion=promotion(proposal["notes"].get("engineering_gate"), report))
    if report["promotion"] == "PASS_ENGINEERING_ONLY":
        report["next_step"] = "FROZEN_ENGINEERING_GATE_MET"
    return report


def authorize_derivation(saved, run, parent_id, observation_id):
    """Recompute local evidence. A decision JSON alone is not an authorization."""
    current = review_run(run, saved["bundle"], saved.get("reference_run"), saved.get("reference_bundle"))
    require(all(saved.get(k) == v for k, v in current.items()), "decision differs from current receipts")
    require(current["child"] == parent_id and current["eligible_development_parent"],
            "decision does not support this development parent")
    require(any(c["status"] == "PAIRED" and observation_id in c["child_observations"] for c in current["cases"]),
            "observation does not apply to the evaluated child")


def print_summary(report):
    print(f"运行 {report['run_id']}  冻结评分 {report['frozen_score']}  "
          f"进程退出 {report['observed_process_exit']}  评分对应退出 {report['runner_exit_by_contract']}")
    print(f"计划 {report['planned']}  有效配对 {report['paired']}  "
          f"预期阻断 {len(report['expected_blocks'])}  异常 {len(report['exceptions'])}")
    for row in report["cases"]:
        print(row["qid"], row["status"], row.get("reason", row.get("error", "")))
        facts = row.get("facts", {})
        if facts.get("status") == "KNOWN":
            for label, key in (("恢复", "restored"), ("丢失", "lost"), ("保留", "retained"), ("仍缺", "still_missing")):
                print(" ", label, [f["text"] for f in facts[key]])
        else:
            print("  必要事实 UNKNOWN")
        if "arms" in row:
            print("  A / B 未满足的断言", row["arms"]["A"]["faults"], row["arms"]["B"]["faults"])
            print("  长度约定及是否满足", row["length_rule"], row["length_assertion_met"],
                  "消息正文字数变化", row["delta"]["message_content_chars"])
        print("  健康参照", row["health"], "证据", row["record"])
    print("下一步", report["next_step"], "可作开发父代", report["eligible_development_parent"],
          "正式效果", report["formal_effect"])


def review_cli(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, help="Default: bundle beside run")
    parser.add_argument("--reference-run", type=Path)
    parser.add_argument("--reference-bundle", type=Path)
    parser.add_argument("--store", type=Path, help="Immutable local decision objects")
    parser.add_argument("--brief", action="store_true", help="Print a concise Chinese readout")
    parser.add_argument("--process-log", type=Path, help="JSON execution log containing this run ID")
    parser.add_argument("--exit-file", type=Path, help="Original process exit sidecar accompanying the log")
    args = parser.parse_args(argv)
    try:
        report = review_run(args.run, args.bundle or args.run.parent / "bundle", args.reference_run, args.reference_bundle)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        parser.exit(2, f"BLOCKED_INSUFFICIENT_RECORDS: {exc}; facts UNKNOWN; eligible_development_parent=False\n")
    if bool(args.process_log) != bool(args.exit_file):
        parser.error("supply both process log and exit file")
    report["observed_process_exit"] = "UNKNOWN"
    if args.process_log:
        require(read_json(args.process_log)["run_id"] == report["run_id"], "process log run_id mismatch")
        report["observed_process_exit"] = int(args.exit_file.read_text(encoding="utf-8-sig").strip())
        report["exit_evidence"] = [str(args.process_log.resolve()), str(args.exit_file.resolve())]
    if args.store:
        key = put(args.store, canonical(report))
        print("decision:", args.store / "objects" / key, file=sys.stderr)
    if args.brief:
        print_summary(report)
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))

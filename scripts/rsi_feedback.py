"""Extract auditable offline feedback from archived receipts; never replay a run.

Only stored required-fact assessments are reused. No answer quality, token saving,
or semantic source contradiction is inferred from shorter context or matching IDs.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path


VERSION = "rsi-feedback-v1"


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode()).hexdigest()


def build_feedback(out, run_id=None):
    """Read an archive without mutation and return evidence-backed feedback."""
    out = Path(out).resolve()
    evidence = {}

    def read(relative, required=False):
        path = out / relative
        if not path.is_file():
            if required:
                raise FileNotFoundError(path)
            return None
        data = path.read_bytes()
        evidence[relative] = {"path": relative, "sha256": hashlib.sha256(data).hexdigest()}
        return json.loads(data.decode("utf-8-sig"))

    results = read("results.json", required=True)
    stored_id = results.get("run_id") or results.get("summary", {}).get("run_id")
    manifest = read("manifest.json") or {}
    stored_id = stored_id or manifest.get("run_id")
    if stored_id and run_id and stored_id != run_id:
        raise ValueError("Explicit run_id differs from archived run_id")
    run_id = run_id or stored_id
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("Pre-R1 archive requires an explicit nonempty run_id")
    rows = results["rows"]
    if not isinstance(rows, list):
        raise ValueError("results.rows must be a list")
    observations, cases = [], []

    def emit(qid, category, details, refs, receipts=(), label="FACT"):
        item = {"run_id": run_id, "qid": qid, "label": label, "category": category,
                "details": details, "evidence": refs, "receipts": list(receipts)}
        item["observation_id"] = "obs-" + _hash(item)[:24]
        observations.append(item)
        return item["observation_id"]

    qids = [r.get("qid") for r in rows]
    valid_pairs = 0
    for index, row in enumerate(rows, 1):
        qid = row.get("qid")
        directory = f"case-{index:03}"
        result_path = directory + "/result.json"
        archived = read(result_path)
        input_path = directory + "/input.json"
        case = read(input_path)
        snapshot_path = directory + ("/arm-input.json" if manifest.get("r1") else "/snapshot.json")
        snapshot = read(snapshot_path)
        refs = [{**evidence["results.json"], "field": f"rows[{index - 1}]"}]
        if archived is not None:
            refs.append(evidence[result_path])
        issues = []
        if not qid or qids.count(qid) != 1:
            issues.append("missing_or_duplicate_qid")
        if archived is None:
            issues.append("case_result_missing")
        elif archived != row:
            issues.append("aggregate_and_case_result_differ")
        if row.get("run_id") not in (None, run_id):
            issues.append("case_run_id_mismatch")
        if case is None:
            emit(qid, "input_missing", {"missing": input_path}, refs)
            issues.append("input_missing")
        elif row.get("input_sha256") and _hash(case) != row["input_sha256"]:
            issues.append("input_fingerprint_mismatch")
        if case is not None and row.get("seed_ids") != case.get("seed_ids"):
            issues.append("seed_ids_input_mismatch")
        status = row.get("status")
        if status == "BLOCKED":
            emit(qid, "input_blocked", {"reason": row.get("reason"),
                 "expected": row.get("meets_expectation"), "quality_assessment": "NOT_RUN"}, refs)
        elif status != "PAIRED":
            category = "invalid_pair" if row.get("failure_stage") == "pairing" else "execution_failure"
            emit(qid, category, {"status": status, "error": row.get("error"),
                 "quality_assessment": "NOT_RUN"}, refs)

        captures, receipts = {}, []
        arms = row.get("arms", {})
        for arm in ("A", "B"):
            capture_path = directory + f"/{arm}/capture.json"
            capture = read(capture_path)
            if capture is None:
                if status == "PAIRED":
                    issues.append(f"{arm}:capture_missing")
                continue
            receipt = {**evidence[capture_path], "arm": arm,
                       "receipt_id": capture.get("receipt_id"),
                       "observation_id": capture.get("observation_id")}
            receipts.append(receipt)
            captures[arm] = capture
            score = arms.get(arm)
            if score is None:
                if status == "PAIRED":
                    issues.append(f"{arm}:assessment_missing")
                continue
            if score.get("run_id") not in (None, run_id):
                issues.append(f"{arm}:assessment_run_id_mismatch")
            if score.get("receipt_path") not in (None, capture_path):
                issues.append(f"{arm}:receipt_path_mismatch")
            if score.get("receipt_sha256") not in (None, evidence[capture_path]["sha256"]):
                issues.append(f"{arm}:receipt_sha256_mismatch")
            if status != "PAIRED":
                continue
            context, members = capture.get("context", ""), capture.get("members", [])
            messages = capture.get("messages", [])
            ids = [m.get("id") for m in members]
            for key, value in {"member_ids": ids, "context_chars": len(context),
                               "context_utf8_bytes": len(context.encode()),
                               "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
                               "messages_sha256": _hash(messages),
                               "request_calls": capture.get("request_calls")}.items():
                if score.get(key) != value:
                    issues.append(f"{arm}:capture_assessment_mismatch:{key}")
            if capture.get("status") != "OK":
                issues.append(f"{arm}:capture_not_ok")
            if capture.get("upstream_sha256") != score.get("upstream_sha256"):
                issues.append(f"{arm}:upstream_mismatch")
            if snapshot is None or _hash(snapshot) != score.get("upstream_sha256"):
                issues.append(f"{arm}:snapshot_fingerprint_mismatch_or_missing")
            if context and (not messages or context not in messages[0].get("content", "")):
                issues.append(f"{arm}:context_not_in_request")
            if len(ids) != len(set(ids)):
                issues.append(f"{arm}:duplicate_delivered_id")
            if [p.get("id") for p in capture.get("proof", [])] != ids:
                issues.append(f"{arm}:proof_member_sequence_mismatch")
            for proof in capture.get("proof", []):
                member = next((m for m in members if m.get("id") == proof.get("id")), None)
                if member is None or context[proof.get("context_start", 0):proof.get("context_end", 0)] != member.get("content"):
                    issues.append(f"{arm}:context_proof_mismatch")
                elif not messages or messages[0].get("content", "")[proof.get("prompt_start", 0):proof.get("prompt_end", 0)] != member.get("content"):
                    issues.append(f"{arm}:prompt_proof_mismatch")
            for fact in score.get("facts", []):
                hits = [m["id"] for m in members if m.get("file_id") == fact.get("file_id")
                        and fact.get("text") and fact["text"] in m.get("content", "")]
                if fact.get("status") != ("PRESENT" if hits else "ABSENT") or fact.get("member_ids") != hits:
                    issues.append(f"{arm}:stored_fact_capture_mismatch")
            if case is not None:
                files = {f["id"]: f for f in case.get("files", [])}
                chunks = {c["id"]: c for c in case.get("chunks", [])}
                conflicts = []
                for m in members:
                    file, chunk = files.get(m.get("file_id")), chunks.get(m.get("id"))
                    if (not file or not chunk or file.get("kb_id") not in case.get("allowed_kb_ids", [])
                            or m.get("kb_id") != file.get("kb_id") or chunk.get("file_id") != m.get("file_id")
                            or chunk.get("content") != m.get("content") or m.get("content", "") not in context):
                        conflicts.append(m.get("id"))
                if conflicts:
                    emit(qid, "source_conflict", {"arm": arm, "member_ids": conflicts,
                         "meaning": "Captured source ownership, scope or content differs from archived input; semantic contradiction not evaluated"},
                         refs + [evidence[input_path]], [receipt])

        if status == "PAIRED":
            for key in ("qid", "upstream_sha256", "fixed_config_sha256", "evaluator_version"):
                values = [arms.get(a, {}).get(key) for a in ("A", "B")]
                if not values[0] or values[0] != values[1] or (key == "qid" and values[0] != qid):
                    issues.append("pair_identity_mismatch:" + key)
            if set(arms) != {"A", "B"}:
                issues.append("pair_arms_missing_or_extra")
            a, b = arms.get("A", {}), arms.get("B", {})
            if all(isinstance(s.get("context_chars"), int) for s in (a, b)):
                if row.get("delta_chars") != b["context_chars"] - a["context_chars"]:
                    issues.append("delta_chars_mismatch")
            if all(isinstance(s.get("context_utf8_bytes"), int) for s in (a, b)):
                if row.get("delta_bytes") != b["context_utf8_bytes"] - a["context_utf8_bytes"]:
                    issues.append("delta_bytes_mismatch")
            facts_a = {(f.get("kind"), f.get("file_id"), f.get("text")): f for f in a.get("facts", [])}
            facts_b = {(f.get("kind"), f.get("file_id"), f.get("text")): f for f in b.get("facts", [])}
            if facts_a.keys() != facts_b.keys():
                issues.append("required_fact_sets_differ")
            if issues:
                emit(qid, "invalid_pair", {"issues": sorted(set(issues)), "quality_assessment": "NOT_ESTABLISHED"}, refs, receipts)
            else:
                valid_pairs += 1
                seed_ids = case.get("seed_ids", [])
                for arm in ("A", "B"):
                    missing = [seed for seed in seed_ids if seed not in arms[arm]["member_ids"]]
                    if missing:
                        emit(qid, "seed_loss", {"arm": arm, "missing_seed_ids": missing,
                             "present_in_parent_A": [s for s in missing if s in a["member_ids"]]}, refs, receipts)
                    absent = [f for f in arms[arm].get("facts", []) if f["status"] == "ABSENT"]
                    if absent:
                        emit(qid, "required_fact_absent", {"arm": arm, "facts": absent}, refs, receipts)
                lost = [f for key, f in facts_b.items() if f["status"] == "ABSENT" and facts_a[key]["status"] == "PRESENT"]
                if lost:
                    emit(qid, "required_fact_loss_vs_parent", {"parent_arm": "A", "candidate_arm": "B", "facts": lost}, refs, receipts)
                known = bool(facts_a) and all(arms[x].get("fact_assessment") == "KNOWN" for x in ("A", "B"))
                emit(qid, "context_length_change" if known else "length_only", {
                     "delta_chars": row.get("delta_chars"), "delta_bytes": row.get("delta_bytes"),
                     "required_fact_evaluation": "KNOWN" if known else "UNKNOWN",
                     "answer_quality": "NOT_RUN", "tokens": "UNKNOWN", "cost_benefit": "NOT_RUN"}, refs, receipts)
                if not known:
                    emit(qid, "fact_evaluation_unknown", {"reason": "No common nonempty KNOWN required-fact assessment; absence of measured losses is not evidence of preserved quality"}, refs, receipts)
        elif issues:
            emit(qid, "evidence_inconsistency", {"issues": sorted(set(issues))}, refs, receipts)
        cases.append({"qid": qid, "status": status, "valid_pair": status == "PAIRED" and not issues,
                      "observation_ids": [o["observation_id"] for o in observations if o["qid"] == qid]})

    category_counts = dict(sorted(Counter(o["category"] for o in observations).items()))
    summary = {"total": len(rows), "recorded_paired": sum(r.get("status") == "PAIRED" for r in rows),
               "valid_pairs": valid_pairs, "blocked": sum(r.get("status") == "BLOCKED" for r in rows),
               "execution_failures": category_counts.get("execution_failure", 0),
               "category_counts": category_counts, "answer_quality": "NOT_RUN"}
    summary["archive_count_consistency"] = {
        key: {"stored": results.get("summary", {}).get(key), "observed": summary[observed],
              "matches": results.get("summary", {}).get(key) == summary[observed]}
        for key, observed in (("total", "total"), ("paired", "recorded_paired"), ("blocked", "blocked"))}
    return {"schema_version": VERSION, "run_id": run_id, "run_id_source": "archive" if stored_id else "explicit_legacy",
            "summary": summary, "cases": cases, "observations": observations,
            "hypotheses": [], "evidence_inventory": list(evidence.values()),
            "limitations": ["Required facts are the stored evaluator assertions, not an exhaustive answer-quality rubric.",
                            "Source conflicts mean identity/scope/content mismatches, not inferred semantic contradictions.",
                            "Legacy captures may have no receipt_id; relative capture path and SHA256 are the factual receipt linkage."]}


def write_feedback(out, run_id=None):
    """Create feedback.json once; rerunning never replaces an earlier artifact."""
    feedback = build_feedback(out, run_id)
    with (Path(out) / "feedback.json").open("x", encoding="utf-8") as file:
        json.dump(feedback, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")
    return feedback


def length_changes_with_preserved_evidence(feedback):
    """Report length changes on comparable pairs with all *measured* facts preserved."""
    grouped = {}
    for item in feedback["observations"]:
        grouped.setdefault(item["qid"], []).append(item)
    findings = []
    for case in feedback["cases"]:
        events = grouped.get(case["qid"], [])
        if not case["valid_pair"] or {e["category"] for e in events} != {"context_length_change"}:
            continue
        for e in events:
            if e["details"]["delta_chars"] or e["details"]["delta_bytes"]:
                findings.append({k: e[k] for k in ("run_id", "qid", "observation_id", "details", "receipts")})
    return findings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, type=Path, help="Existing archived run directory")
    parser.add_argument("--run-id", help="Required explicit identity for pre-R1 archives")
    parser.add_argument("--length-changes", action="store_true", help="Read only: length changed, known evidence preserved")
    args = parser.parse_args()
    if args.length_changes:
        print(json.dumps(length_changes_with_preserved_evidence(build_feedback(args.out, args.run_id)), ensure_ascii=False))
        return
    feedback = write_feedback(args.out, args.run_id)
    print(json.dumps(feedback["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()

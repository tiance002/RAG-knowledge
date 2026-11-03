"""Paired, offline delivery experiments. Provider/SQL fixtures run in child processes."""
import argparse
import hashlib
import uuid
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / "scripts/rsi_delivery_worker.py"
EVALUATOR = "delivered-source-facts-v1"
FIXED = {"window_size": 1, "max_parent_length": 4000, "rewrite": False, "rerank": False,
         "router": "OFF", "provider": "local_request_capture"}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def fingerprint(value):
    return digest(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def save(path, data):
    with path.open("x", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2, allow_nan=False)
        file.write("\n")


def artifact(base, ref):
    value = load(base / ref["path"])
    if fingerprint(value) != ref["sha256"]:
        raise ValueError(f"Artifact fingerprint mismatch: {ref['path']}")
    return value


class PairingError(ValueError):
    """Execution cannot establish identical upstream inputs for the two arms."""


def compare_pair(a, b):
    for key in ("qid", "upstream_sha256", "fixed_config_sha256", "evaluator_version"):
        if not a.get(key) or a[key] != b.get(key):
            raise PairingError(f"Incomparable arms: {key}")


def child(stage, paths, cwd, *, worker=None, helper_dir=None):
    # Never import the legacy CLI or install its process-global patches in this interpreter.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "HOME", "USERPROFILE",
        "RAG_TEST_EXTRA_SITE_PACKAGES"}}
    env.update(PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               DATABASE_URL="sqlite://", LANGFUSE_ENABLED="false", RAG_OBSERVABILITY_ENABLED="false",
               RAG_MODEL_ROUTER_MODE="OFF", RAG_SNAPSHOTS_ENABLED="false")
    if helper_dir:
        env["PYTHONPATH"] = os.pathsep.join(map(str, (ROOT / "backend", helper_dir)))
    with (cwd / "worker.log").open("xb") as log:
        subprocess.run([sys.executable, str(worker or WORKER), stage, *map(str, paths)], env=env, cwd=cwd,
                       stdout=log, stderr=subprocess.STDOUT, check=True, timeout=90)
    return load(paths[-1])


def assess(case, capture, expected, arm):
    members = capture["members"]
    ids = [m["id"] for m in members]
    allowed_files = {f["id"]: f["kb_id"] for f in case["files"] if f["kb_id"] in case["allowed_kb_ids"]}
    chunks = {c["id"]: c for c in case["chunks"]}
    context = capture["context"]
    messages = capture["messages"]
    faults = []
    if case["seed_ids"]:
        if capture["request_calls"] != 1 or not messages or not context or context not in messages[0]["content"]:
            faults.append("request_evidence_missing")
    elif capture["request_calls"] != 0 or members:
        faults.append("unexpected_no_results_delivery")
    if "baseline_members" in capture:
        original = [m["id"] for m in capture["baseline_members"]]
        if ids != [mid for mid in original if mid in set(ids)]:
            faults.append("invalid_selection_order_or_membership")
    if set(case["seed_ids"]) - set(ids):
        faults.append("seed_missing")
    for m in members:
        row = chunks.get(m["id"], {})
        if (m["file_id"] not in allowed_files or m["kb_id"] != allowed_files.get(m["file_id"]) or
                row.get("file_id") != m["file_id"] or row.get("content") != m["content"] or m["content"] not in context):
            faults.append("invalid_source_or_content")
    raw_request = "\n".join(m["content"] for m in messages)
    if any(x in raw_request for x in expected.get("forbidden", [])):
        faults.append("forbidden_request_content")
    facts = []
    for fact in expected.get("facts", []):
        hits = [m["id"] for m in members if m["file_id"] == fact["file_id"] and fact["text"] in m["content"]]
        facts.append({**fact, "status": "PRESENT" if hits else "ABSENT", "member_ids": hits})
        if not hits:
            faults.append("required_" + fact["kind"] + "_absent")
    if "members" in expected and ids != expected["members"][arm]:
        faults.append("unexpected_member_sequence")
    return {"member_ids": ids, "facts": facts, "fact_assessment": ("KNOWN" if facts else "UNKNOWN"),
            "context_chars": len(context), "context_utf8_bytes": len(context.encode()),
            "context_sha256": digest(context.encode()), "messages_sha256": fingerprint(messages),
            "request_calls": capture["request_calls"], "faults": faults}


def execution_view(snapshot):
    """Candidate code sees only verified expansion members and their source metadata."""
    member_ids = {m["id"] for row in snapshot["expanded"]
                  for m in row["context_group"]["source_members"]}
    chunks = [c for c in snapshot["chunks"] if c["id"] in member_ids]
    file_ids = {c["file_id"] for c in chunks}
    files = [f for f in snapshot["files"] if f["id"] in file_ids]
    if any(f["kb_id"] not in snapshot["allowed_kb_ids"] for f in files):
        raise ValueError("Unscoped source in candidate input")
    return {**snapshot, "chunks": chunks, "files": files}


def checked_case(case):
    # Use an explicit projection before prepare. Private evaluator-only keys must
    # never enter the expansion, selector or captured model request.
    def keep(row, keys):
        return {k: row[k] for k in keys if k in row}
    result = keep(case, ("question", "allowed_kb_ids", "seed_ids", "expected_seed_count"))
    result["files"] = [keep(f, ("id", "name", "kb_id")) for f in case["files"]]
    result["chunks"] = []
    for c in case["chunks"]:
        chunk = keep(c, ("id", "file_id", "content", "content_type", "content_sha256", "original_index",
                         "parent_id", "heading_text", "heading_level", "page_number", "chunk_type"))
        if isinstance(c.get("source_locator"), dict):
            chunk["source_locator"] = keep(c["source_locator"], ("page", "bbox", "sheet", "row_start",
                "row_end", "column_start", "column_end", "record_start", "record_end"))
        result["chunks"].append(chunk)
    return result


def run(manifest_path, out):
    manifest_path = Path(manifest_path).resolve()
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError(out)
    manifest = load(manifest_path)
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if manifest["status"] not in ("READY", "READY_DEV_ONLY"):
        raise ValueError("Manifest is not ready")
    base = manifest["base_commit"]
    subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", base, head], check=True)
    subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", base, "--", "backend/app"], check=True)
    run_id = uuid.uuid4().hex
    registered = "r1" in manifest
    if manifest["fixed"] != FIXED or manifest["evaluator_version"] != EVALUATOR:
        raise ValueError("Unsupported fixed configuration or evaluator")
    arms = manifest["arms"]
    if set(arms) != {"A", "B"} or arms["B"]["parent_id"] != arms["A"]["candidate_id"]:
        raise ValueError("Expected two arms from the declared baseline")
    configs = {label: artifact(manifest_path.parent, spec) for label, spec in arms.items()}
    if registered:
        from rsi_candidates import verify_arm_sources, validate_seam
        workers = verify_arm_sources(manifest, manifest_path.parent)
        # Both worker artifacts must be confined to the reviewed R0 selector seam.
        frozen = subprocess.check_output(["git", "-C", str(ROOT), "show",
            "297e66ed049d58060fffe2c15d36efaec182ecc8:scripts/rsi_delivery_worker.py"])
        for source in workers.values():
            validate_seam(frozen, source.read_bytes())
        for source in (WORKER, WORKER.with_name("replay_source_preparation.py")):
            original = subprocess.check_output(["git", "-C", str(ROOT), "show",
                "297e66ed049d58060fffe2c15d36efaec182ecc8:scripts/" + source.name])
            if digest(original) != digest(source.read_bytes()):
                raise ValueError("Working replay substrate differs from frozen R0")
    elif configs["A"] != {"selection": "all"}:
        raise ValueError("A must preserve baseline delivery")
    cases = artifact(manifest_path.parent, manifest["cases"])
    rubric = artifact(manifest_path.parent, manifest["evaluation"])
    ids = [r["qid"] for r in cases]
    if len(set(ids)) != len(ids) or set(ids) != set(rubric):
        raise ValueError("Input and evaluator QIDs do not pair uniquely")
    budget = manifest["budget"]
    if not (0 < len(cases) <= budget["max_questions"] <= 20 and budget["external_calls"] == 0
            and budget["attempts_per_arm"] == 1):
        raise ValueError("Invalid offline budget")
    out.mkdir(parents=True)
    save(out / "manifest.json", manifest)
    recovery = out / "implementation"
    recovery.mkdir()
    # Save runnable experiment additions. Production code is recovered from base_commit.
    sources = [Path(__file__).resolve(), WORKER, WORKER.with_name("replay_source_preparation.py")]
    source_hashes = {}
    for source in sources:
        shutil.copyfile(source, recovery / source.name)
        source_hashes[source.name] = digest(source.read_bytes())
    for label, cfg in configs.items():
        save(recovery / (label + ".json"), cfg)
        if registered:
            spec = arms[label]
            source = workers[label].read_bytes()
            record_data = (manifest_path.parent / spec["record_path"]).read_bytes()
            if digest(source) != spec["worker_sha256"] or digest(record_data) != spec["record_sha256"]:
                raise ValueError("Materialized candidate changed during archival")
            (recovery / (label + "_worker.py")).write_bytes(source)
            (recovery / (label + "_candidate.json")).write_bytes(record_data)
            source_hashes[label + "_worker.py"] = digest(source)
    if registered:
        lineage_dir = out / "lineage"
        lineage_dir.mkdir()
        for name in ("parent_template.json", "observation_manifest.json", "observation_results.json",
                     "observation_feedback.json"):
            shutil.copyfile(manifest_path.parent / name, lineage_dir / name)
        for name in ("rsi_candidates.py", "rsi_feedback.py"):
            shutil.copyfile(WORKER.with_name(name), recovery / name)
            source_hashes[name] = digest((recovery / name).read_bytes())
    save(out / "versions.json", {"run_id": run_id, "base_commit": base, "checkout": head, "implementation": source_hashes,
         "python": sys.version, "executable": sys.executable, "evaluator": EVALUATOR})
    records = []
    for index, item in enumerate(cases, 1):
        qid, case = item["qid"], item["case"]
        expected = rubric[qid]
        directory = out / f"case-{index:03}"
        directory.mkdir()
        if registered:
            case = checked_case(case)
        save(directory / "input.json", case)
        row = {"run_id": run_id, "qid": qid, "input_sha256": fingerprint(case), "seed_ids": case["seed_ids"],
               "status": "FAILED", "meets_expectation": False, "arms": {}}
        records.append(row)
        try:
            snapshot_path = directory / "snapshot.json"
            snapshot = child("prepare", [directory / "input.json", snapshot_path], directory)
            if snapshot["status"] == "BLOCKED":
                reason = str(snapshot["reason"])
                missing = expected.get("missing_file_id")
                row.update(status="BLOCKED", reason=reason, meets_expectation=(
                    expected["status"] == "BLOCKED" and bool(missing) and missing in reason))
            else:
                if registered:
                    snapshot = execution_view(snapshot)
                    snapshot_path = directory / "arm-input.json"
                    save(snapshot_path, snapshot)
                up = fingerprint(snapshot)
                row.update(upstream_sha256=up)
                for label in ("A", "B"):
                    arm_dir = directory / label
                    arm_dir.mkdir()
                    config = recovery / (label + ".json")
                    options = ({"worker": recovery / (label + "_worker.py"), "helper_dir": recovery}
                               if registered else {})
                    capture_path = arm_dir / "capture.json"
                    capture = child("deliver", [snapshot_path, config, capture_path], arm_dir, **options)
                    if capture.get("status") != "OK":
                        raise ValueError(f"{label} delivery failed: {capture.get('reason', 'unknown')}")
                    if (capture["upstream_sha256"] != up or fingerprint(load(snapshot_path)) != up or
                            capture["selection"] != configs[label]["selection"]):
                        raise PairingError("Arm did not use frozen inputs")
                    score = assess(case, capture, expected, label)
                    score.update(run_id=run_id, receipt_path=capture_path.relative_to(out).as_posix(),
                                 receipt_sha256=digest(capture_path.read_bytes()),
                                 qid=qid, upstream_sha256=up, fixed_config_sha256=fingerprint(FIXED),
                                 evaluator_version=EVALUATOR, candidate_id=arms[label]["candidate_id"])
                    row["arms"][label] = score
                a, b = row["arms"]["A"], row["arms"]["B"]
                compare_pair(a, b)
                delta = b["context_chars"] - a["context_chars"]
                length_ok = expected.get("length_change") not in {"equal", "shorter"} or (
                    delta == 0 if expected["length_change"] == "equal" else delta < 0)
                row.update(status="PAIRED", delta_chars=delta,
                           delta_bytes=b["context_utf8_bytes"] - a["context_utf8_bytes"],
                           meets_expectation=not (a["faults"] or b["faults"]) and length_ok and expected["status"] == "PAIRED")
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
            row.update(status="FAILED", error=str(exc),
                       failure_stage="pairing" if isinstance(exc, PairingError) else "execution")
        save(directory / "result.json", row)
    summary = {"run_id": run_id, "experiment_id": manifest["experiment_id"], "total": len(records),
               "paired": sum(r["status"] == "PAIRED" for r in records),
               "blocked": sum(r["status"] == "BLOCKED" for r in records),
               "expected_blocked": sum(r["status"] == "BLOCKED" and r["meets_expectation"] for r in records),
               "failed": sum(not r["meets_expectation"] for r in records),
               "actual_external_model_calls": 0, "answer_quality": "NOT_RUN", "model_latency": "NOT_RUN",
               "token_usage": "UNKNOWN", "cost_benefit": "NOT_RUN"}
    summary["status"] = "FAIL" if summary["failed"] else "PASS"
    save(out / "results.json", {"summary": summary, "rows": records})
    (out / "report.md").write_text(
        f"# {manifest['experiment_id']}\n\n总题数 {summary['total']}，有效配对 {summary['paired']}，"
        f"预期阻断 {summary['expected_blocked']}，工程断言 {summary['status']}。\n\n"
        "|题号|实际状态|符合预期|字符变化 B−A|\n|---|---|---|---|\n" +
        "".join(f"|{r['qid']}|{r['status']}|{r['meets_expectation']}|{r.get('delta_chars','—')}|\n" for r in records),
        encoding="utf-8")
    if registered:
        from rsi_feedback import write_feedback
        write_feedback(out, run_id)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    summary = run(args.manifest, args.out)
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(0 if summary["status"] == "PASS" else 1)

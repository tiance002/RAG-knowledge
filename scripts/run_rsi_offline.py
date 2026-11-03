"""Paired, offline delivery experiments. Provider/SQL fixtures run in child processes."""
import argparse
import hashlib
import importlib.metadata
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


def compare_pair(a, b):
    for key in ("qid", "upstream_sha256", "fixed_config_sha256", "evaluator_version"):
        if not a.get(key) or a[key] != b.get(key):
            raise ValueError(f"Incomparable arms: {key}")


def child(stage, paths, cwd):
    # Never import the legacy CLI or install its process-global patches in this interpreter.
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA", "HOME", "USERPROFILE",
        "RAG_TEST_EXTRA_SITE_PACKAGES"}}
    env.update(PYTHONIOENCODING="utf-8", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               DATABASE_URL="sqlite://", LANGFUSE_ENABLED="false", RAG_OBSERVABILITY_ENABLED="false",
               RAG_MODEL_ROUTER_MODE="OFF", RAG_SNAPSHOTS_ENABLED="false")
    with (cwd / "worker.log").open("xb") as log:
        subprocess.run([sys.executable, str(WORKER), stage, *map(str, paths)], env=env, cwd=cwd,
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


def run(manifest_path, out):
    manifest_path = Path(manifest_path).resolve()
    out = Path(out).resolve()
    if out.exists():
        raise FileExistsError(out)
    manifest = load(manifest_path)
    head = subprocess.check_output(["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True).strip()
    if manifest["status"] != "READY" or head != manifest["base_commit"]:
        raise ValueError("Manifest is not ready or the baseline commit differs")
    subprocess.run(["git", "-C", str(ROOT), "diff", "--quiet", head, "--", "backend/app"], check=True)
    if manifest["fixed"] != FIXED or manifest["evaluator_version"] != EVALUATOR:
        raise ValueError("Unsupported fixed configuration or evaluator")
    arms = manifest["arms"]
    if set(arms) != {"A", "B"} or arms["B"]["parent_id"] != arms["A"]["candidate_id"]:
        raise ValueError("Expected two arms from the declared baseline")
    configs = {label: artifact(manifest_path.parent, spec) for label, spec in arms.items()}
    if configs["A"] != {"selection": "all"}:
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
    sources = sorted({Path(__file__).resolve(), WORKER, *WORKER.parent.glob("rsi_*replay*.py"),
                      *WORKER.parent.glob("*replay_source*.py"), *WORKER.parent.glob("rsi_*delivery*.py")})
    source_hashes = {}
    for source in sources:
        shutil.copyfile(source, recovery / source.name)
        source_hashes[source.name] = digest(source.read_bytes())
    for label, cfg in configs.items():
        save(recovery / (label + ".json"), cfg)
    save(out / "versions.json", {"base_commit": head, "implementation": source_hashes,
         "python": sys.version, "executable": sys.executable, "evaluator": EVALUATOR})
    records = []
    for index, item in enumerate(cases, 1):
        qid, case = item["qid"], item["case"]
        expected = rubric[qid]
        directory = out / f"case-{index:03}"
        directory.mkdir()
        save(directory / "input.json", case)
        row = {"qid": qid, "input_sha256": fingerprint(case), "seed_ids": case["seed_ids"],
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
                up = fingerprint(snapshot)
                row.update(upstream_sha256=up)
                for label in ("A", "B"):
                    arm_dir = directory / label
                    arm_dir.mkdir()
                    config = recovery / (label + ".json")
                    capture = child("deliver", [snapshot_path, config, arm_dir / "capture.json"], arm_dir)
                    if (capture["upstream_sha256"] != up or fingerprint(load(snapshot_path)) != up or
                            capture["selection"] != configs[label]["selection"]):
                        raise ValueError("Arm did not use frozen inputs")
                    score = assess(case, capture, expected, label)
                    score.update(qid=qid, upstream_sha256=up, fixed_config_sha256=fingerprint(FIXED),
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
            row.update(status="FAILED", error=str(exc))
        save(directory / "result.json", row)
    summary = {"experiment_id": manifest["experiment_id"], "total": len(records),
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
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    summary = run(args.manifest, args.out)
    print(json.dumps(summary, ensure_ascii=False))
    sys.exit(0 if summary["status"] == "PASS" else 1)

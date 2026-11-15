"""Read an archived P0 scored trace. Historical observations are not native RSI runs."""
import json
from pathlib import Path

from rsi_candidates import sha


def one(items, label):
    items = list(items)
    if len(items) != 1:
        raise ValueError(f"expected one {label}, found {len(items)}")
    return items[0]


def read_observation(spec, evidence_root):
    if spec["format"] != "p0-live10-v1":
        raise ValueError("unsupported historical format")
    base = Path(evidence_root).resolve()
    payload = {}
    for role, ref in spec["files"].items():
        path = (base / ref["path"]).resolve()
        if not path.is_relative_to(base):
            raise ValueError("evidence path outside approved root")
        data = path.read_bytes()
        if sha(data) != ref["sha256"]:
            raise ValueError(f"historical evidence changed: {role}")
        if role != "original_pdf":
            payload[role] = data.decode("utf-8-sig")

    qid, tid = spec["qid"], spec["trace_id"]
    manifest = json.loads(payload["manifest"])
    frozen = one((r for r in manifest["inputs"] if r["qid"] == qid), "manifest input")
    result = one((r for r in map(json.loads, payload["results"].splitlines())
                  if r["qid"] == qid and r["trace_id"] == tid), "result")
    events = [e for e in map(json.loads, payload["events"].splitlines()) if e["trace_id"] == tid]

    def event(stage, kind="stage"):
        return one((e["metadata"] for e in events if e["stage"] == stage and e["event"] == kind), stage)

    root = event("p0.smoke.question", "start")
    if (root["qid"] != qid or root["kb_id"] != frozen["kb_id"] or
            manifest["experiment_version"] != root["experiment_version"] or root["experiment_version"] != spec["format"]):
        raise ValueError("trace root and frozen case do not match")
    annotations = [r["gold"] for r in json.loads(payload["gold"])["cases"] if r.get("qid") == qid]
    if not annotations or any(g != annotations[0] for g in annotations):
        raise ValueError("missing or conflicting frozen Gold")
    gold = annotations[0]
    if (gold["expected_source_sha256"] != spec["files"]["original_pdf"]["sha256"] or
            sha(gold["question"].encode()) != frozen["question_sha256"]):
        raise ValueError("source or question differs from frozen Gold")

    # Later SSE is used solely as a byte dictionary. It cannot witness delivery on this trace.
    citations = one((item["citations"] for item in
                     (json.loads(l[6:]) for l in payload["body_witness"].splitlines() if l.startswith("data: {"))
                     if "citations" in item), "witness citation list")
    stages = {s: event(s)["candidates"] for s in ("retrieval.before_dedup", "retrieval.after_dedup", "rerank.filtered")}
    expansions = [e["metadata"] for e in events if e["stage"] == "retrieval.context_expansion"]
    mentioned = {cid for e in expansions for cid in (
        [e["chunk_id"]] + e.get("previous_ids", []) + e.get("next_ids", []) + e.get("sibling_ids", []))}
    facts = []
    for target in spec["targets"]:
        cid = target["chunk_id"]
        witness = one((r for r in citations if r["chunk_id"] == cid), "matching source member")
        text = witness["content"]
        ranks = {}
        for name, rows in stages.items():
            row = one((r for r in rows if r["id"] == cid), f"{name} target")
            if (row["content_sha256"] != sha(text.encode()) or row["page_number"] != target["page"]
                    or row["file_id"] != witness["fileId"]):
                raise ValueError("stage identity differs from witnessed text")
            ranks[name] = row["rank"]
        if (target["answer_point"] not in gold["answer_points"] or target["page"] not in gold["expected_pages"]
                or not all(t in text for t in target["required_text"])):
            raise ValueError("Gold or field text mismatch")
        facts.append({"chunk_id": cid, "page": target["page"], "answer_point": target["answer_point"],
                      "content_sha256": sha(text.encode()), "ranks": ranks, "named_in_expansion_events": cid in mentioned})
    return {"type": "rsi-historical-observation-v1", "scope": "development",
            "qid": qid, "source_trace_id": tid, "experiment_version": root["experiment_version"],
            "dataset_freeze_sha256": manifest["dataset_freeze_sha256"], "source_runner_sha256": manifest["runner_sha256"],
            "files": spec["files"], "facts": facts, "stage_counts": {s: len(v) for s, v in stages.items()},
            "context_sha256": result["context_sha256"], "answer_chars": result["answer_chars"],
            "finish_reason": event("llm.response")["finish_reason"], "prompt_receipt": event("llm.request")["prompt"],
            "reviewed_disposition": spec["reviewed_disposition"], "native_rsi_run_id": None}

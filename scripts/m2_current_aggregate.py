"""Aggregate frozen current M2 artifacts without importing any application/runner code.

Only explicit per-case reviews supply quality judgments. Raw review fields remain
available next to normalized statuses; this script never grades answer text.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import itertools
import json
from pathlib import Path
import statistics

ROOT = Path(__file__).resolve().parents[1]
BASE = ROOT / "var/preferences-acceptance"
RUNS = ("m2-current-real-v3", "m2-current-resume-real-v4",
        "m2-current-resume16-real-v2", "m2-current-resume11-real-v2")
REVIEWS = {
    RUNS[0]: ("review-office-current.jsonl", "review-hotpot-current.jsonl", "review-pdf-current.jsonl"),
    RUNS[1]: ("review-hotpot-resume.jsonl", "review-pdf-resume.jsonl"),
    RUNS[2]: ("review-hotpot-resume16.jsonl", "review-pdf-resume16.jsonl"),
    RUNS[3]: ("review-hotpot-resume11.jsonl", "review-pdf-resume11.jsonl"),
}
METRICS = {
    "core": ("core_correctness", "core"),
    "core_evidence_delivery": ("core_evidence_delivery",),
    "core_inline_citation_recall": ("core_inline_citation_recall",),
    "core_request_faithfulness": ("core_request_faithfulness",),
    "whole_answer_faithfulness": ("whole_answer_faithfulness", "faithfulness", "full_faithfulness"),
    "citation_support": ("citation_grounding", "citation_support"),
    "original_chart_core_support": ("original_chart_core_support",),
    "delivered_citation_core_support": ("delivered_citation_core_support",),
    "technical_gold_literal_or": ("technical_gold_literal_or",),
    "technical_historical_strict_all_three": ("technical_historical_strict_all_three",),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rows(path):
    return [(n, json.loads(line)) for n, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), 1) if line.strip()]


def identity(row):
    """Full exact experiment identity; no substring/suffix/fuzzy matching."""
    require(type(row.get("repetition")) is int, "Invalid repetition")
    return tuple(row[k] for k in ("qid", "mode", "arm", "repetition"))


def scoped_match(row, trial):
    return (row.get("run_id") == trial["run_id"] and identity(row) == identity(trial)
            and row.get("dataset_version") == trial["dataset_version"]
            and row.get("kb_scope") == trial["kb_scope"])


def normalize_metric(review, metric, *, negative=False, final_answer=True, technical=False):
    """Preserve uncertainty and qualified results; never turn null into zero."""
    field = next((k for k in METRICS[metric] if k in review), None)
    raw = review.get(field) if field else None
    applicable = True
    if metric.startswith("technical_") and not technical:
        applicable = False
    if metric == "original_chart_core_support" and "mmlongbench-doc" not in review.get("qid", ""):
        applicable = False
    if not applicable:
        return {"status": "N/A", "value": None, "raw": raw, "source_field": field}
    if raw is None:
        # An answerable-source recall denominator does not exist on a negative
        # trial. A trial with no answer is still UNKNOWN, never a successful refusal.
        status = "N/A" if metric == "core_inline_citation_recall" and negative and final_answer else "UNKNOWN"
        return {"status": status, "value": None, "raw": raw, "source_field": field}
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        require(0 <= raw <= 1, f"Invalid ratio: {metric}")
        return {"status": "PASS" if raw == 1 else "FAIL" if raw == 0 else "PARTIAL",
                "value": float(raw), "raw": raw, "source_field": field}
    status = str(raw).upper()
    if status in ("NOT_APPLICABLE", "N/A"):
        status = "N/A"
    # PASS on categorical delivery is only the review's binary trial judgment,
    # not a fabricated evidence-unit denominator.
    return {"status": status, "value": None, "raw": raw, "source_field": field}


def review_index(run, trials):
    result, headers, hashes = {}, [], {}
    for name in REVIEWS[run.name]:
        path = run / name
        hashes[str(path.relative_to(ROOT)).replace("\\", "/")] = sha(path)
        for line, review in rows(path):
            pointer = {"path": str(path), "line": line}
            if "qid" not in review:
                headers.append({"pointer": pointer, "record": review})
                continue
            key = identity(review)
            require(key in trials, "Review has no exact trial identity")
            trial = trials[key][1]
            require(review.get("run_id") == trial["run_id"], "Review run mismatch")
            for k in ("dataset_version", "kb_scope"):
                if k in review:
                    require(review[k] == trial[k], "Review scope/version mismatch")
            require(review.get("provider_call_ids") == trial["provider_call_ids"], "Review call IDs mismatch")
            if "trial_line" in review:
                require(review["trial_line"] == trials[key][0], "Review trial line mismatch")
            require(key not in result, "Duplicate review identity")
            result[key] = (pointer, review)
    require(set(result) == set(trials), "Missing per-case review")
    return result, headers, hashes


def response_metadata(path):
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
        return data
    except json.JSONDecodeError:
        stream = [json.loads(line[5:].strip()) for line in text.splitlines()
                  if line.startswith("data:") and line[5:].strip() not in ("", "[DONE]")]
        return next((x for x in reversed(stream) if x.get("usage")), {})


def audit_ledger(run, trials):
    latest, refs = {}, {}
    for line, record in rows(run / "provider-ledger.jsonl"):
        n = record["call_id"]
        require(type(n) is int and n > 0, "Invalid call ID")
        if n in latest:
            old = latest[n][1]
            require(old["state"] == "UNKNOWN_PENDING" and record["state"] in ("COMPLETED", "UNKNOWN"), "Invalid ledger transition")
            require(all(record.get(k) == v for k, v in old.items() if k != "state"), "Ledger identity/reservation mutated")
        else:
            require(record["state"] == "UNKNOWN_PENDING", "Missing pending reservation")
        latest[n] = (line, record)
    for _, trial in trials.values():
        for n in trial["provider_call_ids"]:
            require(n not in refs, "Call assigned to multiple trials")
            refs[n] = trial
    require(set(refs) == set(latest), "Ledger has missing/unassigned calls")
    audited = {}
    for n, (line, record) in latest.items():
        require(scoped_match(record, refs[n]), "Scoped call identity mismatch")
        request = run / f"request-{n:04d}.json"
        require(request.exists() and sha(request) == record["request_sha256"], "Request hash mismatch")
        item = {"run_id": run.name, "call_id": n, "ledger_line": line,
                "state": record["state"], "request_sha256": record["request_sha256"],
                "request_path": str(request), "reservation_cny": record["reservation_cny"],
                "provider_request_id": record.get("provider_request_id"),
                "usage": None, "price_based_cost_cny": None, "latency_ms": record.get("latency_ms")}
        if record["state"] == "COMPLETED":
            response = run / f"response-{n:04d}.body"
            data = response_metadata(response)
            require(data.get("usage") == record.get("usage") and data.get("model") == record.get("actual_model")
                    and data.get("id") == record.get("provider_request_id"), "Response receipt mismatch")
            usage = record.get("usage") or {}
            require(all(type(usage.get(k)) is int and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens")), "Invalid usage")
            require(record.get("cost_status") == "PRICE_BASED_NOT_PROVIDER_BILL", "Unexpected cost basis")
            item.update(usage=usage, price_based_cost_cny=record.get("price_based_cost_cny"),
                        response_path=str(response), response_sha256=sha(response),
                        returned_model=record.get("actual_model"))
        audited[n] = item
    return audited


def mean(values):
    values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]
    return {"known_count": len(values), "sum": sum(values), "mean": statistics.mean(values) if values else None}


def summarize(trials):
    result = {"recorded": len(trials), "final_answer": sum(t["final_answer"] for t in trials),
              "no_final_answer": sum(not t["final_answer"] for t in trials),
              "trial_states": dict(Counter(t["state"] for t in trials)), "metrics": {}}
    for metric in METRICS:
        values = [t["metrics"][metric] for t in trials]
        result["metrics"][metric] = {"status_counts": dict(Counter(x["status"] for x in values)),
                                     "numeric_only": mean([x["value"] for x in values])}
    result["trial_latency_ms"] = mean([t["latency_ms"] for t in trials])
    calls = [c for t in trials for c in t["provider_calls"]]
    completed = [c for c in calls if c["state"] == "COMPLETED"]
    unknown = [c for c in calls if c["state"] != "COMPLETED"]
    result["provider"] = {
        "actual_calls": len(calls), "completed_calls": len(completed), "unknown_calls": len(unknown),
        "known_prompt_tokens": sum(c["usage"]["prompt_tokens"] for c in completed),
        "known_completion_tokens": sum(c["usage"]["completion_tokens"] for c in completed),
        "all_calls_token_total_status": "UNKNOWN" if unknown else "KNOWN_RECEIPTS",
        "known_price_based_cost_cny": str(sum((Decimal(c["price_based_cost_cny"]) for c in completed), Decimal(0))),
        "unknown_full_reservation_cny": str(sum((Decimal(c["reservation_cny"]) for c in unknown), Decimal(0))),
        "provider_bill": "UNKNOWN", "completed_call_latency_ms": mean([c["latency_ms"] for c in completed])}
    for name in ("tool_search_stage_count", "scheduled_retry_stage_count"):
        result[name] = mean([t["observed"][name] for t in trials])
    result["retrieval_status_review"] = dict(Counter(t["review_raw"].get("retrieval_status_review", "UNKNOWN") for t in trials))
    return result


def aggregate(base=BASE):
    dataset_path = base / "m2-current-dataset-v3.json"
    dataset = json.loads(dataset_path.read_bytes())
    cases = {c["qid"]: c for c in dataset["cases"]}
    planned = set(itertools.product(cases, dataset["modes"], dataset["arms"], range(1, dataset["repetitions"] + 1)))
    combined, output, headers, sources, run_summaries = {}, [], [], {}, {}
    sources[str(dataset_path.relative_to(ROOT)).replace("\\", "/")] = sha(dataset_path)
    for name in RUNS:
        run = base / name
        for filename in ("manifest.json", "summary.json", "dataset.json", "trials.jsonl", "provider-ledger.jsonl", "observations.jsonl", *REVIEWS[name]):
            path = run / filename
            sources[str(path.relative_to(ROOT)).replace("\\", "/")] = sha(path)
        require(json.loads((run / "dataset.json").read_bytes()) == dataset, "Frozen dataset mismatch")
        indexed = {}
        for line, trial in rows(run / "trials.jsonl"):
            key = identity(trial)
            require(key in planned and key not in combined and key not in indexed, "Duplicate or unplanned trial")
            require(trial["run_id"] == name and trial["dataset_version"] == dataset["dataset_version"]
                    and trial["kb_scope"] == cases[key[0]]["kb_scope"], "Trial scope/version mismatch")
            indexed[key] = (line, trial)
        reviews, run_headers, review_hashes = review_index(run, indexed)
        headers.extend(run_headers)
        require(all(sources[path] == value for path, value in review_hashes.items()), "Review changed during aggregation")
        ledger = audit_ledger(run, indexed)
        observation_rows = rows(run / "observations.jsonl")
        # Load once, retaining exact per-trial matching.
        for key, (line, trial) in indexed.items():
            pointer, review = reviews[key]
            done = [e.get("data", {}) for e in trial["events"] if e.get("event") == "done"]
            has_content = any(e.get("event") == "answer_chunk" and e.get("data", {}).get("content") for e in trial["events"])
            final_answer = bool(done and has_content)
            technical = key[0].endswith(":m2:tribometer-mechanism")
            item = {k: trial[k] for k in ("run_id", "qid", "mode", "arm", "repetition", "dataset_version", "kb_scope", "state", "provider_call_ids", "latency_ms")}
            item.update(trial_id=f"{name}:{line}", trial_pointer={"path": str(run / "trials.jsonl"), "line": line},
                        review_pointer=pointer, negative=bool(cases[key[0]].get("negative")), final_answer=final_answer,
                        terminal=done[-1] if done else None, review_raw=review,
                        provider_calls=[ledger[n] for n in trial["provider_call_ids"]])
            item["metrics"] = {m: normalize_metric(review, m, negative=item["negative"], final_answer=final_answer,
                                                  technical=technical) for m in METRICS}
            require(final_answer or item["metrics"]["core"]["status"] == "UNKNOWN", "No-final trial incorrectly graded")
            item["observed"] = observed_metrics_from_rows(observation_rows, trial)
            combined[key] = item
            output.append(item)
        summary = json.loads((run / "summary.json").read_bytes())
        run_items = [x for x in output if x["run_id"] == name]
        stats = summarize(run_items)
        require(summary["recorded_trials"] == stats["recorded"] and summary["actual_http_calls"] == stats["provider"]["actual_calls"], "Run summary counts mismatch")
        require(summary["prompt_tokens"] == stats["provider"]["known_prompt_tokens"] and summary["completion_tokens"] == stats["provider"]["known_completion_tokens"], "Run summary usage mismatch")
        run_summaries[name] = stats
    require(set(combined) == planned, "Unrecorded planned identities")
    # Detect modifications of source artifacts during aggregation.
    require(all(sha(ROOT / path) == value for path, value in sources.items()), "Source changed during aggregation")
    groups = []
    for mode in dataset["modes"]:
        for arm in dataset["arms"]:
            group_trials = [t for t in output if t["mode"] == mode and t["arm"] == arm]
            groups.append({"mode": mode, "arm": arm, "planned": len(cases) * dataset["repetitions"],
                           **summarize(group_trials), "completed_answers_only": summarize([t for t in group_trials if t["final_answer"]])})
    return {"schema_version": "m2-current-reviewed-aggregate/v1", "status": "AGGREGATED_REQUIRES_OWNER_ACCEPTANCE",
            "generated_at_utc": datetime.now(timezone.utc).isoformat(), "model_calls": 0,
            "dataset_version": dataset["dataset_version"], "dataset_sha256": sha(dataset_path),
            "configuration": {k: dataset[k] for k in ("arms", "modes", "repetitions", "fixed_search", "model", "max_output_tokens")},
            "planned_trials": len(planned), "overall": summarize(output),
            "completed_answers_only": summarize([t for t in output if t["final_answer"]]), "by_mode_arm": groups,
            "by_run": run_summaries, "by_case": [{"qid": qid, **summarize([t for t in output if t["qid"] == qid])} for qid in cases],
            "source_sha256": sources, "source_review_headers": headers, "trials": output,
            "limitations": [
                "仅聚合已有独立逐题审查，不重新评分，不调用模型；执行完成不等于业务验收。",
                "core 按冻结 Gold；技术题 literal OR 与历史 strict all-three 分开，绝不用后者替换 Gold。",
                "UNKNOWN、N/A、PARTIAL、ABSENT、PASS_WITH_SOURCE_ALIGNMENT_LIMIT 均保留，null 不补零。",
                "Office delivery PASS 为二值逐题判定；Hotpot 为原始证据单元比例。混合汇总仅给状态数与 numeric_only，不伪造共同的证据单元分母。",
                "core_request_faithfulness 缺少独立审查时 UNKNOWN；Office faithfulness 和 PDF full_faithfulness 仅映射到全文忠实性。",
                "PDF 原图支持不等于扁平文本完成语义送达；source alignment 限定通过单列，负题无答案不算成功拒答。",
                "原始 PDF citation 的 page_number 为 null、location 为未知位置；物理页/原图关系由独立来源审查提供，不把缺失定位自动补成已支持的行列关系。",
                "核心编号引用召回仅保留审查数值；不可回答且有最终答案的 null 为 N/A，无最终答案或正题 null 为 UNKNOWN。全文 Citation Precision UNKNOWN。",
                "provider token/费用按 run_id+qid+mode+arm+repetition+dataset_version+kb_scope 精确匹配，并核对归档 request hash 与 response usage/model/id；UNKNOWN 调用不补 token，保留全额占用。费用为价格计算，供应商账单 UNKNOWN。",
                "耗时均为归档实测；工具/重试仅统计完整 qa.request start/end 的精确 attempt_id 观测；stage count 不扩称所有工具行为。",
                "四组每题仅两次，不能据此推断统计显著性、生产整体质量或偏好的因果影响。真实运行入口是 ChatService.chat_stream，非 HTTP/SSE 网络验收。",
            ]}


def observed_metrics_from_rows(observation_rows, trial):
    # Local trace IDs use the runner's frozen attempt-ID formula. Never join qid alone.
    attempt = f"{trial['mode']}-{trial['arm']}-r{trial['repetition']}-{hashlib.sha256(trial['qid'].encode()).hexdigest()[:12]}"
    selected = [r for _, r in observation_rows if r.get("run_id") == trial["run_id"] and r.get("qid") == trial["qid"]
                and r.get("dataset_version") == trial["dataset_version"] and r.get("attempt_id") == attempt]
    complete = all(any(r.get("stage") == "qa.request" and r.get("event") == event for r in selected) for event in ("start", "end"))
    return {"attempt_id": attempt, "matched_rows": len(selected), "request_start_and_end": complete,
            "tool_search_stage_count": sum(r.get("stage") == "agent.tool_search" for r in selected) if complete else None,
            "scheduled_retry_stage_count": sum(r.get("stage") == "agent.retry_decision" and r.get("metadata", {}).get("action") == "SCHEDULED" for r in selected) if complete else None}


def report(result):
    overall = result["overall"]
    answered_core = result["completed_answers_only"]["metrics"]["core"]["status_counts"]
    lines = ["# 当前解析器 M2 四次真实运行审查聚合", "", f"数据集 `{result['dataset_version']}`；SHA256 `{result['dataset_sha256']}`。",
             f"已记录 {overall['recorded']}/{result['planned_trials']} 个唯一身份，有最终答案 {overall['final_answer']}，无最终答案 {overall['no_final_answer']}。本聚合新增模型调用 0。验收状态待负责人确认。", "",
             f"有最终答案的 {overall['final_answer']} 项：核心 {json.dumps(answered_core, ensure_ascii=False)}。全部计划分母是 {result['planned_trials']}；无最终答案全部保留核心 UNKNOWN，已有答案仍可能核心 UNKNOWN。两者分母含义不同，不作生产正确率推断。", "",
             "| 模式 | 组 | 已记录 | 有答案 | 无最终答案 | 核心 PASS | PARTIAL | FAIL | UNKNOWN |", "|---|---|---:|---:|---:|---:|---:|---:|---:|"]
    for group in result["by_mode_arm"]:
        counts = group["metrics"]["core"]["status_counts"]
        lines.append(f"| {group['mode']} | {group['arm']} | {group['recorded']} | {group['final_answer']} | {group['no_final_answer']} | " + " | ".join(str(counts.get(k, 0)) for k in ("PASS", "PARTIAL", "FAIL", "UNKNOWN")) + " |")
    lines += ["", "| 指标 | 全部 96 项状态（包含 UNKNOWN 与 N/A） | 数值可判定分母 / 均值 |", "|---|---|---|"]
    for metric, values in overall["metrics"].items():
        numeric = values["numeric_only"]
        lines.append(f"| {metric} | {json.dumps(values['status_counts'], ensure_ascii=False)} | {numeric['known_count']} / {numeric['mean']} |")
    lines += ["", "数值分母只包含原始数值，分类 PASS 不擅自变成数值；两种证据送达 schema 不能直接合并为证据单元召回率。每组完整状态分布及逐题原始值见同名 JSON。", "",
              "| 模式 | 组 | 完成/UNKNOWN 调用 | 已知输入/输出 Token | 价格计算元 | UNKNOWN 占用元 | 平均试验秒 | 工具检索 stage 均值 | 重试 stage 均值 |", "|---|---|---|---|---|---|---:|---:|---:|"]
    for group in result["by_mode_arm"]:
        p = group["provider"]
        lines.append(f"| {group['mode']} | {group['arm']} | {p['completed_calls']}/{p['unknown_calls']} | {p['known_prompt_tokens']}/{p['known_completion_tokens']} | {p['known_price_based_cost_cny']} | {p['unknown_full_reservation_cny']} | {group['trial_latency_ms']['mean']/1000:.3f} | {group['tool_search_stage_count']['mean']} | {group['scheduled_retry_stage_count']['mean']} |")
    p = overall["provider"]
    lines += ["", f"当前四次运行完成 {p['completed_calls']} 次供应商调用，UNKNOWN {p['unknown_calls']} 次；已知输入 {p['known_prompt_tokens']}、输出 {p['known_completion_tokens']} Token；已知价格计算 {p['known_price_based_cost_cny']} 元，UNKNOWN 全额占用 {p['unknown_full_reservation_cny']} 元。此处只统计当前四次运行；不覆盖先前历史占用，也不是供应商账单。", "",
              "耗时表每组试验平均分母为 12，包含无最终答案的停止等待时间；完成答案子集的耗时另见 JSON completed_answers_only。供应商调用耗时仅对 220 个完成回执取均值，UNKNOWN 不补值；工具与重试 stage 均值每组分母 12，96 项均有 qa.request start/end。", "",
              "## 分题与技术题双标准", "", "| 题号 | 有答案/记录 | 核心状态 |", "|---|---|---|"]
    for case in result["by_case"]:
        lines.append(f"| {case['qid']} | {case['final_answer']}/{case['recorded']} | {json.dumps(case['metrics']['core']['status_counts'], ensure_ascii=False)} |")
    lines += ["", "技术题冻结原文是 `coefficient of friction, friction force, or wear volume`，literal OR 是当前 Gold。历史 strict all-three 单独展示，不回写 Gold。", "",
              "| 模式 | 组 | literal OR | historical strict all-three |", "|---|---|---|---|"]
    technical = [t for t in result["trials"] if t["qid"].endswith(":m2:tribometer-mechanism")]
    for mode in result["configuration"]["modes"]:
        for arm in result["configuration"]["arms"]:
            group = summarize([t for t in technical if t["mode"] == mode and t["arm"] == arm])
            lines.append(f"| {mode} | {arm} | {json.dumps(group['metrics']['technical_gold_literal_or']['status_counts'])} | {json.dumps(group['metrics']['technical_historical_strict_all_three']['status_counts'])} |")
    lines += ["", "## 证据口径与限制", ""] + [f"- {x}" for x in result["limitations"]]
    lines += ["", "## 原始来源", ""] + [f"- `{name}`：{stats['recorded']} 项；保留原始 run_id、trial 行号、provider_call_ids。" for name, stats in result["by_run"].items()]
    lines += ["", "所有源文件 SHA256、逐条审查路径/行号、原始异构字段、精确调用连接和响应回执哈希均在 JSON 中。聚合前后复核源文件哈希，未覆盖原始记录。", ""]
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, help="New JSON file under var/preferences-acceptance, prefix m2-current-aggregate")
    args = parser.parse_args()
    try:
        output = Path(args.output).resolve()
        require(output.parent == BASE and output.name.startswith("m2-current-aggregate") and output.suffix == ".json", "Invalid output path")
        md = output.with_suffix(".md")
        require(not output.exists() and not md.exists(), "Output exists; immutable artifacts must not be overwritten")
        result = aggregate()
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        md.write_text(report(result), encoding="utf-8")
        print(json.dumps({"status": result["status"], "model_calls": 0, "recorded": result["overall"]["recorded"],
                          "final_answer": result["overall"]["final_answer"], "no_final_answer": result["overall"]["no_final_answer"],
                          "json": str(output), "report": str(md), "json_sha256": sha(output)}, ensure_ascii=True))
        return 0
    except (ValueError, KeyError, OSError) as exc:
        print(json.dumps({"status": "BLOCKED", "model_calls": 0, "error": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

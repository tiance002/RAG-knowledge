"""Read-only M2 trial/ledger/review analysis. Writes sanitized reports under var.

This tool never calls a model or judges unreviewed answers. Citation support in
original visuals and in delivered text remain distinct independent metrics.
"""
from __future__ import annotations

import argparse
from collections import Counter
from decimal import Decimal
import hashlib
from itertools import combinations
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARMS = ("A", "B", "C", "D")
MODES = ("chat", "agent")
REVIEW_FILES = ("review-office001.jsonl", "review-office031.jsonl", "review-hotpot.jsonl",
                "review-pdf.jsonl", "review-technical.jsonl")
CATEGORICAL = ("core", "faithfulness", "citation_support", "citation_support_original",
               "citation_support_delivered_text", "core_evidence_delivery")
LABELS = ("PASS", "FAIL", "PARTIAL", "UNKNOWN", "NOT_APPLICABLE")
NUMERIC = ("core_inline_citation_recall", "context_recall", "exact_qrel_chunk_text_recall",
           "latency_ms", "prompt_tokens", "completion_tokens", "price_derived_cost_cny",
           "provider_http_calls", "bm25_candidate_count", "retrieved_candidate_count", "tool_calls", "retrieval_retries")


def sha(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def private(path):
    value = Path(path).resolve()
    if not value.is_relative_to((ROOT / "var").resolve()):
        raise ValueError("Inputs and outputs must stay under project var")
    return value


def key(row):
    return row["qid"], row["mode"], row["arm"], row["repetition"]


def number(value):
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return value


class Snapshot:
    """Detect concurrent writes and preserve exact source digests, including absence."""
    def __init__(self):
        self.files = {}

    def read(self, path, *, optional=False):
        path = private(path)
        if not path.exists():
            if not optional:
                raise FileNotFoundError(path.name)
            self.files[path] = None
            return None
        raw = path.read_bytes()
        digest = sha(raw)
        previous = self.files.setdefault(path, digest)
        if previous != digest:
            raise ValueError("Input changed while taking immutable analysis snapshot")
        return raw

    def rows(self, path, *, optional=False):
        return [row for _, row in self.records(path, optional=optional)]

    def records(self, path, *, optional=False):
        raw = self.read(path, optional=optional)
        if raw is None:
            return []
        result = []
        for lineno,line in enumerate(raw.decode("utf-8-sig").splitlines(),1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError("JSONL rows must be objects")
            result.append((lineno,row))
        return result

    def verify(self):
        for path, digest in self.files.items():
            actual = sha(path.read_bytes()) if path.exists() else None
            if actual != digest:
                raise ValueError("Input changed during analysis; refuse inconsistent output")

    def provenance(self):
        return [{"path": str(path), "sha256": digest, "status": "PRESENT" if digest else "MISSING"}
                for path, digest in sorted(self.files.items(), key=lambda row: str(row[0]))]


def indexed(rows, kind):
    output = {}
    for row in rows:
        identity = key(row)
        if identity in output:
            raise ValueError(f"Duplicate {kind} identity")
        output[identity] = row
    return output


def latest_attempts(rows):
    attempts = {}
    for row in rows:
        call_id = row.get("call_id")
        if type(call_id) is not int or call_id <= 0:
            raise ValueError("Invalid provider call identity")
        old = attempts.get(call_id)
        if old and old.get("state") != "UNKNOWN_PENDING" and row != old:
            raise ValueError("Provider terminal record changed")
        attempts[call_id] = row
    return attempts


def provider_totals(trial, attempts, pricing):
    ids = trial.get("provider_call_ids")
    if not isinstance(ids, list) or len(ids) != len(set(ids)):
        raise ValueError("Missing or duplicated provider IDs in trial")
    known_prompt = known_completion = 0
    known_cost = Decimal(0)
    usage_complete = cost_complete = True
    unknown_calls = 0
    for call_id in ids:
        attempt = attempts.get(call_id)
        if attempt is None or any(attempt.get(field) != trial.get(field) for field in ("qid", "mode", "arm", "repetition")):
            raise ValueError("Trial and actual provider call identity disagree")
        usage = attempt.get("usage") or {}
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        valid = type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0
        if valid:
            known_prompt += prompt
            known_completion += completion
        else:
            usage_complete = False
        if attempt.get("state") != "COMPLETED" or not valid:
            cost_complete = False
            unknown_calls += 1
            continue
        if not pricing or attempt.get("price_based_cost_cny") is None:
            cost_complete = False
            continue
        derived = (Decimal(prompt) * Decimal(str(pricing["input_cny_per_million"])) +
                   Decimal(completion) * Decimal(str(pricing["output_cny_per_million"]))) / Decimal(1000000)
        if derived != Decimal(str(attempt["price_based_cost_cny"])):
            raise ValueError("Provider usage and recorded price-derived cost disagree")
        known_cost += derived
    return {"provider_http_calls": len(ids), "prompt_tokens": known_prompt if usage_complete else None,
            "completion_tokens": known_completion if usage_complete else None,
            "price_derived_cost_cny": str(known_cost) if cost_complete else None,
            "known_prompt_tokens_partial": known_prompt, "known_completion_tokens_partial": known_completion,
            "known_price_derived_cost_cny_partial": str(known_cost), "unknown_provider_calls": unknown_calls,
            "provider_retry_count": "UNKNOWN_NOT_DIRECTLY_RECORDED", "provider_call_ids": ids}


def observed_metrics(trial, observations):
    attempt_id = f"{trial['mode']}-{trial['arm']}-r{trial['repetition']}-{sha(trial['qid'].encode())[:12]}"
    rows = [row for row in observations if row.get("qid") == trial["qid"] and row.get("attempt_id") == attempt_id
            and (not trial.get("run_id") or row.get("run_id") == trial["run_id"])]
    # Zero is observable only when the request trace has an actual terminal event.
    trace_complete = any(row.get("stage") == "qa.request" and row.get("event") == "end" for row in rows)
    bm25 = [len(row["metadata"]["candidates"]) for row in rows if row.get("stage") == "retrieval.bm25"
            and isinstance(row.get("metadata", {}).get("candidates"), list)]
    reported_bm25 = [row['metadata']['metrics']['bm25_count'] for row in rows if row.get('stage') == 'retrieval.metrics'
                     and type(row.get('metadata', {}).get('metrics', {}).get('bm25_count')) is int]
    if reported_bm25:
        bm25 = reported_bm25
    outcomes = [row["metadata"]["result_count"] for row in rows if row.get("stage") == "retrieval.outcome"
                and type(row.get("metadata", {}).get("result_count")) is int]
    tools = [row for row in rows if row.get("stage") == "agent.tool_search"]
    decisions = [row for row in rows if row.get("stage") == "agent.retry_decision"]
    return {"observation_rows": len(rows), "trace_complete": trace_complete,
            "bm25_candidates_per_round": bm25, "retrieved_candidates_per_round": outcomes,
            "bm25_candidate_count": sum(bm25) if bm25 else None,
            "bm25_candidate_count_source": 'RETRIEVAL_METRICS' if reported_bm25 else 'CANDIDATE_EVENTS' if bm25 else 'UNKNOWN',
            "retrieved_candidate_count": sum(outcomes) if outcomes else None,
            "tool_calls": len(tools) if tools or trace_complete else None,
            "retrieval_retries": sum(row.get("metadata", {}).get("action") == "SCHEDULED" for row in decisions)
                if decisions or trace_complete else None}


def answer_request(trial, review, attempts, run_dir, snapshot):
    ids = trial.get("provider_call_ids", [])
    selected = review.get("final_request_id") if review else None
    if selected is None:
        selected = ids[-1] if ids else None
    if selected is None or selected not in ids or attempts.get(selected, {}).get("state") != "COMPLETED":
        return None, "NO_COMPLETED_FINAL_REQUEST", None
    raw = snapshot.read(run_dir / f"request-{selected:04d}.json", optional=True)
    response = snapshot.read(run_dir / f"response-{selected:04d}.body", optional=True)
    if raw is None or response is None:
        return None, "MISSING_REQUEST_OR_RESPONSE", None
    payload = json.loads(raw)
    try:
        data = json.loads(response)
        choice = data["choices"][0]
        message = choice["message"]
        if message.get("tool_calls") or not str(message.get("content") or "").strip():
            return None, "LAST_CALL_IS_NOT_ANSWER", choice.get("finish_reason")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
        # Never guess which streamed chunk corresponds to the final answer.
        return None, "FINAL_STREAM_RESPONSE_NOT_NORMALIZED", None
    return payload, "REVIEW_POINTER" if review and review.get("final_request_id") is not None else "LAST_COMPLETED_TRIAL_CALL", choice.get("finish_reason")


def trial_row(identity, case, trial, review, attempts, observations, manifest, run_dir, snapshot):
    qid, mode, arm, repetition = identity
    row = {"qid": qid, "mode": mode, "arm": arm, "repetition": repetition,
           "case_type": "negative" if case.get("negative") else "positive", "recorded": trial is not None,
           "review_present": review is not None, "review_usable": False}
    if trial is None:
        row.update({field: "UNKNOWN" for field in CATEGORICAL})
        row.update({field: None for field in NUMERIC})
        row.update(answered=False, missing_answer=True, empty_answer=False, truncated=False,
                   unknown_execution=True, execution_state="NOT_RUN", generation_status="NOT_RUN",
                   unknown_provider_calls=None, provider_call_ids=[],
                   context_recall_status="NOT_APPLICABLE" if case.get("negative") else "NOT_RUN")
        return row
    events = trial.get("events", [])
    answers = [event.get("data", {}).get("content", "") for event in events if event.get("event") == "answer_chunk"]
    answered = bool("".join(value for value in answers if isinstance(value, str)).strip())
    done = [event.get("data", {}) for event in events if event.get("event") == "done"]
    final = done[-1] if done else {}
    row.update(execution_state=trial.get("state", "UNKNOWN"), generation_status=final.get("generation_status", "UNKNOWN"),
               answered=answered, missing_answer=not answers, empty_answer=bool(answers) and not answered,
               truncated=final.get("generation_status") == "TRUNCATED", latency_ms=number(trial.get("latency_ms")),
               review_usable=bool(review), core_inline_citation_recall=None, context_recall=None,
               exact_qrel_chunk_text_recall=None)
    totals = provider_totals(trial, attempts, manifest.get("pricing"))
    row.update(totals)
    row["unknown_execution"] = trial.get("state") != "RETURNED" or not done or bool(totals["unknown_provider_calls"])
    if review and review.get("provider_call_ids") is not None and review["provider_call_ids"] != totals["provider_call_ids"]:
        row["review_usable"] = False
        row["review_mismatch"] = "PROVIDER_CALL_IDS"
    if review and review.get('run_id') is not None and review['run_id'] != trial.get('run_id'):
        row['review_usable'] = False
        row['review_mismatch'] = 'SOURCE_RUN_ID'
    for field in CATEGORICAL:
        value = review.get(field) if row["review_usable"] else None
        row[field] = value if value in LABELS else "UNKNOWN"
    if row["review_usable"]:
        value = number(review.get("core_inline_citation_recall"))
        row["core_inline_citation_recall"] = value if value is not None and 0 <= value <= 1 else None
    row.update(observed_metrics(trial, observations))
    packets = [event.get("data", {}) for event in events if event.get("event") == "rag_result"]
    row["delivered_citation_counts_per_packet"] = [len(packet.get("citations", [])) for packet in packets]
    payload, selection, finish = answer_request(trial, review if row["review_usable"] else None, attempts, run_dir, snapshot)
    row["final_request_selection"] = selection
    row["provider_finish_reason"] = finish
    row["truncated"] = row["truncated"] or finish == "length"
    if case.get("negative"):
        row.update(context_recall_status="NOT_APPLICABLE", core_inline_citation_recall=None)
    elif payload is None:
        row["context_recall_status"] = "UNKNOWN"
    else:
        # Check exact qrel chunks only in the actual final provider request.
        # Text presence does not prove a PDF table's row/column association.
        contents = "\n".join(str(message.get("content", "")) for message in payload.get("messages", []))
        chunks = {chunk["id"]: chunk["content"] for chunk in manifest["chunks"]}
        expected = case.get("expected_chunk_ids", [])
        found = [chunk_id for chunk_id in expected if chunk_id in chunks and chunks[chunk_id] in contents]
        row["expected_qrel_chunks"] = len(expected)
        row["exact_qrel_chunks_in_final_request"] = len(found)
        row["exact_qrel_chunk_text_recall"] = len(found) / len(expected) if expected else None
        reviewed_context = number(review.get("context_recall")) if row["review_usable"] else None
        if reviewed_context is not None and 0 <= reviewed_context <= 1:
            row.update(context_recall=reviewed_context, context_recall_status="INDEPENDENT_REVIEW_AND_REQUEST_CHECK")
        elif row["core_evidence_delivery"] == "PASS" and len(found) == len(expected) and expected:
            row.update(context_recall=1, context_recall_status="ALL_QRELS_EXACT_IN_REQUEST_AND_REVIEWED_DELIVERY_PASS")
        else:
            row["context_recall_status"] = "UNKNOWN_ALIGNMENT_OR_PARTIAL_TEXT"
    return row


def summarize(rows):
    counts = {"planned": len(rows), "recorded": sum(row["recorded"] for row in rows),
              "answered": sum(row["answered"] for row in rows),
              "reviewed_answered": sum(row["answered"] and row["review_usable"] for row in rows)}
    counts.update({field: sum(bool(row.get(field)) for row in rows) for field in (
        "missing_answer", "empty_answer", "truncated", "unknown_execution")})
    counts["unreviewed_recorded"] = sum(row["recorded"] and not row["review_usable"] for row in rows)
    counts['missing_trial_records'] = counts['planned'] - counts['recorded']
    counts['unknown_generation_recorded'] = sum(row['recorded'] and row['generation_status'] == 'UNKNOWN' for row in rows)
    metrics = {}
    for field in CATEGORICAL:
        all_counts = Counter(row[field] for row in rows)
        answered_counts = Counter(row[field] for row in rows if row["answered"])
        metrics[field] = {"planned_denominator": counts["planned"], "answered_denominator": counts["answered"],
                          "reviewed_answered_denominator": counts["reviewed_answered"],
                          "counts_all_planned": {label: all_counts[label] for label in LABELS},
                          "counts_answered": {label: answered_counts[label] for label in LABELS}}
    numeric = {}
    for field in NUMERIC:
        values = [Decimal(str(row[field])) for row in rows if row.get(field) is not None]
        not_applicable = sum(row['case_type'] == 'negative' for row in rows) if field in (
            'core_inline_citation_recall', 'context_recall', 'exact_qrel_chunk_text_recall') else 0
        numeric[field] = {"known_denominator": len(values), "unknown_denominator": len(rows) - len(values) - not_applicable,
                          "not_applicable_denominator": not_applicable,
                          "sum_known": str(sum(values, Decimal(0))) if values else None,
                          "mean_known": str(sum(values) / len(values)) if values else None,
                          "complete_total": str(sum(values, Decimal(0))) if len(values) == len(rows) else None}
    return {"counts": counts, "review_metrics": metrics, "numeric_metrics": numeric}


def paired(rows, arms=ARMS):
    indexed_rows = {(row["qid"], row["repetition"], row["mode"], row["arm"]): row for row in rows}
    output = []
    for mode in MODES:
        for case_type in ("positive", "negative"):
            keys = sorted({(row["qid"], row["repetition"]) for row in rows if row["mode"] == mode and row["case_type"] == case_type})
            for left, right in combinations(arms, 2):
                pairs = [(indexed_rows[(qid, rep, mode, left)], indexed_rows[(qid, rep, mode, right)]) for qid, rep in keys]
                available = [(a, b) for a, b in pairs if a["recorded"] and b["recorded"]]
                answered = [(a, b) for a, b in available if a["answered"] and b["answered"]]
                transitions = {}
                for metric in CATEGORICAL:
                    matrix = Counter(f"{a[metric]}->{b[metric]}" for a, b in answered)
                    transitions[metric] = {"paired_answered_denominator": len(answered), "transitions": dict(matrix),
                                           "both_determinate_denominator": sum(a[metric] not in ("UNKNOWN", "NOT_APPLICABLE")
                                               and b[metric] not in ("UNKNOWN", "NOT_APPLICABLE") for a, b in answered)}
                deltas = {}
                for metric in NUMERIC:
                    values = [Decimal(str(b[metric])) - Decimal(str(a[metric])) for a, b in available
                              if a.get(metric) is not None and b.get(metric) is not None]
                    not_applicable = len(keys) if case_type == 'negative' and metric in (
                        'core_inline_citation_recall', 'context_recall', 'exact_qrel_chunk_text_recall') else 0
                    deltas[metric] = {"paired_known_denominator": len(values), "unknown_or_unpaired": len(keys) - len(values) - not_applicable,
                                      "not_applicable_pair_denominator": not_applicable,
                                      "mean_right_minus_left": str(sum(values) / len(values)) if values else None}
                output.append({"mode": mode, "case_type": case_type, "left": left, "right": right,
                               "planned_pair_denominator": len(keys), "paired_recorded": len(available),
                               "paired_answered": len(answered), "unpaired_recorded": sum(a["recorded"] != b["recorded"] for a, b in pairs),
                               "both_missing": sum(not a["recorded"] and not b["recorded"] for a, b in pairs),
                               "shared_answered_keys": [{"qid": a["qid"], "repetition": a["repetition"]} for a, b in answered],
                               "review_metrics": transitions, "numeric_deltas": deltas})
    return output


def load_context(snapshot, run_dir, manifest):
    run_dir = private(run_dir)
    summary_raw = snapshot.read(run_dir / "summary.json", optional=True)
    summary = json.loads(summary_raw) if summary_raw else None
    zero = bool(summary and summary.get('actual_http_calls') == 0 and summary.get('recorded_trials', 0) == 0)
    trial_records = snapshot.records(run_dir / 'trials.jsonl', optional=zero)
    ledger_records = snapshot.records(run_dir / 'provider-ledger.jsonl', optional=zero)
    trial_map = indexed([row for _, row in trial_records], 'trial')
    attempts = latest_attempts([row for _, row in ledger_records])
    preflight_raw = snapshot.read(run_dir / 'preflight.json', optional=True)
    preflight = json.loads(preflight_raw) if preflight_raw else {}
    return {'run_dir': run_dir, 'manifest': manifest, 'summary': summary, 'trials': trial_map,
            'attempts': attempts, 'observations': snapshot.rows(run_dir / 'observations.jsonl', optional=True),
            'trial_lines': {key(row): line for line, row in trial_records},
            'ledger_lines': {row['call_id']: line for line, row in ledger_records},
            'fixed_search': preflight.get('fixed_search') or (summary or {}).get('fixed_search')}


def load_reviews(snapshot, paths):
    result, pointers = {}, {}
    for path in dict.fromkeys(private(path) for path in paths):
        for line, review in snapshot.records(path, optional=True):
            identity = key(review)
            if identity in result:
                raise ValueError('Duplicate review identity across immutable review inputs')
            result[identity] = review
            pointers[identity] = {'path': str(path), 'line': line}
    return result, pointers


def source_status(context):
    summary = context['summary'] or {}
    return {'run_dir': str(context['run_dir']), 'status': summary.get('status', 'UNKNOWN_LIVE_OR_INCOMPLETE'),
            'final': summary.get('status') in ('STOPPED', 'FAILED', 'RECORDED_REQUIRES_EVIDENCE_REVIEW'),
            'recorded_trials': len(context['trials']), 'provider_attempts': len(context['attempts']),
            'fixed_search': context['fixed_search'], 'model': context['manifest'].get('model'),
            'max_output_tokens': context['manifest'].get('max_output_tokens'),
            'dataset_version': context['manifest'].get('dataset_version'),
            'logical_manifest_sha256': sha(canonical(context['manifest'])),
            'observed_app_code_hashes': sorted({row['metadata']['code_hash'] for row in context['observations']
                if isinstance(row.get('metadata',{}).get('code_hash'),str)})}


def report_for(snapshot, contexts, manifest, reviews, review_pointers, arms=ARMS):
    combined = {}
    for context in contexts:
        for identity, trial in context['trials'].items():
            if identity in combined:
                raise ValueError('Duplicate frozen trial identity across source runs; replay cannot be merged')
            combined[identity] = context, trial
    output_rows, expected = [], set()
    for case in manifest['cases']:
        for mode in MODES:
            for repetition in range(1, manifest.get('repetitions', 2) + 1):
                for arm in arms:
                    identity = case['qid'], mode, arm, repetition
                    expected.add(identity)
                    context, trial = combined.get(identity, (contexts[0], None))
                    result = trial_row(identity, case, trial, reviews.get(identity), context['attempts'],
                                       context['observations'], manifest, context['run_dir'], snapshot)
                    result['source_run_dir'] = str(context['run_dir']) if trial else None
                    result['evidence'] = {'review': review_pointers.get(identity)}
                    if trial:
                        result['evidence']['trial'] = {'path': str(context['run_dir'] / 'trials.jsonl'),
                                                       'line': context['trial_lines'][identity]}
                        result['scoped_provider_calls'] = [{'run_dir': str(context['run_dir']), 'call_id': call_id,
                            'ledger': {'path': str(context['run_dir'] / 'provider-ledger.jsonl'), 'line': context['ledger_lines'][call_id]},
                            'request_path': str(context['run_dir'] / f'request-{call_id:04d}.json'),
                            'response_path': str(context['run_dir'] / f'response-{call_id:04d}.body')}
                            for call_id in result['provider_call_ids']]
                    else:
                        result['scoped_provider_calls'] = []
                    output_rows.append(result)
    groups = []
    for mode in MODES:
        for arm in arms:
            for case_type in ('positive', 'negative'):
                selected = [row for row in output_rows if row['mode'] == mode and row['arm'] == arm and row['case_type'] == case_type]
                groups.append({'mode': mode, 'arm': arm, 'case_type': case_type, **summarize(selected)})
    unassigned = []
    for context in contexts:
        assigned = {call_id for trial in context['trials'].values() for call_id in trial.get('provider_call_ids', [])}
        unassigned.extend({'run_dir': str(context['run_dir']), 'call_id': call_id}
                          for call_id in sorted(set(context['attempts']) - assigned))
    statuses = [source_status(context) for context in contexts]
    states = Counter(attempt.get('state', 'UNKNOWN') for context in contexts for attempt in context['attempts'].values())
    return {'source_run_final': all(status['final'] for status in statuses),
            'source_run_status': statuses[0]['status'] if len(statuses) == 1 else 'MULTIPLE_RUNS', 'source_runs': statuses,
            'dataset_version': manifest.get('dataset_version'), 'totals': summarize(output_rows), 'groups': groups,
            'paired_comparisons': paired(output_rows, arms=arms), 'trials': output_rows,
            'orphan_trial_records': len(set(combined) - expected), 'orphan_review_records': len(set(reviews) - expected),
            'provider_attempts': sum(len(context['attempts']) for context in contexts),
            'unassigned_provider_attempts': unassigned,
            'unassigned_provider_attempt_ids': [row['call_id'] for row in unassigned] if len(contexts) == 1 else None,
            'provider_states_latest': dict(states)}


def shared_budget(contexts, pricing, limit):
    known_total = Decimal(0)
    committed_total = Decimal(0)
    all_costs_known = all_commitments_known = True
    per_run, seen_provider_ids = [], set()
    for context in contexts:
        known = committed = Decimal(0)
        cost_known = commitment_known = True
        unknown_attempts = 0
        for attempt in context['attempts'].values():
            request_id = attempt.get('provider_request_id')
            if request_id:
                if request_id in seen_provider_ids:
                    raise ValueError('Duplicate actual provider request identity across budget sources')
                seen_provider_ids.add(request_id)
            usage = attempt.get('usage') or {}
            prompt, completion = usage.get('prompt_tokens'), usage.get('completion_tokens')
            valid = type(prompt) is int and type(completion) is int and prompt >= 0 and completion >= 0
            if attempt.get('state') == 'COMPLETED' and valid and pricing and attempt.get('price_based_cost_cny') is not None:
                cost = (Decimal(prompt) * Decimal(str(pricing['input_cny_per_million'])) +
                        Decimal(completion) * Decimal(str(pricing['output_cny_per_million']))) / 1000000
                if cost != Decimal(str(attempt['price_based_cost_cny'])):
                    raise ValueError('Actual provider usage and price-derived cost disagree')
                known += cost
                committed += cost
            else:
                cost_known = False
                unknown_attempts += 1
                reserve = attempt.get('reservation_cny')
                if reserve is None:
                    commitment_known = False
                else:
                    reserve = Decimal(str(reserve))
                    if not reserve.is_finite() or reserve < 0:
                        raise ValueError('Invalid unknown provider reservation')
                    committed += reserve
        summary = context['summary'] or {}
        if commitment_known and summary.get('committed_including_unknown_cny') is not None:
            if Decimal(str(summary['committed_including_unknown_cny'])) != committed:
                raise ValueError('Source summary and ledger commitment disagree')
        if summary.get('actual_http_calls') is not None and summary['actual_http_calls'] != len(context['attempts']):
            raise ValueError('Source summary provider denominator disagrees with actual ledger')
        known_total += known
        committed_total += committed
        all_costs_known &= cost_known
        all_commitments_known &= commitment_known
        per_run.append({'run_dir': str(context['run_dir']), 'provider_attempts': len(context['attempts']),
                        'known_price_derived_cost_cny': str(known),
                        'complete_price_derived_cost_cny': str(known) if cost_known else None,
                        'committed_including_unknown_cny': str(committed) if commitment_known else None,
                        'unknown_cost_attempts': unknown_attempts})
    limit = Decimal(str(limit)) if limit is not None else None
    return {'scope': 'Own ledgers only, each run once; inherited global/parent totals are never added again',
            'per_run': per_run, 'known_price_derived_cost_cny': str(known_total),
            'complete_price_derived_cost_cny': str(known_total) if all_costs_known else None,
            'committed_including_unknown_cny': str(committed_total) if all_commitments_known else None,
            'global_limit_cny': str(limit) if limit is not None else None,
            'within_recorded_budget': committed_total <= limit if all_commitments_known and limit is not None else None,
            'provider_bill': 'UNKNOWN'}


def analyze(run_dir, manifest_path, review_paths=None, *, extra_run_dirs=(), extra_review_dirs=(),
            supplement_run_dirs=(), supplement_review_dirs=()):
    run_dir, manifest_path = private(run_dir), private(manifest_path)
    snapshot = Snapshot()
    manifest = json.loads(snapshot.read(manifest_path).decode('utf-8-sig'))
    contexts = [load_context(snapshot, run_dir, manifest)]
    supplement_dirs = list(supplement_run_dirs)
    prior_hashes = []
    seen_runs = {run_dir}
    for directory in extra_run_dirs:
        directory = private(directory)
        if directory in seen_runs:
            raise ValueError('Duplicate source run directory')
        seen_runs.add(directory)
        extra_manifest = json.loads(snapshot.read(directory / 'manifest.json').decode('utf-8-sig'))
        if extra_manifest != manifest:
            raise ValueError('Continuation frozen manifest differs from primary')
        context = load_context(snapshot, directory, extra_manifest)
        if not contexts[0]['fixed_search'] or context['fixed_search'] != contexts[0]['fixed_search']:
            raise ValueError('Missing or different frozen search configuration across runs')
        contexts.append(context)
        contract_raw = snapshot.read(directory / 'continuation-manifest.json', optional=True)
        if contract_raw:
            contract = json.loads(contract_raw)
            if private(contract['parent_output']) != run_dir:
                raise ValueError('Continuation belongs to a different primary run')
            frozen_raw = snapshot.read(contract['parent_manifest_path'])
            if sha(frozen_raw) != contract['parent_manifest_sha256'] or json.loads(frozen_raw) != manifest:
                raise ValueError('Continuation parent manifest hash/content mismatch')
            for role, name in (('summary','summary.json'),('ledger','provider-ledger.jsonl'),('trials','trials.jsonl')):
                if sha(snapshot.read(run_dir / name)) != contract[f'parent_{role}_sha256']:
                    raise ValueError('Continuation primary evidence hash mismatch')
            for entry in contract.get('prior_runs', []):
                if entry.get('kind') == 'D_ELIGIBILITY_ONLY':
                    supplement_dirs.append(entry['output'])
                    prior_hashes.append(entry)
    paths = list(review_paths) if review_paths is not None else [run_dir / name for name in REVIEW_FILES]
    for context in contexts[1:]:
        paths.extend(context['run_dir'] / name for name in REVIEW_FILES)
    for directory in extra_review_dirs:
        paths.extend(private(directory) / name for name in REVIEW_FILES)
        paths.extend(sorted(private(directory).glob('review-*.jsonl')))
    reviews, review_pointers = load_reviews(snapshot, paths)
    result = {'schema_version':'m2-results-analysis/v2','status':'SNAPSHOT_ANALYZED',
              **report_for(snapshot, contexts, manifest, reviews, review_pointers)}
    supplements, budget_contexts, supplement_identities = [], list(contexts), set()
    for directory in dict.fromkeys(private(path) for path in supplement_dirs):
        if directory in seen_runs:
            raise ValueError('Supplement must not also be an original four-arm source run')
        seen_runs.add(directory)
        specification = json.loads(snapshot.read(directory / 'supplement-manifest.json'))
        parent_raw = snapshot.read(specification['parent_manifest_path'])
        if sha(parent_raw) != specification['parent_manifest_sha256'] or json.loads(parent_raw) != manifest:
            raise ValueError('Supplement parent corpus/config differs from frozen primary')
        supplemental_manifest = {**manifest,'cases':[specification['case']]}
        context = load_context(snapshot, directory, supplemental_manifest)
        if supplement_identities & set(context['trials']):
            raise ValueError('Duplicate supplemental frozen trial identity across source runs')
        supplement_identities.update(context['trials'])
        if contexts[0]['fixed_search'] and context['fixed_search'] != contexts[0]['fixed_search']:
            raise ValueError('Supplement frozen search configuration mismatch')
        supp_paths = list(directory.glob('review-*.jsonl'))
        supp_paths.append(directory / 'review-supplement.jsonl')
        for review_dir in supplement_review_dirs:
            supp_paths.extend(private(review_dir).glob('review-*.jsonl'))
        supp_reviews, supp_pointers = load_reviews(snapshot, supp_paths)
        report = report_for(snapshot,[context],supplemental_manifest,supp_reviews,supp_pointers,arms=('A','D'))
        report.update(purpose='D_ELIGIBILITY_ONLY',four_arm_inference='NOT_ESTABLISHED_BY_SUPPLEMENT')
        supplements.append(report)
        budget_contexts.append(context)
    for entry in prior_hashes:
        directory = private(entry['output'])
        for role, name in (('summary','summary.json'),('ledger','provider-ledger.jsonl'),('trials','trials.jsonl')):
            if sha(snapshot.read(directory / name)) != entry[f'{role}_sha256']:
                raise ValueError('Continuation prior supplement evidence hash mismatch')
    result['supplementary_D'] = supplements
    result['budget_accounting'] = shared_budget(budget_contexts, manifest.get('pricing'), manifest.get('budget_cny'))
    result['methodology'] = [
        'Original four-arm denominator is the frozen plan once; continuation fills only disjoint identities, never overwrites a failure.',
        'Provider IDs restart per run; ledger/request/review evidence pointers always retain run scope and JSONL line.',
        'Independent reviews alone determine correctness/faithfulness; missing/stale reviews remain UNKNOWN; PARTIAL stays separate.',
        'Paired comparisons use identical qid/mode/repetition with explicit missing/unpaired denominators.',
        'Original-source and delivered-text citation support remain separate; negative recall is NOT_APPLICABLE.',
        'Context recall requires actual final-request qrel checks and reviewed evidence delivery; PDF alignment uncertainty remains UNKNOWN.',
        'Candidate/tool/retry counts require actual trace events; provider retries are never inferred.',
        'Supplement D has its own eight-slot A/D report and never enters original four-arm statistics.',
        'Shared budget adds each own ledger once, including UNKNOWN reservations, not inherited global totals. Provider bill remains UNKNOWN.']
    snapshot.verify()
    result['inputs'] = snapshot.provenance()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--review", action="append")
    parser.add_argument('--extra-run-dir',action='append',default=[])
    parser.add_argument('--extra-review-dir','--extra-review',dest='extra_review_dirs',action='append',default=[])
    parser.add_argument('--supplement-run-dir',action='append',default=[])
    parser.add_argument('--supplement-review-dir',action='append',default=[])
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        run_dir, output = private(args.run_dir), private(args.output)
        source_dirs=[run_dir,*map(private,args.extra_run_dir),*map(private,args.supplement_run_dir),*map(private,args.extra_review_dirs),*map(private,args.supplement_review_dir)]
        if any(output==directory or output.is_relative_to(directory) for directory in source_dirs):
            raise ValueError("Analysis output must be outside immutable source run")
        result = analyze(run_dir, args.manifest or run_dir / "manifest.json", args.review,
                         extra_run_dirs=args.extra_run_dir,extra_review_dirs=args.extra_review_dirs,
                         supplement_run_dirs=args.supplement_run_dir,supplement_review_dirs=args.supplement_review_dir)
        if any(output==Path(source['run_dir']) or output.is_relative_to(Path(source['run_dir']))
               for report in result['supplementary_D'] for source in report['source_runs']):
            raise ValueError('Analysis output must stay outside discovered immutable supplement run')
        output.mkdir(parents=True, exist_ok=False)
        (output / "analysis.json").write_bytes(canonical(result))
        print(json.dumps({"status": result["status"], "source_run_final": result["source_run_final"],
                          "planned_trials": result["totals"]["counts"]["planned"],
                          "recorded_trials": result["totals"]["counts"]["recorded"], "output": str(output / "analysis.json")}))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

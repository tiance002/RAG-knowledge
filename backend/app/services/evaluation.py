"""Deterministic evidence accounting; missing observations are never inferred."""
from enum import Enum

class EvidenceState(str, Enum):
    PRESENT = 'PRESENT'
    PARTIAL = 'PARTIAL'
    ABSENT = 'ABSENT'
    UNKNOWN = 'UNKNOWN'

STAGES = ('raw', 'merged', 'reranked', 'context', 'cited')
SCORE_FIELDS = ('core_correctness', 'faithfulness', 'citation_correctness', 'citation_recall', 'evidence_recall', 'empty', 'skipped', 'truncated', 'latency_ms', 'tokens', 'cost', 'remediation_success')

def evidence_state(gold, observed):
    if not gold or observed is None:
        return EvidenceState.UNKNOWN.value
    matched = set(gold) & set(observed)
    return (EvidenceState.ABSENT if not matched else EvidenceState.PRESENT if matched == set(gold) else EvidenceState.PARTIAL).value

def score_record(qid, experiment_version, gold, stages, scores=None):
    states = {stage: evidence_state(gold, stages.get(stage)) for stage in STAGES}
    coverage = {stage: (None if not gold or stages.get(stage) is None else len(set(gold) & set(stages[stage])) / len(set(gold))) for stage in STAGES}
    transitions = {}
    for left, right in zip(STAGES, STAGES[1:]):
        a, b = states[left], states[right]
        known = a != 'UNKNOWN' and b != 'UNKNOWN'
        transitions[f'{left}_to_{right}'] = {
            'coverage_delta': None if not known else coverage[right] - coverage[left],
            'retained': None if not known else a == 'PRESENT' and b == 'PRESENT',
            'recovered': None if not known else a in ('ABSENT', 'PARTIAL') and b == 'PRESENT',
            'lost': None if not known else a == 'PRESENT' and b != 'PRESENT',
        }
    return {'qid': qid, 'experiment_version': experiment_version, 'evidence': states,
            'coverage': coverage, 'transitions': transitions, 'scores': {k: (scores or {}).get(k) for k in SCORE_FIELDS}}

def normalize_location(source_format, location=None):
    location = location or {}
    fmt = (source_format or '').lower().lstrip('.')
    keys = ('page', 'bbox', 'sheet', 'row', 'column', 'cell', 'cell_range', 'heading', 'heading_path', 'table_row', 'line', 'source_sha256', 'index_version')
    return {'format': fmt or None, 'location': {**location, **{k: location.get(k) for k in keys}}}


def compare_records(records):
    result = {}
    for record in records:
        key = (record['qid'], record['experiment_version'])
        if key in result:
            raise ValueError(f'duplicate comparison key: {key}')
        result[key] = record
    return result


def evaluate_evidence(gold, observed, *, gold_grain, observed_grain, gold_complete):
    """Only compare complete, nonempty Gold with an actually captured matching grain."""
    if gold_complete is not True or not gold or observed is None or not gold_grain or gold_grain != observed_grain:
        return {"state": "UNKNOWN", "coverage": None}
    g, o = set(gold), set(observed)
    return {"state": evidence_state(g, o), "coverage": len(g & o) / len(g)}


def evaluate_ranking(gold, ranked, *, gold_grain, observed_grain, gold_complete, k, grades=None):
    """Ranking scores require matching units; incomplete qrels can only establish a hit."""
    result = {"hit_at_k": None, "recall_at_k": None, "mrr_at_k": None, "ndcg_at_k": None}
    complete = gold_complete is True
    if not isinstance(k, int) or isinstance(k, bool) or k <= 0:
        raise ValueError("k must be a positive integer")
    if not gold or ranked is None or not gold_grain or gold_grain != observed_grain:
        return result
    import math
    gold_set = set(gold)
    if len(gold_set) != len(gold):
        raise ValueError("duplicate gold units")
    if len(set(ranked)) != len(ranked):
        raise ValueError("duplicate ranked units")
    if grades is not None and any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in grades.values()):
        raise ValueError("grades must be finite nonnegative numbers")
    top = list(ranked[:k])
    matches = [i for i, item in enumerate(top, 1) if item in gold_set]
    if matches:
        result["hit_at_k"] = 1.0
        # Incomplete qrels cannot establish the first relevant rank.
        if complete:
            result["mrr_at_k"] = 1.0 / matches[0]
    elif complete:
        result["hit_at_k"] = result["mrr_at_k"] = 0.0
    if complete:
        result["recall_at_k"] = len(set(top) & gold_set) / len(gold_set)
        if grades is not None and set(grades) == gold_set and all(isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0 for v in grades.values()):
            def dcg(values):
                return sum((2 ** v - 1) / math.log2(i + 2) for i, v in enumerate(values))
            ideal = dcg(sorted(grades.values(), reverse=True)[:k])
            if ideal:
                result["ndcg_at_k"] = dcg([grades.get(item, 0) for item in top]) / ideal
    return result


def evidence_transitions(gold, stages, *, gold_grain, gold_complete):
    """Return only adjacent observed stages; do not infer missing stages."""
    output = {}
    for left, right in zip(stages, stages[1:]):
        a = evaluate_evidence(gold, left.get("units"), gold_grain=gold_grain, observed_grain=left.get("grain"), gold_complete=gold_complete)
        b = evaluate_evidence(gold, right.get("units"), gold_grain=gold_grain, observed_grain=right.get("grain"), gold_complete=gold_complete)
        if a["coverage"] is None or b["coverage"] is None:
            continue
        g = set(gold); x = g & set(left["units"]); y = g & set(right["units"])
        output[left["stage"] + "_to_" + right["stage"]] = {"coverage_delta": b["coverage"] - a["coverage"], "retained": sorted(x & y), "recovered": sorted(y - x), "lost": sorted(x - y)}
    return output


def execution_metrics(*, final_answer=None, finish_reason=None, stream_truncated=None, execution_status=None):
    return {"eval.execution_status": execution_status, "eval.answer_empty": None if final_answer is None else not bool(final_answer.strip()), "eval.answer_truncated": True if finish_reason == "length" or stream_truncated is True else False if finish_reason is not None and stream_truncated is False else None}


def review_metrics(review):
    """Only explicit reviewed fields can produce quality judgments."""
    fields = {"core_correctness": {"PASS", "PARTIAL", "FAIL", "UNKNOWN"}, "answer_completeness": {"COMPLETE", "PARTIAL", "MISSING", "UNKNOWN"}, "faithfulness": {"PASS", "FAIL", "UNKNOWN"}, "citation_support": {"PASS", "PARTIAL", "FAIL", "UNKNOWN"}, "abstention": {"CORRECT", "INCORRECT", "UNKNOWN", "NA"}}
    for key in ("reviewer_type", "rubric_version", "answer_sha256", "evidence_scope"):
        if not review.get(key):
            raise ValueError("missing review linkage: " + key)
    result = {}
    for field, allowed in fields.items():
        value = review.get(field)
        if value is not None and value not in allowed:
            raise ValueError("invalid " + field)
        if value not in (None, "UNKNOWN"):
            result["eval." + field] = value
    core = review.get("core_correctness")
    if core in ("PASS", "FAIL"):
        result["eval.core_pass"] = core == "PASS"
    numerator, denominator = review.get("supported_claims"), review.get("citable_claims")
    if numerator is not None or denominator is not None:
        if not isinstance(numerator, int) or isinstance(numerator, bool) or not isinstance(denominator, int) or isinstance(denominator, bool) or denominator <= 0 or not 0 <= numerator <= denominator:
            raise ValueError("invalid citation coverage counts")
        result["eval.citation_coverage"] = numerator / denominator
    return result


def summarize_attempts(records):
    """Count planned slots independently; unknown is never treated as failure."""
    counts = {key: 0 for key in ("n_planned", "n_sent", "n_returned", "n_nonempty", "n_reviewed", "n_core_pass", "n_partial", "n_fail", "n_unknown", "n_not_run")}
    for row in records:
        counts["n_planned"] += 1
        for field in ("sent", "returned", "nonempty", "reviewed"):
            if row.get(field) is True: counts["n_" + field] += 1
        verdict = row.get("core_correctness")
        if verdict == "PASS": counts["n_core_pass"] += 1
        elif verdict == "PARTIAL": counts["n_partial"] += 1
        elif verdict == "FAIL": counts["n_fail"] += 1
        else: counts["n_unknown"] += 1
        if row.get("execution_status") == "NOT_RUN": counts["n_not_run"] += 1
    n = counts["n_planned"]
    judged = counts["n_core_pass"] + counts["n_partial"] + counts["n_fail"]
    counts["confirmed_pass_per_planned"] = counts["n_core_pass"] / n if n else None
    counts["strict_pass_per_judged"] = counts["n_core_pass"] / judged if judged else None
    return counts

"""Frozen E1.1 metric definitions; p0 metrics retain their historical meaning."""
METRIC_CATALOG_VERSION = "rag-metrics-v1"
# type, owner, scope, grain, denominator / prerequisite
METRIC_CATALOG = {
    "eval.core_correctness": ("CATEGORICAL", "reviewer", "question", "answer", "reviewed core facts"),
    "eval.core_pass": ("BOOLEAN", "local", "question", "answer", "explicit PASS or FAIL"),
    "eval.answer_completeness": ("CATEGORICAL", "reviewer", "question", "answer_point", "frozen required points"),
    "eval.faithfulness": ("CATEGORICAL", "reviewer", "question", "claim", "actual available evidence"),
    "eval.citation_support": ("CATEGORICAL", "reviewer", "question", "claim", "claim to citation review"),
    "eval.citation_coverage": ("NUMERIC", "reviewer", "question", "claim", "supported citable claims / citable claims"),
    "eval.abstention": ("CATEGORICAL", "reviewer", "question", "answer", "answerability Gold"),
    "eval.execution_status": ("CATEGORICAL", "local", "question", "execution", "observed status"),
    "eval.answer_empty": ("BOOLEAN", "local", "question", "answer", "captured final answer"),
    "eval.answer_truncated": ("BOOLEAN", "local", "question", "answer", "observed finish or stream status"),
    "eval.evidence_state.<stage>": ("CATEGORICAL", "local", "stage", "Gold unit", "complete same-grain Gold"),
    "eval.evidence_coverage.<stage>": ("NUMERIC", "local", "stage", "Gold unit", "matched Gold units / complete Gold units"),
    "eval.evidence_lost_or_recovered": ("TEXT", "local", "transition", "Gold unit", "adjacent known stages"),
    "retrieval.hit_at_k": ("NUMERIC", "local", "stage", "qrel unit", "hit known or complete qrel"),
    "retrieval.recall_at_k": ("NUMERIC", "local", "stage", "qrel unit", "matched Gold / complete Gold"),
    "retrieval.mrr_at_k": ("NUMERIC", "local", "stage", "qrel unit", "reciprocal first relevant rank; complete qrel"),
    "retrieval.ndcg_at_k": ("NUMERIC", "local", "stage", "qrel unit", "DCG / ideal DCG; complete graded qrel"),
}

# Frozen labels from the V1 contract. UNKNOWN remains a categorical label,
# never a numeric zero. NOT_RUN is an execution status, not a quality verdict.
CATEGORICAL_ENUMS = {
    "eval.core_correctness": frozenset({"PASS", "PARTIAL", "FAIL", "UNKNOWN"}),
    "eval.answer_completeness": frozenset({"COMPLETE", "PARTIAL", "MISSING", "UNKNOWN"}),
    "eval.faithfulness": frozenset({"PASS", "FAIL", "UNKNOWN"}),
    "eval.citation_support": frozenset({"PASS", "PARTIAL", "FAIL", "UNKNOWN"}),
    "eval.abstention": frozenset({"CORRECT", "INCORRECT", "UNKNOWN", "NA"}),
    "eval.execution_status": frozenset({"RETURNED", "FAILED", "UNKNOWN", "TRUNCATED", "CANCELLED", "NOT_RUN"}),
    "eval.evidence_state.<stage>": frozenset({"PRESENT", "PARTIAL", "ABSENT", "UNKNOWN"}),
}

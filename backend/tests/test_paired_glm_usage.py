from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from summarize_affinity_pairs import summarize


def test_glm_token_reduction_excludes_cheap_usage_and_unknown_identity():
    glm = {"model": "zai-org/GLM-5.3-Flash", "state": "COMPLETED", "prompt_tokens": 6,
           "completion_tokens": 4, "total_tokens": 10, "answer_sha256": "answer-hash"}
    choices = [glm, {**glm, "model": "Qwen/Qwen3.5-4B", "total_tokens": 114, "prompt_tokens": 110},
               {**glm, "model": None}, {**glm, "state": "UNKNOWN"}]
    rows = [{"qid": n, "same_messages_confirmed": True, "dynamic": d, "glm": glm}
            for n, d in enumerate(choices)]
    records = [{"qid": r["qid"], "arm": arm, "outcome": {**r[k], "actual_model": r[k]["model"]}}
               for r in rows for arm, k in [("AFFINITY_V2", "dynamic"), ("GLM_ONLY", "glm")]]
    assert summarize(rows)["paired_glm_only_tokens"]["n"] == 0
    summary = summarize(rows, records)
    role = summary["paired_glm_only_tokens"]
    assert role["n"] == 2 and role["dynamic_glm_questions"] == 1
    assert role["excluded_missing_identity_or_usage"] == 1
    assert role["usage"]["total_tokens"] == {"dynamic": 10, "all_glm": 20, "reduction_percent": 50}
    assert summary["paired_tokens"]["dynamic_total"] == 134

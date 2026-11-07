import json
import sys
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from langchain_core.messages import AIMessage, ToolMessage
from app.services import agent_workflow, grounded_validation


def state(status="SUCCESS", score=0.9, content="收入为 42 元。"):
    tool = ToolMessage(name="search_knowledge_base", tool_call_id="call", content=json.dumps({
        "retrieval_status": status, "citations": [{"chunk_id": "a", "content": content}]
    }))
    return {"messages": [tool], "last_score": score}


def answer(text="收入为 42 元。[1]", reason="stop"):
    return AIMessage(content=text, response_metadata={"finish_reason": reason})


def test_generation_disabled_falls_back(monkeypatch):
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "false")
    response = answer()
    agent_workflow._finalize_knowledge_answer(state(), response)
    assert response.content != "收入为 42 元。[1]"


def test_complete_unchecked_candidate_is_sent_without_validator(monkeypatch):
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "true")
    monkeypatch.setattr(grounded_validation, "validate_grounded_answer", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    events = []
    monkeypatch.setattr(agent_workflow, "event", lambda name, **values: events.append((name, values)))
    response = answer("可能不同的解释：收入为 99 元。[2]")
    citations, mode = agent_workflow._finalize_knowledge_answer(state(), response, include_mode=True)
    assert response.content == "可能不同的解释：收入为 99 元。[2]"
    assert mode == "GROUNDED_GENERATION" and citations[0]["chunk_id"] == "a"
    assert events[-1][1]["validation_status"] == "NOT_RUN"


def test_completion_and_retrieval_quality_gates_remain(monkeypatch):
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "true")
    for source, response in [(state(), answer(reason="length")),
                             (state(), answer(text="")),
                             (state(status="UNKNOWN"), answer()),
                             (state(status="RETRIEVAL_ERROR"), answer()),
                             (state(status="LOW_RELEVANCE"), answer()),
                             (state(score=0.4), answer())]:
        _, mode = agent_workflow._finalize_knowledge_answer(source, response, include_mode=True)
        assert mode != "GROUNDED_GENERATION"


def test_missing_finish_reason_and_partial_score_still_generate(monkeypatch):
    """网关不返回 finish_reason、或评分只到"部分相关"时，不丢弃已生成的答案。"""
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "true")
    for source, response in [(state(), answer(reason=None)), (state(score=0.5), answer())]:
        _, mode = agent_workflow._finalize_knowledge_answer(source, response, include_mode=True)
        assert mode == "GROUNDED_GENERATION"


def test_invalid_score_format_does_not_block_generation(monkeypatch):
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "true")
    _, mode = agent_workflow._finalize_knowledge_answer(state(score=None), answer(), include_mode=True)
    assert mode == "GROUNDED_GENERATION"


@pytest.mark.parametrize("raw, expected", [
    ("0.85", 0.85),
    ("0.85/1.0", 0.85),
    ("得分：0.85，理由：覆盖了主要信息", 0.85),
    ("8.5/10", 0.85),
    ("0.85 out of 1.0", 0.85),
    ("no error, score 0.9", 0.9),
    ("完全不相关", None),
    ("error 500", None),
    ("0.2 到 0.8 之间", None),
])
def test_relevance_score_tolerates_common_wrappers(raw, expected):
    assert agent_workflow._relevance_score(raw) == expected


def test_multiple_success_rounds_are_allowed_but_unknown_is_not(monkeypatch):
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "true")
    first = state(); second = state(content="其他证据")
    second["messages"][0].tool_call_id = "other"
    both = {"messages": first["messages"] + second["messages"], "last_score": 0.9}
    response = answer("综合的结果，不按固定模板。")
    _, mode = agent_workflow._finalize_knowledge_answer(both, response, include_mode=True)
    assert mode == "GROUNDED_GENERATION" and response.content == "综合的结果，不按固定模板。"
    both["messages"].append(state(status="UNKNOWN")["messages"][0])
    _, mode = agent_workflow._finalize_knowledge_answer(both, answer(), include_mode=True)
    assert mode == "EXTRACTIVE_FALLBACK"


def test_empty_search_then_success_can_generate(monkeypatch):
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "true")
    empty = ToolMessage(name="search_knowledge_base", tool_call_id="empty", content=json.dumps({
        "retrieval_status": "NO_RESULTS", "citations": []
    }))
    source = state()
    source["messages"].insert(0, empty)
    response = answer()
    citations, mode = agent_workflow._finalize_knowledge_answer(source, response, include_mode=True)
    assert mode == "GROUNDED_GENERATION"
    assert response.content == "收入为 42 元。[1]"
    assert citations[0]["chunk_id"] == "a"
    source["messages"].append(empty)
    # 末尾一轮为空不再否决前面成功轮次的证据。
    _, mode = agent_workflow._finalize_knowledge_answer(source, answer(), include_mode=True)
    assert mode == "GROUNDED_GENERATION"


def test_mixed_image_preserves_unverified_disclosure(monkeypatch):
    monkeypatch.setenv("RAG_GROUNDED_GENERATION", "true")
    response = answer("基于文字资料的回答。")
    _, mode = agent_workflow._finalize_knowledge_answer(state(content="文字 [图片: x]"), response, include_mode=True)
    assert mode == "GROUNDED_GENERATION" and "图片资料，其内容需要进一步核验" in response.content

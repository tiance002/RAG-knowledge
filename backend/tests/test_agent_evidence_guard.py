import json
from pathlib import Path
from unittest.mock import MagicMock
import sys

import pytest
from langchain_core.messages import AIMessage, ToolMessage

sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from app.services.agent_workflow import _guard_knowledge_answer


def test_agent_thought_does_not_expose_unsupported_model_claim(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from app.services import agent_workflow
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    workflow.tools = []
    workflow._get_llm_service = Mock(return_value=SimpleNamespace(
        model='test', get_chat_model=Mock(return_value=SimpleNamespace(bind_tools=lambda _: object()))))
    monkeypatch.setattr(agent_workflow, 'invoke_provider_model', AsyncMock(return_value=AIMessage(content='Thinking: unsupported factual claim\n\nAnother unsupported factual claim')))
    state = {'messages': [], 'citations': [], 'last_score': 0}
    result = asyncio.run(workflow.agent_node(state, {}))
    assert 'unsupported factual claim' not in str(result)


def tool_result(status, citations=None):
    return ToolMessage(name="search_knowledge_base", tool_call_id="call", content=json.dumps({
        "retrieval_status": status, "citations": citations or [], "context": ""
    }))


@pytest.mark.parametrize("status, expected", [
    ("NO_RESULTS", "未在所选知识库"),
    ("RETRIEVAL_ERROR", "检索发生错误"),
    ("UNKNOWN", "检索状态不确定"),
    ("invalid", "检索状态不确定"),
])
def test_empty_status_replaces_unsubstantiated_answer(status, expected):
    raw = Path("var/reports/p0-agent-http-20261028-202852-4bedf843/response.sse").read_text(encoding="utf-8")
    answer = next(json.loads(line[6:])["content"] for line in raw.splitlines()
                  if line.startswith('data: {"content":'))
    assert "Pieter van Musschenbroek" in answer
    response = AIMessage(content=answer)
    assert _guard_knowledge_answer({"messages": [tool_result(status)]}, response) == []
    assert expected in response.content
    assert "Pieter van Musschenbroek" not in response.content


def test_partial_evidence_survives_later_failure_without_model_claims():
    citation = {"chunk_id": "a", "content": "原文支持的部分事实", "fileName": "source"}
    response = AIMessage(content="原文支持的部分事实以及无依据的其他事实")
    state = {"messages": [tool_result("SUCCESS", [citation]), tool_result("RETRIEVAL_ERROR")], "last_score": 0.0}
    assert _guard_knowledge_answer(state, response) == [citation]
    assert "原文支持的部分事实" in response.content
    assert "无依据的其他事实" not in response.content
    assert "未完全成功" in response.content


def test_low_score_renders_only_original_evidence():
    citation = {"chunk_id": "a", "content": "证据原文"}
    response = AIMessage(content="模型推断")
    _guard_knowledge_answer({"messages": [tool_result("SUCCESS", [citation])], "last_score": 0.4}, response)
    assert "证据原文" in response.content and "模型推断" not in response.content


def test_high_score_returns_original_evidence_without_model_claims():
    citation = {"chunk_id": "a", "content": "项目收入为 42 万元。"}
    response = AIMessage(content="项目收入为 42 万元，利润为 99 万元。")
    assert _guard_knowledge_answer({"messages": [tool_result("SUCCESS", [citation])], "last_score": 0.8}, response) == [citation]
    assert "项目收入为 42 万元。" in response.content
    assert "利润为 99 万元" not in response.content


def test_low_relevance_empty_and_partial_are_distinct():
    response = AIMessage(content="unsupported")
    _guard_knowledge_answer({"messages": [tool_result("LOW_RELEVANCE")]}, response)
    assert "相关性不足" in response.content
    citation = {"chunk_id": "c", "content": "候选原文"}
    _guard_knowledge_answer({"messages": [tool_result("LOW_RELEVANCE", [citation])], "last_score": 1.0}, response)
    assert "不足" in response.content and "候选原文" in response.content


def test_image_placeholder_is_not_textual_evidence():
    response = AIMessage(content="unsupported image description")
    image = {"chunk_id": "image", "content": "[图片: kb/image.png]"}
    assert _guard_knowledge_answer({"messages": [tool_result("SUCCESS", [image])], "last_score": 1.0}, response) == []
    assert "图片内容需要进一步核验" in response.content
    assert "[图片:" not in response.content and "unsupported" not in response.content

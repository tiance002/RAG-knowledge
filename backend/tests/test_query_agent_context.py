"""Offline contracts for existing Agent intent routing and real retrieval-tool plumbing."""
import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# Match the existing Agent request-parameter tests; storage is never exercised.
sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from app.services import agent_workflow as aw
from app.tools import retrieval_tool


def test_agent_reuses_understanding_intent_without_second_model_call():
    workflow = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    workflow._get_llm_service = MagicMock(side_effect=AssertionError('unexpected intent model call'))
    result = asyncio.run(workflow.analyze_intent({
        'messages': [HumanMessage(content='Hello there')],
        'kb_ids': [], 'understanding_intent': 'chat',
    }, {}))
    assert result['intent'] == 'chat'
    workflow._get_llm_service.assert_not_called()


@pytest.fixture(autouse=True)
def isolate_telemetry(monkeypatch, tmp_path):
    monkeypatch.setenv("RAG_OBSERVATION_LOG", str(tmp_path / "query-agent-events.jsonl"))
    monkeypatch.setenv("RAG_OBSERVABILITY_ENABLED", "false")
    monkeypatch.setenv("RAG_SNAPSHOTS_ENABLED", "false")
    from app.services import observability
    monkeypatch.setattr(observability, "native_enabled", lambda: False)


def test_disabled_understanding_defaults_to_qa_without_model_call(monkeypatch):
    workflow = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    provider_model = object()
    factory = MagicMock(return_value=provider_model)
    workflow._get_llm_service = lambda state: SimpleNamespace(model="offline", get_chat_model=factory)
    invoke = AsyncMock(return_value=AIMessage(content="qa"))
    monkeypatch.setattr(aw, "invoke_provider_model", invoke)
    current = "它的内存淘汰策略有什么不同？"
    history = [
        HumanMessage(content="比较 Redis 7.2 与 Memcached 的实现。"),
        AIMessage(content="Redis 7.2 支持多种数据类型，Memcached 使用简单键值存储。"),
    ]
    state = {"messages": [SystemMessage(content="private output preference"), *history,
                          HumanMessage(content=current), AIMessage(content="trailing draft")], "kb_ids": ["allowed"]}
    result = asyncio.run(workflow.analyze_intent(state, {}))
    assert result["intent"] == "qa" and result["original_query"] == current
    factory.assert_not_called()
    invoke.assert_not_awaited()


def test_server_original_query_and_scope_reach_real_tool_despite_model_spoof(monkeypatch):
    # Keep the real StructuredTool/schema and Agent run_tools. Intercept only
    # database and shared retrieval boundaries, as in existing parameter tests.
    search = AsyncMock(return_value=([], {"retrieval_status": "NO_RESULTS"}))
    monkeypatch.setattr(retrieval_tool, "get_retrieval_service", lambda: SimpleNamespace(search=search))
    session_manager = MagicMock()
    monkeypatch.setattr(retrieval_tool, "Session", MagicMock(return_value=session_manager))
    tool_events = []
    monkeypatch.setattr(retrieval_tool, "event", lambda name, **data: tool_events.append((name, data)))
    params = {"search_mode": "fulltext", "top_k": 9, "score_threshold": 0.22,
              "vector_weight": 0.4, "bm25_weight": 0.6, "rerank_enabled": True,
              "rerank_top_k": 4, "rerank_score_threshold": 0.25,
              "rerank_model_id": "selected-model", "context_window": 2}
    current = "它的失效策略呢？"
    message = AIMessage(content="", tool_calls=[{"name": "search_knowledge_base", "id": "q1", "args": {
        "query": "Redis 7.2 eviction policy", "original_query": "spoofed original",
        "kb_ids": ["other"], "search_mode": "vector", "top_k": 1,
        "score_threshold": 0.9, "use_rerank": False, "rerank_model_id": "other-model",
        "context_window": 9,
    }}])
    state = {"messages": [message], "original_query": current,
             "kb_ids": ["allowed"], "search_params": params}
    workflow = aw.AgentWorkflow.__new__(aw.AgentWorkflow)
    result = asyncio.run(workflow.run_tools(state, {}))
    search.assert_awaited_once()
    assert search.await_args.kwargs == {
        "session": session_manager.__enter__.return_value,
        "query": "Redis 7.2 eviction policy", "original_query": current,
        "kb_ids": ["allowed"], "search_mode": "fulltext", "top_k": 18,
        "score_threshold": 0.22, "vector_weight": 0.4, "bm25_weight": 0.6,
    }
    actual = next(data for name, data in tool_events if name == "agent.tool_search")
    assert actual["use_rerank"] is True
    assert actual["rerank_top_k"] == 4 and actual["rerank_score_threshold"] == 0.25
    assert actual["rerank_model_id"] == "selected-model" and actual["context_window"] == 2
    assert json.loads(result["messages"][0].content)["retrieval_status"] == "NO_RESULTS"

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())

from app.services import agent_workflow


@pytest.mark.parametrize(
    "citations, response, error, expected_score, low_relevance",
    [
        ([{"id": "candidate"}], "0.65", None, 0.65, True),
        ([], None, None, 0.0, False),
        ([{"id": "candidate"}], "not a score", None, 0.5, False),
        ([{"id": "candidate"}], "-0.5", None, 0.5, False),
        ([{"id": "candidate"}], None, RuntimeError("unavailable"), 0.5, False),
        ([{"id": "candidate"}], "0.8", None, 0.8, False),
    ],
)
def test_grade_outcomes(monkeypatch, citations, response, error, expected_score, low_relevance):
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    fake_model = SimpleNamespace(ainvoke=AsyncMock(
        side_effect=error, return_value=SimpleNamespace(content=response)
    ))
    fake_llm = SimpleNamespace(model="fake", get_chat_model=Mock(return_value=fake_model))
    workflow._get_llm_service = Mock(return_value=fake_llm)
    events = []
    monkeypatch.setattr(agent_workflow, "event", lambda name, **data: events.append((name, data)))
    monkeypatch.setattr(agent_workflow, "model_response", lambda *args, **kwargs: None)
    state = {"citations": citations, "original_query": "question", "rag_context": "context", "retry_count": 0}

    if error is not None:
        with pytest.raises(RuntimeError):
            asyncio.run(workflow.grade_documents(state, {}))
        fake_model.ainvoke.assert_awaited_once()
        assert not any(name == 'agent.evidence_outcome' for name, _ in events)
        return
    result = asyncio.run(workflow.grade_documents(state, {}))

    assert result["last_score"] == expected_score
    outcomes = [data for name, data in events if name == "agent.evidence_outcome"]
    quality = next(data for name, data in events if name == "agent.quality")
    assert quality['is_fallback'] is (bool(citations) and (error is not None or response in ('not a score', '-0.5')))
    assert bool(outcomes) is low_relevance
    if low_relevance:
        assert outcomes == [{"status": "LOW_RELEVANCE", "score": 0.65, "retry_count": 0}]
    if citations:
        fake_model.ainvoke.assert_awaited_once()
    else:
        fake_model.ainvoke.assert_not_awaited()

@pytest.mark.parametrize("question", ["根据知识库回答这个问题", "请基于所选资料回答", "从我的文档中查找答案", "检索知识库里的信息"])
def test_explicit_source_request_routes_to_qa_without_model(question):
    from unittest.mock import Mock
    from langchain_core.messages import HumanMessage
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    workflow._get_llm_service = Mock(side_effect=AssertionError("must not call intent model"))
    result = asyncio.run(workflow.analyze_intent({"messages": [HumanMessage(content=question)]}, {}))
    assert result["intent"] == "qa"
    workflow._get_llm_service.assert_not_called()


def test_greeting_remains_chat_fast_path():
    from unittest.mock import Mock
    from langchain_core.messages import HumanMessage
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    workflow._get_llm_service = Mock(side_effect=AssertionError("must not call intent model"))
    result = asyncio.run(workflow.analyze_intent({"messages": [HumanMessage(content="你好")]}, {}))
    assert result["intent"] == "chat"
    workflow._get_llm_service.assert_not_called()


@pytest.mark.parametrize('question', ['知识库是什么？', '什么是知识库？', 'What is a knowledge base?'])
def test_knowledge_base_concept_is_not_explicit_corpus_request(question):
    assert not agent_workflow.explicitly_requests_sources(question)

@pytest.mark.parametrize("question", [
    "According to the knowledge base, who invented it?",
    "Use my documents to answer this question.",
    "Search the knowledge base for this topic.",
    "Based on the selected documents, summarize the findings.",
])
def test_explicit_english_source_request_routes_to_qa_without_model(question):
    from unittest.mock import Mock
    from langchain_core.messages import HumanMessage
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    workflow._get_llm_service = Mock(side_effect=AssertionError("must not call intent model"))
    result = asyncio.run(workflow.analyze_intent({"messages": [HumanMessage(content=question)]}, {}))
    assert result["intent"] == "qa"
    workflow._get_llm_service.assert_not_called()

@pytest.mark.parametrize("question", [
    "请参考我上传的文档回答这个问题", "请查阅所选资料说明预算", "请阅读上传的文件",
    "Please consult my uploaded documents for the answer.",
    "Refer to the selected materials to explain the budget.",
    "Read the provided file and summarize it.",
])
def test_reference_and_uploaded_source_requests_route_to_qa(question):
    from unittest.mock import Mock
    from langchain_core.messages import HumanMessage
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    workflow._get_llm_service = Mock(side_effect=AssertionError("must not call intent model"))
    result = asyncio.run(workflow.analyze_intent({"messages": [HumanMessage(content=question)]}, {}))
    assert result["intent"] == "qa"


@pytest.mark.parametrize("question", ["你好", "Tell me a story about a document."])
def test_general_chat_is_not_explicit_source_request(question):
    assert not agent_workflow.explicitly_requests_sources(question)

@pytest.mark.parametrize("question", ["知识库里的预算是多少？", "What is in the knowledge base?"])
def test_direct_knowledge_base_mention_is_source_request(question):
    assert agent_workflow.explicitly_requests_sources(question)


@pytest.mark.parametrize("question", ["不用知识库，聊聊天", "不需要知识库，随便聊聊", "Don't use the knowledge base, just chat."])
def test_clear_knowledge_base_negation_is_not_source_request(question):
    assert not agent_workflow.explicitly_requests_sources(question)

import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from langchain_core.messages import AIMessage, ToolMessage
from app.schemas import Message, MessageRole
from app.services import agent_workflow, chat


def run_stream(monkeypatch, node):
    citation = {"chunk_id": "chunk-1", "content": "可回读的原文", "fileName": "source"}
    response = AIMessage(content="未经验证的模型断言")
    if node == "agent":
        tool = ToolMessage(name="search_knowledge_base", tool_call_id="call", content=json.dumps({
            "retrieval_status": "SUCCESS", "citations": [citation]
        }))
        citations = agent_workflow._guard_knowledge_answer({"messages": [tool], "last_score": 0.4}, response)
    else:
        citations = []
    class FakeApp:
        async def astream_events(self, initial_state, version):
            if node == 'agent':
                yield {"event": "on_chain_end", "name": "process_tool_output", "metadata": {"langgraph_node": "process_tool_output"},
                       "data": {"output": {"retrieval_status": "SUCCESS"}}}
            yield {"event": "on_chat_model_stream", "name": "model", "metadata": {"langgraph_node": node},
                   "data": {"chunk": SimpleNamespace(content="未经验证的模型断言")}}
            yield {"event": "on_chain_end", "name": node, "metadata": {"langgraph_node": node},
                   "data": {"output": {"messages": [response], "citations": citations}}}
    monkeypatch.setattr(agent_workflow, "AgentWorkflow", lambda: SimpleNamespace(app=FakeApp()))
    service = chat.ChatService.__new__(chat.ChatService)
    async def collect():
        return [event async for event in service._chat_stream_langgraph(
            [Message(role=MessageRole.USER, content="问题")], ["kb"], 5, 0.1, None)]
    return asyncio.run(collect()), citation


def test_langgraph_emits_only_guarded_agent_answer_and_citation(monkeypatch):
    events, citation = run_stream(monkeypatch, "agent")
    answers = [data["content"] for kind, data in events if kind == "answer_chunk"]
    assert len(answers) == 1
    assert "可回读的原文" in answers[0]
    assert "未经验证的模型断言" not in answers[0]
    assert [data["citations"] for kind, data in events if kind == "rag_result"] == [[citation]]
    assert [kind for kind, _ in events].count("done") == 1
    assert next(data['status'] for kind, data in events if kind == 'retrieval_status') == 'SUCCESS'
    assert events[-1][1]['retrieval_status'] == 'SUCCESS'


def test_langgraph_normal_chat_stream_is_preserved(monkeypatch):
    events, _ = run_stream(monkeypatch, "generate_chat")
    assert [data["content"] for kind, data in events if kind == "answer_chunk"] == ["未经验证的模型断言"]


def test_empty_scope_is_not_expanded_to_all_knowledge_bases(monkeypatch):
    seen = []
    class FakeApp:
        async def astream_events(self, initial_state, version):
            seen.append(initial_state['kb_ids'])
            if False:
                yield None
    monkeypatch.setattr(agent_workflow, 'AgentWorkflow', lambda: SimpleNamespace(app=FakeApp()))
    service = chat.ChatService.__new__(chat.ChatService)
    async def collect():
        return [event async for event in service._chat_stream_langgraph(
            [Message(role=MessageRole.USER, content='你好')], [], 5, 0.1, None)]
    assert asyncio.run(collect())[-1][0] == 'done'
    assert seen == [[]]


import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from langchain_core.messages import HumanMessage
from app.services import agent_workflow, llm


def test_client_and_langchain_disable_sdk_retries(monkeypatch):
    captured = {}
    monkeypatch.setattr(llm, "AsyncOpenAI", lambda **kw: captured.setdefault("client", kw))
    monkeypatch.setattr(llm, "ChatOpenAI", lambda **kw: captured.setdefault("model", kw))
    service = llm.LLMService(base_url="https://example.invalid", api_key="test", model="test")
    service._get_client()
    service.get_chat_model()
    assert captured["client"]["max_retries"] == 0
    assert captured["model"]["max_retries"] == 0


@pytest.mark.parametrize("node", ["intent", "grade"])
def test_agent_timeout_does_not_fallback(node):
    workflow = agent_workflow.AgentWorkflow.__new__(agent_workflow.AgentWorkflow)
    model = SimpleNamespace(ainvoke=AsyncMock(side_effect=TimeoutError("sensitive")))
    workflow._get_llm_service = Mock(return_value=SimpleNamespace(model="test", get_chat_model=Mock(return_value=model)))
    if node == "intent":
        call = workflow.analyze_intent({"messages": [HumanMessage(content="a sufficiently long question")]}, {})
    else:
        call = workflow.grade_documents({"citations": [{"id": "x"}], "original_query": "q", "rag_context": "c"}, {})
    with pytest.raises(TimeoutError):
        asyncio.run(call)
    model.ainvoke.assert_awaited_once()


@pytest.mark.parametrize("exc", [llm.requests.exceptions.Timeout(), llm.requests.exceptions.ConnectionError(), llm.httpx.ReadTimeout("timeout"), llm.httpx.RemoteProtocolError("truncated"), llm.requests.exceptions.ChunkedEncodingError("truncated")])
def test_transport_failures_are_unknown(exc):
    assert llm.is_unknown_provider_error(exc)


def test_unknown_payload_has_no_exception_text(monkeypatch):
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append(data))
    result = llm.provider_error_payload(TimeoutError("secret"), stage="rewrite")
    assert result["status"] == "UNKNOWN"
    assert result["local_request_id"] is None  # No provider attempt was recorded.
    assert "secret" not in str(result) + str(events)


def test_provider_attempt_records_model_with_namespace(monkeypatch):
    events = []
    monkeypatch.setattr(llm, 'event', lambda name, **data: events.append((name, data)))
    with llm.provider_attempt('agent', 'XingChenAGI/Xing4.0-29B'):
        pass
    assert events[0][1]['model'] == 'XingChenAGI/Xing4.0-29B'

@pytest.mark.parametrize("rewrite", [True, False])
def test_normal_stream_failure_is_terminal(rewrite):
    from app.services.chat import ChatService
    service = ChatService.__new__(ChatService)
    async def failed_stream(**kwargs):
        yield "partial"
        raise TimeoutError("secret")
    fake = SimpleNamespace(rewrite_query=AsyncMock(side_effect=TimeoutError("secret")), generate_stream=failed_stream)
    service.llm_service = fake
    service._retrieve_with_new_service = AsyncMock()
    message = SimpleNamespace(role=SimpleNamespace(value="user"), content="question")
    async def run():
        return [item async for item in service._chat_stream_normal(
            messages=[message], kb_ids=["kb"] if rewrite else [], top_k=1,
            score_threshold=0, model_id=None, rerank_enabled=False,
            rerank_score_threshold=0, rerank_model_id=None, use_rewrite=rewrite)]
    events = asyncio.run(run())
    assert events[-1][0] == "error"
    assert events[-1][1]["status"] == "UNKNOWN"
    assert not any(name == "done" for name, _ in events)
    assert "secret" not in str(events)
    service._retrieve_with_new_service.assert_not_awaited()


@pytest.mark.parametrize("exc, expected", [
    (TimeoutError("secret"), "UNKNOWN"),
    (llm.httpx.ConnectError("secret"), "PRE_CONNECT_FAILED"),
    (llm.httpx.HTTPStatusError("secret", request=llm.httpx.Request("POST", "https://example.invalid"), response=llm.httpx.Response(429)), "FAILED"),
])
def test_attempt_id_precedes_dispatch_and_correlates_sse(monkeypatch, exc, expected):
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    async def dispatch(*args, **kwargs):
        assert events[-1][0] == "provider.request_started"
        raise exc
    model = SimpleNamespace(ainvoke=dispatch)
    with pytest.raises(type(exc)) as raised:
        asyncio.run(llm.invoke_provider_model(model, [], config={}, stage="intent", model_name="test"))
    payload = llm.provider_error_payload(raised.value, stage="agent_workflow")
    start = events[0][1]
    finish = events[1][1]
    assert start["local_request_id"] == finish["local_request_id"] == payload["local_request_id"]
    assert finish["status"] == payload["status"] == expected
    assert start["attempt_count"] == 1 and start["model"] == "test"
    assert "secret" not in str(events) + str(payload)


def test_provider_success_id_is_recorded(monkeypatch):
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    model = SimpleNamespace(ainvoke=AsyncMock(return_value=SimpleNamespace(response_metadata={"request_id": "req_123"})))
    asyncio.run(llm.invoke_provider_model(model, [], config={}, stage="agent", model_name="test"))
    assert events[-1][1]["provider_request_id"] == "req_123"
    assert events[-1][1]["status"] == "RETURNED"


def test_real_service_stream_timeout_correlates_dispatch(monkeypatch):
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    class Stream:
        _request_id = "req_stream"
        def __aiter__(self):
            return self
        async def __anext__(self):
            raise TimeoutError("private")
    async def create(**kwargs):
        assert any(name == "provider.request_started" for name, _ in events)
        return Stream()
    service = llm.LLMService(base_url="https://example.invalid", api_key="test", model="test")
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    async def run():
        return [x async for x in service.generate_stream(messages=[])]
    with pytest.raises(TimeoutError) as raised:
        asyncio.run(run())
    payload = llm.provider_error_payload(raised.value, stage="answer_stream")
    start = next(data for name, data in events if name == "provider.request_started")
    assert payload["local_request_id"] == start["local_request_id"]
    assert payload["provider_request_id"] == "req_stream"
    assert payload["status"] == "UNKNOWN"

@pytest.mark.parametrize("finish_reason, status", [("length", "TRUNCATED"), (None, "UNKNOWN")])
def test_stream_incomplete_is_terminal_after_partial(monkeypatch, finish_reason, status):
    class Stream:
        _request_id = "req_stream"
        def __aiter__(self):
            self.items = iter([SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="partial"), finish_reason=finish_reason)])])
            return self
        async def __anext__(self):
            try:
                return next(self.items)
            except StopIteration:
                raise StopAsyncIteration
    create = AsyncMock(return_value=Stream())
    service = llm.LLMService(base_url="https://example.invalid", api_key="test", model="test")
    service._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    from app.services.chat import ChatService
    chat = ChatService.__new__(ChatService)
    chat.llm_service = service
    message = SimpleNamespace(role=SimpleNamespace(value="user"), content="question")
    async def run():
        return [item async for item in chat._chat_stream_normal(messages=[message], kb_ids=[], top_k=1, score_threshold=0, model_id=None, rerank_enabled=False, rerank_score_threshold=0, rerank_model_id=None)]
    events = asyncio.run(run())
    assert ("answer_chunk", {"content": "partial"}) in events
    assert events[-1][0] == "error" and events[-1][1]["status"] == status
    assert events[-1][1]["finish_reason"] == finish_reason
    assert events[-1][1]["local_request_id"] and events[-1][1]["provider_request_id"] == "req_stream"
    assert not any(name == "done" for name, _ in events)
    create.assert_awaited_once()


def test_embedding_http_attempt_and_header(monkeypatch):
    from app.services import embedding
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    async def post(*args, **kwargs):
        assert events[-1][0] == "provider.request_started"
        return llm.httpx.Response(200, headers={"x-request-id": "req_embed"}, json={"data": [{"index": 0, "embedding": [0.2]}]}, request=llm.httpx.Request("POST", "https://example.invalid"))
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return await post(*args, **kwargs)
    monkeypatch.setattr(embedding.httpx, "AsyncClient", lambda **kwargs: Client())
    service = embedding.EmbeddingService(base_url="https://example.invalid", api_key="secret", model="embed")
    assert asyncio.run(service.embed_query("private")) == [0.2]
    assert events[-1][1]["provider_request_id"] == "req_embed"
    assert events[0][1]["local_request_id"] == events[-1][1]["local_request_id"]
    assert "secret" not in str(events) and "private" not in str(events)


def test_rerank_connection_disables_retries_and_records_failure(monkeypatch):
    from app.services import rerank
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    def factory(**kwargs):
        assert kwargs["max_retries"] == 0
        def create(**kwargs):
            assert events[-1][0] == "provider.request_started"
            raise TimeoutError("secret")
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(rerank, "OpenAI", factory)
    result = rerank.test_rerank_connection({"base_url": "https://example.invalid", "model_name": "test", "api_key": "secret"})
    assert result["status"] == "UNKNOWN" and result["local_request_id"] == events[0][1]["local_request_id"]
    assert "secret" not in str(result) + str(events)


def test_embedding_timeout_stops_sequential_batch(monkeypatch):
    from app.services import embedding
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    send = AsyncMock(side_effect=llm.httpx.ReadTimeout("private"))
    class Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, *args, **kwargs): return await send(*args, **kwargs)
    monkeypatch.setattr(embedding.httpx, "AsyncClient", lambda **kwargs: Client())
    service = embedding.EmbeddingService(base_url="https://example.invalid/api/embeddings", api_key="secret", model="embed")
    with pytest.raises(llm.httpx.ReadTimeout) as raised:
        asyncio.run(service.embed_documents(["first", "second"]))
    send.assert_awaited_once()
    assert raised.value.provider_status == "UNKNOWN"
    assert events[0][1]["local_request_id"] == events[-1][1]["local_request_id"]


def test_http_error_status_and_safe_header(monkeypatch):
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    response = llm.httpx.Response(429, headers={"x-request-id": "req_http"}, request=llm.httpx.Request("POST", "https://example.invalid"))
    with pytest.raises(llm.httpx.HTTPStatusError) as raised:
        llm.invoke_provider_http(lambda: response, stage="rerank", model_name="test")
    assert raised.value.provider_status == "FAILED"
    assert raised.value.provider_request_id == "req_http"
    assert events[0][1]["local_request_id"] == events[1][1]["local_request_id"]

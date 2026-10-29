from types import SimpleNamespace
from unittest.mock import Mock
import sys

import httpx
import pytest

sys.modules.setdefault("chromadb", Mock())
sys.modules.setdefault("chromadb.config", Mock())
from app.services import rerank


MODEL = SimpleNamespace(base_url="https://example.invalid", api_key="test", model_name="test")


@pytest.mark.parametrize("error", [httpx.ReadTimeout("timeout")])
def test_unknown_endpoint_does_not_fallback_or_hide_error(monkeypatch, error):
    post = Mock(side_effect=error)
    monkeypatch.setattr(rerank.httpx, "post", post)
    service = rerank.RerankService(None)
    fallback = Mock()
    monkeypatch.setattr(service, "_rerank_via_chat", fallback)
    with pytest.raises(type(error)):
        service.rerank("q", ["candidate"], MODEL)
    assert post.call_count == 1
    fallback.assert_not_called()


def test_known_endpoint_failure_preserves_chat_fallback(monkeypatch):
    response = Mock()
    response.raise_for_status.side_effect = ValueError("known failure")
    monkeypatch.setattr(rerank.httpx, "post", Mock(return_value=response))
    service = rerank.RerankService(None)
    fallback = Mock(return_value=[{"index": 0, "score": 1.0}])
    monkeypatch.setattr(service, "_rerank_via_chat", fallback)
    assert service.rerank("q", ["candidate"], MODEL) == [{"index": 0, "score": 1.0}]
    fallback.assert_called_once()


def test_unknown_chat_failure_propagates_and_retries_disabled(monkeypatch):
    create = Mock(side_effect=httpx.ReadTimeout("timeout"))
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    factory = Mock(return_value=client)
    monkeypatch.setattr(rerank, "OpenAI", factory)
    service = rerank.RerankService(None)
    with pytest.raises(httpx.ReadTimeout):
        service._rerank_via_chat("q", ["candidate"], MODEL)
    assert factory.call_args.kwargs["max_retries"] == 0
    create.assert_called_once()


def test_unknown_batch_stops_before_next_paid_call():
    service = SimpleNamespace(rerank=Mock(side_effect=httpx.ReadTimeout("timeout")))
    candidates = [{"content": "first document"}, {"content": "second document"}]
    with pytest.raises(httpx.ReadTimeout):
        rerank.batch_rerank("q", candidates, service, MODEL, max_context_tokens=1)
    assert service.rerank.call_count == 1


def test_known_batch_failure_retains_original_scores():
    service = SimpleNamespace(rerank=Mock(side_effect=ValueError("known failure")))
    results = rerank.batch_rerank("q", [{"content": "document", "score": 0.7}], service, MODEL)
    assert results[0]["rerank_score"] == 0.7


def test_known_preconnect_failure_records_status_before_fallback(monkeypatch):
    from app.services import llm
    events = []
    monkeypatch.setattr(llm, "event", lambda name, **data: events.append((name, data)))
    monkeypatch.setattr(rerank.httpx, "post", Mock(side_effect=httpx.ConnectError("offline")))
    service = rerank.RerankService(None)
    fallback = Mock(return_value=[])
    monkeypatch.setattr(service, "_rerank_via_chat", fallback)
    assert service.rerank("q", ["candidate"], MODEL) == []
    fallback.assert_called_once()
    assert events[1][1]["status"] == "PRE_CONNECT_FAILED"
    assert events[0][1]["local_request_id"] == events[1][1]["local_request_id"]

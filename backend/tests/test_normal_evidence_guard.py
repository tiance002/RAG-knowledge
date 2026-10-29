import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
sys.modules.setdefault("chromadb", MagicMock())
sys.modules.setdefault("chromadb.config", MagicMock())
from app.schemas import Message, MessageRole
from app.services import chat


class FakeSession:
    def __init__(self, *args): pass
    def __enter__(self): return self
    def __exit__(self, *args): pass


def collect(monkeypatch, results, status, kb_ids, question="根据知识库回答"):
    monkeypatch.setenv('RAG_GROUNDED_GENERATION', 'false')
    monkeypatch.setattr(chat, "Session", FakeSession)
    service = chat.ChatService.__new__(chat.ChatService)
    async def generate_stream(**kwargs):
        yield "普通聊天内容"
    service.llm_service = SimpleNamespace(generate_stream=generate_stream)
    service._retrieve_with_new_service = AsyncMock(return_value=(results, {"retrieval_status": status}))
    async def run():
        return [event async for event in service._chat_stream_normal(
            [Message(role=MessageRole.USER, content=question)], kb_ids, 5, 0.1,
            None, False, 0.0, None)]
    return asyncio.run(run()), service


@pytest.mark.parametrize("status, expected", [
    ("NO_RESULTS", "未在所选知识库"),
    ("RETRIEVAL_ERROR", "检索发生错误"),
    ("UNKNOWN", "检索状态不确定"),
])
def test_normal_kb_empty_status_is_explicit(monkeypatch, status, expected):
    events, _ = collect(monkeypatch, [], status, ["kb"])
    assert expected in next(data["content"] for kind, data in events if kind == "answer_chunk")
    assert not any("普通聊天内容" in str(data) for _, data in events)
    assert events[-1][0] == "done"
    assert events[-1][1]['retrieval_status'] == status


@pytest.mark.parametrize("status", ["SUCCESS", "UNKNOWN", "RETRIEVAL_ERROR"])
def test_normal_kb_outputs_real_excerpts_and_citations(monkeypatch, status):
    events, _ = collect(monkeypatch, [{"id": "c1", "content": "收入为 42 万元。", "file_name": "report"}], status, ["kb"])
    answer = next(data["content"] for kind, data in events if kind == "answer_chunk")
    citations = next(data["citations"] for kind, data in events if kind == "rag_result")
    assert "收入为 42 万元。" in answer
    assert "普通聊天内容" not in answer
    assert citations[0]["chunk_id"] == "c1" and citations[0]["content"] == "收入为 42 万元。"
    if status != "SUCCESS": assert "未完全成功" in answer


def test_normal_no_kb_explicit_source_does_not_answer_from_model(monkeypatch):
    events, service = collect(monkeypatch, [], "NO_RESULTS", [], "According to the knowledge base, answer this")
    assert "请先选择知识库" in next(data["content"] for kind, data in events if kind == "answer_chunk")
    service._retrieve_with_new_service.assert_not_awaited()


def test_normal_no_kb_general_chat_streams(monkeypatch):
    events, service = collect(monkeypatch, [], "NO_RESULTS", [], "你好")
    assert next(data["content"] for kind, data in events if kind == "answer_chunk") == "普通聊天内容"
    service._retrieve_with_new_service.assert_not_awaited()


def test_normal_low_relevance_empty_and_with_candidates(monkeypatch):
    events, _ = collect(monkeypatch, [], "LOW_RELEVANCE", ["kb"])
    assert "相关性不足" in next(data["content"] for kind, data in events if kind == "answer_chunk")
    events, _ = collect(monkeypatch, [{"id": "c", "content": "候选原文"}], "LOW_RELEVANCE", ["kb"])
    answer = next(data["content"] for kind, data in events if kind == "answer_chunk")
    assert "相关性不足" in answer and "候选原文" in answer


def test_normal_image_only_requires_verification(monkeypatch):
    events, _ = collect(monkeypatch, [{"id": "i", "content": "[图片: kb/image.png]", "content_type": "image"}], "SUCCESS", ["kb"])
    answer = next(data["content"] for kind, data in events if kind == "answer_chunk")
    assert "图片内容需要进一步核验" in answer and "[图片:" not in answer
    assert next(data["citations"] for kind, data in events if kind == "rag_result") == []
def test_empty_rerank_preserves_evidence_and_marks_unknown(monkeypatch):
    from unittest.mock import AsyncMock
    from app.services import chat
    service = chat.ChatService.__new__(chat.ChatService)
    candidate = {'id': 'chunk-1', 'file_id': 'file-1', 'content': 'source text', 'score': 0.9}
    service.retrieval_service = SimpleNamespace(search=AsyncMock(return_value=([candidate], {'retrieval_status': 'SUCCESS'})))
    monkeypatch.setattr(chat, 'RerankService', lambda session: object())
    monkeypatch.setattr(chat, 'batch_rerank', lambda **kwargs: [])
    results, metrics = asyncio.run(service._retrieve_with_new_service(
        session=None, query='q', kb_ids=['kb'], top_k=5, score_threshold=0,
        search_mode='fulltext', vector_weight=0.7, bm25_weight=0.3,
        rerank_enabled=True, rerank_score_threshold=0, rerank_model_id=None,
        rerank_top_k=3, context_window=0))
    assert results == [candidate]
    assert metrics['retrieval_status'] == 'UNKNOWN'


def test_normal_no_kb_uploaded_document_request_is_guarded(monkeypatch):
    events, service = collect(monkeypatch, [], "NO_RESULTS", [], "请参考我上传的文档回答这个问题")
    assert "请先选择知识库" in next(data["content"] for kind, data in events if kind == "answer_chunk")
    service._retrieve_with_new_service.assert_not_awaited()


def test_no_kb_direct_mention_does_not_answer_without_sources(monkeypatch):
    events, _ = collect(monkeypatch, [], "NO_RESULTS", [], "知识库里的预算是多少？")
    assert "请先选择知识库" in next(data["content"] for kind, data in events if kind == "answer_chunk")


def test_no_kb_explicit_negation_remains_chat(monkeypatch):
    events, _ = collect(monkeypatch, [], "NO_RESULTS", [], "不用知识库，聊聊天")
    assert next(data["content"] for kind, data in events if kind == "answer_chunk") == "普通聊天内容"


@pytest.mark.parametrize("original", ["page seven evidence", None, " "])
def test_expanded_evidence_uses_only_original_page(monkeypatch, original):
    result = {"id": "c1", "file_id": "f1", "file_name": "source.pdf", "page_number": 7,
              "content": "page six secret\npage seven evidence\npage eight secret",
              "original_content": original, "metadata": {"is_expanded": True}}
    events, _ = collect(monkeypatch, [result], "SUCCESS", ["kb"])
    citations = next(data["citations"] for kind, data in events if kind == "rag_result")
    answer = next(data["content"] for kind, data in events if kind == "answer_chunk")
    assert "page six secret" not in str(events) and "page eight secret" not in str(events)
    if original and original.strip():
        assert citations[0]["content"] == original
        assert citations[0]["fileId"] == "f1" and citations[0]["location"] == "第 7 页"
        assert original in answer
    else:
        assert citations == []
        assert events[-1][1]["retrieval_status"] == "UNKNOWN"

"""Run the existing chat pipeline under a durable conversation turn lease."""
from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import suppress
from typing import Any, AsyncIterator

from app.schemas import Message, MessageRole


async def run_turn(store, reservation: dict, *, kb_ids: list[str], params: dict,
                   runtime_snapshot, model_binding=None, memory_scope=None,
                   request=None, admission_lease=None) -> AsyncIterator[tuple[str, dict]]:
    """Persist a terminal outcome before exposing it; never retry provider work.

    The pending anext task survives heartbeat timeouts. A timeout must never
    cancel and restart the provider request. Only completion/cancellation writes
    the accumulated answer; answer chunks do not create database transactions.
    """
    turn = reservation["turn"]
    session = reservation["session"]
    turn_id, session_id = turn["id"], session["id"]
    revision = session["revision"]
    identity = {"turn_id": turn_id, "session_id": session_id, "session_revision": revision}
    if not reservation["created"]:
        # Idempotent repeats only report the original receipt. They never open
        # another stream or cause an interrupted/unknown request to be retried.
        yield "turn_started", {**identity, "revision": revision, "created": False}
        yield "done", {**identity, "status": turn["status"],
                       "generation_status": turn["generation_status"], "replayed": True,
                       "assistant_content": turn.get("assistant_content", ""),
                       "citations": turn.get("citations", [])}
        return

    token = reservation["lease_token"]
    parts: list[str] = []
    citations: list[dict] = []
    metadata: dict[str, Any] = {}
    stream = None
    pending = None
    finalized = False
    provider_possible = False
    last_heartbeat = time.monotonic()
    last_control_read = 0.0
    delivery_state = {"citations": [], "provider_requests": [],
                      "source_engine": getattr(store, 'source_engine', store.engine), "kb_ids": kb_ids}

    def finish(status: str, generation_status: str, data: dict):
        nonlocal finalized
        delivered = {(
            evidence.get("chunk_id"), evidence.get("file_id"), evidence.get("content_hash"), evidence.get("url")
        ) for call in delivery_state["provider_requests"] for evidence in call.get("evidence", [])}
        persisted_citations = []
        for position, source in enumerate(citations, start=1):
            body = source.get("content") or ""
            source_identity = (source.get("chunk_id") or source.get("chunkId"),
                               source.get("file_id") or source.get("fileId"),
                               hashlib.sha256(body.encode("utf-8")).hexdigest(), source.get("url"))
            source_index = source.get("source_id")
            citation_index = (source_index if params.get('mode') == 'agent'
                              and type(source_index) is int else position)
            persisted_citations.append({**source, "content_hash": source_identity[2],
                "citation_index": citation_index,
                "origin": source.get("origin") or "CURRENT_RETRIEVAL",
                "delivery_status": "PROVIDER_REQUEST_PREPARED" if source_identity in delivered else "UNKNOWN"})
        from app.services.conversation_store import ConversationError
        try:
            result = store.finish_turn(
                turn_id, token, "".join(parts), status, generation_status,
                {**metadata, **data, "provider_requests": delivery_state["provider_requests"]},
                persisted_citations, understanding=delivery_state.get("understanding"),
            )
        except ConversationError as exc:
            if exc.code != "LEASE_LOST":
                raise
            # A recovery/fencing transaction has already established the
            # authoritative UNKNOWN outcome. Never overwrite another lease.
            result = store.get_turn(turn_id)
            if result["status"] == "RUNNING":
                raise
        identity["session_revision"] = store.get_session(session_id)["revision"]
        finalized = True
        return result

    async def stop_pending():
        nonlocal pending
        if pending is not None:
            task = pending
            pending = None
            # Attach ownership before any await: an ASGI disconnect can cancel
            # cleanup repeatedly while the provider still suppresses cancellation.
            def discard_result(completed):
                with suppress(asyncio.CancelledError, Exception):
                    completed.result()
            if admission_lease is not None:
                admission_lease.hold_until(task)
            task.add_done_callback(discard_result)
            task.cancel()
            await asyncio.wait({task}, timeout=1.0)

    try:
        yield "turn_started", {**identity, "revision": revision, "created": True}
        if not kb_ids and not params.get("web_search_enabled", False):
            message = "当前没有可用的知识库，请先创建或选择知识库并上传资料。"
            parts.append(message)
            yield "answer_chunk", {"content": message}
            data = {"usage": {}, "retrieval_status": "NO_SCOPE", "generation_status": "NOT_RUN"}
            result = finish("COMPLETED", "NOT_RUN", data)
            yield "done", {**data, **identity, "status": result["status"],
                           "generation_status": result["generation_status"]}
            return

        from app.services.conversation_context import prepare_history, delivery_capture_scope
        from app.services.chat import ChatService
        history, candidates, manifest = prepare_history(
            store, session_id, turn["sequence_no"],
            runtime_snapshot.max_chat_history_rounds, kb_ids,
            allow_web=bool(params.get("web_search_enabled", False)),
        )
        metadata["history_manifest"] = manifest
        from app.services.llm import LLMService
        resolved_llm = None
        if model_binding is not None:
            resolved_llm = LLMService(base_url=model_binding.base_url,
                                      api_key=model_binding.api_key, model=model_binding.model_name)
            resolved_llm.history_rounds = runtime_snapshot.max_chat_history_rounds
        stream = ChatService().chat_stream(
            messages=history + [Message(role=MessageRole.USER, content=turn["user_content"])],
            kb_ids=kb_ids, memory_scope=memory_scope, runtime_snapshot=runtime_snapshot,
            resolved_llm=resolved_llm, history_evidence=candidates,
            history_manifest=manifest, **params,
        )

        async def next_event():
            # A ContextVar token stays in this task and is closed before the
            # runner exposes any event to ASGI's generator/cancellation task.
            with delivery_capture_scope(delivery_state):
                return await anext(stream)

        while True:
            current = None
            now = time.monotonic()
            if now - last_control_read >= 2.0:
                current = store.read_turn_control(turn_id, token)
                last_control_read = now
            if now - last_heartbeat >= 30.0:
                current = store.heartbeat_turn(turn_id, token)
                last_heartbeat = now
            if current and (current.get("cancel_requested") or current["status"] == "CANCELLED"):
                await stop_pending()
                data = {"code": "TURN_CANCELLED", "status": "CANCELLED",
                        "generation_status": "UNKNOWN" if provider_possible else "NOT_RUN",
                        "message": "本轮生成已取消", "retryable": False}
                result = finish("CANCELLED", data["generation_status"], data)
                yield "error", {**data, **identity, "status": result["status"],
                                "generation_status": result["generation_status"]}
                return
            if request is not None and await request.is_disconnected():
                raise asyncio.CancelledError()
            if pending is None:
                provider_possible = True
                pending = asyncio.create_task(next_event())
            ready, _ = await asyncio.wait({pending}, timeout=2.0)
            if not ready:
                continue
            task, pending = pending, None
            try:
                event, raw = task.result()
            except StopAsyncIteration:
                data = {"code": "TURN_STREAM_INCOMPLETE", "status": "UNKNOWN",
                        "generation_status": "UNKNOWN", "message": "生成结果未确认，请查看本轮记录。",
                        "retryable": False}
                result = finish("UNKNOWN", "UNKNOWN", data)
                yield "error", {**data, **identity, "status": result["status"],
                                "generation_status": result["generation_status"]}
                return
            data = dict(raw)
            if event == "answer_chunk":
                parts.append(str(data.get("content", "")))
            elif event == "rag_result":
                if isinstance(data.get("citations"), list):
                    citations = data["citations"]
                    delivery_state["citations"] = citations
                metadata["retrieval"] = {key: value for key, value in data.items()
                                         if key not in ("citations", "original_citations")}
            elif event in ("done", "error"):
                if event == "done":
                    generation = data.get("generation_status", "COMPLETED")
                    status = ("UNKNOWN" if generation == "UNKNOWN" else
                              "FAILED" if generation in ("TRUNCATED", "EMPTY", "FAILED") else "COMPLETED")
                else:
                    generation = data.get("generation_status") or data.get("status", "FAILED")
                    status = "UNKNOWN" if str(generation).upper() == "UNKNOWN" else "FAILED"
                result = finish(status, str(generation), data)
                terminal_event = "error" if result["status"] == "CANCELLED" else event
                if result["status"] == "CANCELLED":
                    data = {**data, "code": "TURN_CANCELLED", "message": "本轮生成已取消", "retryable": False}
                yield terminal_event, {**data, **identity, "status": result["status"],
                              "generation_status": result["generation_status"]}
                return
            yield event, data
    except (asyncio.CancelledError, GeneratorExit):
        # The terminal write runs in finally before any cancellable cleanup.
        raise
    except Exception as exc:
        await stop_pending()
        from app.services.llm import provider_error_payload
        data = provider_error_payload(exc, stage="conversation")
        status = "UNKNOWN" if data.get("status") == "UNKNOWN" else "FAILED"
        if not finalized:
            result = finish(status, status, data)
            status = result["status"]
        yield "error", {**data, **identity, "status": status,
                        "generation_status": status, "retryable": False}
    finally:
        if not finalized:
            # AnyIO may repeatedly cancel awaited cleanup after a disconnect.
            # Persist the fenced receipt synchronously first, including a user
            # cancellation recorded before the browser closed its stream.
            from app.services.conversation_store import ConversationError
            try:
                control = store.read_turn_control(turn_id, token)
                cancelled = bool(control.get("cancel_requested"))
            except ConversationError as exc:
                if exc.code != "LEASE_LOST":
                    raise
                cancelled = False
            finish("CANCELLED" if cancelled or not provider_possible else "UNKNOWN",
                   "UNKNOWN" if provider_possible else "NOT_RUN",
                   {"code": "TURN_CANCELLED" if cancelled else "CLIENT_DISCONNECTED", "retryable": False})
        await stop_pending()
        if stream is not None:
            with suppress(asyncio.CancelledError, Exception):
                await stream.aclose()

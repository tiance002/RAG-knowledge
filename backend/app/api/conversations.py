"""Durable server-authoritative conversation API.

Clients submit only a new question and a concurrency/idempotency receipt. Scope,
configuration, previous messages and evidence are read from server storage.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import json
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlmodel import Session, select

from app.core.database import engine, get_session
from app.models import KnowledgeBase
from app.schemas import ChatRequest
from app.services.conversation_store import ConversationError, ConversationStore, validate_config_overrides
from app.services.memory import PreferenceScope, get_preference_scope
from app.services.request_admission import acquire_request, AdmittedStreamingResponse

router = APIRouter(tags=["Conversations"])


class StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


CONFIG_KEYS = set(ChatRequest.model_fields) - {"messages", "kb_ids", "stream", "query_images"}


def validate_overrides(value: dict[str, Any]) -> dict[str, Any]:
    # The store is the single authority for types/ranges on persisted settings.
    return validate_config_overrides(value)


class SessionCreate(StrictBody):
    title: str = Field(default="新会话", max_length=200)
    scope_mode: Literal["SELECTED", "ALL_ENABLED"] = "ALL_ENABLED"
    kb_ids: list[str] = Field(default_factory=list, max_length=1000)
    config_overrides: dict[str, Any] = Field(default_factory=dict)

    _overrides = field_validator("config_overrides")(validate_overrides)


class SessionPatch(StrictBody):
    expected_revision: int = Field(ge=0)
    title: str | None = Field(default=None, max_length=200)
    scope_mode: Literal["SELECTED", "ALL_ENABLED"] | None = None
    kb_ids: list[str] | None = Field(default=None, max_length=1000)
    config_overrides: dict[str, Any] | None = None

    @field_validator("config_overrides")
    @classmethod
    def check_overrides(cls, value):
        return validate_overrides(value) if value is not None else value


class TurnCreate(StrictBody):
    question: str = Field(min_length=1, max_length=100000)
    client_request_id: str = Field(min_length=1, max_length=200)
    expected_revision: int = Field(ge=0)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("question cannot be blank")
        return value


class SessionRewrite(StrictBody):
    query: str = Field(min_length=1, max_length=100000)


class HistoryImport(StrictBody):
    sessions: list[Any] = Field(max_length=10000)


def get_store():
    return ConversationStore(engine)


def store_call(method, *args, **kwargs):
    try:
        return method(*args, **kwargs)
    except ConversationError as exc:
        raise HTTPException(status_code=exc.status_code,
                            detail={"code": exc.code, "message": str(exc)}) from exc


def resolve_scope(db: Session, session: dict) -> list[str]:
    active = list(db.exec(select(KnowledgeBase.id).where(KnowledgeBase.is_deleted == False)).all())
    if session["scope_mode"] == "ALL_ENABLED":
        return sorted(active)
    selected = list(dict.fromkeys(session["kb_ids"]))
    unavailable = set(selected) - set(active)
    if unavailable:
        raise HTTPException(409, detail={"code": "KNOWLEDGE_SCOPE_UNAVAILABLE",
                                        "message": "会话选定的知识库已删除或不可用，请创建新的会话。"})
    return selected


def effective_settings(db: Session, session: dict):
    from app.services.runtime_settings import get_runtime_settings, RuntimeSettingsUnavailable, SettingsConflict
    try:
        snapshot = get_runtime_settings(db)
    except (RuntimeSettingsUnavailable, SettingsConflict) as exc:
        raise HTTPException(503, detail={"code": "SETTINGS_UNAVAILABLE",
                                         "message": "系统设置暂不可用，请先检查设置后再试"}) from exc
    overrides = validate_overrides(session["config_overrides"])
    if "max_chat_history_rounds" in overrides:
        snapshot = replace(snapshot, max_chat_history_rounds=overrides["max_chat_history_rounds"])
    request = ChatRequest(messages=[], kb_ids=[], **{k: v for k, v in overrides.items() if k in CONFIG_KEYS})
    params = {key: getattr(request, key) for key in CONFIG_KEYS}
    # Freeze the resolved default identity, not a client-side display name.
    if params["model_id"] is None:
        params["model_id"] = snapshot.default_llm_model
    return params, snapshot


def public_provider_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if parts.port is not None:
        host += ":" + str(parts.port)
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


@router.post("/chat/sessions", status_code=201)
def create_session(data: SessionCreate, db: Session = Depends(get_session), store=Depends(get_store)):
    payload = data.model_dump()
    resolve_scope(db, payload)
    return store_call(store.create_session, payload)


@router.get("/chat/sessions")
def list_sessions(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0), store=Depends(get_store)):
    return {"items": store_call(store.list_sessions, limit=limit, offset=offset)}


@router.get("/chat/sessions/{session_id}")
def get_session_detail(session_id: str, store=Depends(get_store)):
    return store_call(store.get_session, session_id)


@router.patch("/chat/sessions/{session_id}")
def patch_session(session_id: str, data: SessionPatch, db: Session = Depends(get_session), store=Depends(get_store)):
    changes = data.model_dump(exclude_unset=True, exclude={"expected_revision"})
    if any(value is None for value in changes.values()):
        raise HTTPException(422, detail={"code": "NULL_CONFIGURATION", "message": "会话字段不能为 null"})
    original = store_call(store.get_session, session_id)
    resolve_scope(db, {**original, **changes})
    return store_call(store.patch_session, session_id, changes, data.expected_revision)


@router.delete("/chat/sessions/{session_id}")
def archive_session(session_id: str, store=Depends(get_store)):
    return store_call(store.archive_session, session_id)


@router.get("/chat/sessions/{session_id}/turns")
def list_turns(session_id: str, limit: int = Query(50, ge=1, le=200),
               before_seq: int | None = Query(None, ge=1), store=Depends(get_store)):
    return {"items": store_call(store.list_turns, session_id, limit=limit, before_seq=before_seq)}


@router.get("/chat/turns/{turn_id}")
def get_turn(turn_id: str, store=Depends(get_store)):
    return store_call(store.get_turn, turn_id)


@router.post("/chat/turns/{turn_id}/cancel")
def cancel_turn(turn_id: str, store=Depends(get_store)):
    return store_call(store.cancel_turn, turn_id)


@router.post("/chat/sessions/{session_id}/turns")
async def create_turn(session_id: str, data: TurnCreate, request: Request,
                      db: Session = Depends(get_session), store=Depends(get_store),
                      memory_scope: PreferenceScope = Depends(get_preference_scope)):
    # Read an existing request receipt before resolving today's settings/scope.
    # A model or KB removed after the original call cannot invalidate its replay.
    reservation = store_call(store.find_request, session_id, data.client_request_id, data.question)
    binding, snapshot, kb_ids, params = None, None, [], {}
    lease = None
    if reservation is None:
        lease = await acquire_request(request)
    try:
        if reservation is None:
            reservation = store_call(store.find_request, session_id, data.client_request_id, data.question)
        if reservation is None:
            conversation = store_call(store.get_session, session_id)
            kb_ids = resolve_scope(db, conversation)
            params, snapshot = effective_settings(db, conversation)
            from app.services.model_bindings import resolve_model, ModelBindingError
            if kb_ids or params.get("web_search_enabled", False):
                try:
                    binding = resolve_model(db, "chat", params["model_id"], use_default=False)
                except ModelBindingError as exc:
                    raise HTTPException(400, detail={"code": "MODEL_BINDING_INVALID", "message": str(exc)}) from exc
            effective = {**params, "runtime_settings": asdict(snapshot),
                         "kb_ids": kb_ids, "scope_mode": conversation["scope_mode"]}
            if binding is not None:
                effective["model_binding"] = {"binding_id": binding.binding_id,
                                              "model_name": binding.model_name,
                                              "base_url": public_provider_url(binding.base_url), "source": binding.source}
            reservation = store_call(store.start_turn, session_id, data.question,
                                     data.client_request_id, data.expected_revision, effective)

        if reservation is not None and not reservation["created"] and lease is not None:
            lease.release()
            lease = None
    except BaseException:
        if lease is not None:
            lease.release()
        raise
    started = False

    def cleanup_unstarted():
        if not started and reservation["created"]:
            try:
                store.finish_turn(reservation["turn"]["id"], reservation["lease_token"], "",
                                  "CANCELLED", "NOT_RUN", {"code": "STREAM_NOT_STARTED"}, [])
            except ConversationError:
                pass

    async def events():
        nonlocal started
        started = True
        from app.services.conversation_runner import run_turn
        stream = run_turn(store, reservation, kb_ids=kb_ids, params=params,
                          runtime_snapshot=snapshot, model_binding=binding,
                          memory_scope=memory_scope, request=request, admission_lease=lease)
        try:
            async for event, payload in stream:
                from app.services.metrics import sse_event
                sse_event(event, payload)
                yield f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
        finally:
            await stream.aclose()

    return AdmittedStreamingResponse(events(), lease=lease, on_unstarted=cleanup_unstarted, media_type="text/event-stream", headers={
        "X-Chat-Admission-Wait-Ms": str(round(lease.wait_seconds * 1000, 2)) if lease else "0",
        "Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no",
    })


@router.post("/chat/sessions/{session_id}/rewrite")
async def rewrite_session(session_id: str, data: SessionRewrite, request: Request,
                          db: Session = Depends(get_session), store=Depends(get_store)):
    lease = await acquire_request(request)
    try:
        conversation = store_call(store.get_session, session_id)
        kb_ids = resolve_scope(db, conversation)
        params, snapshot = effective_settings(db, conversation)
        from app.services.conversation_context import prepare_history
        from app.services.model_bindings import resolve_model
        from app.services.llm import LLMService
        # Rewrite previews do not start a turn or mutate history.
        history, _, _ = prepare_history(store, session_id, 2**63 - 1,
                                         snapshot.max_chat_history_rounds, kb_ids)
        try:
            binding = resolve_model(db, "chat", params["model_id"], use_default=False)
            llm = LLMService(base_url=binding.base_url, api_key=binding.api_key, model=binding.model_name)
            llm.history_rounds = snapshot.max_chat_history_rounds
            result = await llm.understand_query(data.query, history=[
                {"role": item.role.value, "content": item.content} for item in history])
            return {"rewritten_query": result.get("rewrite_query") or data.query,
                    "intent": result.get("intent"), "image_description": result.get("image_description")}
        except Exception as exc:
            from app.services.llm import provider_error_payload
            from app.services.model_bindings import ModelBindingError
            if isinstance(exc, ModelBindingError):
                raise HTTPException(400, detail={"code": "MODEL_BINDING_INVALID", "message": str(exc)}) from exc
            raise HTTPException(502, detail=provider_error_payload(exc, stage="rewrite")) from exc

    finally:
        lease.release()

@router.post("/chat/history-import/preview")
def preview_import(data: HistoryImport, store=Depends(get_store)):
    return store_call(store.preview_import, data.model_dump())


@router.post("/chat/history-import")
def import_history(data: HistoryImport, store=Depends(get_store)):
    return store_call(store.import_history, data.model_dump())

"""
对话 API 路由
"""

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlmodel import Session
from typing import AsyncGenerator
import json

from app.core.database import get_session
from app.models import KnowledgeBase
from app.schemas import (
    RewriteRequest,
    RewriteResponse,
    ChatRequest,
    ApiResponse,
)
from app.services.chat import ChatService
from app.services.llm import LLMService
from app.services.memory import get_preference_scope, PreferenceScope

router = APIRouter(tags=["Chat"])


@router.post("/chat/rewrite", response_model=ApiResponse[RewriteResponse])
async def rewrite_query(data: RewriteRequest, session: Session = Depends(get_session)):
    """问题改写"""
    try:
        from app.services.model_bindings import resolve_model
        from app.services.runtime_settings import get_runtime_settings
        snapshot = get_runtime_settings(session)
        selected_id = data.model_id if data.model_id is not None else snapshot.default_llm_model
        binding = resolve_model(session, 'chat', selected_id, use_default=False)
        llm_service = LLMService(base_url=binding.base_url, api_key=binding.api_key,
                                 model=binding.model_name)
        llm_service.history_rounds = snapshot.max_chat_history_rounds
        understanding = await llm_service.understand_query(
            data.query,
            history=[{"role": msg.role.value, "content": msg.content} for msg in data.history],
            images=data.images,
        )
        return ApiResponse(data=RewriteResponse(
            rewritten_query=understanding.get('rewrite_query') or data.query,
            intent=understanding.get('intent') or None,
            image_description=understanding.get('image_description') or None,
        ))
    except Exception as e:
        from app.services.model_bindings import ModelBindingError
        from app.services.runtime_settings import RuntimeSettingsUnavailable, SettingsConflict
        from app.services.llm import provider_error_payload
        if isinstance(e, ModelBindingError):
            raise HTTPException(status_code=400, detail={'code': 'MODEL_BINDING_INVALID', 'message': str(e)}) from e
        if isinstance(e, (RuntimeSettingsUnavailable, SettingsConflict)):
            raise HTTPException(status_code=503, detail={'code': 'SETTINGS_UNAVAILABLE', 'message': '系统设置暂不可用，请先检查设置后再试'}) from e
        raise HTTPException(status_code=502, detail=provider_error_payload(e, stage='rewrite')) from e


@router.post("/chat/completions")
async def chat_completions(
    data: ChatRequest,
    session: Session = Depends(get_session),
    memory_scope: PreferenceScope = Depends(get_preference_scope),
):
    """对话接口 (SSE 流式响应)"""
    # 验证知识库存在
    for kb_id in data.kb_ids:
        kb = session.get(KnowledgeBase, kb_id)
        if not kb:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={"code": 40401, "message": f"知识库不存在: {kb_id}"},
            )

    async def generate_sse() -> AsyncGenerator[str, None]:
        """生成 SSE 事件流"""
        chat_service = ChatService()

        try:
            async for event_type, event_data in chat_service.chat_stream(
                messages=data.messages,
                kb_ids=data.kb_ids,
                use_rewrite=data.use_rewrite,
                query_images=data.query_images,
                mode=data.mode,
                top_k=data.top_k,
                score_threshold=data.score_threshold,
                model_id=data.model_id,
                rerank_enabled=data.rerank_enabled,
                rerank_score_threshold=data.rerank_score_threshold,
                rerank_model_id=data.rerank_model_id,
                # 新增检索参数
                search_mode=data.search_mode,
                vector_weight=data.vector_weight,
                bm25_weight=data.bm25_weight,
                rerank_top_k=data.rerank_top_k,
                context_window=data.context_window,
                memory_scope=memory_scope,
                web_search_enabled=data.web_search_enabled,
            ):
                from app.services.metrics import sse_event
                sse_event(event_type, event_data)
                # 格式化 SSE 事件
                yield f"event: {event_type}\n"
                yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

        except Exception as e:
            # 发送错误事件
            from app.services.llm import provider_error_payload
            from app.services.model_bindings import ModelBindingError
            from app.services.runtime_settings import RuntimeSettingsUnavailable, SettingsConflict
            error_data = ({'code': 'MODEL_BINDING_INVALID', 'status': 'FAILED',
                           'error': str(e), 'stage': 'model_selection', 'retryable': False}
                          if isinstance(e, ModelBindingError)
                          else {'code': 'SETTINGS_UNAVAILABLE', 'status': 'FAILED',
                                'error': '系统设置暂不可用，请先检查设置后再试', 'stage': 'configuration', 'retryable': False}
                          if isinstance(e, (RuntimeSettingsUnavailable, SettingsConflict))
                          else provider_error_payload(e, stage="sse"))
            from app.services.metrics import sse_event
            sse_event("error", error_data)
            yield f"event: error\n"
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

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
from app.services.llm import LLMService, close_quietly, safely_closing
from app.services.memory import get_preference_scope, PreferenceScope
from app.services.model_bindings import resolve_model, ModelBindingError

router = APIRouter(tags=["Chat"])


def _resolve_chat_binding(session, model_id):
    """Freeze the explicit or persisted default identity at the API boundary."""
    try:
        return resolve_model(session, 'chat', model_id)
    except ModelBindingError as exc:
        raise HTTPException(status_code=400, detail={'code': 'MODEL_BINDING_INVALID', 'message': str(exc)}) from exc


@router.post("/chat/rewrite", response_model=ApiResponse[RewriteResponse])
async def rewrite_query(data: RewriteRequest, session: Session = Depends(get_session)):
    """问题改写"""
    llm_service = None
    try:
        binding = _resolve_chat_binding(session, getattr(data, 'model_id', None))
        llm_service = LLMService(base_url=binding.base_url, api_key=binding.api_key,
                                 model=binding.model_name)
        rewritten = await llm_service.rewrite_query(
            data.query,
            history=[{"role": msg.role.value, "content": msg.content} for msg in data.history],
        )
        return ApiResponse(data=RewriteResponse(rewritten_query=rewritten))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": 50001, "message": f"问题改写失败: {str(e)}"},
        )
    finally:
        if llm_service is not None:
            await close_quietly(llm_service.aclose)


@router.post("/chat/completions")
async def chat_completions(
    data: ChatRequest,
    session: Session = Depends(get_session),
    memory_scope: PreferenceScope = Depends(get_preference_scope),
):
    """对话接口 (SSE 流式响应)"""
    effective_model_id = _resolve_chat_binding(session, data.model_id).binding_id
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
            async with safely_closing(chat_service.chat_stream(
                messages=data.messages,
                kb_ids=data.kb_ids,
                use_rewrite=data.use_rewrite,
                mode=data.mode,
                top_k=data.top_k,
                score_threshold=data.score_threshold,
                model_id=effective_model_id,
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
            )) as stream:
                async for event_type, event_data in stream:
                    from app.services.metrics import sse_event
                    sse_event(event_type, event_data)
                    # 格式化 SSE 事件
                    yield f"event: {event_type}\n"
                    yield f"data: {json.dumps(event_data, ensure_ascii=False)}\n\n"

        except Exception as e:
            # 发送错误事件
            from app.services.llm import provider_error_payload
            error_data = provider_error_payload(e, stage="sse")
            from app.services.metrics import sse_event
            sse_event("error", error_data)
            yield f"event: error\n"
            yield f"data: {json.dumps(error_data, ensure_ascii=False)}\n\n"
        finally:
            await close_quietly(chat_service.aclose)

    return StreamingResponse(
        generate_sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )

from app.services.observability import observe, event, snapshot, enabled, model_response
from app.services.metrics import record_provider_attempt
"""
LLM 服务
"""

from openai import AsyncOpenAI, APIConnectionError
import httpx
import requests
from typing import List, Dict, AsyncGenerator, Any
from langchain_openai import ChatOpenAI
import logging
import time
import uuid
from contextlib import contextmanager
from openai import APIStatusError

from app.core.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

class ProviderStreamError(Exception):
    def __init__(self, status, finish_reason=None):
        super().__init__("Provider stream did not complete")
        self.provider_status = status
        self.finish_reason = finish_reason


def _provider_status(exc: BaseException) -> str:
    if isinstance(exc, ProviderStreamError):
        return exc.provider_status
    if isinstance(exc, (httpx.ConnectError, requests.exceptions.ConnectTimeout)):
        return "PRE_CONNECT_FAILED"
    cause = getattr(exc, "__cause__", None)
    if isinstance(exc, APIConnectionError) and isinstance(cause, httpx.ConnectError):
        return "PRE_CONNECT_FAILED"
    if isinstance(exc, (APIStatusError, httpx.HTTPStatusError, requests.exceptions.HTTPError)):
        return "FAILED"
    if isinstance(exc, (APIConnectionError, TimeoutError, httpx.TimeoutException, httpx.NetworkError,
                        httpx.ProtocolError, httpx.DecodingError,
                        requests.exceptions.Timeout, requests.exceptions.ConnectionError,
                        requests.exceptions.ChunkedEncodingError, requests.exceptions.ContentDecodingError)):
        return "UNKNOWN"
    return "FAILED"


def is_unknown_provider_error(exc: BaseException) -> bool:
    return getattr(exc, "provider_status", None) == "UNKNOWN" or _provider_status(exc) == "UNKNOWN"


def should_abort_provider_error(exc: BaseException) -> bool:
    return hasattr(exc, "local_request_id") or is_unknown_provider_error(exc)


def _safe_id(value):
    if isinstance(value, str) and 0 < len(value) <= 128 and all(c.isalnum() or c in "-_." for c in value):
        return value
    return None


def _provider_id(value):
    metadata = getattr(value, "response_metadata", None) or {}
    headers = getattr(value, "headers", None) or getattr(getattr(value, "response", None), "headers", None) or {}
    return _safe_id(getattr(value, "request_id", None) or getattr(value, "_request_id", None) or metadata.get("request_id") or headers.get("x-request-id"))


def _safe_model(value):
    if isinstance(value, str) and 0 < len(value) <= 128 and all(c.isalnum() or c in "-_./:" for c in value):
        return value
    return None


@contextmanager
def provider_attempt(stage: str, model: str, *, stream: bool = False):
    attempt = {"local_request_id": uuid.uuid4().hex, "stage": stage, "model": _safe_model(model), "attempt_count": 1, "provider_request_id": None}
    started = time.monotonic()
    event("provider.request_started", **attempt)
    try:
        yield attempt
    except BaseException as exc:
        status = _provider_status(exc)
        if isinstance(exc, (GeneratorExit, KeyboardInterrupt, SystemExit)) or type(exc).__name__ == "CancelledError":
            status = "CANCELLED"
        # Once a stream has started, an incomplete response is unknown unless an explicit HTTP failure is available.
        if stream and attempt.get("stream_started") and status not in ("FAILED", "TRUNCATED", "CANCELLED"):
            status = "UNKNOWN"
        elif stream and attempt.get("stream_started") and status not in ("TRUNCATED", "CANCELLED") and not isinstance(exc, (APIStatusError, httpx.HTTPStatusError, requests.exceptions.HTTPError)):
            status = "UNKNOWN"
        attempt["provider_request_id"] = _provider_id(exc) or attempt["provider_request_id"]
        exc.local_request_id = attempt["local_request_id"]
        exc.provider_status = status
        exc.provider_request_id = attempt["provider_request_id"]
        event("provider.request_finished", status=status, error_type=type(exc).__name__, finish_reason=getattr(exc, "finish_reason", None), **{k: v for k, v in attempt.items() if k != "stream_started"})
        record_provider_attempt(stage, status, time.monotonic() - started, streaming=stream, error=exc)
        raise
    else:
        event("provider.request_finished", status="RETURNED", **{k: v for k, v in attempt.items() if k != "stream_started"})
        record_provider_attempt(stage, "RETURNED", time.monotonic() - started, streaming=stream)


async def invoke_provider_http_async(send, *, stage, model_name):
    with provider_attempt(stage, model_name) as attempt:
        response = await send()
        attempt["provider_request_id"] = _provider_id(response)
        response.raise_for_status()
        return response


def invoke_provider_http(send, *, stage, model_name, check_status=True):
    with provider_attempt(stage, model_name) as attempt:
        response = send()
        attempt["provider_request_id"] = _provider_id(response)
        if check_status:
            response.raise_for_status()
        return response


async def invoke_provider_model(model, messages, *, config, stage, model_name):
    with provider_attempt(stage, model_name) as attempt:
        response = await model.ainvoke(messages, config=config)
        attempt["provider_request_id"] = _provider_id(response)
        from app.services.metrics import record_provider_usage
        record_provider_usage(stage, getattr(response, "usage_metadata", None))
        return response


def provider_error_payload(exc: BaseException, *, stage: str, unknown: bool = False) -> dict:
    status = getattr(exc, "provider_status", None) or ("UNKNOWN" if unknown else _provider_status(exc))
    local_request_id = getattr(exc, "local_request_id", None)
    provider_request_id = _safe_id(getattr(exc, "provider_request_id", None)) or _provider_id(exc)
    event("provider.request_error", status=status, stage=stage, error_type=type(exc).__name__, local_request_id=local_request_id, provider_request_id=provider_request_id)
    message = "检索请求未完成，请检查后再手动重试。" if stage == 'retrieval' else "模型请求未完成，请检查后再手动重试。"
    return {"error": message, "status": status, "local_request_id": local_request_id, "provider_request_id": provider_request_id, "finish_reason": getattr(exc, "finish_reason", None)}


class LLMService:
    """LLM 服务"""

    def __init__(self, base_url: str = None, api_key: str = None, model: str = None):
        self.base_url = base_url or settings.LLM_BASE_URL
        
        # 自动修正 base_url: 如果包含 /chat/completions 后缀则移除
        if self.base_url:
            self.base_url = self.base_url.rstrip("/")
            if self.base_url.endswith("/chat/completions"):
                self.base_url = self.base_url[:-17]
            self.base_url = self.base_url.rstrip("/")

        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL
        self._client = None

    def _get_client(self):
        if not self._client:
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=120.0,
                max_retries=0,
                http_client=httpx.AsyncClient(
                    trust_env=False
                )
            )
        return self._client

    @observe("llm.rewrite_query")
    async def rewrite_query(self, query: str) -> str:
        """问题改写"""
        system_prompt = """你是一个问题优化助手。请将用户输入的口语化问题改写为更清晰、更完整的查询语句。
要求：
1. 保持原意不变
2. 补充缺失的上下文
3. 使用更专业的表述
4. 直接返回改写后的问题，不要添加解释"""

        client = self._get_client()
        
        try:
            with provider_attempt("rewrite", self.model) as attempt:
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": query},
                    ],
                    max_tokens=200,
                    temperature=0.3,
                )
                attempt["provider_request_id"] = _provider_id(response)
                model_response(response, role="rewrite", model=self.model)
                choice = response.choices[0]
                reason = getattr(choice, 'finish_reason', None)
                content = getattr(choice.message, 'content', None) or ''
                if reason == 'length':
                    event('query.rewrite_result', status='TRUNCATED', finish_reason=reason,
                          fallback='original_query')
                    return ''
                if reason != 'stop':
                    raise ProviderStreamError('UNKNOWN', reason)
                if not content.strip():
                    event('query.rewrite_result', status='EMPTY', finish_reason=reason,
                          fallback='original_query')
                    return ''
                event('query.rewrite_result', status='COMPLETED', finish_reason=reason)
                event("query.rewritten", query=snapshot(content) if enabled() else None)
                return content.strip()
        except Exception as e:
            logger.warning("Rewrite query failed: %s", type(e).__name__)
            raise e

    @observe("llm.generate_stream")
    async def generate_stream(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None,
        model_id: str = None,
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        """
        流式生成回答
        """
        client = self._get_client()
        target_model = model_id or self.model

        # 构造消息列表
        request_messages = []
        if system_prompt:
            request_messages.append({"role": "system", "content": system_prompt})
        
        request_messages.extend(messages)
        if enabled():
            event("llm.request", prompt=snapshot(request_messages), model=target_model, max_tokens=None)

        try:
            with provider_attempt("answer_stream", target_model, stream=True) as attempt:
                print(f"DEBUG_LLM: Sending request to {target_model}")
                # print(f"DEBUG_LLM: Messages structure: {request_messages}") # 调试用，注意日志脱敏

                response = await client.chat.completions.create(
                    model=target_model,
                    messages=request_messages,
                    stream=True,
                    temperature=temperature,
                )

                attempt["stream_started"] = True
                attempt["provider_request_id"] = _provider_id(response)
                answer_chars = 0
                finish_reason = None
                async for chunk in response:
                    if chunk.choices and getattr(chunk.choices[0], 'finish_reason', None):
                        finish_reason = chunk.choices[0].finish_reason
                    if chunk.choices and chunk.choices[0].delta.content:
                        answer_chars += len(chunk.choices[0].delta.content)
                        yield chunk.choices[0].delta.content
                event("llm.stream_result", model=target_model, answer_chars=answer_chars, empty=answer_chars == 0, truncated=finish_reason == "length", finish_reason=finish_reason, tokens=None, cost=None)
                if finish_reason == "length":
                    raise ProviderStreamError("TRUNCATED", finish_reason)
                if finish_reason is None:
                    raise ProviderStreamError("UNKNOWN", finish_reason)

        except Exception as e:
            logger.warning("LLM generation failed: %s", type(e).__name__)
            raise e

    @observe("llm.generate")
    async def generate(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None,
        model_id: str = None,
    ) -> str:
        """非流式生成回答"""
        client = self._get_client()
        target_model = model_id or self.model
        request_messages = []

        if system_prompt:
            request_messages.append({"role": "system", "content": system_prompt})

        request_messages.extend(messages)
        if enabled():
            event("llm.request", prompt=snapshot(request_messages), model=target_model, max_tokens=settings.LLM_ANSWER_MAX_TOKENS)

        try:
            with provider_attempt("answer", target_model) as attempt:
                response = await client.chat.completions.create(
                    model=target_model,
                    messages=request_messages,
                    max_tokens=settings.LLM_ANSWER_MAX_TOKENS,
                    temperature=0.7,
                )
                attempt["provider_request_id"] = _provider_id(response)
                model_response(response, model=target_model)
                event("llm.answer", empty=not bool(response.choices[0].message.content), answer=snapshot(response.choices[0].message.content) if enabled() else None)
                return response.choices[0].message.content
        except Exception as e:
             logger.warning("LLM generation failed: %s", type(e).__name__)
             raise e

    def get_chat_model(self, temperature: float = 0.5, streaming: bool = True, max_tokens: int = None) -> ChatOpenAI:
        """获取 LangChain ChatOpenAI 实例"""
        return ChatOpenAI(
            model=self.model,
            openai_api_key=self.api_key,
            openai_api_base=self.base_url,
            temperature=temperature,
            streaming=streaming,
            max_retries=0,
            max_tokens=max_tokens
        )

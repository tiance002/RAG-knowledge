from app.services.observability import observe, event, snapshot, enabled, model_response, capture, span, observe_model_history
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
import json
import time
import uuid
from contextlib import contextmanager, nullcontext
from types import SimpleNamespace
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
def _provider_attempt_recorded(stage: str, model: str, *, stream: bool = False):
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
        event("provider.request_finished", status=status, duration_ms=(time.monotonic()-started)*1000, error_type=type(exc).__name__, finish_reason=getattr(exc, "finish_reason", None), **{k: v for k, v in attempt.items() if k != "stream_started"})
        record_provider_attempt(stage, status, time.monotonic() - started, streaming=stream, error=exc)
        raise
    else:
        event("provider.request_finished", status="RETURNED", duration_ms=(time.monotonic()-started)*1000, **{k: v for k, v in attempt.items() if k != "stream_started"})
        record_provider_attempt(stage, "RETURNED", time.monotonic() - started, streaming=stream)


@contextmanager
def provider_attempt(stage: str, model: str, *, stream: bool = False):
    """Time the real invocation; response events are not its latency measurement."""
    with span('provider.call', model=_safe_model(model), stage=stage, streaming=stream):
        with _provider_attempt_recorded(stage, model, stream=stream) as attempt:
            yield attempt


def _update_generation(handle, **values):
    if handle is not None:
        try:
            handle.update(**values)
        except Exception:
            pass


def _response_usage(response):
    usage = getattr(response, 'usage', None) or getattr(response, 'usage_metadata', None)
    if hasattr(usage, 'model_dump'):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return None
    # Langfuse usage_details uses input/output/total; values remain provider-reported.
    result = {}
    for dest, keys in {'input': ('input_tokens', 'prompt_tokens'),
                       'output': ('output_tokens', 'completion_tokens'),
                       'total': ('total_tokens',)}.items():
        for key in keys:
            value = usage.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                result[dest] = value
                break
    return result or None


def _completion_status(finish_reason):
    return 'TRUNCATED' if finish_reason == 'length' else 'UNKNOWN' if finish_reason is None else 'RETURNED'


@contextmanager
def provider_generation(stage, model, messages, *, stream=False, **parameters):
    """One E1.2 generation surrounds one direct provider request; E1.1 is unchanged."""
    from app.services.observability import native_enabled, generation
    if not native_enabled():
        with provider_attempt(stage, model, stream=stream) as attempt:
            yield attempt, None
        return
    name = 'query.rewrite' if stage == 'rewrite' else 'rerank.chat' if stage in ('rerank_chat', 'rerank_connection_test') else 'answer.generate'
    with generation(name, model=_safe_model(model),
                    input=capture(messages, content_role='provider_request'),
                    stage=stage, streaming=stream, **parameters) as observation:
        try:
            with _provider_attempt_recorded(stage, model, stream=stream) as attempt:
                yield attempt, observation
        except BaseException as exc:
            status = getattr(exc, 'provider_status', None) or _provider_status(exc)
            if isinstance(exc, (GeneratorExit, KeyboardInterrupt, SystemExit)) or type(exc).__name__ == 'CancelledError':
                status = 'CANCELLED'
            _update_generation(observation, execution_status=status, finish_reason=getattr(exc, 'finish_reason', None))
            raise


def _visible_provider_content(content):
    # Provider reasoning blocks are not telemetry, including in local snapshots.
    if isinstance(content, list):
        return [part for part in content if isinstance(part, dict)
                and part.get('type') in ('text', 'image', 'image_url')]
    return content


def _langchain_messages(messages):
    """Safe projection; don't claim this is the provider's wire serialization."""
    rows = []
    for message in messages:
        row = {'role': getattr(message, 'type', None), 'content': _visible_provider_content(getattr(message, 'content', None))}
        if getattr(message, 'name', None):
            row['name'] = message.name
        if getattr(message, 'tool_call_id', None):
            row['tool_call_id'] = message.tool_call_id
        if getattr(message, 'tool_calls', None):
            row['tool_calls'] = message.tool_calls
        rows.append(row)
    return rows


async def invoke_provider_http_async(send, *, stage, model_name):
    with provider_attempt(stage, model_name) as attempt:
        response = await send()
        attempt["provider_request_id"] = _provider_id(response)
        response.raise_for_status()
        return response


def invoke_provider_http(send, *, stage, model_name, check_status=True, messages=None, model_parameters=None):
    from app.services.observability import native_enabled
    if not native_enabled() or messages is None:
        with provider_attempt(stage, model_name) as attempt:
            response = send()
            attempt["provider_request_id"] = _provider_id(response)
            if check_status:
                response.raise_for_status()
            return response
    with provider_generation(stage, model_name, messages, **(model_parameters or {})) as (attempt, observation):
        response = send()
        attempt["provider_request_id"] = _provider_id(response)
        if check_status:
            response.raise_for_status()
        if observation is not None:
            # Observability parsing is fail-open and never modifies HTTP response.
            try:
                choices = getattr(response, 'choices', None)
                if choices is not None:
                    choice = choices[0] if choices else None
                    message = getattr(choice, 'message', None)
                    text = getattr(message, 'content', None)
                    finish = getattr(choice, 'finish_reason', None)
                    usage = _response_usage(response)
                else:
                    data = response.json()
                    if isinstance(data, str):
                        data = json.loads(data)
                    choice = (data.get('choices') or [{}])[0]
                    text = (choice.get('message') or {}).get('content')
                    usage = _response_usage(SimpleNamespace(usage=data.get('usage')))
                    finish = choice.get('finish_reason')
                code = getattr(response, 'status_code', None)
                status = 'FAILED' if isinstance(code, int) and not 200 <= code < 300 else _completion_status(finish)
                _update_generation(observation, output=capture(text, content_role='provider_response') if text is not None else None,
                                   usage=usage, finish_reason=finish, execution_status=status)
            except Exception:
                _update_generation(observation, execution_status='UNKNOWN')
        return response


async def invoke_provider_model(model, messages, *, config, stage, model_name):
    from app.services.observability import native_enabled, generation, langchain_callback_config
    if not native_enabled():
        with provider_attempt(stage, model_name) as attempt:
            response = await model.ainvoke(messages, config=config)
            attempt["provider_request_id"] = _provider_id(response)
            from app.services.metrics import record_provider_usage
            record_provider_usage(stage, getattr(response, "usage_metadata", None))
            return response
    invocation_config, callback_enabled, reason = langchain_callback_config(config, messages=messages)
    # Official callbacks own compatible calls. When privacy cannot be guaranteed,
    # capture a bounded projection around the single real call instead.
    manual = nullcontext(None) if callback_enabled else generation(
        'agent.' + stage, model=_safe_model(model_name),
        input=capture(_langchain_messages(messages), content_role='provider_request'),
        stage=stage, capture_semantics='provider_message_projection',
        reason_code=reason, is_exact_provider_payload=False)
    with manual as observation:
        try:
            with _provider_attempt_recorded(stage, model_name) as attempt:
                response = await model.ainvoke(messages, config=invocation_config)
                attempt["provider_request_id"] = _provider_id(response)
                from app.services.metrics import record_provider_usage
                record_provider_usage(stage, getattr(response, "usage_metadata", None))
                response_metadata = getattr(response, 'response_metadata', {}) or {}
                _update_generation(observation,
                    output=capture(_visible_provider_content(getattr(response, 'content', None)), content_role='provider_response'),
                    usage=_response_usage(response), finish_reason=response_metadata.get('finish_reason'),
                    execution_status=_completion_status(response_metadata.get('finish_reason')))
                return response
        except BaseException as exc:
            status = getattr(exc, 'provider_status', None) or _provider_status(exc)
            if type(exc).__name__ == 'CancelledError' or isinstance(exc, GeneratorExit):
                status = 'CANCELLED'
            _update_generation(observation, execution_status=status)
            raise


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
            request_messages = [{"role": "system", "content": system_prompt},
                                {"role": "user", "content": query}]
            with provider_generation("rewrite", self.model, request_messages, max_tokens=200, temperature=0.3) as (attempt, observation):
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=request_messages,
                    max_tokens=200,
                    temperature=0.3,
                )
                attempt["provider_request_id"] = _provider_id(response)
                model_response(response, role="rewrite", model=self.model)
                choice = response.choices[0]
                reason = getattr(choice, 'finish_reason', None)
                content = getattr(choice.message, 'content', None) or ''
                if observation is not None:
                    _update_generation(observation, output=capture(content, content_role='provider_response'),
                                       usage=_response_usage(response), finish_reason=reason, execution_status=_completion_status(reason))
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
        observe_model_history(request_messages)
        if enabled():
            event("llm.request", input=capture(request_messages, content_role='provider_request'), prompt=snapshot(request_messages), model=target_model, max_tokens=None, temperature=temperature)

        try:
            with provider_generation("answer_stream", target_model, request_messages, stream=True, temperature=temperature) as (attempt, observation):
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
                observed_parts = []
                finish_reason = None
                stream_usage = None
                try:
                    async for chunk in response:
                        if observation is not None:
                            stream_usage = _response_usage(chunk) or stream_usage
                        if chunk.choices and getattr(chunk.choices[0], 'finish_reason', None):
                            finish_reason = chunk.choices[0].finish_reason
                        if chunk.choices and chunk.choices[0].delta.content:
                            answer_chars += len(chunk.choices[0].delta.content)
                            if enabled():
                                observed_parts.append(chunk.choices[0].delta.content)
                            yield chunk.choices[0].delta.content
                finally:
                    if observation is not None:
                        _update_generation(observation, output=capture(''.join(observed_parts), content_role='provider_response'),
                                           finish_reason=finish_reason, usage=stream_usage)
                event("llm.stream_result", model=target_model, answer_chars=answer_chars, empty=answer_chars == 0, truncated=finish_reason == "length", finish_reason=finish_reason, tokens=None, cost=None)
                if enabled():
                    event('llm.visible_response', output=capture(''.join(observed_parts), content_role='provider_response'),
                          model=target_model, finish_reason=finish_reason)
                if observation is not None:
                    _update_generation(observation, finish_reason=finish_reason,
                                       execution_status='TRUNCATED' if finish_reason == 'length' else 'UNKNOWN' if finish_reason is None else 'RETURNED')
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
        observe_model_history(request_messages)
        if enabled():
            event("llm.request", input=capture(request_messages, content_role='provider_request'), prompt=snapshot(request_messages), model=target_model, max_tokens=settings.LLM_ANSWER_MAX_TOKENS, temperature=0.7)

        try:
            with provider_generation("answer", target_model, request_messages, temperature=0.7, max_tokens=settings.LLM_ANSWER_MAX_TOKENS) as (attempt, observation):
                response = await client.chat.completions.create(
                    model=target_model,
                    messages=request_messages,
                    max_tokens=settings.LLM_ANSWER_MAX_TOKENS,
                    temperature=0.7,
                )
                attempt["provider_request_id"] = _provider_id(response)
                model_response(response, model=target_model)
                if observation is not None:
                    _update_generation(observation, output=capture(response.choices[0].message.content or '', content_role='provider_response'),
                                       usage=_response_usage(response), finish_reason=getattr(response.choices[0], 'finish_reason', None),
                                       execution_status=_completion_status(getattr(response.choices[0], 'finish_reason', None)))
                event('llm.visible_response', output=capture(response.choices[0].message.content or '', content_role='provider_response'), model=target_model)
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

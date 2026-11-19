from app.services.observability import observe, event, snapshot, enabled, model_response, capture, span, observe_model_history, trace_response_usage
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
import os
import json
import time
import uuid
import asyncio
from urllib.parse import urlsplit
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager, nullcontext, asynccontextmanager, aclosing
from functools import wraps
from contextvars import ContextVar
from types import SimpleNamespace
from openai import APIStatusError

from app.core.config import get_settings
from app.services.provider_http import ProviderBorrowingTransport, _await_cleanup

settings = get_settings()
logger = logging.getLogger(__name__)
_owned_llm_services = ContextVar("owned_llm_services", default=None)


ANSWER_RETRY_DELAYS = (0.5, 1.0)


def _transient_answer_error(exc):
    if isinstance(exc, (APIStatusError, httpx.HTTPStatusError, requests.exceptions.HTTPError)):
        code = getattr(exc, 'status_code', None) or getattr(exc.response, 'status_code', None)
        return code in (408, 429, 500, 502, 503, 504)
    return (isinstance(exc, (APIConnectionError, TimeoutError, httpx.TimeoutException,
                             httpx.NetworkError, httpx.RemoteProtocolError, requests.exceptions.Timeout,
                             requests.exceptions.ConnectionError))
            or isinstance(exc, ProviderStreamError) and exc.provider_status == 'UNKNOWN')


async def retry_answer_call(call, *, stage):
    for attempt in range(len(ANSWER_RETRY_DELAYS) + 1):
        try:
            return await call()
        except Exception as exc:
            if attempt == len(ANSWER_RETRY_DELAYS) or not _transient_answer_error(exc):
                raise
            event('llm.answer_retry', stage=stage, retry_number=attempt + 1, error_type=type(exc).__name__)
            await asyncio.sleep(ANSWER_RETRY_DELAYS[attempt])


def _retry_answer_stream(stream):
    @wraps(stream)
    async def wrapped(*args, **kwargs):
        stage = kwargs.get('stage', 'answer_stream')
        emitted = False
        for attempt in range(len(ANSWER_RETRY_DELAYS) + 1):
            try:
                async with aclosing(stream(*args, **kwargs)) as response:
                    async for chunk in response:
                        emitted = True
                        yield chunk
                return
            except Exception as exc:
                if emitted or attempt == len(ANSWER_RETRY_DELAYS) or not _transient_answer_error(exc):
                    raise
                event('llm.answer_retry', stage=stage, retry_number=attempt + 1, error_type=type(exc).__name__)
                await asyncio.sleep(ANSWER_RETRY_DELAYS[attempt])
    return wrapped


async def close_quietly(close):
    """Finish cleanup under cancellation without replacing a request with a close error."""
    async def run_close():
        try:
            result = close()
            if hasattr(result, "__await__"):
                await result
        except Exception as exc:
            logger.warning("Resource close failed: %s", type(exc).__name__)
    await _await_cleanup(asyncio.create_task(run_close()))


@asynccontextmanager
async def safely_closing(resource):
    try:
        yield resource
    finally:
        close = getattr(resource, "aclose", None)
        if close is not None:
            await close_quietly(close)


class GenerationInputBudgetExceeded(ValueError):
    """The final application request was rejected before generation dispatch."""
    code = "FINAL_REQUEST_OVER_CHAR_BUDGET"

    def __init__(self, request_chars: int, limit_chars: int):
        super().__init__(self.code)
        self.request_chars = request_chars
        self.limit_chars = limit_chars


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
    # Usage is attached only from a provider response, never estimated.
    attempt["usage_status"] = "UNKNOWN"
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



def _numeric_usage(usage):
    """Normalize only actual nonnegative provider counters; never fill gaps."""
    if hasattr(usage, 'model_dump'):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return {}, []
    result, invalid = {}, []
    aliases = {'input_tokens': ('input_tokens', 'prompt_tokens'),
               'output_tokens': ('output_tokens', 'completion_tokens'),
               'total_tokens': ('total_tokens',)}
    for canonical, names in aliases.items():
        values = [usage[name] for name in names if name in usage and usage[name] is not None]
        if not values:
            continue
        if any(type(value) is not int or value < 0 for value in values) or len(set(values)) != 1:
            invalid.append(canonical)
            continue
        result[canonical] = values[0]
    return result, invalid


def attach_provider_usage(attempt, response=None, *, raw_usage=None):
    """Add numeric provider usage to the existing attempt, fail open.

    HTTP JSON is read only when the response body was already consumed. Raw
    text, credentials, nested metadata and inferred token values are excluded.
    """
    try:
        if raw_usage is None and response is not None:
            if isinstance(response, httpx.Response):
                if not response.is_stream_consumed:
                    return
                body = response.json()
                raw_usage = body.get('usage') if isinstance(body, dict) else None
            elif isinstance(response, requests.Response):
                if response._content is False or response._content is None:
                    return
                body = response.json()
                raw_usage = body.get('usage') if isinstance(body, dict) else None
            else:
                raw_usage, status = trace_response_usage(response)
                if status != 'PROVIDER_REPORTED':
                    return
        numeric, invalid = _numeric_usage(raw_usage)
        current = dict(attempt.get('usage') or {})
        invalid_fields = set(attempt.get('usage_invalid_fields') or ()) | set(invalid)
        for key in invalid_fields:
            current.pop(key, None)
        for key, value in numeric.items():
            if key in invalid_fields:
                continue
            if key in current and current[key] != value:
                invalid_fields.add(key)
                current.pop(key, None)
            else:
                current[key] = value
        if current:
            attempt['usage'] = current
            attempt['usage_status'] = 'PROVIDER_REPORTED'
        else:
            attempt.pop('usage', None)
            attempt['usage_status'] = 'UNKNOWN'
        if invalid_fields:
            attempt['usage_invalid_fields'] = sorted(invalid_fields)
    except Exception:
        pass


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


async def invoke_provider_http_async(send, *, stage, model_name, check_status=True, messages=None, model_parameters=None):
    from app.services.observability import native_enabled
    context = (provider_generation(stage, model_name, messages, **(model_parameters or {}))
               if native_enabled() and messages is not None else None)
    if context is None:
        with provider_attempt(stage, model_name) as attempt:
            response = await send()
            attempt["provider_request_id"] = _provider_id(response)
            attach_provider_usage(attempt, response)
            from app.services.task_effect_context import record_provider_id
            record_provider_id(attempt["provider_request_id"], getattr(response, "status_code", None))
            if check_status:
                response.raise_for_status()
            return response
    with context as (attempt, observation):
        response = await send()
        attempt["provider_request_id"] = _provider_id(response)
        attach_provider_usage(attempt, response)
        from app.services.task_effect_context import record_provider_id
        record_provider_id(attempt["provider_request_id"], getattr(response, "status_code", None))
        if check_status:
            response.raise_for_status()
        if observation is not None:
            try:
                choices = getattr(response, 'choices', None)
                if choices is not None:
                    choice = choices[0] if choices else None
                    text = getattr(getattr(choice, 'message', None), 'content', None)
                    finish = getattr(choice, 'finish_reason', None)
                    usage = _response_usage(response)
                else:
                    data = response.json()
                    if isinstance(data, str):
                        data = json.loads(data)
                    choice = (data.get('choices') or [{}])[0]
                    text = (choice.get('message') or {}).get('content')
                    finish = choice.get('finish_reason')
                    usage = _response_usage(SimpleNamespace(usage=data.get('usage')))
                code = getattr(response, 'status_code', None)
                status = 'FAILED' if isinstance(code, int) and not 200 <= code < 300 else _completion_status(finish)
                _update_generation(observation, output=capture(text, content_role='provider_response') if text is not None else None,
                                   usage=usage, finish_reason=finish, execution_status=status)
            except Exception:
                _update_generation(observation, execution_status='UNKNOWN')
        return response


def invoke_provider_http(send, *, stage, model_name, check_status=True, messages=None, model_parameters=None):
    from app.services.observability import native_enabled
    if not native_enabled() or messages is None:
        with provider_attempt(stage, model_name) as attempt:
            response = send()
            attempt["provider_request_id"] = _provider_id(response)
            attach_provider_usage(attempt, response)
            if check_status:
                response.raise_for_status()
            return response
    with provider_generation(stage, model_name, messages, **(model_parameters or {})) as (attempt, observation):
        response = send()
        attempt["provider_request_id"] = _provider_id(response)
        attach_provider_usage(attempt, response)
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


async def invoke_provider_model(model, messages, *, config, stage, model_name, timeout_seconds=None):
    call = lambda: _invoke_provider_model_once(model, messages, config=config, stage=stage,
                                               model_name=model_name, timeout_seconds=timeout_seconds)
    if stage in ('agent', 'agent_chat'):
        return await retry_answer_call(call, stage=stage)
    return await call()


async def _invoke_provider_model_once(model, messages, *, config, stage, model_name, timeout_seconds=None):
    from app.services.observability import native_enabled, generation, langchain_callback_config

    async def invoke_once(invocation_config):
        call = model.ainvoke(messages, config=invocation_config)
        if timeout_seconds is None:
            return await call
        # Keep the deadline inside provider_attempt so an expired request is
        # recorded as UNKNOWN, rather than a successful call or a retry trigger.
        return await asyncio.wait_for(call, timeout=timeout_seconds)

    if not native_enabled():
        with provider_attempt(stage, model_name) as attempt:
            response = await invoke_once(config)
            attempt["provider_request_id"] = _provider_id(response)
            attach_provider_usage(attempt, response)
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
                response = await invoke_once(invocation_config)
                attempt["provider_request_id"] = _provider_id(response)
                attach_provider_usage(attempt, response)
                from app.services.metrics import record_provider_usage
                record_provider_usage(stage, getattr(response, "usage_metadata", None))
                response_metadata = getattr(response, 'response_metadata', {}) or {}
                trace_usage, usage_status = trace_response_usage(response)
                _update_generation(observation,
                    output=capture(_visible_provider_content(getattr(response, 'content', None)), content_role='provider_response'),
                    usage=_response_usage(SimpleNamespace(usage=trace_usage)), usage_status=usage_status,
                    finish_reason=response_metadata.get('finish_reason'),
                    execution_status=_completion_status(response_metadata.get('finish_reason')))
                return response
        except BaseException as exc:
            status = getattr(exc, 'provider_status', None) or _provider_status(exc)
            if type(exc).__name__ == 'CancelledError' or isinstance(exc, GeneratorExit):
                status = 'CANCELLED'
            _update_generation(observation, execution_status=status)
            raise


def provider_error_payload(exc: BaseException, *, stage: str, unknown: bool = False) -> dict:
    if isinstance(exc, GenerationInputBudgetExceeded):
        return {"error": "生成请求输入超过长度限制，本次生成请求未发送；请减少历史轮次或缩小检索范围后再试。",
                "status": "FAILED", "code": exc.code, "stage": "generation_input_budget",
                "generation_request_sent": False, "retryable": False,
                "request_chars": exc.request_chars, "limit_chars": exc.limit_chars}
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
        self._langchain_http_clients = []
        self._closed = False
        self._close_task = None
        owner = _owned_llm_services.get()
        if owner is not None:
            owner.append(self)

    async def _close(self):
        if self._client is not None:
            close = getattr(self._client, "close", None)
            if close is not None:
                await close_quietly(close)
        for client in self._langchain_http_clients:
            await close_quietly(client.aclose)

    async def aclose(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await _await_cleanup(self._close_task)

    def _uses_siliconflow_qwen_parameters(self, target_model=None):
        return ((target_model or self.model) == 'Qwen/Qwen3.5-4B'
                and urlsplit(self.base_url).hostname in
                ('api.siliconflow.cn', 'api.siliconflow.com'))

    def provider_request_parameters(self, target_model=None, *, extra_body=None):
        """Keep provider-specific defaults shared by direct and LangChain calls."""
        if extra_body is None:
            extra_body = getattr(self, '_router_extra_body', None)
        options = dict(extra_body or {})
        if self._uses_siliconflow_qwen_parameters(target_model):
            options.setdefault('enable_thinking', False)
        return {'extra_body': options} if options else {}

    def _get_client(self):
        if self._closed:
            raise RuntimeError("LLMService is closed")
        if not self._client:
            self._client = AsyncOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                timeout=120.0,
                max_retries=0,
                http_client=httpx.AsyncClient(
                    trust_env=False,
                    transport=ProviderBorrowingTransport(base_url=self.base_url, api_key=self.api_key, stage="llm", trust_env=False),
                )
            )
        return self._client

    @observe("llm.rewrite_query")
    async def understand_query(self, query: str, history: List[Dict[str, str]] = None,
                               images=None) -> dict:
        """Run WeKnora's default query understanding prompt in a single call."""
        # Use original conversational turns as data, never saved output preferences
        # or system instructions. Keep the same history bound as chat/Agent.
        from app.services.conversation_history import bound_rewrite_history
        from app.services.runtime_settings import get_runtime_settings
        rounds = getattr(self, 'history_rounds', None)
        if rounds is None:
            rounds = get_runtime_settings().max_chat_history_rounds
        conversation_history = bound_rewrite_history(history, rounds)

        prompt_dir = Path(__file__).resolve().parent.parent / 'prompts'
        now = datetime.now().astimezone()
        query_content = query + (f'\n\n<images_uploaded count="{len(images)}" />'
                                 if images else '\n\n<no_image_attached />')
        query_content += '\n<no_document_attached />'
        values = {'conversation': json.dumps(conversation_history, ensure_ascii=False),
                  'query': query_content, 'language': 'the original request language',
                  'current_time': now.isoformat(timespec='seconds'), 'current_week': now.strftime('%A')}
        def render(name):
            result = (prompt_dir / name).read_text(encoding='utf-8')
            for key, value in values.items():
                result = result.replace('{{' + key + '}}', value)
            return result
        user_content = render('weknora_rewrite_user.txt')
        if images:
            user_content = [{'type': 'text', 'text': user_content}] + [
                {'type': 'image_url', 'image_url': {'url': url}} for url in images]
        client = self._get_client()
        max_tokens = 2048 if images else 200
        try:
            request_messages = [{"role": "system", "content": render('weknora_rewrite_system.txt')},
                                {"role": "user", "content": user_content}]
            trace_messages = request_messages if not images else [
                request_messages[0], {'role': 'user', 'content': [
                    {'type': 'text', 'text': render('weknora_rewrite_user.txt')},
                    {'type': 'image', 'count': len(images), 'content': '[REDACTED]'}]}]
            with provider_generation("rewrite", self.model, trace_messages, max_tokens=max_tokens, temperature=0.3) as (attempt, observation):
                from app.services.conversation_context import record_provider_request
                record_provider_request(request_messages, 'query_understanding')
                response = await client.chat.completions.create(
                    model=self.model,
                    messages=request_messages,
                    max_tokens=max_tokens,
                    temperature=0.3,
                    **self.provider_request_parameters(self.model),
                )
                attempt["provider_request_id"] = _provider_id(response)
                attach_provider_usage(attempt, response)
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
                    return {'rewrite_query': '', 'intent': '', 'image_description': ''}
                if reason != 'stop':
                    raise ProviderStreamError('UNKNOWN', reason)
                if not content.strip():
                    event('query.rewrite_result', status='EMPTY', finish_reason=reason,
                          fallback='original_query')
                    return {'rewrite_query': '', 'intent': '', 'image_description': ''}
                cleaned = content.strip()
                if cleaned.startswith('```'):
                    cleaned = cleaned.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
                if cleaned.startswith('{'):
                    try:
                        data = json.loads(cleaned)
                    except (ValueError, TypeError):
                        event('query.rewrite_result', status='INVALID_OUTPUT', fallback='original_query')
                        return {'rewrite_query': '', 'intent': '', 'image_description': ''}
                    candidate = data.get('rewrite_query') if isinstance(data, dict) else None
                    if not isinstance(candidate, str) or not candidate.strip():
                        event('query.rewrite_result', status='INVALID_OUTPUT', fallback='original_query')
                        return {'rewrite_query': '', 'intent': '', 'image_description': ''}
                    content = candidate.strip()
                    intent = data.get('intent') if isinstance(data.get('intent'), str) else ''
                    image_description = data.get('image_description') if isinstance(data.get('image_description'), str) else ''
                else:
                    intent, image_description = '', ''
                event('query.rewrite_result', status='COMPLETED', finish_reason=reason)
                event("query.rewritten", query=snapshot(content) if enabled() else None)
                from app.services.conversation_context import record_understanding
                record_understanding({'rewrite_query': content.strip(), 'intent': intent.strip(),
                                      'image_description': image_description.strip(), 'status': 'COMPLETED'})
                return {'rewrite_query': content.strip(), 'intent': intent.strip(),
                        'image_description': image_description.strip()}
        except Exception as e:
            logger.warning("Rewrite query failed: %s", type(e).__name__)
            raise e

    async def rewrite_query(self, query: str, history: List[Dict[str, str]] = None) -> str:
        """Backward-compatible rewrite-only API."""
        result = await self.understand_query(query, history=history)
        return result['rewrite_query']

    @observe("llm.generate_stream")
    @_retry_answer_stream
    async def generate_stream(
        self,
        messages: List[Dict[str, str]],
        system_prompt: str = None,
        model_id: str = None,
        temperature: float = 0.7,
        *, max_tokens: int | None = None, stage: str = "answer_stream", on_final=None,
        generation_metadata: dict | None = None,
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
            event("llm.request", input=capture(request_messages, content_role='provider_request'), prompt=snapshot(request_messages), model=target_model, max_tokens=max_tokens, temperature=temperature)

        try:
            generation_parameters = {} if max_tokens is None else {"max_tokens": max_tokens}
            provider_parameters = self.provider_request_parameters(target_model)
            with provider_generation(stage, target_model, request_messages, stream=True, temperature=temperature, **generation_parameters) as (attempt, observation):
                if generation_metadata:
                    _update_generation(observation, **generation_metadata)
                print(f"DEBUG_LLM: Sending request to {target_model}")
                from app.services.conversation_context import record_provider_request
                record_provider_request(request_messages, stage)
                # print(f"DEBUG_LLM: Messages structure: {request_messages}") # 调试用，注意日志脱敏

                response = await client.chat.completions.create(
                    model=target_model,
                    messages=request_messages,
                    stream=True,
                    temperature=temperature,
                    **generation_parameters,
                    **provider_parameters,
                )

                attempt["stream_started"] = True
                attempt["provider_request_id"] = _provider_id(response)
                answer_chars = 0
                observed_parts = []
                finish_reason = None
                stream_usage = None
                raw_usage = None
                provider_model = None
                try:
                    async for chunk in response:
                        actual_model = getattr(chunk, 'model', None)
                        if provider_model is None and isinstance(actual_model, str) and actual_model.strip():
                            provider_model = actual_model
                        usage = getattr(chunk, 'usage', None)
                        if usage is not None:
                            attach_provider_usage(attempt, raw_usage=usage)
                            if on_final is not None:
                                raw_usage = usage.model_dump() if hasattr(usage, 'model_dump') else usage
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
                    if on_final is not None:
                        try:
                            on_final({'finish_reason': finish_reason, 'usage': raw_usage, 'model': provider_model})
                        except Exception:
                            pass
                    if getattr(self, '_router_direct_history', False):
                        identity = 'UNKNOWN' if provider_model is None else 'MATCH' if provider_model == target_model else 'MISMATCH'
                        event('router.provider_identity', stage='direct_history', evidence_scope='HISTORY_ONLY',
                              requested_model_id=target_model, provider_model_id=provider_model, status=identity)
                    close = getattr(response, "close", None)
                    if close is not None:
                        await close_quietly(close)
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
        return await retry_answer_call(lambda: self._generate_once(messages, system_prompt, model_id), stage='answer')

    async def _generate_once(
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
                    **self.provider_request_parameters(target_model),
                )
                attempt["provider_request_id"] = _provider_id(response)
                attach_provider_usage(attempt, response)
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

    def get_chat_model(self, temperature: float = 0.5, streaming: bool = True, max_tokens: int = None,
                       *, timeout: float | None = None) -> ChatOpenAI:
        """获取 LangChain ChatOpenAI 实例"""
        if self._closed:
            raise RuntimeError("LLMService is closed")
        options = {} if timeout is None else {"timeout": timeout}
        options.update(self.provider_request_parameters(self.model))
        # LangChain owns proxy configuration and rejects a custom async client
        # alongside OPENAI_PROXY. Preserve its existing proxy path in that case.
        if not os.environ.get("OPENAI_PROXY"):
            http_options = {} if timeout is None else {"timeout": timeout}
            http_client = httpx.AsyncClient(
                follow_redirects=True, trust_env=True, **http_options,
                transport=ProviderBorrowingTransport(
                    base_url=self.base_url, api_key=self.api_key, stage="llm",
                    trust_env=True))
            self._langchain_http_clients.append(http_client)
            options["http_async_client"] = http_client
        return ChatOpenAI(
            model=self.model,
            openai_api_key=self.api_key,
            openai_api_base=self.base_url,
            temperature=temperature,
            streaming=streaming,
            max_retries=0,
            max_tokens=max_tokens,
            **options,
        )

from app.services.observability import observe, event, snapshot, enabled, model_response, capture, observe_model_history
"""
基于 LangGraph 的 Agent 工作流 (Cyclic Graph)
"""

from typing import TypedDict, List, Annotated, Dict, Any
import operator
import json
import re
import os
import math
import asyncio

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from app.services.llm import LLMService, is_unknown_provider_error, should_abort_provider_error, invoke_provider_model
from app.services.metrics import record_agent_retry_decision
from app.core.config import get_settings
from app.tools import search_knowledge_base
from app.tools.read_knowledge_context import read_knowledge_context
from app.tools.read_tool_result import read_tool_result
from app.services.agent_context_budget import EVIDENCE_TOOLS, consolidate_agent_context, request_chars, history_as_sent
from app.services.agent_source_registry import resolve_source_alias, restore_known_source_aliases
from app.tools.retrieval_tool import retrieval_attempt
import uuid
import hashlib
from contextlib import nullcontext


async def _store_io(operation, *args, **kwargs):
    """Finish a bounded disk operation before cancellation can close its store."""
    pending = asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs))
    try:
        return await asyncio.shield(pending)
    except asyncio.CancelledError:
        try:
            await pending
        finally:
            raise


def _visible_content(content, *, assistant=False):
    """Exclude reasoning blocks even when a provider puts them in content."""
    if isinstance(content, str):
        if assistant:
            content = re.sub(r"<(think|thinking|analysis|reasoning)>.*?(?:</\1>|\Z)", "[REDACTED]", content,
                             flags=re.DOTALL | re.IGNORECASE)
            content = re.sub(r"Thinking:\s*.*?(?:\n\n|\Z)", "[REDACTED]", content,
                             flags=re.DOTALL | re.IGNORECASE)
        return content
    if isinstance(content, list):
        result = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("text", "image_url", "image"):
                part = dict(item)
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    part["text"] = _visible_content(item["text"], assistant=assistant)
                result.append(part)
        return result
    return None


def _observe_model_request(stage, messages, model, provider_model=None):
    from app.services.conversation_context import record_provider_request
    record_provider_request(messages, stage)
    # Telemetry must never change the model call or fail the request.
    try:
        if not enabled():
            return
        rows = []
        for message in messages:
            row = {"role": message.type, "content": _visible_content(
                message.content, assistant=isinstance(message, AIMessage))}
            if isinstance(message, AIMessage) and message.tool_calls:
                row["tool_calls"] = message.tool_calls
            if getattr(message, "name", None):
                row["name"] = message.name
            if isinstance(message, ToolMessage):
                row["tool_call_id"] = message.tool_call_id
            rows.append(row)
        parameters = getattr(provider_model, 'bound', provider_model)
        observe_model_history(rows)
        event("llm.request", role=stage, model=model,
              temperature=getattr(parameters, 'temperature', None), max_tokens=getattr(parameters, 'max_tokens', None),
              **({'extra_body': provider_model.kwargs['extra_body']}
                 if isinstance(getattr(provider_model, 'kwargs', None), dict) and 'extra_body' in provider_model.kwargs else {}),
              input=capture(rows, content_role="provider_request"),
              reasoning_policy="REDACTED", reason_code="provider_message_projection",
              is_exact_provider_payload=False,
              projection_excludes=["reasoning", "provider_specific_fields", "unsupported_content_blocks"])
    except Exception:
        pass


def _observe_model_response(stage, response, model):
    try:
        if enabled():
            event("llm.visible_response", role=stage, model=model,
                  output=capture(_visible_content(response.content, assistant=True),
                                 content_role="provider_response"),
                  reasoning_policy="REDACTED")
    except Exception:
        pass


# 定义状态
class AgentState(TypedDict):
    # 消息历史 (自动管理)
    messages: Annotated[List[BaseMessage], add_messages]
    
    # 业务上下文
    kb_ids: List[str]
    intent: str  # 'chat', 'qa'
    understanding_intent: str | None  # Intent from the optional unified query understanding
    retrieval_query: str | None # Optional once-per-request clarified retrieval query
    has_prior_conversation: bool # Raw inbound history, before round trimming.
    original_query: str # 用户原始问题
    current_plan: str # 当前执行计划
    
    # RAG 相关 (用于前端展示)
    rag_context: str
    retrieval_status: str
    degraded: bool
    fallback_used: bool
    citations: List[dict]
    history_evidence: List[dict]
    retain_retrieval_history: bool
    retrieved_docs: List[Any] # 累计召回文档 (暂存)
    search_params: Dict[str, Any] # 前端传递的默认检索参数
    
    # 流程控制
    retry_count: int
    retrieval_retry_scheduled: bool
    router_escalations_used: int
    retrieval_attempt_id: str
    retrieval_attempt_count: int
    last_score: float | None # None means unavailable, never a relevance score.
    execution_history: List[str] # 执行路径记录
    
    agent_steps: Annotated[List[dict], operator.add] # Append only
    final_system_prompt: str
    preference_policy: str
    answer_mode: str
    fallback_strategy: str
    generation_status: str
    generation_finish_reason: str | None
    
    # LLM 配置 (动态注入)
    model_config: Dict[str, Any]
    source_registry: List[dict]
    model_evidence_source_ids: List[int]
    context_delivery: Dict[str, Any]
    executed_context_reads: List[str]
    executed_tool_result_reads: List[str]
    executed_retrieval_requests: List[str]
    retrieval_blocked: bool
    grading_status: str
    validation_status: str
    grading_scope: Dict[str, Any]
    source_mapping_status: str
    expansion_delivery_status: str
    model_usage_records: Annotated[List[Any], operator.add]


def _source_key(citation):
    metadata = citation.get('source_metadata')
    metadata = metadata if isinstance(metadata, dict) else {}
    # Identity comes only from explicit server-supplied publication/version
    # fields. A missing version never implies another source's known version.
    build = next((value for value in (citation.get('index_build_id'), citation.get('build_id'))
                  if isinstance(value, str) and value), None)
    source_hash = next((value for value in (citation.get('source_sha256'), metadata.get('source_sha256'))
                        if isinstance(value, str) and value), None)
    return (citation.get('fileId') or citation.get('file_id'), citation.get('chunk_id'),
            build, source_hash, citation.get('content'))


def _source_context(citations):
    return '\n\n'.join(f"Document {c['source_id']} (Source: {c.get('fileName', 'Unknown File')}):\n{c['content']}"
                       for c in citations)


def _prepare_history_evidence(state):
    """Register opted-in verified references; the model receives a read index.

    Agent history does not use ordinary chat's lexical top-three reuse policy.
    Every in-scope validated reference remains addressable in this request.
    """
    if (not state.get('retain_retrieval_history', False)
            or state.get('source_registry') or state.get('retrieval_attempt_count')):
        return {}
    allowed = set(state.get('kb_ids') or [])
    candidates = [dict(c) for c in state.get('history_evidence') or []
                  if c.get('verification_status') == 'VERIFIED'
                  and c.get('kb_id') in allowed and c.get('origin') == 'HISTORY_REUSE']
    if not candidates:
        return {}
    registry = []
    data = _register_sources({'citations': candidates, 'retrieval_status': 'SUCCESS',
                              'source_mapping_status': 'VERIFIED',
                              'execution_origin': 'history_evidence_reuse'}, registry)
    if not registry:
        return {}
    ident = 'history_' + uuid.uuid4().hex
    dispatch = AIMessage(content='', tool_calls=[{'name': 'read_knowledge_context',
        'id': ident, 'args': {'source_id': registry[0]['source_id']}}],
        additional_kwargs={'execution_origin': 'history_evidence_reuse'})
    message = ToolMessage(content=json.dumps(data, ensure_ascii=False),
                          name='read_knowledge_context', tool_call_id=ident,
                          additional_kwargs={'historical_retrieval': True})
    event('context.history_reuse', added_count=len(registry), mode='agent_first_turn',
          execution_origin='history_evidence_reuse')
    return {'messages': [dispatch, message], 'source_registry': registry,
            'citations': registry, 'rag_context': data['context'],
            'last_score': None, 'grading_status': 'NOT_RUN',
            'retrieval_status': 'SUCCESS', 'source_mapping_status': 'VERIFIED'}


_SCORE_NUMBER = re.compile(r'(?<![\w.])[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?(?![\w.])')
_SCORE_LABELLED = re.compile(
    r'(?:score|rating|relevance|评分|相关性|得分)\s*[:：=为]?\s*'
    r'(\d+(?:\.\d+)?)\s*(?:/\s*(1(?:\.0+)?|10|100))?',
    re.IGNORECASE,
)
_SCORE_ERROR = re.compile(
    r'\b(?:error|failed|failure|timeout|unavailable|exception|invalid|not\s+a\s+score)\b|错误|失败|故障|超时|不可用',
    re.IGNORECASE,
)
# 分数型写法：0.85/1.0、8.5/10、0.85 out of 1.0
_SCORE_FRACTION = re.compile(
    r'(?<![\w.])(\d+(?:\.\d+)?)\s*(?:/\s*|out\s+of\s*)(1(?:\.0+)?|10|100)(?![\w.])',
    re.IGNORECASE,
)


def _bounded_score(value, scale=1.0):
    """把候选数值换算到 0–1；越界或非数值返回 None。"""
    try:
        number = float(value)
        scale = float(scale) if scale else 1.0
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or scale <= 0:
        return None
    score = number / scale
    return score if 0 <= score <= 1 else None


def _relevance_score(content):
    """读取一个 0–1 的相关性分数，容忍常见包装写法，不臆造分数。

    原实现要求全文恰好一个数字，评分模型回 "0.85/1.0"、"得分 0.85，理由…"
    都会判无效，进而阻断答案生成。这里先看唯一数值，再看显式分数写法。
    """
    if not isinstance(content, str):
        return None
    # 1) 显式标注的分数优先：`score: 0.85`、`得分 0.85`。
    labelled = [s for s in (_bounded_score(*groups) for groups in _SCORE_LABELLED.findall(content))
                if s is not None]
    if len(set(labelled)) == 1:
        return labelled[0]
    # 2) 分数型写法：0.85/1.0、8.5/10、0.85 out of 1.0。
    fractions = {round(s, 6) for s in
                 (_bounded_score(*groups) for groups in _SCORE_FRACTION.findall(content))
                 if s is not None}
    if len(fractions) == 1:
        return fractions.pop()
    # 3) 有错误词而没有显式分数时，不把数字当分数（"timeout code 0.5"、"Error code 0"）。
    if _SCORE_ERROR.search(content):
        return None
    # 4) 最后才接受全文唯一的 0–1 数值。
    bounded = [s for s in (_bounded_score(number) for number in _SCORE_NUMBER.findall(content))
               if s is not None]
    return bounded[0] if len(bounded) == 1 else None


def _grounded_min_score():
    """允许生成答案的最低相关性分数；0.5 对应评分提示里的"部分相关"。"""
    try:
        value = float(os.getenv('RAG_GROUNDED_MIN_SCORE', '0.5'))
    except (TypeError, ValueError):
        return 0.5
    return value if 0.0 <= value <= 1.0 else 0.5


def _register_sources(data, registry):
    """Number the actual tool delivery once for this answer, before model use."""
    selected = []
    image_sources = []
    omitted_images = bool(data.get('omitted_images'))
    for citation in data.get('citations', []):
        if not isinstance(citation, dict):
            continue
        content, has_image = evidence_text(citation)
        omitted_images |= has_image
        if citation.get('content_type') == 'image':
            image_sources.append({**citation, 'content': '', 'evidence_status': 'UNVERIFIED_IMAGE'})
            continue
        if not citation.get('chunk_id') or not content:
            continue
        citation = {**citation, 'content': content}
        description_provenance = generated_image_description_provenance(citation)
        if description_provenance:
            citation.update(description_provenance)
        existing = next((c for c in registry if _source_key(c) == _source_key(citation)), None)
        if existing is None:
            existing = {**citation, 'source_id': len(registry) + 1}
            registry.append(existing)
        if existing not in selected:
            selected.append(existing)
    from app.services.conversation_context import register_request_sources
    register_request_sources(selected)
    return {**data, 'citations': selected + image_sources, 'context': _source_context(selected),
            'source_ids': [c['source_id'] for c in selected], 'omitted_images': omitted_images}

def explicitly_requests_sources(content: str) -> bool:
    # A definition request about the concept does not ask to consult a corpus.
    if re.fullmatch(r"\s*(?:什么是知识库|知识库(?:是)?什么|what\s+is\s+(?:a\s+)?knowledge\s+base)\s*[？?。.!！]*\s*", content, re.IGNORECASE):
        return False
    # A direct mention can express scope without an imperative (e.g. 知识库里的预算).
    explicit_kb = re.search(r"知识库|\bknowledge\s+base\b", content, re.IGNORECASE)
    negated_kb = re.search(
        r"(?:不(?:用|需要|使用|要)|无需|别用)\s*(?:这个|该|所选)?知识库"
        r"|\b(?:don't|do\s+not|no\s+need\s+to)\s+(?:use|search|consult)\s+(?:(?:the|my|our)\s+)?knowledge\s+base\b",
        content, re.IGNORECASE,
    )
    if explicit_kb:
        return not bool(negated_kb)
    chinese = re.search(r"(?:根据|基于|依据|按照|从|在|查看|查阅|阅读|参照|参考|检索|查询|搜索)(?:我(?:的)?|用户)?(?:上传的?|所选的?|选定的?|当前的?|这些|这个|的){0,2}(?:知识库|资料|文档|文件)", content)
    english = re.search(
        r"\b(?:according\s+to|based\s+on|use|using|search|searching|look\s+in|look\s+through|refer\s+to|reference|consult|read|review|check)\s+(?:(?:the|my|our|selected|uploaded|provided)\s+){0,3}(?:knowledge\s+base|documents?|files?|materials?)\b",
        content, re.IGNORECASE,
    )
    return bool(chinese or english)


def evidence_text(citation: dict):
    """Return textual source evidence and whether image material was omitted."""
    content = citation.get("content")
    if not isinstance(content, str):
        return "", False
    from app.services.parent_child import IMAGE_DESCRIPTION_LABEL, IMAGE_DESCRIPTION_ORIGIN
    source = citation.get('source_metadata') or {}
    origins = source.get('content_origins') if isinstance(source, dict) else None
    heading = citation.get('heading_text')
    description_heading = (isinstance(heading, str) and
        (heading == IMAGE_DESCRIPTION_LABEL or heading.startswith(IMAGE_DESCRIPTION_LABEL + ' ')
            or heading.startswith(IMAGE_DESCRIPTION_LABEL + '\n')))
    claimed_description = (isinstance(origins, list) and IMAGE_DESCRIPTION_ORIGIN in origins
        or description_heading
        or citation.get('content_origin') == IMAGE_DESCRIPTION_ORIGIN
        or citation.get('evidence_status') == 'MODEL_GENERATED_IMAGE_DESCRIPTION'
        or content.startswith(IMAGE_DESCRIPTION_LABEL))
    placeholders = re.compile(r"\[图片\s*[:：][^\]]*\]")
    has_image = bool(placeholders.search(content)) or citation.get("content_type") == "image"
    if citation.get("content_type") == "image":
        return "", True
    if claimed_description:
        if generated_image_description_provenance(citation) is None:
            return '', True
        body = content
        if body.startswith(IMAGE_DESCRIPTION_LABEL + '\n'):
            body = body[len(IMAGE_DESCRIPTION_LABEL)+1:]
        return (IMAGE_DESCRIPTION_LABEL + '\n' + body, False) if body.strip() else ('', True)
    return placeholders.sub("", content).strip(), has_image


def generated_image_description_provenance(citation):
    """Recognize manifest-backed derived text; geometry locates the image only."""
    from pathlib import PurePosixPath
    from app.services.parent_child import IMAGE_DESCRIPTION_ORIGIN, IMAGE_DESCRIPTION_DERIVATION, IMAGE_DESCRIPTION_LABEL
    if citation.get('content_type') == 'image':
        return None
    source = citation.get('source_metadata')
    origins = source.get('content_origins') if isinstance(source, dict) else None
    if not isinstance(origins, list) or not origins or any(origin != IMAGE_DESCRIPTION_ORIGIN for origin in origins):
        return None
    page = citation.get('page_number')
    registered = source.get('provenance_schema') == 'registered-image-caption-json-v1'
    digest = source.get('source_sha256')
    if (not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
            or (registered and page is not None)
            or (not registered and (type(page) is not int or page < 1))):
        return None
    descriptions, locators = source.get('image_descriptions'), source.get('source_locators')
    if not isinstance(descriptions, list) or not descriptions or not isinstance(locators, list) or not locators:
        return None
    if any(not isinstance(locator, dict) or locator.get('kind') != 'pdf-image-description' for locator in locators):
        return None
    if not registered and any(locator.get('page') != page or locator.get('source_sha256') != digest for locator in locators):
        return None
    content = citation.get('content')
    if not isinstance(content, str):
        return None
    variants = {hashlib.sha256(content.encode('utf-8')).hexdigest()}
    if content.startswith(IMAGE_DESCRIPTION_LABEL + '\n'):
        variants.add(hashlib.sha256(content[len(IMAGE_DESCRIPTION_LABEL)+1:].encode('utf-8')).hexdigest())
    references = []
    for value in descriptions:
        if not isinstance(value, dict):
            return None
        path = value.get('source_image_path')
        if (value.get('status') != 'COMPLETED' or value.get('derivation') != IMAGE_DESCRIPTION_DERIVATION
                or type(value.get('source_page')) is not int or value['source_page'] < 1
                or (not registered and value['source_page'] != page)
                or value.get('indexed_text_sha256') not in variants
                or not isinstance(value.get('model'), str) or not value['model'].strip()
                or not isinstance(value.get('image_sha256'), str) or not re.fullmatch('[0-9a-f]{64}', value['image_sha256'])
                or value.get('requires_original_verification') is not True
                or not isinstance(path, str) or not path or '\\' in path
                or PurePosixPath(path).is_absolute() or '..' in PurePosixPath(path).parts):
            return None
        if registered:
            original = value.get('original_source_sha256')
            identifiers = ('original_file_id', 'original_build_id', 'original_chunk_id')
            if (not isinstance(original, str) or not re.fullmatch('[0-9a-f]{64}', original)
                    or any(not isinstance(value.get(key), str) or not value[key] for key in identifiers)
                    or type(value.get('original_generation')) is not int or value['original_generation'] < 0):
                return None
            matching = [locator for locator in locators if locator.get('derivation') == IMAGE_DESCRIPTION_DERIVATION
                and locator.get('derived_document_sha256') == digest
                and locator.get('page') == value['source_page']
                and locator.get('image_sha256') == value['image_sha256']
                and locator.get('source_image_path') == path
                and all(locator.get(key) == value.get(key) for key in
                    (*identifiers, 'original_source_sha256', 'original_generation'))]
            if not matching or any(not isinstance(locator.get(key), str) or not re.fullmatch('[0-9a-f]{64}', locator[key])
                for locator in matching for key in ('original_chunk_sha256', 'source_manifest_sha256')):
                return None
        reference = {key: value.get(key) for key in ('source_image_path', 'source_page', 'image_sha256',
            'model', 'prompt_sha256', 'max_tokens', 'effect_key', 'original_source_sha256',
            'original_file_id', 'original_build_id', 'original_generation', 'original_chunk_id')}
        if reference not in references:
            references.append(reference)
    pages = '、'.join(str(number) for number in sorted({value['source_page'] for value in references}))
    return dict(content_origin=IMAGE_DESCRIPTION_ORIGIN, evidence_status='MODEL_GENERATED_IMAGE_DESCRIPTION',
        requires_original_verification=True, source_image_references=references,
        location=f"{'原 PDF' if registered else 'PDF'} 第 {pages} 页（模型生成图片描述，需核对原图）")


def _guard_knowledge_answer(state: AgentState, response: AIMessage):
    """Conservatively render evidence when retrieval is incomplete or weak."""
    statuses = []
    citations = []
    seen = set()
    omitted_images = False
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage) or message.name not in EVIDENCE_TOOLS:
            continue
        try:
            data = json.loads(message.content)
            status = data.get("retrieval_status", "RETRIEVAL_ERROR" if data.get("error") else "UNKNOWN")
            if status not in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE", "RETRIEVAL_ERROR", "UNKNOWN", "PARTIAL"}:
                status = "UNKNOWN"
            statuses.append(status)
            omitted_images |= bool(data.get('omitted_images'))
            for citation in data.get("citations", []):
                if not isinstance(citation, dict):
                    continue
                chunk_id = citation.get("chunk_id")
                content, has_image = evidence_text(citation)
                omitted_images = omitted_images or has_image
                if chunk_id and content and chunk_id not in seen:
                    seen.add(chunk_id)
                    citations.append({**citation, "content": content})
        except (TypeError, ValueError, AttributeError):
            statuses.append("UNKNOWN")

    if state.get('source_registry'):
        citations = [dict(c) for c in state['source_registry']]
        if 'model_evidence_source_ids' in state:
            delivered = set(state['model_evidence_source_ids'])
            citations = [c for c in citations if c.get('source_id') in delivered]
    incomplete = (state.get('retrieval_status') in {'RETRIEVAL_ERROR', 'UNKNOWN', 'PARTIAL'}
                  or any(status in {"RETRIEVAL_ERROR", "UNKNOWN", "PARTIAL"} for status in statuses))
    if not citations:
        if (state.get('context_delivery') or {}).get('omitted_source_ids'):
            answer = "已找到资料，但完整原文超出本次上下文预算；请缩小问题范围后重试。"
        elif "RETRIEVAL_ERROR" in statuses:
            answer = "知识库检索发生错误，无法确认是否存在相关资料，请稍后重试。"
        elif not statuses or incomplete:
            answer = "知识库检索状态不确定，无法确认是否存在相关资料，请稍后重试。"
        elif omitted_images:
            answer = "检索到图片资料，但没有可用于回答的文本证据；图片内容需要进一步核验。"
        elif "LOW_RELEVANCE" in statuses:
            answer = "找到的候选资料相关性不足，无法依据现有证据回答。"
        else:
            answer = "未在所选知识库中找到可用于回答的相关资料。"
        if omitted_images and incomplete:
            answer += " 检索到的图片资料需要进一步核验。"
        response.content = answer
        return []

    if incomplete:
        prefix = "检索未完全成功，以下是已找到的原文，无法确认是否足以完整回答："
    elif "LOW_RELEVANCE" in statuses or (
            state.get("last_score") is not None and state.get("last_score", 0.0) < _grounded_min_score()):
        prefix = "现有证据不足以完整回答，以下是相关原文："
    else:
        prefix = "无法确认以下资料是否足以完整回答，以下为检索到的原文："
    excerpts = [f"[{item.get('source_id', i)}] {item['content'].strip()}" for i, item in enumerate(citations, 1)]
    response.content = prefix + "\n\n" + "\n\n".join(excerpts)
    if omitted_images:
        response.content += "\n\n另有图片资料，其内容需要进一步核验。"
    return citations


def _answer_generation_metadata(response: AIMessage, *, generation_ran=True):
    """Describe the model reply before evidence fallback changes its content."""
    if not generation_ran:
        return {'generation_status': 'NOT_RUN', 'generation_finish_reason': None}
    finish = (getattr(response, 'response_metadata', None) or {}).get('finish_reason')
    content = response.content
    # Match ordinary grounded chat's completion policy. Unknown content shapes
    # remain unknown; neither fallback text nor a tool call proves completion.
    status = ('TRUNCATED' if finish == 'length' else
              'EMPTY' if isinstance(content, str) and not content.strip() else
              'COMPLETED' if isinstance(content, str) and content.strip()
              and finish in ('stop', None) else 'UNKNOWN')
    return {'generation_status': status, 'generation_finish_reason': finish}


def _finalize_knowledge_answer(state: AgentState, response: AIMessage, *, include_mode=False, generation_ran=True):
    """Return a complete generated answer when retrieval is usable, else fallback."""
    response.content, alias_stats = restore_known_source_aliases(
        response.content, state.get('source_registry'), state.get('model_evidence_source_ids'))
    if any(alias_stats.values()):
        event('agent.source_alias_restore', **alias_stats)
    generation_metadata = _answer_generation_metadata(response, generation_ran=generation_ran)
    candidate = response.content
    citations = _guard_knowledge_answer(state, response)
    statuses = []
    omitted_images = False
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage) or message.name not in EVIDENCE_TOOLS:
            continue
        try:
            data = json.loads(message.content)
            status = data.get("retrieval_status", "RETRIEVAL_ERROR" if data.get("error") else "UNKNOWN")
            statuses.append(status if status in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE", "RETRIEVAL_ERROR", "UNKNOWN", "PARTIAL"} else "UNKNOWN")
            omitted_images |= bool(data.get('omitted_images'))
            for item in data.get("citations", []):
                if isinstance(item, dict):
                    omitted_images |= evidence_text(item)[1]
        except (TypeError, ValueError, AttributeError):
            statuses.append("UNKNOWN")
    if citations:
        mode = "EXTRACTIVE_FALLBACK"
    elif "RETRIEVAL_ERROR" in statuses:
        mode = "RETRIEVAL_ERROR"
    elif not statuses or "UNKNOWN" in statuses or "PARTIAL" in statuses:
        mode = "UNKNOWN"
    else:
        mode = "INSUFFICIENT_EVIDENCE"
    validation_status = "NOT_RUN"
    finish_reason = (getattr(response, "response_metadata", None) or {}).get("finish_reason")
    # 评分格式异常（last_score=None）不再否决已生成的答案；只有明确的低分才降级。
    score = state.get("last_score")
    score_usable = score is None or score >= _grounded_min_score()
    # 部分网关不返回 finish_reason；只要内容非空且不是显式截断，就按完成处理。
    completed = finish_reason in ("stop", None)
    if (os.getenv("RAG_GROUNDED_GENERATION", "true").lower() == "true"
            and citations and "SUCCESS" in statuses
            and all(status in {"SUCCESS", "NO_RESULTS"} for status in statuses)
            and score_usable and completed
            and isinstance(candidate, str) and candidate.strip()):
        # 答案不再经过自定义内容/引用规则；早前某轮检索为空不影响后续成功轮次，
        # 但 RETRIEVAL_ERROR / UNKNOWN / 低分仍会阻断生成。
        disclosure = "\n\n另有图片资料，其内容需要进一步核验。" if omitted_images else ""
        response.content = candidate + disclosure
        mode = "GROUNDED_GENERATION"
    event("agent.answer_validation", mode=mode, status=validation_status)
    event("qa.answer", answer_mode=mode, validation_status=validation_status, **generation_metadata)
    return (citations, mode) if include_mode else citations

class AgentWorkflow:
    def _result_store(self, registry):
        """Lazily allocate one bounded store for this workflow invocation."""
        if not getattr(self, '_tool_result_store_enabled', False):
            return None
        store = getattr(self, '_tool_result_store', None)
        try:
            if store is None:
                from app.services.agent_tool_results import AgentToolResultStore
                settings = get_settings()
                store = AgentToolResultStore(registry,
                    root=getattr(settings, 'AGENT_TOOL_RESULT_STORE_DIR', None),
                    max_bytes=getattr(settings, 'AGENT_TOOL_RESULT_MAX_BYTES', 8 * 1024 * 1024),
                    max_entries=getattr(settings, 'AGENT_TOOL_RESULT_MAX_ENTRIES', 32))
                self._tool_result_store = store
            store.update_registry(registry)
            return store
        except (OSError, ValueError, TypeError):
            self._tool_result_store_enabled = False
            event('agent.tool_result_store', status='UNAVAILABLE', fallback='WHOLE_SOURCE_BUDGET')
            return None

    def close(self):
        self._tool_result_store_enabled = False
        store = getattr(self, '_tool_result_store', None)
        if store is not None:
            store.close()

    def _route_post_retrieval_model(self, state, llm, invocation_messages, *, return_decision=False):
        """Route only post-retrieval Agent execution; auxiliary stages stay fixed."""
        from app.services.grounded_generation import routing_is_off
        if routing_is_off():
            return (llm, None, None) if return_decision else llm
        if (not state.get('kb_ids') or state.get('retrieval_status') != 'SUCCESS'
                or not state.get('citations')):
            event('router.agent', stage='agent_post_retrieval', reason='NO_RELIABLE_EVIDENCE_FOR_ROUTING')
            return (llm, None, None) if return_decision else llm
        from app.services.grounded_generation import _route_generation
        roles = {'human': 'user', 'ai': 'assistant', 'system': 'system', 'tool': 'tool'}
        actual_messages = []
        for message in invocation_messages:
            row = {'role': roles.get(message.type, message.type), 'content': message.content}
            for key in ('tool_calls', 'tool_call_id', 'name'):
                value = getattr(message, key, None)
                if value:
                    row[key] = value
            actual_messages.append(row)
        routed = _route_generation(
            llm, llm.model, actual_messages, state.get('source_registry') or state['citations'],
            question=state.get('original_query', ''), retrieval_status=state['retrieval_status'],
            **({'return_decision': True} if return_decision else {}))
        selected = routed[0]
        event('router.agent', stage='agent_post_retrieval', model=selected.model,
              reason='PRE_ROUTE_ONLY', quality_status='NOT_RUN')
        return (selected, routed[2], routed[3]) if return_decision else selected

    def __init__(self):
        self.llm_service = LLMService()
        self.tools = [search_knowledge_base, read_knowledge_context, read_tool_result]
        self._tool_result_store = None
        self._tool_result_store_enabled = getattr(get_settings(), 'AGENT_TOOL_RESULT_INLINE_CHARS', 6000) > 0
        
        # 构建图
        workflow = StateGraph(AgentState)
        
        # 添加节点
        workflow.add_node("intent_analysis", self.analyze_intent)
        workflow.add_node("direct_retrieval", self.direct_first_retrieval)
        workflow.add_node("agent", self.agent_node)
        workflow.add_node("tools", self.run_tools)
        workflow.add_node("process_tool_output", self.process_tool_output) # Observation
        workflow.add_node("generate_chat", self.generate_chat)
        
        # 设置入口
        workflow.set_entry_point("intent_analysis")
        
        # 添加条件边: Intent Analysis -> (Agent or Chat)
        workflow.add_conditional_edges(
            "intent_analysis",
            self.route_based_on_intent,
            {
                "chat": "generate_chat",
                "qa": "agent",
                "direct_retrieval": "direct_retrieval",
            }
        )
        
        # 添加条件边: Agent -> (Tools or End)
        workflow.add_conditional_edges(
            "agent",
            self.should_continue,
            {
                "continue": "tools",
                "end": END
            }
        )
        
        # Tools -> Process Output
        workflow.add_edge("tools", "process_tool_output")
        workflow.add_edge("direct_retrieval", "process_tool_output")
        
        # Evidence sufficiency is decided by the Agent over delivered sources.
        # The historical prefix-only grading method is offline compatibility;
        # it is deliberately absent from the production graph.
        workflow.add_edge("process_tool_output", "agent")
        
        workflow.add_edge("generate_chat", END)
        
        self.app = workflow.compile()

    def _get_llm_service(self, state: AgentState) -> LLMService:
        """从 State 中获取 LLM 配置并创建服务实例"""
        config = state.get("model_config", {})
        if config and config.get("model_id"):
            # 如果配置中有 model_id，说明是自定义模型
            return LLMService(
                base_url=config.get("base_url"),
                api_key=config.get("api_key"),
                model=config.get("model_id")
            )
        return self.llm_service

    @observe("agent.analyze_intent")
    async def analyze_intent(self, state: AgentState, config: RunnableConfig):
        """意图分析节点"""
        # Trailing assistant drafts are not the active user request.
        current_index = next((i for i in range(len(state["messages"]) - 1, -1, -1)
                              if isinstance(state["messages"][i], HumanMessage)),
                             len(state["messages"]) - 1)
        last_message = state["messages"][current_index]
        content = last_message.content

        # Explicit requests to use selected sources must not be routed to chat.
        if explicitly_requests_sources(content):
            return {
                "intent": "qa",
                "agent_steps": [{"step": "decision", "content": "检测到明确的知识库资料请求"}],
                "original_query": content,
                "retry_count": 0,
                "execution_history": [],
            }

        understood_intent = state.get("understanding_intent")
        if understood_intent in ("chat", "qa"):
            return {
                "intent": understood_intent,
                "agent_steps": [{"step": "decision", "content": f"意图分析: {understood_intent}"}],
                "original_query": content,
                "retry_count": 0,
                "execution_history": [],
            }
        
        # Without a usable unified understanding, default to retrieval.  Do not
        # run a separate intent classifier or infer chitchat from keywords.
        return {
            "intent": "qa",
            "agent_steps": [{"step": "decision", "content": "默认使用知识库问答"}],
            "original_query": content,
            "retry_count": 0,
            "execution_history": [],
        }

    def route_based_on_intent(self, state: AgentState):
        # Experimental workflow only. Complex tool selection and unresolved
        # conversational/image inputs retain the existing model-led entry.
        if (os.getenv("RAG_AGENT_FIRST_RETRIEVAL", "MODEL").strip().upper() == "DIRECT_KB_V1"
                and state.get("intent") == "qa" and state.get("kb_ids")
                and state.get("has_prior_conversation") is False
                and not state.get("retrieval_attempt_count")
                and not state.get("executed_retrieval_requests")
                and not state.get("retrieval_blocked")
                and all(getattr(tool, 'name', None) in EVIDENCE_TOOLS for tool in self.tools)
                and getattr(self.tools[0], "name", None) == "search_knowledge_base"):
            users = [m for m in state.get("messages", []) if isinstance(m, HumanMessage)]
            query = state.get("retrieval_query") or state.get("original_query")
            # A truthy retrieval_query can also be the unchanged original
            # question after failed understanding. V1 therefore leaves every
            # multi-turn request on the existing model-led path.
            if (len(users) == 1 and isinstance(users[-1].content, str)
                    and isinstance(query, str) and query.strip()):
                return "direct_retrieval"
        return state["intent"]

    @observe("agent.direct_first_retrieval")
    async def direct_first_retrieval(self, state: AgentState, config: RunnableConfig):
        """Deterministic tool dispatch; this is not a model-generated call.

        Keep the protocol pair in history so the final model sees a valid tool
        exchange. The normal tool executor, source registry, grader and retry
        loop remain authoritative.
        """
        dispatch = AIMessage(
            content="", tool_calls=[{
                "name": "search_knowledge_base", "id": "workflow_" + uuid.uuid4().hex,
                "args": {"query": state.get("retrieval_query") or state["original_query"]},
            }], additional_kwargs={"execution_origin": "workflow_direct_retrieval"})
        event("agent.first_retrieval", action="DIRECT_KB_V1",
              origin="workflow_direct_retrieval", model_selection_status="NOT_RUN")
        result = await self.run_tools(
            {**state, "messages": [*state["messages"], dispatch]}, config)
        result["messages"] = [dispatch, *result.get("messages", [])]
        result["agent_steps"] = [{"step": "action", "content": "工作流直接执行首次知识库检索"}]
        return result

    @observe("agent.agent_node")
    async def agent_node(self, state: AgentState, config: RunnableConfig):
        """Agent 思考节点"""
        history_update = _prepare_history_evidence(state)
        history_messages = history_update.get('messages', [])
        if history_update:
            state = {**state, **history_update, 'messages': [*state['messages'], *history_messages]}
        if state.get('retrieval_blocked'):
            response = AIMessage(content='')
            citations, mode = _finalize_knowledge_answer(state, response, include_mode=True, generation_ran=False)
            return {'messages': [response], 'citations': citations, 'answer_mode': mode,
                    'validation_status': 'NOT_RUN',
                    **_answer_generation_metadata(response, generation_ran=False)}
        # Only a completed sequence of empty retrievals permits the configured
        # no-evidence answer; earlier failures or evidence remain authoritative.
        if (state.get('retrieval_status') == 'NO_RESULTS'
                and not state.get('retrieval_retry_scheduled')
                and not state.get('degraded') and not state.get('source_registry')
                and not state.get('citations') and state.get('kb_ids')):
            search_results = []
            for message in state.get('messages', []):
                if isinstance(message, ToolMessage) and message.name == 'search_knowledge_base':
                    try:
                        data = json.loads(message.content)
                        if not isinstance(data, dict):
                            raise ValueError('invalid search result')
                        search_results.append(data)
                    except (TypeError, ValueError):
                        search_results.append({})
            if (search_results and all(
                    item.get('retrieval_status') == 'NO_RESULTS'
                    and not item.get('degraded') and not item.get('citations')
                    and not item.get('omitted_images')
                    for item in search_results)):
                from app.services.grounded_generation import generate_no_results_fallback
                text, strategy, finish, usage = await generate_no_results_fallback(
                    self._get_llm_service(state), state.get('original_query', ''))
                response = AIMessage(content=text, response_metadata={'finish_reason': finish})
                usage_record = None
                if isinstance(usage, dict) and all(isinstance(usage.get(key), int) for key in
                        ('prompt_tokens', 'completion_tokens', 'total_tokens')):
                    usage_record = {'input': usage.get('prompt_tokens'),
                                    'output': usage.get('completion_tokens'),
                                    'total': usage.get('total_tokens')}
                return {'messages': [response], 'citations': [],
                        'answer_mode': 'NO_RESULTS_' + strategy.upper(),
                        'fallback_strategy': strategy,
                        'validation_status': 'NOT_RUN',
                        'generation_status': ('NOT_RUN' if strategy == 'fixed' else
                                              'COMPLETED' if finish == 'stop' else
                                              'TRUNCATED' if finish == 'length' else
                                              'EMPTY' if finish == 'empty' else 'UNKNOWN'),
                        'generation_finish_reason': finish,
                        'model_usage_records': [usage_record] if usage_record is not None else []}
        # 准备 System Prompt
        system_msg = SystemMessage(content=f"""你是一个专业的智能助手，专门用于帮助用户从知识库中检索和解答问题。

你的工作流程如下：
1. **分析意图**：仔细分析用户的问题，确定需要查询什么信息。
2. **执行检索**：使用 `search_knowledge_base` 工具查询知识库。
   - 用户要求依据所选资料回答时，先检索获取证据；工具返回前，不要声称资料缺失或检索无结果。
   - 只需提供 `query` 参数。系统会自动处理知识库 ID 和检索配置。
   - 尽量使用准确的关键词。
   - 如果用户的问题比较复杂，可以拆解为多次查询。
   - 先搜索命中的原始片段。需要上下文、父块或相邻内容时，再调用 `read_knowledge_context`，将已返回的短别名（例如 `s1`）作为 `source_id` 参数；不要猜测来源。
   - 历史助手回答本身不是原文证据。历史检索默认省略，应重新搜索；启用保留历史时，可以按索引读取已核实的历史原文。
   - 大工具结果会返回 `rN` 结果句柄和 `sN` 来源索引；预览是不完整摘录，不能当作已读原文。用 `read_tool_result` 传入句柄及所需来源别名，读取完整来源后再引用；可按来源分页读取。
3. **检查证据**：
   - 阅读本轮已提供的来源，判断其是否包含回答所需的信息；不因第一条片段无关而忽略后续来源。
   - 证据已支持问题时直接作答。缺少必要信息时，选择更准确的关键词继续搜索，或读取已知来源的上下文。
   - 补充搜索时可以改写关键词或拆分问题；检索参数由服务端统一控制，不要在工具参数里调整。
   - 不要重复相同查询或反复读取相同上下文。搜索和读取次数由服务端限制。
4. **生成回答**：
   - 基于本轮实际提供的原文证据回答；无需等待额外评分或批准。
   - 仅依据工具返回的文本证据作答；不要补充证据未明确支持的细节、单位、时间或推断。证据只支持部分答案时，说明缺少什么，不要猜测。检索片段未提供某信息时，只说明现有片段未提供，不要据此断言整份文档没有该信息。
   - 每个依据资料作出的事实陈述旁使用对应来源的短别名引用，例如 `[s1]`；短别名取自工具结果（与 context 中的 `Document s1` 对应）。多个来源均支持时可分别引用，例如 `[s1][s2]`；不要编造来源或编号。
   - 如果多次尝试后仍无结果，请诚实告知用户。

注意事项：
- 始终使用中文回答。
- 优先使用工具获取信息，不要编造事实。
- 需要更多证据时选择下一项搜索或读取操作。
- **重要**：最终回答用户时，**不要**包含 "Thinking: ..." 这样的思考过程块。只输出最终的回答内容。思考过程仅用于中间步骤。""")
        
        messages = history_as_sent(state["messages"], original_query=state.get('original_query'),
            retain_retrieval_history=state.get('retain_retrieval_history', False))
        if state.get('retrieval_query') and not state.get('retrieval_attempt_count'):
            system_msg = SystemMessage(content=system_msg.content +
                '\n\n本轮首轮检索使用已解析的问题：' + state['retrieval_query'] +
                '\n最终回答仍针对用户原始问题；是否补充搜索或读取上下文取决于已有证据是否足够。')
        invocation_messages = [system_msg] + messages
        
        llm = self._get_llm_service(state)
        llm, route, router_config = self._route_post_retrieval_model(
            state, llm, invocation_messages, return_decision=True)
        # This workflow combines tool selection and final composition in one
        # model call. Keep preferences out of routing/tool state and constrain
        # this independent segment to visible final answer composition only.
        forced_retry = state.get('retrieval_retry_scheduled', False)
        answer_possible = (bool(state.get('source_registry') or state.get('citations'))
                           and state.get('retrieval_status') == 'SUCCESS' and not forced_retry)
        if state.get('preference_policy') and answer_possible:
            invocation_messages = [SystemMessage(content=system_msg.content + '\n\n' + state['preference_policy'])] + messages
        event('memory.delivery', stage='agent_tool_and_answer',
              preference_policy_applied=bool(state.get('preference_policy') and answer_possible))
        # Use streaming=False to prevent crashes on local models
        from app.services.grounded_generation import _routed_chat_model, _routed_response_identity, _router_outcome
        model = _routed_chat_model(llm, self.tools)
        from app.services.grounded_generation import enforce_request_budget
        from app.services.agent_evidence_projection import VERSION, project_agent_evidence_messages, encode_agent_sources, persist_agent_tool_results
        projection_mode = os.getenv('RAG_AGENT_EVIDENCE_PROJECTION', VERSION).strip().upper()
        if projection_mode != 'OFF' and state.get('source_registry'):
            invocation_messages, result_stats = await _store_io(persist_agent_tool_results, invocation_messages,
                store=lambda: self._result_store(state.get('source_registry')),
                canonical_context=_source_context, source_registry=state.get('source_registry'),
                inline_chars=getattr(get_settings(), 'AGENT_TOOL_RESULT_INLINE_CHARS', 6000))
            event('agent.tool_result_projection', **result_stats)
        if projection_mode != 'OFF':
            if projection_mode != VERSION:
                raise ValueError('Unsupported Agent evidence projection mode')
            invocation_messages, projection_stats = project_agent_evidence_messages(
                invocation_messages, canonical_context=_source_context,
                source_registry=state.get('source_registry'),
                tools=getattr(model, 'kwargs', {}).get('tools'))
            event('agent.evidence_projection', **projection_stats)
        budget_messages = invocation_messages
        invocation_messages, delivery = consolidate_agent_context(
            invocation_messages, canonical_context=_source_context,
            source_registry=state.get('source_registry'),
            tools=getattr(model, 'kwargs', {}).get('tools'),
            limit_chars=get_settings().GROUNDED_REQUEST_MAX_CHARS,
            original_query=state.get('original_query'),
            measure_messages=lambda rows: request_chars(
                encode_agent_sources(rows, state.get('source_registry'))[0],
                getattr(model, 'kwargs', {}).get('tools')))
        state = {**state, 'model_evidence_source_ids': delivery['source_ids'], 'context_delivery': delivery}
        event('agent.context_delivery', **delivery)
        invocation_messages, alias_stats = encode_agent_sources(invocation_messages, state.get('source_registry'))
        event('agent.source_alias_encode', **alias_stats)
        enforce_request_budget(invocation_messages, getattr(model, 'kwargs', {}).get('tools'))
        
        _observe_model_request("agent", invocation_messages, llm.model, provider_model=model)
        try:
            response = await invoke_provider_model(model, invocation_messages, config=config, stage="agent", model_name=llm.model)
        except BaseException as error:
            _router_outcome(route, llm.model, error=error, stage='agent')
            raise
        _observe_model_response("agent", response, llm.model)
        
        model_response(response, role="agent", model=llm.model)
        from app.services.llm import _response_usage
        usage_records = [_response_usage(response)]
        identity_status = (_routed_response_identity(response, llm.model, stage='agent')
                           if route is not None else 'UNKNOWN')
        escalation_used = state.get('router_escalations_used', 0)
        finish_reason = (getattr(response, 'response_metadata', None) or {}).get('finish_reason')
        if (route is not None and route.mode == 'DYNAMIC' and route.selected_role == 'CHEAP'
                and os.getenv('RAG_ROUTER_QUALITY_CHECK', 'NONE').strip().upper() == 'REFERENCE_RANGE'
                and not response.tool_calls and finish_reason == 'stop' and identity_status == 'MATCH'
                and isinstance(response.content, str) and response.content.strip()):
            from app.services.grounded_generation import _reference_range_quality, _role_service
            from app.services.model_router import decide_escalation
            try:
                numeric_content, _ = restore_known_source_aliases(
                    response.content, state.get('source_registry'), state.get('model_evidence_source_ids'))
                quality_status = _reference_range_quality(numeric_content, len(state.get('source_registry') or state['citations']))
            except Exception:
                quality_status = 'UNKNOWN'
            escalation = decide_escalation(route, completion_status='COMPLETED', quality_status=quality_status,
                                           retries_used=escalation_used, config=router_config)
            event('router.agent_quality', stage='agent_post_retrieval', status=quality_status, reason=escalation.reason)
            if escalation.should_retry and quality_status == 'FAIL' and escalation_used == 0:
                event('router.escalation', stage='agent_post_retrieval', status='QUALITY_ESCALATION',
                      model=escalation.selected_model, retry_count=1, cost={'status': 'UNKNOWN'})
                upgraded = _role_service('EXPENSIVE', escalation.selected_model)
                upgraded_model = _routed_chat_model(upgraded, self.tools)
                invocation_messages, delivery = consolidate_agent_context(
                    budget_messages, canonical_context=_source_context,
                    source_registry=state.get('source_registry'),
                    tools=getattr(upgraded_model, 'kwargs', {}).get('tools'),
                    limit_chars=get_settings().GROUNDED_REQUEST_MAX_CHARS,
                    original_query=state.get('original_query'),
                    measure_messages=lambda rows: request_chars(
                        encode_agent_sources(rows, state.get('source_registry'))[0],
                        getattr(upgraded_model, 'kwargs', {}).get('tools')))
                state = {**state, 'model_evidence_source_ids': delivery['source_ids'], 'context_delivery': delivery}
                event('agent.context_delivery', **delivery)
                invocation_messages, alias_stats = encode_agent_sources(invocation_messages, state.get('source_registry'))
                event('agent.source_alias_encode', **alias_stats)
                event('memory.delivery', stage='agent_tool_and_answer',
                      preference_policy_applied=bool(state.get('preference_policy') and answer_possible))
                enforce_request_budget(invocation_messages, getattr(upgraded_model, 'kwargs', {}).get('tools'))
                _observe_model_request('agent', invocation_messages, upgraded.model, provider_model=upgraded_model)
                try:
                    response = await invoke_provider_model(upgraded_model, invocation_messages, config=config,
                                                           stage='agent', model_name=upgraded.model)
                except BaseException as error:
                    _router_outcome(route, upgraded.model, escalations=1, error=error, stage='agent')
                    raise
                _routed_response_identity(response, upgraded.model, stage='agent')
                _observe_model_response('agent', response, upgraded.model)
                model_response(response, role='agent', model=upgraded.model)
                usage_records.append(_response_usage(response))
                escalation_used += 1
        _router_outcome(route, upgraded.model if escalation_used > state.get('router_escalations_used', 0) else llm.model,
                        finish=(getattr(response, 'response_metadata', None) or {}).get('finish_reason'),
                        content=response.content if not response.tool_calls else None,
                        escalations=escalation_used, stage='agent', tool_calls=bool(response.tool_calls),
                        selected_role='EXPENSIVE' if escalation_used > state.get('router_escalations_used', 0) else getattr(route, 'selected_role', None))
        # 处理 Thinking 块：将其从 content 中剥离，放入 agent_steps
        import re
        content = response.content
        thinking_blocks = re.findall(r"Thinking:\s*(.*?)(?:\n\n|\Z)", content, re.DOTALL | re.IGNORECASE)
        
        # 清理 content 中的 Thinking 块
        clean_content = re.sub(r"Thinking:\s*.*?(?:\n\n|\Z)", "", content, flags=re.DOTALL | re.IGNORECASE).strip()
        
        # 如果清理后内容为空（只剩 Thinking），则保留原始内容或者设为占位符（视情况而定）
        # 但通常 Agent 会输出 Thinking 后再输出 Action 或 Answer
        if clean_content:
             response.content = clean_content
        
        steps = []
        if thinking_blocks:
            for block in thinking_blocks:
                steps.append({
                    "step": "thinking",
                    "content": "Agent 正在处理检索与证据。"
                })
        else:
            steps.append({
                "step": "thinking",
                "content": "Agent 正在思考..."
            })
        
        if response.tool_calls:
            for tool_call in response.tool_calls:
                function_name = tool_call.get("name")
                
                steps.append({
                    "step": "action",
                    "content": f"调用工具: {function_name}"
                })
        
        result = {**{k: v for k, v in history_update.items() if k != 'messages'},
                  "messages": [*history_messages, response], "agent_steps": steps,
                  'model_usage_records': usage_records,
                  'model_evidence_source_ids': delivery['source_ids'], 'context_delivery': delivery}
        if delivery['omitted_source_ids']:
            result['expansion_delivery_status'] = 'PARTIAL'
        if escalation_used:
            result['router_escalations_used'] = escalation_used
        if not response.tool_calls:
            # Tool binding permits a plain first draft. A QA draft without any
            # evidence must enter retrieval, rather than be replaced by UNKNOWN
            # or trigger a second identical model invocation.
            if (not state.get('source_registry') and not state.get('citations')
                    and not state.get('retrieval_attempt_count') and state.get('kb_ids')):
                response.content = ''
                response.tool_calls = [{'name': 'search_knowledge_base',
                    'id': 'workflow_' + uuid.uuid4().hex,
                    'args': {'query': state.get('retrieval_query') or state.get('original_query', '')}}]
                response.additional_kwargs['execution_origin'] = 'workflow_direct_retrieval'
                event('agent.first_retrieval', action='NO_EVIDENCE_DRAFT_RETRIEVAL',
                      origin='workflow_direct_retrieval')
                return result
            result.update(_answer_generation_metadata(response))
            result["citations"], result["answer_mode"] = _finalize_knowledge_answer(state, response, include_mode=True)
            result['validation_status'] = 'NOT_RUN'
        return result

    @observe("agent.run_tools")
    async def run_tools(self, state: AgentState, config: RunnableConfig):
        """自定义工具执行节点，注入配置参数"""
        messages = state["messages"]
        last_message = messages[-1]
        
        if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
            return {}
            
        tool_outputs = []
        registry = [dict(c) for c in state.get('source_registry', [])]
        executed = list(state.get('executed_retrieval_requests', []))
        context_reads = list(state.get('executed_context_reads', []))
        result_reads = list(state.get('executed_tool_result_reads', []))
        blocked = False
        attempt_count = state.get("retrieval_attempt_count", 0)
        last_attempt_id = state.get("retrieval_attempt_id")
        for tool_call in last_message.tool_calls:
            if tool_call['name'] == 'read_tool_result':
                args = tool_call.get('args') or {}
                key = json.dumps([args.get('result_handle'), args.get('source_ids'),
                    args.get('offset', 0), args.get('limit', 3)], sort_keys=True)
                data = {'retrieval_status': 'UNKNOWN', 'context': '', 'citations': [],
                        'source_ids': [], 'execution_status': 'SAVED_RESULT_UNAVAILABLE'}
                requested_sources = [resolve_source_alias(alias, registry)
                    for alias in (args.get('source_ids') or [])] if isinstance(args.get('source_ids'), list) else []
                delivered_ids = set(state.get('model_evidence_source_ids') or [])
                already_delivered = bool(requested_sources) and all(
                    source is not None and source.get('source_id') in delivered_ids for source in requested_sources)
                if (key in result_reads and already_delivered) or len(result_reads) >= 6:
                    data['execution_status'] = 'TOOL_RESULT_READ_LIMIT'
                    blocked = True
                else:
                    result_reads.append(key)
                    store = self._result_store(registry)
                    if store is not None:
                        try:
                            data = await _store_io(store.read, args.get('result_handle'), args.get('source_ids'),
                                offset=args.get('offset', 0), limit=args.get('limit', 3))
                            data['source_mapping_status'] = 'VERIFIED'
                        except (OSError, ValueError, TypeError, KeyError):
                            event('agent.tool_result_read', status='UNAVAILABLE')
                tool_outputs.append(ToolMessage(content=json.dumps(data, ensure_ascii=False),
                    name='read_tool_result', tool_call_id=tool_call['id']))
                continue
            if tool_call['name'] == 'read_knowledge_context':
                args = tool_call.get('args') or {}
                source = resolve_source_alias(args.get('source_id'), registry)
                window = args.get('window', 1)
                key = json.dumps([source.get('chunk_id') if source else None, window])
                data = {'retrieval_status': 'NO_RESULTS', 'context': '', 'citations': [],
                        'execution_status': 'SOURCE_NOT_AVAILABLE'}
                if source and type(window) is int and 0 <= window <= 2:
                    if key in context_reads or len(context_reads) >= 6:
                        blocked = True
                        data.update(retrieval_status='UNKNOWN', execution_status='CONTEXT_READ_LIMIT')
                    else:
                        context_reads.append(key)
                        try:
                            from app.tools.read_knowledge_context import read_registered_context
                            data = json.loads(await read_registered_context(
                                source, state.get('kb_ids') or [], window=window))
                        except Exception as error:
                            if is_unknown_provider_error(error):
                                raise
                            event('agent.context_read_failed', error_type=type(error).__name__)
                            data = {'retrieval_status': 'RETRIEVAL_ERROR', 'context': '', 'citations': []}
                data = _register_sources(data, registry)
                tool_outputs.append(ToolMessage(content=json.dumps(data, ensure_ascii=False),
                    name='read_knowledge_context', tool_call_id=tool_call['id']))
                continue
            if tool_call["name"] == "search_knowledge_base":
                # Only the server state determines scope and requested settings. The
                # model may choose the query, but cannot override supplied settings.
                last_attempt_id = uuid.uuid4().hex
                attempt_count += 1
                agent_args = tool_call["args"].copy()
                search_params = state.get("search_params", {})
                parameter_names = (
                    "search_mode", "top_k", "score_threshold", "vector_weight",
                    "bm25_weight", "rerank_top_k", "rerank_score_threshold",
                    "rerank_model_id", "context_window",
                )
                query = (state.get('retrieval_query') if attempt_count == 1
                         and state.get('retrieval_query') else agent_args.get('query'))
                final_args = {"query": query, "kb_ids": state.get("kb_ids", []), 'expand_evidence': False}
                if state.get("original_query"):
                    final_args["original_query"] = state["original_query"]
                for name in parameter_names:
                    if name in search_params:
                        final_args[name] = search_params[name]
                    elif name in agent_args and name != "rerank_model_id":
                        final_args[name] = agent_args[name]
                if "rerank_enabled" in search_params:
                    final_args["use_rerank"] = search_params["rerank_enabled"]
                elif "use_rerank" in agent_args:
                    final_args["use_rerank"] = agent_args["use_rerank"]

                # Log only bounded, non-content settings; query and resource IDs
                # can contain sensitive data and must not enter this metadata.
                def safe_settings(values):
                    safe = {}
                    for key in ("top_k", "score_threshold", "vector_weight", "bm25_weight",
                                "rerank_top_k", "rerank_score_threshold", "context_window"):
                        value = values.get(key)
                        if type(value) in (int, float):
                            safe[key] = value
                    if values.get("search_mode") in ("vector", "fulltext", "hybrid"):
                        safe["search_mode"] = values["search_mode"]
                    if type(values.get("use_rerank")) is bool:
                        safe["use_rerank"] = values["use_rerank"]
                    return safe

                requested = dict(agent_args)
                if "rerank_enabled" in requested:
                    requested["use_rerank"] = requested["rerank_enabled"]
                try:
                    effective_args = {
                        name: field.default
                        for name, field in search_knowledge_base.args_schema.model_fields.items()
                        if not field.is_required()
                    }
                    effective_args.update(final_args)
                    request_key = hashlib.sha256(json.dumps(effective_args, sort_keys=True, default=str).encode('utf-8')).hexdigest()
                    block_reason = ('RETRY_LIMIT' if attempt_count > 4 else
                                    'DUPLICATE_EFFECTIVE_REQUEST' if request_key in executed else None)
                    if block_reason:
                        attempt_count -= 1
                    scope_hash = hashlib.sha256(json.dumps(final_args["kb_ids"], sort_keys=True).encode("utf-8")).hexdigest()
                    event("agent.actual_query", retrieval_attempt_id=last_attempt_id,
                          tool_call_id=tool_call.get("id"), origin=(
                              "workflow_direct_retrieval" if last_message.additional_kwargs.get(
                                  "execution_origin") == "workflow_direct_retrieval" else "agent_tool_call"),
                          retrieval_attempt_count=attempt_count, query=snapshot(final_args.get("query")) if enabled() else None,
                          kb_scope_sha256=scope_hash, retry_count=state.get("retry_count", 0),
                          requested_parameters=safe_settings(requested),
                          actual_parameters=safe_settings(effective_args))
                    if block_reason:
                        blocked = True
                        result = json.dumps({'retrieval_status': 'UNKNOWN', 'context': '', 'citations': [],
                            'execution_status': block_reason, 'retry_reason': block_reason,
                            'requested_parameters': safe_settings(requested),
                            'actual_parameters': safe_settings(effective_args)})
                        event('agent.tool_blocked', reason_code=block_reason, retry_count=state.get('retry_count', 0))
                    elif not final_args["kb_ids"]:
                        result = json.dumps({"retrieval_status": "NO_RESULTS", "context": "No knowledge bases found in the system.", "citations": []})
                    else:
                        # Reserve before invoking: unknown outcomes cannot be replayed.
                        executed.append(request_key)
                        token = retrieval_attempt.set(last_attempt_id)
                        try:
                            from app.services.observability import native_enabled, langchain_callback_config, tool_observation
                            if native_enabled():
                                tool_config, callback_enabled, reason = langchain_callback_config(
                                    config, tool_input=final_args,
                                    tool_metadata={'tool_call_id': tool_call.get('id'),
                                                   'retrieval_attempt_id': last_attempt_id})
                                manager = nullcontext(None) if callback_enabled else tool_observation(
                                    'search_knowledge_base', input=capture(final_args, content_role='provider_request'),
                                    tool_call_id=tool_call.get('id'), retrieval_attempt_id=last_attempt_id,
                                    reason_code=reason)
                                with manager as observation:
                                    result = await search_knowledge_base.ainvoke(final_args, config=tool_config)
                                    if observation is not None:
                                        try:
                                            observation.update(output=capture(result, content_role='context_sent'),
                                                               execution_status='RETURNED')
                                        except Exception:
                                            pass
                            else:
                                result = await search_knowledge_base.ainvoke(final_args, config=config)
                        finally:
                            retrieval_attempt.reset(token)
                except Exception as e:
                    if is_unknown_provider_error(e):
                        raise
                    event('agent.tool_failed', status='FAILED', error_type=type(e).__name__, retry_count=state.get('retry_count', 0))
                    result = json.dumps({"retrieval_status": "RETRIEVAL_ERROR", "context": "", "citations": []})
                
                try:
                    data = json.loads(result)
                    if not isinstance(data, dict):
                        raise ValueError('tool response must be an object')
                    result = json.dumps(_register_sources(data, registry), ensure_ascii=False)
                except (ValueError, TypeError):
                    result = json.dumps({'retrieval_status': 'UNKNOWN', 'context': '', 'citations': []})
                tool_outputs.append(
                    ToolMessage(
                        content=str(result),
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"],
                    )
                )
        
        return {"messages": tool_outputs, "retrieval_attempt_id": last_attempt_id,
                "retrieval_attempt_count": attempt_count, 'source_registry': registry,
                'executed_retrieval_requests': executed, 'retrieval_blocked': blocked,
                'executed_context_reads': context_reads,
                'executed_tool_result_reads': result_reads}

    def should_continue(self, state: AgentState):
        """决定是继续调用工具还是结束"""
        messages = state["messages"]
        last_message = messages[-1]
        
        # 如果有 tool_calls，则继续
        if last_message.tool_calls:
            return "continue"
        
        # 否则结束
        return "end"

    @observe("agent.process_tool_output")
    async def process_tool_output(self, state: AgentState):
        """处理工具输出，解析 JSON，记录 Observation"""
        # Parse this round's tool results. The answer registry remains cumulative;
        # the production graph has no independent prefix-only relevance grader.
        batch = []
        for message in reversed(state["messages"]):
            if not isinstance(message, ToolMessage):
                break
            if message.name in EVIDENCE_TOOLS:
                batch.append(message)
        if not batch:
            return {}
        citations, contexts, statuses, mappings, expansions = [], [], [], [], []
        degraded = bool(state.get('degraded', False))
        fallback_used = bool(state.get('fallback_used', False))
        for message in reversed(batch):
            try:
                data = json.loads(message.content)
                if not isinstance(data, dict):
                    raise ValueError('tool response must be an object')
                # Keep partial-route failure visible even after a healthy retry.
                degraded = degraded or data.get('degraded') is True
                fallback_used = fallback_used or data.get('fallback_used') is True
                status = data.get('retrieval_status', 'UNKNOWN')
                statuses.append(status if status in {'SUCCESS', 'NO_RESULTS', 'LOW_RELEVANCE', 'RETRIEVAL_ERROR', 'UNKNOWN', 'PARTIAL'} else 'UNKNOWN')
                contexts.append(data.get('context', ''))
                mappings.append(data.get('source_mapping_status', 'UNKNOWN'))
                expansions.append(data.get('expansion_delivery_status', 'UNKNOWN'))
                for citation in data.get('citations', []):
                    if isinstance(citation, dict) and citation not in citations:
                        citations.append(citation)
            except (TypeError, ValueError):
                statuses.append('UNKNOWN')
        status = ('RETRIEVAL_ERROR' if 'RETRIEVAL_ERROR' in statuses else
                  'UNKNOWN' if 'UNKNOWN' in statuses else
                  'PARTIAL' if 'PARTIAL' in statuses and citations else
                  'UNKNOWN' if 'PARTIAL' in statuses else
                  'LOW_RELEVANCE' if 'LOW_RELEVANCE' in statuses else
                  'SUCCESS' if citations else 'NO_RESULTS')
        if status == 'NO_RESULTS' and degraded:
            status = 'UNKNOWN'
        context = '\n\n'.join(c for c in contexts if isinstance(c, str))
        return {'citations': citations, 'rag_context': context,
                'last_score': None, 'grading_status': 'NOT_RUN', 'grading_scope': {},
                'retrieval_retry_scheduled': False,
                'retrieval_status': status,
                'degraded': degraded, 'fallback_used': fallback_used,
                'source_mapping_status': 'UNKNOWN' if 'UNKNOWN' in mappings else 'VERIFIED',
                'expansion_delivery_status': next((value for value in
                    ('OMITTED', 'SEED_ONLY', 'PARTIAL', 'UNKNOWN', 'DELIVERED') if value in expansions), 'NOT_REQUESTED'),
                'agent_steps': [{'step': 'observation', 'content': f'检索到 {len(citations)} 条相关记录'}]}

    @observe("agent.grade_documents")
    async def grade_documents(self, state: AgentState, config: RunnableConfig):
        """Legacy offline compatibility only; never invoked by the production graph.

        Retain historical scoring contracts for direct offline diagnostics. The
        Agent now decides evidence sufficiency from the delivered sources.
        """
        citations = state.get("citations", [])
        quality_fallback = False
        score_raw = None
        score_valid = None
        score_parsed = None
        context = state.get("rag_context", "")
        examined = context[:2000]
        quality_reason = "NO_CITATIONS" if not citations else "MODEL_RESPONSE"
        
        # 0. 如果无结果
        if not citations:
             score = 0.0
        else:
            # 1. 评分
            query = state.get("original_query", "")
            context = state.get("rag_context", "")
            
            prompt = f"""你是一个文档相关性评分员。
用户问题: {query}
检索到的文档片段:
{context[:2000]}... (截断)

请对文档与问题的相关性进行评分 (0.0 到 1.0)。
- 1.0: 完美匹配，包含直接答案。
- 0.8: 高度相关，包含大部分信息。
- 0.5: 部分相关，可能需要推断。
- 0.0: 完全不相关。

只输出一个数字，例如: 0.85
"""
            try:
                llm = self._get_llm_service(state)
                invocation_messages = [HumanMessage(content=prompt)]
                grading_timeout = get_settings().AGENT_GRADING_TIMEOUT_SECONDS
                model = llm.get_chat_model(temperature=0.1, streaming=False, timeout=grading_timeout)
                _observe_model_request("quality", invocation_messages, llm.model, provider_model=model)
                response = await invoke_provider_model(model, invocation_messages, config=config, stage="quality",
                                                       model_name=llm.model, timeout_seconds=grading_timeout)
                _observe_model_response("quality", response, llm.model)
                model_response(response, role="quality", model=llm.model)
                try:
                    score_raw = capture(_visible_content(response.content, assistant=True),
                                        content_role="provider_response") if enabled() else None
                except Exception:
                    score_raw = None
                score_parsed = _relevance_score(response.content)
                score = score_parsed
                quality_fallback = score is None
                score_valid = not quality_fallback
                if quality_fallback:
                    quality_reason = 'INVALID_FORMAT'
                    score = None
            except Exception as e:
                if should_abort_provider_error(e):
                    raise
                print(f"Grading failed: {type(e).__name__}")
                event('agent.quality_fallback', status='FALLBACK', error_type=type(e).__name__)
                quality_fallback = True
                score_valid = False
                quality_reason = "PROVIDER_FAILURE"
                score = None
            
        steps = [{
            "step": "reflection",
            "content": f"Score: {score} (相关性评分)"
        }]
        
        # 2. 检查重试逻辑
        messages = []
        retry_count = state.get("retry_count", 0)
        event("agent.quality", score=score, score_parsed=score_parsed,
              score_raw=score_raw, score_valid=score_valid, quality_fallback=quality_fallback,
              threshold=0.8, context_sent_sha256=hashlib.sha256(examined.encode("utf-8")).hexdigest() if citations else None,
              context_chars_examined=len(examined) if citations else 0,
              context_chars_total=len(context), context_clipped=bool(citations and len(context) > 2000),
              reason=quality_reason, retrieval_attempt_id=state.get("retrieval_attempt_id"),
              retry_count=retry_count, is_fallback=quality_fallback, cost=None)
        # A low score only represents low relevance when candidate evidence exists.
        if citations and not quality_fallback and score is not None and score < 0.8:
            event("agent.evidence_outcome", status="LOW_RELEVANCE", score=score, retry_count=retry_count)
        
        grading_scope = {'source_ids': [c.get('source_id') for c in citations],
                         'chunk_ids': [c.get('chunk_id') for c in citations],
                         'examined_source_ids': [c.get('source_id') for c in citations
                             if c.get('source_id') is not None and
                             0 <= context.find(f"Document {c['source_id']} (") < len(examined)],
                         'context_chars_examined': len(examined),
                         'context_chars_total': len(context), 'context_clipped': len(context) > 2000,
                         'scope': 'CURRENT_TOOL_ROUND_PREFIX', 'max_chars': 2000}
        if quality_fallback or state.get('retrieval_status') in ('UNKNOWN', 'RETRIEVAL_ERROR', 'PARTIAL'):
            event('agent.retry_decision', action='NOT_SCHEDULED', reason_code='QUALITY_OR_RETRIEVAL_UNKNOWN')
            return {'last_score': None, 'grading_status': 'UNKNOWN', 'retrieval_retry_scheduled': False,
                    'grading_scope': grading_scope, 'agent_steps': steps}
        if score < 0.8: # 阈值 0.8
            if retry_count < 3:
                new_retry_count = retry_count + 1
                record_agent_retry_decision("SCHEDULED", new_retry_count)
                event("agent.retry_decision", action="SCHEDULED", attempt=new_retry_count,
                      previous_attempt_id=state.get("retrieval_attempt_id"), decision="SCHEDULED",
                      reason_code="SCORE_BELOW_THRESHOLD", retry_count=retry_count,
                      next_action="PROMPT_AGENT", retrieval_attempt_count=state.get("retrieval_attempt_count", 0))
                hint = f"Observation: 检索结果相关性评分较低 ({score})。请尝试改写查询或拆分问题进行重试。这是第 {new_retry_count}/3 次重试。"
                messages.append(HumanMessage(content=hint)) # 注入 HumanMessage 作为提示
                
                steps.append({
                    "step": "reflection",
                    "content": f"评分过低 ({score} < 0.8)，准备第 {new_retry_count} 次重试..."
                })
                
                return {
                    "last_score": score, "grading_status": "VALID", "grading_scope": grading_scope,
                    "retrieval_retry_scheduled": True,
                    "retry_count": new_retry_count,
                    "messages": messages,
                    "agent_steps": steps
                }
            else:
                record_agent_retry_decision("EXHAUSTED", retry_count)
                event("agent.retry_decision", action="EXHAUSTED", attempt=retry_count,
                      previous_attempt_id=state.get("retrieval_attempt_id"), decision="EXHAUSTED",
                      reason_code="RETRY_LIMIT", retry_count=retry_count,
                      next_action="ANSWER", retrieval_attempt_count=state.get("retrieval_attempt_count", 0))
                hint = f"Observation: 检索结果相关性评分较低 ({score})，且已达到最大重试次数。请基于现有信息尽力回答，并在回答中说明信息可能不完整。"
                messages.append(HumanMessage(content=hint))
                
                steps.append({
                    "step": "reflection",
                    "content": "已达到最大重试次数，将基于现有信息回答。"
                })
                
                return {
                    "last_score": score, "grading_status": "VALID", "grading_scope": grading_scope,
                    "retrieval_retry_scheduled": False,
                    "messages": messages,
                    "agent_steps": steps
                }
        
        event("agent.retry_decision", action="NOT_SCHEDULED", previous_attempt_id=state.get("retrieval_attempt_id"),
              decision="NOT_SCHEDULED", reason_code="SCORE_AT_OR_ABOVE_THRESHOLD", retry_count=retry_count,
              next_action="ANSWER", retrieval_attempt_count=state.get("retrieval_attempt_count", 0))
        return {"last_score": score, "grading_status": "VALID", "grading_scope": grading_scope,
                "retrieval_retry_scheduled": False, "agent_steps": steps}

    @observe("agent.generate_chat")
    async def generate_chat(self, state: AgentState, config: RunnableConfig):
        """闲聊生成节点"""
        system_prompt = """你是一个智能问答助手。请根据你的训练知识，准确、完整、有条理地回答用户的问题。使用中文回答。"""
        
        # 闲聊不涉及 RAG，直接结束
        # 这里其实应该调用 LLM 生成一个回复，但根据旧逻辑，似乎是返回 prompt 让前端/外部流式调用？
        # 旧逻辑：return {"final_system_prompt": system_prompt}
        # 新逻辑：我们需要生成回复。
        
        messages = state["messages"]
        llm = self._get_llm_service(state)
        # Use streaming=False to ensure compatibility
        model = llm.get_chat_model(streaming=False)
        if state.get('preference_policy'):
            system_prompt += '\n\n' + state['preference_policy']
        invocation_messages = [SystemMessage(content=system_prompt)] + messages
        event('memory.delivery', stage='agent_chat',
              preference_policy_applied=bool(state.get('preference_policy')))
        _observe_model_request("agent_chat", invocation_messages, llm.model, provider_model=model)
        response = await invoke_provider_model(model, invocation_messages, config=config, stage="agent_chat", model_name=llm.model)
        _observe_model_response("agent_chat", response, llm.model)
        model_response(response, role='agent_chat', model=llm.model)
        
        return {
            "messages": [response],
            **_answer_generation_metadata(response),
            "agent_steps": [{"step": "response", "content": "生成闲聊回复"}]
        }

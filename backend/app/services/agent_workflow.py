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

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

from app.services.llm import LLMService, is_unknown_provider_error, should_abort_provider_error, invoke_provider_model
from app.services.metrics import record_agent_retry_decision
from app.core.config import get_settings
from app.tools import search_knowledge_base
from app.tools.retrieval_tool import retrieval_attempt
import uuid
import hashlib
from contextlib import nullcontext

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
    original_query: str # 用户原始问题
    current_plan: str # 当前执行计划
    
    # RAG 相关 (用于前端展示)
    rag_context: str
    retrieval_status: str
    degraded: bool
    fallback_used: bool
    citations: List[dict]
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
    executed_retrieval_requests: List[str]
    retrieval_blocked: bool
    grading_status: str
    validation_status: str
    grading_scope: Dict[str, Any]
    source_mapping_status: str
    expansion_delivery_status: str
    model_usage_records: Annotated[List[Any], operator.add]


def _source_key(citation):
    return (citation.get('fileId') or citation.get('file_id'), citation.get('chunk_id'),
            citation.get('content'))


def _source_context(citations):
    return '\n\n'.join(f"Document {c['source_id']} (Source: {c.get('fileName', 'Unknown File')}):\n{c['content']}"
                       for c in citations)


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
        existing = next((c for c in registry if _source_key(c) == _source_key(citation)), None)
        if existing is None:
            existing = {**citation, 'source_id': len(registry) + 1}
            registry.append(existing)
        if existing not in selected:
            selected.append(existing)
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
    placeholders = re.compile(r"\[图片\s*[:：][^\]]*\]")
    has_image = bool(placeholders.search(content)) or citation.get("content_type") == "image"
    if citation.get("content_type") == "image":
        return "", True
    return placeholders.sub("", content).strip(), has_image


def _guard_knowledge_answer(state: AgentState, response: AIMessage):
    """Conservatively render evidence when retrieval is incomplete or weak."""
    statuses = []
    citations = []
    seen = set()
    omitted_images = False
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage) or message.name != "search_knowledge_base":
            continue
        try:
            data = json.loads(message.content)
            status = data.get("retrieval_status", "RETRIEVAL_ERROR" if data.get("error") else "UNKNOWN")
            if status not in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE", "RETRIEVAL_ERROR", "UNKNOWN"}:
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
    incomplete = (state.get('retrieval_status') in {'RETRIEVAL_ERROR', 'UNKNOWN'}
                  or any(status in {"RETRIEVAL_ERROR", "UNKNOWN"} for status in statuses))
    if not citations:
        if "RETRIEVAL_ERROR" in statuses:
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
    generation_metadata = _answer_generation_metadata(response, generation_ran=generation_ran)
    candidate = response.content
    citations = _guard_knowledge_answer(state, response)
    statuses = []
    omitted_images = False
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage) or message.name != "search_knowledge_base":
            continue
        try:
            data = json.loads(message.content)
            status = data.get("retrieval_status", "RETRIEVAL_ERROR" if data.get("error") else "UNKNOWN")
            statuses.append(status if status in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE", "RETRIEVAL_ERROR", "UNKNOWN"} else "UNKNOWN")
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
    elif not statuses or "UNKNOWN" in statuses:
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
        self.tools = [search_knowledge_base]
        
        # 构建图
        workflow = StateGraph(AgentState)
        
        # 添加节点
        workflow.add_node("intent_analysis", self.analyze_intent)
        workflow.add_node("agent", self.agent_node)
        workflow.add_node("tools", self.run_tools)
        workflow.add_node("process_tool_output", self.process_tool_output) # Observation
        workflow.add_node("grading", self.grade_documents) # Scoring
        workflow.add_node("generate_chat", self.generate_chat)
        
        # 设置入口
        workflow.set_entry_point("intent_analysis")
        
        # 添加条件边: Intent Analysis -> (Agent or Chat)
        workflow.add_conditional_edges(
            "intent_analysis",
            self.route_based_on_intent,
            {
                "chat": "generate_chat",
                "qa": "agent"
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
        
        # Process Output -> Grading
        workflow.add_edge("process_tool_output", "grading")
        
        # Grading -> Agent (Loop back)
        workflow.add_edge("grading", "agent")
        
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
        return state["intent"]

    @observe("agent.agent_node")
    async def agent_node(self, state: AgentState, config: RunnableConfig):
        """Agent 思考节点"""
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
3. **响应反馈**：
   - 检索完成后，系统会评估检索结果的相关性。
   - 如果系统提示 "检索结果相关性评分较低"，**你必须** 尝试改写查询关键词，并再次调用工具。
   - 在重试时，请改写查询关键词或换一个切入点；检索参数由服务端统一控制，不要在工具参数里调整。
   - 不要重复使用相同的查询。尝试不同的切入点。
4. **生成回答**：
   - 当检索结果被系统接受，或者系统提示 "已达到最大重试次数" 时，请基于现有的所有搜索结果回答用户的问题。
   - 仅依据工具返回的文本证据作答；不要补充证据未明确支持的细节、单位、时间或推断。证据只支持部分答案时，说明缺少什么，不要猜测。检索片段未提供某信息时，只说明现有片段未提供，不要据此断言整份文档没有该信息。
   - 每个依据资料作出的事实陈述旁使用对应来源的编号引用，例如 `[1]`；编号取自工具结果中的 `source_id`（与 context 中的 `Document 1` 对应）。多个来源均支持时可分别引用，例如 `[1][2]`；不要编造来源或编号。
   - 如果多次尝试后仍无结果，请诚实告知用户。

注意事项：
- 始终使用中文回答。
- 优先使用工具获取信息，不要编造事实。
- 在思考过程中，请明确输出你的决策逻辑 (例如 "Thinking: 收到重试提示，尝试改写关键词为...")。
- **重要**：最终回答用户时，**不要**包含 "Thinking: ..." 这样的思考过程块。只输出最终的回答内容。思考过程仅用于中间步骤。""")
        
        messages = state["messages"]
        if state.get('retrieval_query') and not state.get('retrieval_attempt_count'):
            system_msg = SystemMessage(content=system_msg.content +
                '\n\n本轮首轮检索使用已解析的问题：' + state['retrieval_query'] +
                '\n最终回答仍针对用户原始问题；后续重试由现有证据评分流程决定。')
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
                quality_status = _reference_range_quality(response.content, len(state.get('source_registry') or state['citations']))
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
                event('memory.delivery', stage='agent_tool_and_answer',
                      preference_policy_applied=bool(state.get('preference_policy') and answer_possible))
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
        
        result = {"messages": [response], "agent_steps": steps, 'model_usage_records': usage_records}
        if escalation_used:
            result['router_escalations_used'] = escalation_used
        if not response.tool_calls:
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
        blocked = False
        attempt_count = state.get("retrieval_attempt_count", 0)
        last_attempt_id = state.get("retrieval_attempt_id")
        for tool_call in last_message.tool_calls:
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
                final_args = {"query": query, "kb_ids": state.get("kb_ids", [])}
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
                          tool_call_id=tool_call.get("id"), origin="agent_tool_call",
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
                'executed_retrieval_requests': executed, 'retrieval_blocked': blocked}

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
        # Only this tool round enters the grader. The answer registry remains
        # cumulative, while the grading scope stays bounded and explicit.
        batch = []
        for message in reversed(state["messages"]):
            if not isinstance(message, ToolMessage):
                break
            if message.name == 'search_knowledge_base':
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
                statuses.append(status if status in {'SUCCESS', 'NO_RESULTS', 'LOW_RELEVANCE', 'RETRIEVAL_ERROR', 'UNKNOWN'} else 'UNKNOWN')
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
                  'LOW_RELEVANCE' if 'LOW_RELEVANCE' in statuses else
                  'SUCCESS' if citations else 'NO_RESULTS')
        if status == 'NO_RESULTS' and degraded:
            status = 'UNKNOWN'
        context = '\n\n'.join(c for c in contexts if isinstance(c, str))
        return {'citations': citations, 'rag_context': context,
                'retrieval_status': status,
                'degraded': degraded, 'fallback_used': fallback_used,
                'source_mapping_status': 'UNKNOWN' if 'UNKNOWN' in mappings else 'VERIFIED',
                'expansion_delivery_status': next((value for value in
                    ('OMITTED', 'SEED_ONLY', 'PARTIAL', 'UNKNOWN', 'DELIVERED') if value in expansions), 'NOT_REQUESTED'),
                'agent_steps': [{'step': 'observation', 'content': f'检索到 {len(citations)} 条相关记录'}]}

    @observe("agent.grade_documents")
    async def grade_documents(self, state: AgentState, config: RunnableConfig):
        """文档评分节点，处理重试逻辑"""
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
        if quality_fallback or state.get('retrieval_status') in ('UNKNOWN', 'RETRIEVAL_ERROR'):
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

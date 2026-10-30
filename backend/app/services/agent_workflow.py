from app.services.observability import observe, event, snapshot, enabled, model_response, capture, observe_model_history
"""
基于 LangGraph 的 Agent 工作流 (Cyclic Graph)
"""

from typing import TypedDict, List, Annotated, Literal, Dict, Any
import operator
import json
import re
import os

from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.graph.message import add_messages

from app.services.llm import LLMService, is_unknown_provider_error, should_abort_provider_error, invoke_provider_model
from app.services.metrics import record_agent_retry_decision
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
    original_query: str # 用户原始问题
    current_plan: str # 当前执行计划
    
    # RAG 相关 (用于前端展示)
    rag_context: str
    retrieval_status: str
    citations: List[dict]
    retrieved_docs: List[Any] # 累计召回文档 (暂存)
    search_params: Dict[str, Any] # 前端传递的默认检索参数
    
    # 流程控制
    retry_count: int
    retrieval_attempt_id: str
    retrieval_attempt_count: int
    last_score: float # 最近一次评分
    execution_history: List[str] # 执行路径记录
    
    agent_steps: Annotated[List[dict], operator.add] # Append only
    final_system_prompt: str
    answer_mode: str
    
    # LLM 配置 (动态注入)
    model_config: Dict[str, Any]

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

    incomplete = any(status in {"RETRIEVAL_ERROR", "UNKNOWN"} for status in statuses)
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
    elif "LOW_RELEVANCE" in statuses or state.get("last_score", 0.0) < 0.8:
        prefix = "现有证据不足以完整回答，以下是相关原文："
    else:
        prefix = "无法确认以下资料是否足以完整回答，以下为检索到的原文："
    excerpts = [f"[{i}] {item['content'].strip()}" for i, item in enumerate(citations, 1)]
    response.content = prefix + "\n\n" + "\n\n".join(excerpts)
    if omitted_images:
        response.content += "\n\n另有图片资料，其内容需要进一步核验。"
    return citations


def _finalize_knowledge_answer(state: AgentState, response: AIMessage, *, include_mode=False):
    """Return a complete generated answer when retrieval is usable, else fallback."""
    candidate = response.content
    citations = _guard_knowledge_answer(state, response)
    statuses = []
    omitted_images = False
    raw_citations = []
    for message in state.get("messages", []):
        if not isinstance(message, ToolMessage) or message.name != "search_knowledge_base":
            continue
        try:
            data = json.loads(message.content)
            status = data.get("retrieval_status", "RETRIEVAL_ERROR" if data.get("error") else "UNKNOWN")
            statuses.append(status if status in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE", "RETRIEVAL_ERROR", "UNKNOWN"} else "UNKNOWN")
            raw_citations = data.get("citations", [])
            for item in raw_citations:
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
    if (os.getenv("RAG_GROUNDED_GENERATION", "true").lower() == "true"
            and citations and statuses and statuses[-1] == "SUCCESS"
            and all(status in {"SUCCESS", "NO_RESULTS"} for status in statuses)
            and state.get("last_score", 0.0) >= 0.8
            and isinstance(candidate, str) and candidate.strip() and finish_reason == "stop"):
        # The answer is not subjected to custom content/citation rules; all
        # Earlier empty searches are harmless when the latest search succeeds;
        # errors, unknown status, and low relevance still block generation.
        disclosure = "\n\n另有图片资料，其内容需要进一步核验。" if omitted_images else ""
        response.content = candidate + disclosure
        mode = "GROUNDED_GENERATION"
    event("agent.answer_validation", mode=mode, status=validation_status)
    event("qa.answer", answer_mode=mode, validation_status=validation_status)
    return (citations, mode) if include_mode else citations

class AgentWorkflow:
    def __init__(self):
        self.llm_service = LLMService()
        self.tools = [search_knowledge_base]
        self.tool_node = ToolNode(self.tools)
        
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
        # 获取最新的一条 HumanMessage
        last_message = state["messages"][-1]
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
        
        # 简单规则快速判断
        if len(content.strip()) < 10 and content.strip().lower() in ["你好", "在吗", "hello", "hi", "help", "test"]:
            return {
                "intent": "chat", 
                "agent_steps": [{
                    "step": "decision",
                    "content": "检测到闲聊意图 (Fast Path)"
                }],
                "original_query": content,
                "retry_count": 0,
                "execution_history": []
            }

        # LLM 意图判断
        prompt = f"""Analyze the user input: "{content}"
        
Classify intent as:
- "chat": Greeting, small talk, or general knowledge question that DOES NOT require external documentation.
- "qa": Question that likely requires retrieving specific information from a knowledge base.

Output ONLY one word: "chat" or "qa".
"""
        try:
            llm = self._get_llm_service(state)
            # Use streaming=False to avoid "No generations found in stream" error with some local models
            invocation_messages = [HumanMessage(content=prompt)]
            model = llm.get_chat_model(temperature=0.01, streaming=False)
            _observe_model_request("intent", invocation_messages, llm.model, provider_model=model)
            response = await invoke_provider_model(model, invocation_messages, config=config, stage="intent", model_name=llm.model)
            _observe_model_response("intent", response, llm.model)
            model_response(response, role="intent", model=llm.model)
            intent = response.content.strip().lower()
            if "qa" in intent or "knowledge" in intent:
                intent = "qa"
            else:
                intent = "chat"
        except Exception as e:
            if should_abort_provider_error(e):
                raise
            print(f"Intent analysis failed: {e}")
            event('agent.intent_fallback', status='FALLBACK', error_type=type(e).__name__)
            intent = "qa" # Fallback to QA
            
        new_steps = [{
            "step": "decision",
            "content": f"意图分析: {intent}"
        }]
        
        return {
            "intent": intent, 
            "agent_steps": new_steps,
            "original_query": content,
            "retry_count": 0,
            "execution_history": []
        }

    def route_based_on_intent(self, state: AgentState):
        return state["intent"]

    @observe("agent.agent_node")
    async def agent_node(self, state: AgentState, config: RunnableConfig):
        """Agent 思考节点"""
        # 准备 System Prompt
        system_msg = SystemMessage(content=f"""你是一个专业的智能助手，专门用于帮助用户从知识库中检索和解答问题。

你的工作流程如下：
1. **分析意图**：仔细分析用户的问题，确定需要查询什么信息。
2. **执行检索**：使用 `search_knowledge_base` 工具查询知识库。
   - 只需提供 `query` 参数。系统会自动处理知识库 ID 和检索配置。
   - 尽量使用准确的关键词。
   - 如果用户的问题比较复杂，可以拆解为多次查询。
3. **响应反馈**：
   - 检索完成后，系统会评估检索结果的相关性。
   - 如果系统提示 "检索结果相关性评分较低"，**你必须** 尝试改写查询关键词，并再次调用工具。
   - 在重试时，如果之前的尝试没有结果，请尝试在参数中降低 `score_threshold` (例如 0.1) 以获取更多结果。
   - 不要重复使用相同的查询。尝试不同的切入点。
4. **生成回答**：
   - 当检索结果被系统接受，或者系统提示 "已达到最大重试次数" 时，请基于现有的所有搜索结果回答用户的问题。
   - 严谨引用来源，回答要准确、完整。
   - 如果多次尝试后仍无结果，请诚实告知用户。

注意事项：
- 始终使用中文回答。
- 优先使用工具获取信息，不要编造事实。
- 在思考过程中，请明确输出你的决策逻辑 (例如 "Thinking: 收到重试提示，尝试改写关键词为...")。
- **重要**：最终回答用户时，**不要**包含 "Thinking: ..." 这样的思考过程块。只输出最终的回答内容。思考过程仅用于中间步骤。""")
        
        messages = state["messages"]
        invocation_messages = [system_msg] + messages
        
        llm = self._get_llm_service(state)
        # Use streaming=False to prevent crashes on local models
        model = llm.get_chat_model(streaming=False).bind_tools(self.tools)
        
        _observe_model_request("agent", invocation_messages, llm.model, provider_model=model)
        response = await invoke_provider_model(model, invocation_messages, config=config, stage="agent", model_name=llm.model)
        _observe_model_response("agent", response, llm.model)
        
        model_response(response, role="agent", model=llm.model)
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
        
        result = {"messages": [response], "agent_steps": steps}
        if not response.tool_calls:
            result["citations"], result["answer_mode"] = _finalize_knowledge_answer(state, response, include_mode=True)
        return result

    @observe("agent.run_tools")
    async def run_tools(self, state: AgentState, config: RunnableConfig):
        """自定义工具执行节点，注入配置参数"""
        messages = state["messages"]
        last_message = messages[-1]
        
        if not hasattr(last_message, "tool_calls") or not last_message.tool_calls:
            return {}
            
        tool_outputs = []
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
                final_args = {"query": agent_args.get("query"), "kb_ids": state.get("kb_ids", [])}
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

                requested = dict(search_params)
                if "rerank_enabled" in requested:
                    requested["use_rerank"] = requested["rerank_enabled"]
                try:
                    effective_args = {
                        name: field.default
                        for name, field in search_knowledge_base.args_schema.model_fields.items()
                        if not field.is_required()
                    }
                    effective_args.update(final_args)
                    scope_hash = hashlib.sha256(json.dumps(final_args["kb_ids"], sort_keys=True).encode("utf-8")).hexdigest()
                    event("agent.actual_query", retrieval_attempt_id=last_attempt_id,
                          tool_call_id=tool_call.get("id"), origin="agent_tool_call",
                          retrieval_attempt_count=attempt_count, query=snapshot(final_args.get("query")) if enabled() else None,
                          kb_scope_sha256=scope_hash, retry_count=state.get("retry_count", 0),
                          requested_parameters=safe_settings(requested),
                          actual_parameters=safe_settings(effective_args))
                    if not final_args["kb_ids"]:
                        result = json.dumps({"retrieval_status": "NO_RESULTS", "context": "No knowledge bases found in the system.", "citations": []})
                    else:
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
                
                tool_outputs.append(
                    ToolMessage(
                        content=str(result),
                        name=tool_call["name"],
                        tool_call_id=tool_call["id"],
                    )
                )
        
        return {"messages": tool_outputs, "retrieval_attempt_id": last_attempt_id,
                "retrieval_attempt_count": attempt_count}

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
        messages = state["messages"]
        last_message = messages[-1] # ToolMessage
        
        if not isinstance(last_message, ToolMessage):
            return {}

        new_steps = []
        citations = []
        rag_context = ""
        retrieval_status = "UNKNOWN"
        
        try:
            # 解析工具返回的 JSON
            data = json.loads(last_message.content)
            rag_context = data.get("context", "")
            citations = data.get("citations", [])
            retrieval_status = data.get("retrieval_status", "UNKNOWN")
            if retrieval_status not in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE", "RETRIEVAL_ERROR", "UNKNOWN"}:
                retrieval_status = "UNKNOWN"
            
            # 记录 Observation
            new_steps.append({
                "step": "observation",
                "content": f"检索到 {len(citations)} 条相关记录"
            })
            
        except Exception as e:
            print(f"Error parsing tool output: {e}")
            rag_context = str(last_message.content) # Fallback
            new_steps.append({
                "step": "observation",
                "content": f"检索出错或格式异常: {str(e)}"
            })
            
        return {
            "citations": citations, # 更新 citations
            "rag_context": rag_context,
            "retrieval_status": retrieval_status,
            "agent_steps": new_steps
        }

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
                model = llm.get_chat_model(temperature=0.1)
                _observe_model_request("quality", invocation_messages, llm.model, provider_model=model)
                response = await invoke_provider_model(model, invocation_messages, config=config, stage="quality", model_name=llm.model)
                _observe_model_response("quality", response, llm.model)
                model_response(response, role="quality", model=llm.model)
                try:
                    score_raw = capture(_visible_content(response.content, assistant=True),
                                        content_role="provider_response") if enabled() else None
                except Exception:
                    score_raw = None
                score_str = response.content.strip()
                import re
                match = re.search(r"0\.\d+|1\.0|0|1", score_str)
                score_parsed = float(match.group()) if match else None
                score = score_parsed if match else 0.5
                # Preserve legacy grading, but do not claim a valid score when
                # a malformed response merely contains a numeric substring.
                quality_fallback = re.fullmatch(r'(?:0(?:\.\d+)?|1(?:\.0+)?)', score_str) is None
                score_valid = not quality_fallback
                if quality_fallback:
                    quality_reason = 'INVALID_FORMAT'
            except Exception as e:
                if should_abort_provider_error(e):
                    raise
                print(f"Grading failed: {e}")
                event('agent.quality_fallback', status='FALLBACK', error_type=type(e).__name__)
                quality_fallback = True
                score_valid = False
                quality_reason = "PROVIDER_FAILURE"
                score = 0.5 # Fallback
            
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
        if citations and not quality_fallback and score < 0.8:
            event("agent.evidence_outcome", status="LOW_RELEVANCE", score=score, retry_count=retry_count)
        
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
                    "last_score": score,
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
                    "last_score": score,
                    "messages": messages,
                    "agent_steps": steps
                }
        
        event("agent.retry_decision", action="NOT_SCHEDULED", previous_attempt_id=state.get("retrieval_attempt_id"),
              decision="NOT_SCHEDULED", reason_code="SCORE_AT_OR_ABOVE_THRESHOLD", retry_count=retry_count,
              next_action="ANSWER", retrieval_attempt_count=state.get("retrieval_attempt_count", 0))
        return {"last_score": score, "agent_steps": steps}

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
        invocation_messages = [SystemMessage(content=system_prompt)] + messages
        _observe_model_request("agent_chat", invocation_messages, llm.model, provider_model=model)
        response = await invoke_provider_model(model, invocation_messages, config=config, stage="agent_chat", model_name=llm.model)
        _observe_model_response("agent_chat", response, llm.model)
        model_response(response, role='agent_chat', model=llm.model)
        
        return {
            "messages": [response],
            "agent_steps": [{"step": "response", "content": "生成闲聊回复"}]
        }

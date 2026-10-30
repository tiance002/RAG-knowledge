from app.services.observability import native_span, observe, event as trace_event, candidates as observe_candidates, snapshot, enabled, fingerprint
"""
对话服务 (Agent 实现)

集成混合检索（向量 + BM25）、分批 Rerank、上下文扩展
"""

from typing import List, Dict, Any, AsyncGenerator, Tuple
from app.schemas import Message, RecallResult
from app.services.llm import LLMService, is_unknown_provider_error, should_abort_provider_error, provider_error_payload
from app.services.embedding import EmbeddingService
from app.services.vector_store import VectorStoreService
from app.services.rerank import RerankService, batch_rerank, expand_context
from app.services.retrieval import get_retrieval_service
from app.services.agent_workflow import AgentWorkflow
from langchain_core.messages import HumanMessage, AIMessage
from sqlmodel import Session, select
from app.core.database import engine
from app.models import KnowledgeBase, CustomModel, FileDocument, FileStatus, DocumentChunk, ChunkIndex


import os
import base64
import time
import json
import re
from app.core.config import get_settings

settings = get_settings()


def _scope_preflight(kb_ids: List[str]):
    """Return a conclusive fileless or indexing state, otherwise defer."""
    try:
        with Session(engine) as session:
            for kb_id in set(kb_ids):
                kb = session.get(KnowledgeBase, kb_id)
                if not kb or kb.is_deleted or kb.chunk_count != 0:
                    return None
            if session.exec(select(ChunkIndex.id).where(ChunkIndex.kb_id.in_(kb_ids)).limit(1)).first():
                return None
            files = session.exec(select(FileDocument).where(FileDocument.kb_id.in_(kb_ids))).all()
            if not files:
                return "NO_RESULTS"
            pending = {FileStatus.PENDING, FileStatus.PARSING, FileStatus.EMBEDDING, FileStatus.PENDING_CONFIRM}
            if not all(file.status in pending for file in files):
                return None
            ids = [file.id for file in files]
            files_with_chunks = set(session.exec(select(DocumentChunk.file_id).where(DocumentChunk.file_id.in_(ids))).all())
            if any((file.id in files_with_chunks) != (file.status == FileStatus.PENDING_CONFIRM) for file in files):
                return None
            return "INDEXING_PENDING"
    except Exception:
        # An unavailable or inconsistent database is not proof of emptiness.
        return None

# 图片处理策略：caption = 先用 VLM 转文字再参与回答；off = 完全不使用图片
IMAGE_MODE = os.environ.get("AMRAG_IMAGE_MODE", "caption").lower()
# 单次回答最多消费几张图片，防止图表题把整页图都塞进来导致超长与高延迟
MAX_IMAGES = int(os.environ.get("AMRAG_MAX_IMAGES", "4"))
# 图片描述缓存目录，避免同一张图在多题、多次实验中重复调用 VLM（省时省钱）
_CAPTION_DIR = os.path.join(settings.IMAGE_DIR, "_captions")


async def _load_or_caption(vlm, image_key: str, full_path: str) -> str:
    """优先读磁盘缓存，未命中时调用 VLM 生成描述并落盘。"""
    import hashlib

    digest = hashlib.md5(image_key.encode("utf-8")).hexdigest()
    cache_path = os.path.join(_CAPTION_DIR, f"{digest}.txt")
    if os.path.exists(cache_path):
        try:
            with open(cache_path, "r", encoding="utf-8") as fh:
                cached = fh.read().strip()
            if cached:
                return cached
        except OSError:
            pass
    text = await vlm.describe_image(full_path)
    if text and text != "图片解析失败":
        os.makedirs(_CAPTION_DIR, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return text.strip()
    return ""


class ChatService:
    """对话服务"""

    def __init__(self):
        self.llm_service = LLMService()
        self.embedding_service = EmbeddingService()
        self.vector_service = VectorStoreService()
        self.retrieval_service = get_retrieval_service()
        self.agent_workflow = AgentWorkflow()

    async def _retrieve_with_new_service(
        self,
        session: Session,
        query: str,
        kb_ids: List[str],
        top_k: int,
        score_threshold: float,
        search_mode: str,
        vector_weight: float,
        bm25_weight: float,
        rerank_enabled: bool,
        rerank_score_threshold: float,
        rerank_model_id: str,
        rerank_top_k: int,
        context_window: int,
    ) -> Tuple[List[Dict], Dict]:
        """
        使用新的统一检索服务进行检索

        流程：
        1. 混合检索（向量 + BM25）
        2. 分批 Rerank
        3. 上下文扩展
        """
        # 1. 混合检索
        results, metrics = await self.retrieval_service.search(
            session=session,
            query=query,
            kb_ids=kb_ids,
            search_mode=search_mode,
            top_k=top_k,
            score_threshold=score_threshold,
            vector_weight=vector_weight,
            bm25_weight=bm25_weight,
            use_cache=True,
            log_retrieval=True,
        )

        # 2. Rerank
        if rerank_enabled and results:
            rerank_model = None
            if rerank_model_id:
                rerank_model = session.get(CustomModel, rerank_model_id)

            rerank_service = RerankService(session)
            original_results = results
            results = batch_rerank(
                query=query,
                candidates=results,
                rerank_service=rerank_service,
                model=rerank_model,
                top_k=rerank_top_k,
                score_threshold=rerank_score_threshold
            )
            metrics["rerank_count"] = len(results)
            if not results:
                # Empty can mean either unavailable reranking or filtered
                # candidates. Keep the evidence and report uncertainty.
                results = original_results
                metrics["retrieval_status"] = "UNKNOWN"
                trace_event('rerank.fallback', execution_status='UNKNOWN', reason_code='rerank_fallback',
                            fallback_score_origin='original_retrieval_score', input_ids=[],
                            output_ids=[r.get('id', r.get('chunk_id')) for r in results])

        # 3. 上下文扩展
        if results and context_window > 0:
            results = expand_context(
                top_chunks=results,
                session=session,
                window_size=context_window,
                include_source_members=True,
            )
            metrics["expanded"] = True

        return results, metrics

    async def _chat_stream_agent(
        self,
        messages: List[Message],
        kb_ids: List[str],
        top_k: int,
        score_threshold: float,
        model_id: str,
        rerank_enabled: bool,
        rerank_score_threshold: float,
        rerank_model_id: str,
        search_mode: str = "hybrid",
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
        rerank_top_k: int = 20,
        context_window: int = 1,
    ) -> AsyncGenerator[Tuple[str, Dict[str, Any]], None]:
        """
        Agent 模式流式对话 (LangGraph 实现)
        """
        # 1. 构造初始状态
        langchain_messages = []
        for msg in messages:
             if msg.role.value == "user":
                 langchain_messages.append(HumanMessage(content=msg.content))
             elif msg.role.value == "assistant":
                 langchain_messages.append(AIMessage(content=msg.content))
        
        search_params = {
            "top_k": top_k,
            "score_threshold": score_threshold,
            "search_mode": search_mode,
            "rerank_enabled": rerank_enabled,
            "rerank_score_threshold": rerank_score_threshold,
            "rerank_model_id": rerank_model_id,
            "rerank_top_k": rerank_top_k,
            "vector_weight": vector_weight,
            "bm25_weight": bm25_weight,
            "context_window": context_window
        }

        # 确保初始状态包含必要字段
        last_user_content = langchain_messages[-1].content if langchain_messages else ""

        initial_state = {
            "messages": langchain_messages,
            "kb_ids": kb_ids,
            "search_params": search_params,
            "retry_count": 0,
            "citations": [],
            "agent_steps": [],
            "original_query": last_user_content,
            "intent": "",
            "current_plan": "",
            "rag_context": "",
            "retrieved_docs": [],
            "execution_history": [],
            "last_score": 0.0,
            "final_system_prompt": "",
            "model_config": model_config
        }

        print(f"DEBUG_CHAT: Starting LangGraph Agent with params: {search_params}")

        try:
            async for event in self.agent_workflow.app.astream_events(initial_state, version="v1"):
                kind = event["event"]
                name = event["name"]
                data = event["data"]
                
                # 1. 处理流式 Token (Answer Chunk)
                if kind == "on_chat_model_stream":
                    # 过滤掉 intent_analysis 和 grading 的输出
                    node_name = event.get("metadata", {}).get("langgraph_node")
                    if node_name in ["agent", "generate_chat"]:
                         chunk = data["chunk"]
                         if chunk.content:
                             yield ("answer_chunk", {"content": chunk.content})

                # 2. 处理节点状态更新 (Steps & Citations)
                elif kind == "on_chain_end":
                    node_name = event.get("metadata", {}).get("langgraph_node")
                    if node_name in ["intent_analysis", "agent", "process_tool_output", "grading", "generate_chat"]:
                        outputs = data["output"]
                        if isinstance(outputs, dict):
                            # 发送步骤
                            if "agent_steps" in outputs:
                                for step in outputs["agent_steps"]:
                                     yield ("agent_thought", step)
                            
                            # 发送引用
                            if "citations" in outputs:
                                 observe_candidates("qa.citations", outputs["citations"])
                                 yield ("rag_result", {"citations": outputs["citations"]})
                
                # 3. 处理工具开始 (Action)
                elif kind == "on_tool_start":
                    if name == "search_knowledge_base":
                        yield ("agent_thought", {
                            "step": "action", 
                            "content": f"调用检索工具..." 
                        })

            yield ("done", {"usage": {}})
            
        except Exception as e:
            yield ("error", provider_error_payload(e, stage="agent_workflow"))

    async def _chat_stream_normal(
        self,
        messages: List[Message],
        kb_ids: List[str],
        top_k: int,
        score_threshold: float,
        model_id: str,
        rerank_enabled: bool,
        rerank_score_threshold: float,
        rerank_model_id: str,
        search_mode: str = "hybrid",
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
        rerank_top_k: int = 20,
        context_window: int = 1,
        use_rewrite: bool = False,
    ) -> AsyncGenerator[Tuple[str, Dict[str, Any]], None]:
        """
        普通模式：
        1. 如果没有指定知识库 -> 直接 LLM 对话
        2. 如果指定了知识库 -> RAG (混合检索 -> Rerank -> 上下文扩展 -> Generate)
        """
        total_start_time = time.time()
        print(f"DEBUG_CHAT: Start _chat_stream_normal. KBs: {kb_ids}, Mode: {search_mode}, TopK: {top_k}, Rewrite: {use_rewrite}", flush=True)

        # Get user message
        user_message = None
        for msg in reversed(messages):
            if msg.role.value == "user":
                user_message = msg.content
                break

        if not user_message:
            yield ("error", {"message": "没有找到用户消息"})
            return

        # Prepare LLM Service
        current_llm_service = self.llm_service
        actual_model_id = model_id
        if model_id:
            with Session(engine) as session:
                custom_llm = session.get(CustomModel, model_id)
                if custom_llm:
                    current_llm_service = LLMService(
                        base_url=custom_llm.base_url,
                        api_key=custom_llm.api_key,
                        model=custom_llm.model_name
                    )
                    actual_model_id = custom_llm.model_name

        # Case 1: No Knowledge Base selected -> Direct Chat
        if not kb_ids:
            from app.services.agent_workflow import explicitly_requests_sources
            if explicitly_requests_sources(user_message):
                yield ("answer_chunk", {"content": "请先选择知识库，再根据资料回答。"})
                yield ("done", {"usage": {}})
                return
            yield ("agent_thought", {
                "step": "response",
                "content": "未指定知识库，正在直接生成回答..."
            })

            chat_messages = [{"role": msg.role.value, "content": msg.content} for msg in messages]
            try:
                async for chunk in current_llm_service.generate_stream(
                    messages=chat_messages,
                    model_id=actual_model_id,
                ):
                    yield ("answer_chunk", {"content": chunk})
            except Exception as e:
                trace_event("qa.error", status="UNKNOWN" if is_unknown_provider_error(e) else "FAILED", error_type=type(e).__name__)
                yield ("error", provider_error_payload(e, stage="answer_stream", unknown=True))
                return

            total_duration = time.time() - total_start_time
            yield ("agent_thought", {
                "step": "response",
                "content": "回答生成完成",
                "duration": total_duration,
                "cost": total_duration
            })
            yield ("done", {"usage": {}})
            return

        # Case 2: Knowledge Base selected -> RAG with new retrieval service
        
        # Query Rewrite
        search_query = user_message
        if use_rewrite:
            yield ("agent_thought", {
                "step": "thinking",
                "content": "正在进行问题改写..."
            })
            try:
                rewritten_query = await current_llm_service.rewrite_query(user_message)
                if rewritten_query:
                    search_query = rewritten_query
                    yield ("agent_thought", {
                        "step": "thinking",
                        "content": f"问题已改写为: {rewritten_query}"
                    })
            except Exception as e:
                if should_abort_provider_error(e):
                    yield ("error", provider_error_payload(e, stage="rewrite"))
                    return
                print(f"DEBUG_CHAT: Rewrite failed: {type(e).__name__}")
                # Fallback to original query
        
        yield ("agent_thought", {
            "step": "thinking",
            "content": f"正在使用 {search_mode} 模式从 {len(kb_ids)} 个知识库中检索 (Top K: {top_k})..."
        })

        all_results = []
        original_results = []

        with Session(engine) as session:
            try:
                # 使用新的检索服务
                all_results, metrics = await self._retrieve_with_new_service(
                    session=session,
                    query=search_query,
                    kb_ids=kb_ids,
                    top_k=top_k,
                    score_threshold=score_threshold,
                    search_mode=search_mode,
                    vector_weight=vector_weight,
                    bm25_weight=bm25_weight,
                    rerank_enabled=rerank_enabled,
                    rerank_score_threshold=rerank_score_threshold,
                    rerank_model_id=rerank_model_id,
                    rerank_top_k=rerank_top_k,
                    context_window=context_window,
                )

                print(f"DEBUG_CHAT: Retrieved {len(all_results)} results, metrics: {metrics}")

            except Exception as e:
                trace_event("qa.error", status="UNKNOWN" if is_unknown_provider_error(e) else "FAILED", error_type=type(e).__name__)
                yield ("error", provider_error_payload(e, stage="retrieval"))
                return

        yield ("agent_thought", {
            "step": "action",
            "content": f"检索完成，共找到 {len(all_results)} 条相关记录 (vector={metrics.get('vector_count', 0)}, bm25={metrics.get('bm25_count', 0)})。",
        })

        final_results = all_results

        # Sort
        if rerank_enabled and final_results and final_results[0].get("rerank_score") is not None:
            final_results.sort(key=lambda x: x.get("rerank_score", 0), reverse=True)
        else:
            final_results.sort(key=lambda x: x.get("score", 0), reverse=True)

        final_results = final_results[:rerank_top_k if rerank_enabled else top_k]

        # Send RAG Result
        def format_citation(r, content):
            from pathlib import PurePath
            import re
            extension = PurePath(r.get('file_name') or '').suffix.lower()
            metadata = r.get('source_metadata') or {}
            if extension == '.pdf' and r.get('page_number'):
                location = f"第 {r['page_number']} 页"
            elif extension in {'.xlsx', '.xls'}:
                sheet = metadata.get('sheet') or metadata.get('sheet_name')
                if not sheet:
                    match = re.match(r'^\s*\[工作表:\s*([^\]\r\n]+)\]', content)
                    sheet = match.group(1).strip() if match else None
                location = f"工作表: {sheet}" if sheet else "未知位置"
            else:
                location = r.get('heading_text') or "未知位置"
            return {
                "chunk_id": r.get("id"),
                "score": r.get("score", 0),
                "rerank_score": r.get("rerank_score"),
                "vector_score": r.get("vector_score"),
                "bm25_score": r.get("bm25_score"),
                "content": r.get("content", ""),
                "source_metadata": r.get("source_metadata", {}),
                "fileName": r.get("file_name", "未知"),
                "kb_name": r.get("kb_name", ""),
                "kb_id": r.get("kb_id"),
                "location": location,
                "fileId": r.get("file_id"),
                "image_path": r.get("image_path"),
                "imageUrl": f"/static/images/{r.get('image_path')}" if r.get("image_path") else None,
                "heading_text": r.get("heading_text"),
                "heading_level": r.get("heading_level"),
            }

        with native_span("context.build", as_type="chain"):
            observe_candidates("qa.citations", final_results)
            from app.services.context_builder import build_chat_context
            # Process-level experiment selector, never accepted from user fields.
            # Agent has a different builder and retains its existing behavior.
            citations, context_summary = build_chat_context(final_results, format_citation,
                strategy=os.getenv('RAG_CHAT_CONTEXT_STRATEGY', 'EXPANDED_CONTENT'))
            omitted_images = context_summary['omitted_images']
            omitted_expanded = context_summary['omitted_expanded']
            trace_event('context.build.summary', **context_summary)
            trace_event('context.delivery', delivery_target='citation_selection', is_final_model_delivery=False,
                        changes=context_summary['changes'], input_ids=[r.get('id') for r in final_results],
                        output_ids=[c.get('chunk_id') for c in citations],
                        context_strategy=context_summary['context_strategy'],
                        source_mapping_status=context_summary['source_mapping_status'],
                        rule_version='source-linked-citation-selection-v2')

        yield ("rag_result", {
            "citations": citations,
            "original_citations": []
        })

        status = metrics.get("retrieval_status", "UNKNOWN")
        if omitted_expanded and status in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE"}:
            status = "UNKNOWN"
        if status not in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE", "RETRIEVAL_ERROR", "UNKNOWN"}:
            status = "UNKNOWN"
        from app.services.grounded_generation import grounded_generation_enabled, generate_grounded_candidate
        generation_usage = None
        generation_status = 'NOT_RUN'
        spreadsheet_context = None
        pdf_context = None
        if citations and status == 'SUCCESS' and grounded_generation_enabled():
            source_started = time.monotonic()
            source_status = 'UNAVAILABLE'
            try:
                import asyncio
                from app.services.spreadsheet_grounding import prepare_spreadsheet_grounding
                def prepare_spreadsheet():
                    with Session(engine) as source_session:
                        return prepare_spreadsheet_grounding(source_session, citations, kb_ids, user_message)
                spreadsheet_context = await asyncio.to_thread(prepare_spreadsheet)
                source_status = 'VERIFIED' if spreadsheet_context is not None else 'UNAVAILABLE'
            except Exception:
                spreadsheet_context = None
                source_status = 'ERROR'
            if spreadsheet_context is not None:
                proof = spreadsheet_context.proof
                citation = citations[spreadsheet_context.citation_index - 1]
                metadata = dict(citation.get('source_metadata') or {})
                metadata['spreadsheet_table_verification'] = {
                    'source_sha256': proof['source_sha256'], 'sheet': proof['sheet'],
                    'row': proof['row'], 'label_col': proof['label_col'],
                    'value_col': proof['value_col'], 'footnotes': proof['footnotes'],
                }
                citation['source_metadata'] = metadata
                yield ('rag_result', {'citations': citations, 'original_citations': []})
            trace_event('qa.source_verification', status=source_status,
                        reason_code=('provider_or_parser_error' if source_status == 'ERROR' else
                                     'not_applicable' if not any(str(c.get('fileName', '')).lower().endswith('.xlsx') for c in citations) else
                                     'unknown' if source_status == 'UNAVAILABLE' else None),
                        source_type='native_source_expansion',
                        source_sha256=spreadsheet_context.proof.get('source_sha256') if spreadsheet_context is not None else None,
                        position={k: spreadsheet_context.proof.get(k) for k in ('sheet', 'row', 'label_col', 'value_col')} if spreadsheet_context is not None else None,
                        duration_ms=round((time.monotonic() - source_started) * 1000, 2),
                        evidence_count=1 if spreadsheet_context is not None else 0)
            if os.getenv('RAG_PDF_TABLE_GROUNDING', 'false').lower() == 'true':
                started_expansion = time.monotonic()
                original_count = len(citations)
                expansion_status = 'UNAVAILABLE'
                added = []
                try:
                    import asyncio
                    from app.services.pdf_table_grounding import prepare_pdf_table_grounding, build_pdf_supplementary_citations
                    def prepare_pdf_expansion():
                        with Session(engine) as source_session:
                            return prepare_pdf_table_grounding(source_session, citations, kb_ids, user_message)
                    pdf_context = await asyncio.to_thread(prepare_pdf_expansion)
                    if pdf_context is not None:
                        added = build_pdf_supplementary_citations(pdf_context, citations)
                        if len(added) != 2:
                            pdf_context = None
                            added = []
                        else:
                            expansion_status = 'VERIFIED'
                            citations = citations + added
                            yield ('rag_result', {'citations': citations, 'original_citations': []})
                except Exception:
                    pdf_context = None
                    added = []
                    expansion_status = 'ERROR'
                trace_event('qa.source_expansion', status=expansion_status,
                            reason_code='provider_or_parser_error' if expansion_status == 'ERROR' else 'unknown' if expansion_status == 'UNAVAILABLE' else None,
                            source_type='native_source_expansion',
                            duration_ms=round((time.monotonic() - started_expansion) * 1000, 2),
                            original_count=original_count, added_count=len(added),
                            locators=[{'file_id': x['fileId'], 'page': x['source_metadata']['page'],
                                       'table_bbox': x['source_metadata']['table_bbox']} for x in added])
            try:
                chat_messages = [{"role": msg.role.value, "content": msg.content} for msg in messages]
                generation_options = {'model_id': actual_model_id}
                if spreadsheet_context is not None:
                    generation_options['spreadsheet_context'] = spreadsheet_context
                if pdf_context is not None:
                    generation_options['pdf_context'] = pdf_context
                candidate, finish_reason, usage = await generate_grounded_candidate(
                    current_llm_service, chat_messages, citations, **generation_options)
                generation_usage = usage
                generation_status = ('TRUNCATED' if finish_reason == 'length' else
                                     'EMPTY' if not candidate.strip() else
                                     'COMPLETED' if finish_reason == 'stop' else 'UNKNOWN')
            except Exception as exc:
                # A provider call with an unknown outcome must not be replayed or
                # represented as a successfully generated answer.
                trace_event('qa.error', status='UNKNOWN' if is_unknown_provider_error(exc) else 'FAILED', error_type=type(exc).__name__)
                yield ('error', provider_error_payload(exc, stage='grounded_answer'))
                return
            # Owner disabled answer-content rules. A complete, nonempty model
            # response with usable retrieval evidence is returned as generated;
            # attribution and semantic correctness remain externally assessed.
            trace_event('qa.validation', status='NOT_RUN', reason_codes=['owner_disabled'])
            if generation_status == 'COMPLETED':
                trace_event('qa.answer', answer_mode='GROUNDED_GENERATION', validation_status='NOT_RUN',
                            semantic_relevance='UNKNOWN', answer_completeness='UNKNOWN')
                disclosure = '\n\n另有图片资料，其内容需要进一步核验。' if omitted_images else ''
                yield ('answer_chunk', {'content': candidate + disclosure})
                yield ('done', {'usage': usage or {}, 'retrieval_status': status, 'answer_mode': 'GROUNDED_GENERATION', 'generation_status': generation_status})
                return
        if citations:
            if status in {"RETRIEVAL_ERROR", "UNKNOWN"}:
                prefix = "检索未完全成功，以下是已找到的原文，无法确认是否足以完整回答："
            elif status == "LOW_RELEVANCE":
                prefix = "现有证据相关性不足，以下是检索到的原文："
            else:
                prefix = "目前无法确认问题的答案；以下是检索到的原文，证据是否充分尚待核对："
            # Complete chunks remain available in rag_result. Keep only one
            # short excerpt in the answer, which avoids overwhelming an
            # unanswerable question with several unrelated source passages.
            excerpts = []
            omitted = len(citations) > 1
            for i, citation in enumerate(citations[:1], 1):
                content = citation['content'].strip()
                shortened = len(content) > 160
                omitted = omitted or shortened
                excerpts.append(f"[{i}] {content[:160]}" + ("…" if shortened else ""))
            answer = prefix + "\n\n" + "\n\n".join(excerpts)
            if omitted:
                answer += "\n\n以上仅展示部分摘录；请查看引用中的完整资料。"
        elif omitted_images and status in {"SUCCESS", "NO_RESULTS", "LOW_RELEVANCE"}:
            answer = "检索到图片资料，但没有可用于回答的文本证据；图片内容需要进一步核验。"
        elif status == "LOW_RELEVANCE":
            answer = "找到的候选资料相关性不足，无法依据现有证据回答。"
        elif status == "NO_RESULTS":
            answer = "未在所选知识库中找到可用于回答的相关资料。"
        elif status == "RETRIEVAL_ERROR":
            answer = "知识库检索发生错误，无法确认是否存在相关资料，请稍后重试。"
        else:
            answer = "知识库检索状态不确定，无法确认是否存在相关资料，请稍后重试。"
        if omitted_images and (citations or status in {"RETRIEVAL_ERROR", "UNKNOWN"}):
            answer += "\n\n另有图片资料，其内容需要进一步核验。"
        mode = 'EXTRACTIVE_FALLBACK' if citations else ('RETRIEVAL_ERROR' if status == 'RETRIEVAL_ERROR' else 'UNKNOWN' if status == 'UNKNOWN' else 'INSUFFICIENT_EVIDENCE')
        trace_event('qa.answer', answer_mode=mode, retrieval_status=status, generation_status=generation_status)
        yield ("answer_chunk", {"content": answer})
        yield ("done", {"usage": generation_usage or {}, "retrieval_status": status, 'answer_mode': mode, 'generation_status': generation_status})

    async def _chat_stream_langgraph(
        self,
        messages: List[Message],
        kb_ids: List[str],
        top_k: int,
        score_threshold: float,
        model_id: str,
        **kwargs
    ) -> AsyncGenerator[Tuple[str, Dict[str, Any]], None]:
        """
        基于 LangGraph 的 Agent 模式流式对话
        """
        from app.services.agent_workflow import AgentWorkflow
        from langchain_core.messages import HumanMessage, AIMessage
        
        # 1. 初始化 Workflow
        workflow = AgentWorkflow()
        
        # 2. 构造初始状态
        # 限制历史对话长度，防止上下文爆炸
        # 只保留最近的 N 轮对话 (N * 2 条消息)
        max_history_msgs = settings.MAX_CHAT_HISTORY_ROUNDS * 2
        effective_messages = messages[-max_history_msgs:] if len(messages) > max_history_msgs else messages
        
        langchain_messages = []
        for msg in effective_messages:
            if msg.role.value == "user":
                langchain_messages.append(HumanMessage(content=msg.content))
            elif msg.role.value == "assistant":
                langchain_messages.append(AIMessage(content=msg.content))
        
        # 1.5 获取模型配置
        model_config = {}
        if model_id:
            with Session(engine) as session:
                custom_llm = session.get(CustomModel, model_id)
                if custom_llm:
                    model_config = {
                        "base_url": custom_llm.base_url,
                        "api_key": custom_llm.api_key,
                        "model_id": custom_llm.model_name
                    }

        # 构造检索参数
        search_params = {
            "top_k": top_k,
            "score_threshold": score_threshold,
            "search_mode": kwargs.get("search_mode", "hybrid"),
            "rerank_enabled": kwargs.get("rerank_enabled", False),
            "rerank_score_threshold": kwargs.get("rerank_score_threshold", 0.0),
            "rerank_model_id": kwargs.get("rerank_model_id"),
            "vector_weight": kwargs.get("vector_weight", 0.7),
            "bm25_weight": kwargs.get("bm25_weight", 0.3),
            "rerank_top_k": kwargs.get("rerank_top_k", 20),
            "context_window": kwargs.get("context_window", 1),
        }

        initial_state = {
            "messages": langchain_messages,
            "kb_ids": kb_ids,
            "search_params": search_params,
            "retry_count": 0,
            "agent_steps": [],
            "intent": "qa", # Default, will be analyzed
            "original_query": langchain_messages[-1].content if langchain_messages else "",
            "execution_history": [],
            "citations": [],
            "retrieved_docs": [],
            "last_score": 0.0,
            "rag_context": "",
            "final_system_prompt": "",
            "model_config": model_config
        }
        
        # 3. 执行并流式输出
        last_citations = []
        has_streamed = False
        last_retrieval_status = None
        last_answer_mode = None
        
        try:
            async for event in workflow.app.astream_events(initial_state, version="v1"):
                kind = event["event"]
                name = event["name"]
                data = event["data"]
                
                # 安全检查：data 必须是字典
                if not isinstance(data, dict):
                    continue

                # 3.1 监听 LLM 生成 (流式回答)
                if kind == "on_chat_model_stream":
                    # 过滤掉 intent_analysis 和 grading 的输出
                    node_name = event.get("metadata", {}).get("langgraph_node")
                    
                    # Agent output must pass the evidence guard before reaching SSE.
                    if node_name == "generate_chat":
                        chunk = data.get("chunk")
                        if chunk and chunk.content:
                            yield ("answer_chunk", {"content": chunk.content})
                            has_streamed = True
                
                # 3.2 监听 Node 输出 (Thinking/Decision/Action)
                elif kind == "on_chain_end":
                    node_name = event.get("metadata", {}).get("langgraph_node")
                    
                    # 监听所有关键节点的输出
                    if node_name in ["intent_analysis", "agent", "process_tool_output", "generate_chat"]:
                        output = data.get("output")
                        if output and isinstance(output, dict):
                            if node_name == 'agent' and output.get('answer_mode'):
                                last_answer_mode = output['answer_mode']
                            if node_name == "process_tool_output" and output.get("retrieval_status"):
                                last_retrieval_status = output["retrieval_status"]
                                yield ("retrieval_status", {"status": last_retrieval_status})
                            # 处理 agent_steps
                            steps = output.get("agent_steps", [])
                            if steps:
                                for step in steps:
                                     yield ("agent_thought", step)
                        
                            # NEW: 如果没有流式输出过 (streaming=False)，在这里发送完整内容
                            # 仅针对 agent 和 generate_chat 节点
                            if node_name in ["agent", "generate_chat"]:
                                # 检查是否已经流式输出过 (通过判断是否有 chunk 发送)
                                # 注意：如果是流式模式，on_chat_model_stream 应该已经发送了 answer_chunk
                                # 但为了保险起见，我们可以检查 has_streamed 标记
                                # 然而，has_streamed 在这里可能无法精确控制（因为是 loop）
                                # 更好的方式是：如果 streaming=False (我们可以从 model_config 判断，或者假设如果没有 stream event 就是 False)
                                # 简单起见，利用 has_streamed 变量
                                if not has_streamed:
                                    messages = output.get("messages", [])
                                    if messages:
                                        last_msg = messages[-1]
                                        # 检查是否为 AIMessage 且有 content
                                        content = getattr(last_msg, "content", "")
                                        tool_calls = getattr(last_msg, "tool_calls", [])
                                        
                                        # 只有当有内容且没有工具调用时才发送
                                        if content and not tool_calls:
                                             yield ("answer_chunk", {"content": content})
                                             has_streamed = True # 避免重复
                            
                            # 处理 citations (只有变化时才发送)
                            citations = output.get("citations")
                            if citations and citations != last_citations:
                                yield ("rag_result", {
                                    "citations": citations,
                                    "original_citations": []
                                })
                                last_citations = citations

                # 3.3 错误处理 (Optional)
                # if kind == "on_chain_error": ...
                
        except Exception as e:
            trace_event("qa.error", status="UNKNOWN" if is_unknown_provider_error(e) else "FAILED", error_type=type(e).__name__)
            yield ("error", provider_error_payload(e, stage="agent_workflow"))
            return
            
        yield ("done", {"usage": {}, "retrieval_status": last_retrieval_status, 'answer_mode': last_answer_mode})

    @observe("qa.request")
    async def chat_stream(
        self,
        messages: List[Message],
        kb_ids: List[str],
        use_rewrite: bool = False,
        mode: str = "chat",
        top_k: int = 50,
        score_threshold: float = 0.0,
        model_id: str = None,
        rerank_enabled: bool = False,
        rerank_score_threshold: float = 0.0,
        rerank_model_id: str = None,
        search_mode: str = "hybrid",
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
        rerank_top_k: int = 20,
        context_window: int = 1,
    ) -> AsyncGenerator[Tuple[str, Dict[str, Any]], None]:
        """
        流式对话

        Args:
            messages: 消息列表
            kb_ids: 知识库 ID 列表
            use_rewrite: 是否使用问题改写
            mode: 对话模式 (chat/agent)
            top_k: 检索数量
            score_threshold: 分数阈值
            model_id: LLM 模型 ID
            rerank_enabled: 是否启用 Rerank
            rerank_score_threshold: Rerank 分数阈值
            rerank_model_id: Rerank 模型 ID
            search_mode: 检索模式 (vector/fulltext/hybrid)
            vector_weight: 向量权重
            bm25_weight: BM25 权重
            rerank_top_k: Rerank 返回数量
            context_window: 上下文窗口大小
        """
        trace_event("qa.config", mode=mode, model_id=model_id, kb_ids=kb_ids,
                    search_mode=search_mode, top_k=top_k, score_threshold=score_threshold,
                    vector_weight=vector_weight, bm25_weight=bm25_weight,
                    rerank_enabled=rerank_enabled, rerank_model_id=rerank_model_id,
                    rerank_score_threshold=rerank_score_threshold, rerank_top_k=rerank_top_k,
                    context_window=context_window, use_rewrite=use_rewrite)
        import sys
        print(f"DEBUG_CHAT: Start chat_stream. Mode: {mode}, SearchMode: {search_mode}, TopK: {top_k}", flush=True)
        sys.stdout.flush()

        from app.services.agent_workflow import explicitly_requests_sources
        user_message = next((msg.content for msg in reversed(messages) if msg.role.value == "user"), None)
        if not kb_ids and user_message and explicitly_requests_sources(user_message):
            trace_event('qa.answer', answer_mode='INSUFFICIENT_EVIDENCE', retrieval_status='NO_SCOPE')
            yield ('answer_chunk', {'content': '请先选择知识库，再根据资料回答。'})
            yield ('done', {'usage': {}, 'retrieval_status': 'NO_SCOPE', 'answer_mode': 'INSUFFICIENT_EVIDENCE'})
            return
        if kb_ids and user_message and explicitly_requests_sources(user_message):
            preflight_status = _scope_preflight(kb_ids)
            if preflight_status:
                trace_event("qa.answer", answer_mode="INSUFFICIENT_EVIDENCE", retrieval_status=preflight_status)
                content = ("所选知识库中的资料正在处理或等待索引，请完成索引后再提问。"
                           if preflight_status == "INDEXING_PENDING" else "所选知识库中暂无资料，请先上传并完成索引。")
                yield ("answer_chunk", {"content": content})
                yield ("done", {"usage": {}, "retrieval_status": preflight_status, 'answer_mode': 'INSUFFICIENT_EVIDENCE'})
                return

        if mode == "agent":
            # 使用 LangGraph 新流程
            async for item in self._chat_stream_langgraph(
                messages=messages,
                kb_ids=kb_ids,
                top_k=top_k,
                score_threshold=score_threshold,
                model_id=model_id,
                search_mode=search_mode,
                rerank_enabled=rerank_enabled,
                rerank_score_threshold=rerank_score_threshold,
                rerank_model_id=rerank_model_id,
                vector_weight=vector_weight,
                bm25_weight=bm25_weight,
                rerank_top_k=rerank_top_k,
                context_window=context_window
            ):
                yield item
        else:
            # Normal mode (includes "chat" or any other non-agent mode)
            async for item in self._chat_stream_normal(
                messages=messages,
                kb_ids=kb_ids,
                top_k=top_k,
                score_threshold=score_threshold,
                model_id=model_id,
                rerank_enabled=rerank_enabled,
                rerank_score_threshold=rerank_score_threshold,
                rerank_model_id=rerank_model_id,
                search_mode=search_mode,
                vector_weight=vector_weight,
                bm25_weight=bm25_weight,
                rerank_top_k=rerank_top_k,
                context_window=context_window,
                use_rewrite=use_rewrite,
            ):
                yield item

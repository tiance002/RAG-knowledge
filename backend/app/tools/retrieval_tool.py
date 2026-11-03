
from langchain_core.tools import tool
from typing import List, Optional, Literal
from sqlmodel import Session, select
from app.core.database import engine
from app.models import KnowledgeBase, CustomModel
from app.services.retrieval import get_retrieval_service
from app.services.rerank import RerankService, expand_context, batch_rerank
from app.services.llm import is_unknown_provider_error

import json
import uuid
import re
from app.services.observability import retrieval_attempt
from app.services.observability import event, snapshot, enabled

# The caller supplies this through task-local context, never through the tool schema.

def _content_ref(value):
    return snapshot(value) if enabled() and isinstance(value, str) else None


@tool
async def search_knowledge_base(
    query: str,
    kb_ids: Optional[List[str]] = None,
    search_mode: Literal["vector", "fulltext", "hybrid"] = "hybrid",
    top_k: int = 5,
    score_threshold: float = 0.1,
    use_rerank: bool = False,
    rerank_top_k: int = 3,
    rerank_score_threshold: float = 0.0,
    rerank_model_id: Optional[str] = None,
    vector_weight: float = 0.7,
    bm25_weight: float = 0.3,
    context_window: int = 0
) -> str:
    """
    Search for information in the knowledge base.
    
    Args:
        query: The search query string.
        kb_ids: List of knowledge base IDs to search in. If None or empty, searches all available knowledge bases.
        search_mode: Search mode. Options: "vector" (semantic), "fulltext" (keyword), "hybrid" (both). Default is "hybrid".
        top_k: Number of initial results to retrieve. Default is 5.
        score_threshold: Minimum similarity score threshold for retrieval. Default is 0.4.
        use_rerank: Whether to use a reranker model to re-order results. Default is False.
        rerank_top_k: Number of results to keep after reranking. Default is 3.
        rerank_score_threshold: Minimum score threshold for reranking. Default is 0.0.
        rerank_model_id: Optional ID of the rerank model to use.
        vector_weight: Weight for vector search score in hybrid mode. Default 0.7.
        bm25_weight: Weight for BM25 search score in hybrid mode. Default 0.3.
        context_window: Number of adjacent chunks to include for context expansion. Default 0.
        
    Returns:
        A JSON string containing:
        - "context": A formatted string of retrieved documents for reading.
        - "citations": A list of structured citation objects.
    """
    
    attempt_id = retrieval_attempt.get() or uuid.uuid4().hex
    retrieval_service = get_retrieval_service()
    
    with Session(engine) as session:
        # 1. Resolve Knowledge Bases
        target_kb_ids = kb_ids
        if not target_kb_ids:
            all_kbs = session.exec(select(KnowledgeBase).where(KnowledgeBase.is_deleted == False)).all()
            target_kb_ids = [kb.id for kb in all_kbs]
            
        if not target_kb_ids:
            return json.dumps({
                "context": "No knowledge bases found in the system.",
                "citations": [], "retrieval_status": "NO_RESULTS"
            }, ensure_ascii=False)
            
        event("agent.tool_search", retrieval_attempt_id=attempt_id,
              query=_content_ref(query), kb_ids=target_kb_ids, search_mode=search_mode,
              top_k=top_k if not use_rerank else top_k * 2, score_threshold=score_threshold,
              vector_weight=vector_weight, bm25_weight=bm25_weight,
              use_rerank=use_rerank, rerank_top_k=rerank_top_k,
              rerank_score_threshold=rerank_score_threshold, rerank_model_id=rerank_model_id,
              context_window=context_window)
        # 2. Execute Retrieval
        try:
            results, retrieval_metrics = await retrieval_service.search(
                session=session,
                query=query,
                kb_ids=target_kb_ids,
                search_mode=search_mode,
                top_k=top_k if not use_rerank else top_k * 2, # Fetch more if reranking
                score_threshold=score_threshold,
                vector_weight=vector_weight,
                bm25_weight=bm25_weight
            )
        except Exception as e:
            if is_unknown_provider_error(e):
                raise
            return json.dumps({
                "context": "Retrieval failed.",
                "citations": [], "retrieval_status": "RETRIEVAL_ERROR"
            }, ensure_ascii=False)

        retrieval_status = retrieval_metrics.get("retrieval_status", "UNKNOWN")
        if retrieval_status not in ("SUCCESS", "NO_RESULTS", "RETRIEVAL_ERROR", "UNKNOWN"):
            retrieval_status = "UNKNOWN"
            
        if not results:
            return json.dumps({
                "context": "No relevant documents found." if retrieval_status == "NO_RESULTS" else "Retrieval status is unavailable or failed.",
                "citations": [], "retrieval_status": retrieval_status
            }, ensure_ascii=False)
            
        # Use the shared batch fallback contract for every retrieval consumer.
        if use_rerank and results:
            try:
                service = RerankService(session)
                model = session.get(CustomModel, rerank_model_id) if rerank_model_id else None
                before_ids = [r.get('id') for r in results]
                results = batch_rerank(query=query, candidates=results, rerank_service=service,
                                       model=model, top_k=rerank_top_k,
                                       score_threshold=rerank_score_threshold)
                if any(r.get('rerank_score') is None for r in results):
                    retrieval_status = 'UNKNOWN'
                elif not results and retrieval_status == 'SUCCESS':
                    retrieval_status = 'LOW_RELEVANCE'
                event('rerank.batch_output', retrieval_attempt_id=attempt_id,
                      results=[{'id': r.get('id'), 'score': r.get('rerank_score'),
                                'rerank_status': r.get('rerank_status')} for r in results])
                event('rerank.filtered', retrieval_attempt_id=attempt_id,
                      input_ids=before_ids, output_ids=[r.get('id') for r in results],
                      threshold=rerank_score_threshold)
                event('rerank.selected', retrieval_attempt_id=attempt_id,
                      output_ids=[r.get('id') for r in results], top_k=rerank_top_k)
            except Exception as error:
                if is_unknown_provider_error(error):
                    raise
                retrieval_status = 'RETRIEVAL_ERROR'
                event('agent.rerank_failed', retrieval_attempt_id=attempt_id, error_type=type(error).__name__)
        else:
            results = results[:top_k]

        # 4. Context Expansion (Optional)
        expansion_failed = False
        if context_window > 0 and results:
            try:
                results = expand_context(
                    top_chunks=results,
                    session=session,
                    window_size=context_window, allowed_kb_ids=target_kb_ids,
                    include_source_members=True
                )
            except Exception as e:
                expansion_failed = True
                retrieval_status = 'UNKNOWN'
                event('agent.expansion_failed', error_type=type(e).__name__,
                      retrieval_attempt_id=attempt_id, expansion_delivery_status='SEED_ONLY')

        # Share source-member delivery with ordinary chat; never collapse a
        # hit into neighbours and then send only its original seed.
        from pathlib import PurePath
        from app.services.context_builder import build_chat_context
        def format_citation(row, text):
            source = row.get('file_name', 'Unknown File')
            extension = PurePath(source).suffix.lower()
            metadata = row.get('source_metadata') or {}
            if extension in {'.xlsx', '.xls'}:
                sheet = metadata.get('sheet') or metadata.get('sheet_name')
                if not sheet:
                    match = re.match(r'^\s*\[工作表:\s*([^\]\r\n]+)\]', text)
                    sheet = match.group(1).strip() if match else None
                location = f'工作表: {sheet}' if sheet else '未知位置'
            elif extension == '.pdf':
                page = row.get('page_number')
                location = f'第 {page} 页' if page is not None else row.get('location_info', '')
            else:
                location = row.get('heading_text') or '未知位置'
            return {'fileName': source, 'fileId': row.get('file_id'),
                    'kb_id': row.get('kb_id'), 'page_number': row.get('page_number') if extension == '.pdf' else None,
                    'content_type': row.get('content_type'), 'source_metadata': metadata,
                    'kb_name': row.get('kb_name', 'Unknown KB'), 'score': row.get('score'),
                    'rerank_score': row.get('rerank_score'), 'rerank_status': row.get('rerank_status'),
                    'chunk_id': row.get('id'), 'location': location}
        citations, summary = build_chat_context(results, format_citation)
        # Preserve unsupported image presence/locators for the final guard, but
        # do not send generated captions as verified source text.
        for row in results:
            if row.get('content_type') == 'image' and row.get('id'):
                citations.append({**format_citation(row, ''), 'content': '',
                                  'evidence_status': 'UNVERIFIED_IMAGE'})
        if expansion_failed:
            summary['expansion_delivery_status'] = 'SEED_ONLY'
        formatted_results = [f"Document {i} (Source: {c['fileName']}, KB: {c['kb_name']}):\n{c['content']}\n"
                             for i, c in enumerate(citations, 1) if c.get('content_type') != 'image']
        delivery = []
        for change in summary['changes']:
            row = next((r for r in results if r.get('id') == change['candidate_id']), {})
            original = change['used_original_content']
            delivery.append({'id': change['candidate_id'], 'content_role': 'context_sent',
                             'content_field': 'original_content' if original else 'content',
                             'expanded': bool(row.get('is_expanded')), 'delivered': not change['omitted_from_llm'],
                             'content_sha256': change['delivered_content_sha256'],
                             'delivered_evidence_ids': change['delivered_evidence_ids']})
        event('context.delivery', retrieval_attempt_id=attempt_id, delivery_target='tool_response',
              is_final_model_delivery=False, candidates=delivery)
        if summary['source_mapping_status'] == 'UNKNOWN' and retrieval_status in ('SUCCESS', 'NO_RESULTS', 'LOW_RELEVANCE'):
            retrieval_status = 'UNKNOWN'
        return json.dumps({'context': '\n'.join(formatted_results), 'citations': citations,
                           'retrieval_status': retrieval_status,
                           'omitted_images': summary['omitted_images'],
                           'source_mapping_status': summary['source_mapping_status'],
                           'expansion_delivery_status': summary.get('expansion_delivery_status', 'UNKNOWN')}, ensure_ascii=False)


from langchain_core.tools import tool
from typing import List, Optional, Literal, Dict, Any
from sqlmodel import Session, select
from app.core.database import engine
from app.models import KnowledgeBase, CustomModel
from app.services.retrieval import get_retrieval_service
from app.services.rerank import RerankService, expand_context
from app.services.llm import is_unknown_provider_error

import json
import uuid
from app.services.observability import retrieval_attempt
from app.services.observability import event, snapshot, enabled, fingerprint

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
            
        # 3. Execute Reranking (Optional)
        if use_rerank and results:
            try:
                rerank_service = RerankService(session)
                # Resolve Rerank Model
                rerank_model = None
                if rerank_model_id:
                    rerank_model = session.get(CustomModel, rerank_model_id)

                # Prepare candidates for rerank
                candidates = [r["content"] for r in results]
                
                event("rerank.batch_input", retrieval_attempt_id=attempt_id,
                      query=_content_ref(query), model_id=rerank_model_id,
                      candidates=[{"id": r.get("id"), "content_sha256": fingerprint(r.get("content", "")),
                                   "rank": i + 1} for i, r in enumerate(results)])
                # Execute rerank
                reranked = rerank_service.rerank(query, candidates, model=rerank_model)

                event("rerank.batch_output", retrieval_attempt_id=attempt_id,
                      results=[{"index": item.get("index"), "score": item.get("score"),
                                "id": results[item["index"]].get("id")
                                if isinstance(item, dict) and isinstance(item.get("index"), int)
                                and 0 <= item["index"] < len(results) else None}
                               for item in reranked if isinstance(item, dict)] if isinstance(reranked, list) else [],
                      status="RETURNED" if reranked else "UNKNOWN")
                # The existing service returns [] both for an unavailable reranker
                # and for an empty response. Preserve real retrieval evidence and
                # do not report either case as a confirmed absence of documents.
                if not reranked:
                    retrieval_status = "UNKNOWN"
                    reranked = None
                
                # Filter and map back to results
                final_results = []
                threshold_removed = []
                for item in reranked or []:
                    idx = item["index"]
                    score = item["score"]
                    if score >= rerank_score_threshold:
                        res = results[idx]
                        res["rerank_score"] = score
                        # Update main score to rerank score for consistency
                        res["score"] = score 
                        final_results.append(res)
                    else:
                        threshold_removed.append({"id": results[idx].get("id") if isinstance(idx, int) and -len(results) <= idx < len(results) else None, "index": idx, "score": score,
                                                  "reason_code": "below_threshold"})
                
                if reranked is not None:
                    event("rerank.filtered", retrieval_attempt_id=attempt_id,
                          threshold=rerank_score_threshold,
                          input_ids=[r.get("id") for r in results],
                          output_ids=[r.get("id") for r in final_results], removed=threshold_removed)
                    event('rag.candidate_transition', operation='threshold_filter', retrieval_attempt_id=attempt_id,
                          input_ids=[r.get('id') for r in results], output_ids=[r.get('id') for r in final_results],
                          threshold=rerank_score_threshold, score_kind='rerank', rule_version='tool_rerank_threshold_v1',
                          removed=[dict(r, candidate_id=r.get('id'), threshold=rerank_score_threshold) for r in threshold_removed])
                else:
                    event('rerank.fallback', retrieval_attempt_id=attempt_id, execution_status='UNKNOWN',
                          fallback_score_origin='original_retrieval_score', reason_code='rerank_fallback',
                          input_ids=[r.get('id') for r in results], output_ids=[r.get('id') for r in results])
                # Sort and slice
                before_sort = [r.get('id') for r in final_results] if enabled() else None
                final_results.sort(key=lambda x: x["score"], reverse=True)
                if reranked is not None:
                    event('rag.candidate_transition', operation='sort', retrieval_attempt_id=attempt_id,
                          input_ids=before_sort, output_ids=[r.get('id') for r in final_results],
                          sort_key='rerank_score_desc', removed=[], rule_version='tool_rerank_sort_v1')
                    event("rerank.selected", retrieval_attempt_id=attempt_id,
                          top_k=rerank_top_k, input_ids=[r.get("id") for r in final_results],
                          output_ids=[r.get("id") for r in final_results[:rerank_top_k]],
                          removed=[{"id": r.get("id"), "reason_code": "outside_topk"}
                                   for r in final_results[rerank_top_k:]])
                    event('rag.candidate_transition', operation='topk', retrieval_attempt_id=attempt_id,
                          input_ids=[r.get('id') for r in final_results], output_ids=[r.get('id') for r in final_results[:rerank_top_k]],
                          top_k=rerank_top_k, score_kind='rerank', rule_version='tool_rerank_topk_v1',
                          removed=[{'candidate_id': r.get('id'), 'reason_code': 'outside_topk', 'rank_before': i}
                                   for i, r in enumerate(final_results, 1) if i > rerank_top_k])
                    results = final_results[:rerank_top_k]
                    if not results and retrieval_status == "SUCCESS":
                        retrieval_status = "LOW_RELEVANCE"
                
            except Exception as e:
                if is_unknown_provider_error(e):
                    raise
                retrieval_status = "RETRIEVAL_ERROR"
        else:
            # If not reranking, just ensure we respect top_k
            results = results[:top_k]

        # 4. Context Expansion (Optional)
        if context_window > 0 and results:
            try:
                results = expand_context(
                    top_chunks=results,
                    session=session,
                    window_size=context_window
                )
            except Exception as e:
                # Log error but continue with unexpanded results
                print(f"Context expansion failed: {e}")

        # 5. Format Output
        formatted_results = []
        citations = []
        
        omitted_expanded = False
        delivery = []
        for r in results:
            from pathlib import PurePath
            import re
            source = r.get("file_name", "Unknown File")
            kb_name = r.get("kb_name", "Unknown KB")
            score = r.get("score", 0.0)
            expanded = r.get("is_expanded") or (r.get("metadata") or {}).get("is_expanded")
            content = r.get("original_content") if expanded else r.get("content")
            if expanded and (not isinstance(content, str) or not content.strip()):
                delivery.append({"id": r.get("id"), "content_role": "context_sent", "content_field": "original_content", "reason_code": "missing_source", "delivered": False})
                omitted_expanded = True
                continue
            if not r.get("id") or not isinstance(content, str) or not content.strip():
                delivery.append({"id": r.get("id"), "reason": "missing_id_or_content", "delivered": False})
                continue
            content = content.strip()
            delivery.append({"id": r.get("id"), "content_role": "context_sent", "content_field": "original_content" if expanded else "content",
                             "expanded": bool(expanded), "expanded_content_sha256": fingerprint(r.get("content", "")),
                             "content_sha256": fingerprint(content),
                             "delivered": True, "rank": len(citations) + 1})
            extension = PurePath(source).suffix.lower()
            page_number = r.get('page_number') if extension == '.pdf' else None
            if extension in {'.xlsx', '.xls'}:
                metadata = r.get('source_metadata') or {}
                sheet = metadata.get('sheet') or metadata.get('sheet_name')
                if not sheet:
                    match = re.match(r'^\s*\[工作表:\s*([^\]\r\n]+)\]', content)
                    sheet = match.group(1).strip() if match else None
                location = f'工作表: {sheet}' if sheet else '未知位置'
            elif extension == '.pdf':
                location = r.get('location_info', '')
            else:
                location = r.get('heading_text') or '未知位置'
            i = len(citations) + 1
            
            formatted_results.append(
                f"Document {i} (Source: {source}, KB: {kb_name}, Score: {score:.4f}):\n{content}\n"
            )
            
            citations.append({
                "fileName": source,
                "fileId": r.get("file_id"),
                "page_number": page_number,
                "content_type": r.get("content_type"),
                "source_metadata": r.get("source_metadata", {}),
                "kb_name": kb_name,
                "score": score,
                "content": content,
                "chunk_id": r.get("id"),
                "location": location
            })
            
        event("context.delivery", retrieval_attempt_id=attempt_id, delivery_target="tool_response",
              is_final_model_delivery=False, candidates=delivery)
        if omitted_expanded and retrieval_status in ("SUCCESS", "NO_RESULTS", "LOW_RELEVANCE"):
            retrieval_status = "UNKNOWN"
        return json.dumps({
            "context": "\n".join(formatted_results),
            "citations": citations, "retrieval_status": retrieval_status
        }, ensure_ascii=False)

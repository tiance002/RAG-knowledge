import math
from app.services.observability import event, candidates as observe_candidates, observe, model_response, enabled, snapshot
"""
Rerank 服务模块
"""

from openai import OpenAI
import httpx
import json
import re
from typing import List, Dict, Any, Optional
from app.models.models import CustomModel, ModelType
from app.core.database import get_session
from sqlmodel import select, Session
from app.core.rerank_model import RerankModelLoader
from app.services.llm import is_unknown_provider_error, invoke_provider_http, provider_error_payload

def extract_json_from_text(text: str) -> str:
    """从模型响应中提取 JSON 字符串"""
    # 1. 移除 <think> 思考过程
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    
    # 2. 尝试提取 markdown 代码块
    match = re.search(r'```(?:json)?\s*(.*?)```', text, re.DOTALL)
    if match:
        return match.group(1).strip()
        
    # 3. 尝试寻找第一个 { 和最后一个 }
    start = text.find('{')
    end = text.rfind('}')
    if start != -1 and end != -1 and end > start:
        return text[start:end+1]
        
    return text.strip()

class RerankService:
    def __init__(self, session: Session):
        self.session = session

    def get_rerank_model_by_id(self, model_id: str) -> Optional[CustomModel]:
        """根据 ID 获取 Rerank 模型"""
        return self.session.get(CustomModel, model_id)

    def get_default_rerank_model(self) -> Optional[CustomModel]:
        """获取默认（激活）的 Rerank 模型"""
        statement = select(CustomModel).where(
            CustomModel.model_type == ModelType.RERANK,
            CustomModel.is_active == True
        )
        return self.session.exec(statement).first()

    @observe('rerank.call')
    def rerank(self, query: str, candidates: List[str], model: Optional[CustomModel] = None) -> List[Dict[str, Any]]:
        """
        执行重排序
        :param query: 查询语句
        :param candidates: 候选文本列表
        :param model: 指定模型
        :return: 排序后的结果 [{"text": "...", "score": 0.xx, "index": original_index}, ...]
        """
        if not candidates:
            return []

        if not model:
            model = self.get_default_rerank_model()
        
        if not model:
            event("rerank.unavailable", status="UNKNOWN", reason="no_active_model")
            print("No active rerank model found.")
            return []

        try:
            # 判断是否为本地模型 (根据 base_url 是否为 http 开头)
            # 如果是本地路径，使用 RerankModelLoader
            is_remote_api = model.base_url.startswith("http://") or model.base_url.startswith("https://")
            
            if not is_remote_api:
                # 使用本地模型加载器
                loader = RerankModelLoader()
                results = loader.predict(
                    query=query,
                    passages=candidates,
                    model_path=model.base_url,
                    max_len=model.context_length
                )
                return results
            else:
                # 使用 OpenAI 兼容 API (保留原有逻辑作为远程调用支持)
                return self._rerank_via_api(query, candidates, model)

        except Exception as e:
            if is_unknown_provider_error(e):
                raise
            print(f"Rerank execution failed: {type(e).__name__}")
            event('rerank.call_failed', status='FAILED', error_type=type(e).__name__)
            return []

    def _rerank_via_api(self, query: str, candidates: List[str], model: CustomModel) -> List[Dict[str, Any]]:
        """调用远程 /rerank 端点；端点不可用时回退到用 chat 接口打分。"""
        if not candidates:
            return []
        try:
            url = model.base_url.rstrip("/") + "/rerank"
            resp = invoke_provider_http(lambda: httpx.post(
                url,
                headers={"Authorization": f"Bearer {model.api_key}",
                         "Content-Type": "application/json"},
                json={"model": model.model_name, "query": query, "documents": candidates,
                      "top_n": len(candidates), "return_documents": False},
                timeout=120.0,
                trust_env=False,
            ), stage="rerank", model_name=model.model_name)
            resp.raise_for_status()
            payload = resp.json()
            event('rerank.usage', model=model.model_name, usage=payload.get('usage'), cost=None)
            from app.services.metrics import record_provider_usage
            record_provider_usage("rerank", payload.get("usage"))
            results = []
            for row in payload.get("results") or []:
                idx = row.get("index")
                if idx is None or not (0 <= int(idx) < len(candidates)):
                    continue
                score = row.get("relevance_score", row.get("score", 0.0))
                results.append({
                    "index": int(idx),
                    "score": float(score),
                    "text": candidates[int(idx)],
                })
            if results:
                results.sort(key=lambda x: x["score"], reverse=True)
                return results
            print("API Rerank (/rerank) returned empty results, falling back to chat scoring")
        except Exception as exc:
            if is_unknown_provider_error(exc):
                raise
            print(f"API Rerank (/rerank) failed: {type(exc).__name__}; falling back to chat scoring")
        event('rerank.chat_fallback', status='FALLBACK', reason='rerank_endpoint_unavailable_or_empty')
        return self._rerank_via_chat(query, candidates, model)

    def _rerank_via_chat(self, query: str, candidates: List[str], model: CustomModel) -> List[Dict[str, Any]]:
        """通过 API 调用 Rerank"""
        client = OpenAI(
            base_url=model.base_url,
            api_key=model.api_key,
            max_retries=0,
            http_client=httpx.Client(
                trust_env=False
            )
        )

        prompt = f"""你是一个文本相关性重排序模型，请计算查询与每个候选文本的相关性得分（0-1，越接近1越相关）。

查询："{query}"

候选文本列表：
"""
        for i, text in enumerate(candidates):
            safe_text = text[:500].replace("\n", " ")
            if enabled():
                event("rag.candidate_transition", operation="truncate", input_ids=[i], output_ids=[i], input_candidate_ids=[i], output_candidate_ids=[i], input_kind="rerank_chat_candidate", output_kind="rerank_sent", start=0, end=min(len(text), 500), original_content=snapshot(text), rerank_sent_content=snapshot(safe_text), clipped=len(text) > 500, removed=[], rule_version="chat_prefix_500_replace_newlines_v1")
            prompt += f"[{i}] {safe_text}\n"

        prompt += """
任务要求：
1. 仅对上述【候选文本列表】中的条目进行打分，绝对不要把【查询】本身作为候选文本。
2. 仅返回 JSON 格式，不要包含任何推理过程或额外文字。
3. JSON 结构示例：{"candidates": [{"index": 0, "score": 0.95, "text": "..."}, {"index": 1, "score": 0.12, "text": "..."}]}
   - index: 必须对应候选文本列表中的方括号索引 [i]
   - text: 候选文本的内容
   - score: 相关性得分 (0-1)
4. 结果按 score 降序排列。

/no_think
"""
        try:
            response = invoke_provider_http(lambda: client.chat.completions.create(
                model=model.model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
                stream=False,
            ), stage="rerank_chat", model_name=model.model_name, check_status=False,
               messages=[{"role": "user", "content": prompt}], model_parameters={"temperature": 0.0})
            model_response(response, role='rerank_chat', model=model.model_name)
            result_text = response.choices[0].message.content
            json_str = extract_json_from_text(result_text)
            data = json.loads(json_str)
            
            if "candidates" in data:
                results = data["candidates"]
            elif isinstance(data, list):
                results = data
            else:
                return []
            
            final_results = []
            for item in results:
                if "index" in item and "score" in item:
                    final_results.append({
                        "index": item["index"],
                        "score": float(item["score"]),
                        "text": item.get("text", "")
                    })
            final_results.sort(key=lambda x: x["score"], reverse=True)
            return final_results
        except Exception as e:
            if is_unknown_provider_error(e):
                raise
            print(f"API Rerank failed: {type(e).__name__}")
            event('rerank.chat_failed', status='FAILED', error_type=type(e).__name__)
            return []

def test_rerank_connection(model_in: Any) -> Dict[str, Any]:
    """测试 Rerank 模型连接"""
    try:
        # 兼容 Pydantic v1/v2 和 SQLModel
        base_url = getattr(model_in, "base_url", None)
        model_name = getattr(model_in, "model_name", None)
        context_length = getattr(model_in, "context_length", 4096)
        
        # 兼容字典访问 (如果是 dict)
        if base_url is None and isinstance(model_in, dict):
            base_url = model_in.get("base_url")
            model_name = model_in.get("model_name")
            context_length = model_in.get("context_length", 4096)

        if not base_url:
            return {"success": False, "message": "模型路径/地址不能为空"}

        is_remote_api = base_url.startswith("http://") or base_url.startswith("https://")

        if not is_remote_api:
            # 测试本地加载
            try:
                loader = RerankModelLoader()
                # 尝试加载模型
                loader.load_model(base_url, max_len=context_length)
                
                # 尝试简单推理
                query = "测试"
                passages = ["这是一个测试文档", "这是另一个无关文档"]
                results = loader.predict(query, passages, base_url, context_length)
                
                return {"success": True, "message": "本地模型加载并测试成功", "data": results}
            except Exception as e:
                return {"success": False, "message": f"本地模型加载失败: {str(e)}", "error": str(e)}
        else:
            # 测试远程 API (保持原有逻辑)
            # ... (原有 API 测试逻辑，略简化或复用)
            # 为保持简洁，这里只保留基本的 API 测试
            api_key = getattr(model_in, "api_key", "")
            if isinstance(model_in, dict):
                api_key = model_in.get("api_key", "")
                
            client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0)
            # ... 发送简单请求 ...
            # 鉴于用户主要关注本地，这里简单做个连通性检查即可，或者复用上面的 prompt
            # 为了完整性，还是写全一点
            try:
                 response = invoke_provider_http(lambda: client.chat.completions.create(
                    model=model_name,
                    messages=[{"role": "user", "content": "test"}],
                    max_tokens=10
                ), stage="rerank_connection_test", model_name=model_name, check_status=False,
                   messages=[{"role": "user", "content": "test"}], model_parameters={"max_tokens": 10})
                 return {"success": True, "message": "API 连接成功"}
            except Exception as e:
                return {"success": False, "message": "API 连接失败", **provider_error_payload(e, stage="rerank_connection_test")}

    except Exception as e:
        return {"success": False, "message": f"未知错误: {str(e)}", "error": str(e)}


def estimate_tokens(text: str) -> int:
    """
    估算文本的 token 数量

    策略：中文按字符数估算，英文按单词数估算
    """
    import re
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    english_words = len(re.findall(r'[a-zA-Z]+', text))
    return chinese_chars + english_words


@observe("rerank.batch")
def batch_rerank(
    query: str,
    candidates: List[Dict[str, Any]],
    rerank_service: RerankService,
    model: CustomModel,
    max_context_tokens: int = None,
    top_k: int = None,
    score_threshold: float = 0.0
) -> List[Dict[str, Any]]:
    """
    分批调用 Rerank 模型

    当候选集超过模型上下文限制时，分批处理后合并结果

    Args:
        query: 查询语句
        candidates: 候选文档列表，每个包含 "content" 字段
        rerank_service: Rerank 服务实例
        model: Rerank 模型配置
        max_context_tokens: 单批最大 token 数
        top_k: 返回的 top-k 数量
        score_threshold: 分数阈值

    Returns:
        重排序后的结果列表
    """
    from app.core.config import get_settings
    settings = get_settings()

    max_context_tokens = max_context_tokens or settings.RERANK_MAX_CONTEXT
    top_k = top_k or settings.RERANK_DEFAULT_TOP_K

    if not candidates:
        return []

    # 1. 估算每个 candidate 的 token 数
    candidates_with_tokens = []
    for c in candidates:
        tokens = estimate_tokens(c.get("content", ""))
        candidates_with_tokens.append({**c, "_tokens": tokens, "_original_content": c.get("content", "")})

    # 2. 分批
    batches = []
    current_batch = []
    current_tokens = estimate_tokens(query)  # 查询也占用 token

    for candidate in candidates_with_tokens:
        tokens = candidate["_tokens"]

        # 如果单个文档就超过限制，单独成批
        if tokens > max_context_tokens:
            if current_batch:
                batches.append(current_batch)
                current_batch = []
                current_tokens = estimate_tokens(query)
            # 截断过长的文档
            truncated_content = candidate.get("content", "")[:max_context_tokens * 2]
            if enabled():
                cid = candidate.get("id", candidate.get("chunk_id"))
                event("rag.candidate_transition", operation="truncate", input_ids=[cid], output_ids=[cid], input_candidate_ids=[cid], output_candidate_ids=[cid], start=0, end=len(truncated_content), original_content=snapshot(candidate.get("content", "")), rerank_sent_content=snapshot(truncated_content), estimated_tokens=tokens, max_context_tokens=max_context_tokens, removed=[], rule_version="batch_prefix_max_tokens_times_two_v1")
            batches.append([{**candidate, "content": truncated_content}])
            continue

        # 如果加入当前候选后超过限制，先结算当前批次
        if current_tokens + tokens > max_context_tokens and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_tokens = estimate_tokens(query)

        current_batch.append(candidate)
        current_tokens += tokens

    if current_batch:
        batches.append(current_batch)

    print(f"📊 [Rerank] 分 {len(batches)} 批处理 {len(candidates)} 条候选")

    # 3. 分批调用 rerank
    all_results = []
    for batch_idx, batch in enumerate(batches):
        observe_candidates("rerank.batch_input", batch, batch_index=batch_idx)
        batch_texts = [c.get("content", "") for c in batch]
        if enabled():
            ids = [c.get("id", c.get("chunk_id")) for c in batch]
            event("rag.candidate_transition", operation="batch_rerank", input_ids=ids, output_ids=ids, input_candidate_ids=ids, output_candidate_ids=ids, batch_index=batch_idx, input_snapshots=[snapshot(text) for text in batch_texts], removed=[], rule_version="batch_rerank_input_v1")

        try:
            rerank_results = rerank_service.rerank(query, batch_texts, model=model)
            mapped = []
            mapped_indices = set()

            if not rerank_results:
                event("rerank.empty", status="UNKNOWN", batch_index=batch_idx, reason="empty_response_no_score_fallback")
                for candidate in batch:
                    result = {**candidate, 'content': candidate['_original_content'],
                              'rerank_score': None, 'rerank_status': 'UNKNOWN'}
                    result.pop('_tokens', None)
                    result.pop('_original_content', None)
                    all_results.append(result)
            for rr in rerank_results or []:
                idx = rr.get("index")
                if isinstance(idx, int) and 0 <= idx < len(batch) and idx not in mapped_indices:
                    score = rr.get('score')
                    if not isinstance(score, (int, float)) or not math.isfinite(score):
                        continue
                    mapped_indices.add(idx)
                    result = {**batch[idx]}
                    result["rerank_score"] = rr.get("score", 0)
                    result['content'] = result.pop('_original_content')
                    result['rerank_status'] = 'SCORED'
                    # 移除临时字段
                    result.pop("_tokens", None)
                    all_results.append(result)
                    if enabled():
                        mapped.append({"response_index": idx, "candidate_id": result.get("id", result.get("chunk_id")), "rerank_score": result["rerank_score"]})

            if rerank_results:
                for idx, candidate in enumerate(batch):
                    if idx not in mapped_indices:
                        result = {**candidate, 'content': candidate['_original_content'],
                                  'rerank_score': None, 'rerank_status': 'UNKNOWN'}
                        result.pop('_tokens', None)
                        result.pop('_original_content', None)
                        all_results.append(result)
            if enabled():
                output_ids = [item["candidate_id"] for item in mapped]
                event("rag.candidate_transition", operation="batch_rerank", input_ids=ids, output_ids=output_ids, input_candidate_ids=ids, output_candidate_ids=output_ids, batch_index=batch_idx, response_mapping=mapped, execution_status="RETURNED" if rerank_results else "UNKNOWN", removed=[], rule_version="batch_rerank_output_v1")
            print(f"   批次 {batch_idx + 1}/{len(batches)} 完成")

        except Exception as e:
            if is_unknown_provider_error(e):
                raise
            print(f"⚠️ [Rerank] 批次 {batch_idx + 1} 失败: {type(e).__name__}")
            # This origin describes the exported retrieval score, not a replacement rerank score.
            event('rerank.fallback', status='FALLBACK', batch_index=batch_idx, reason='rerank_score_unknown', fallback_score_origin='original_score', input_ids=[c.get('id', c.get('chunk_id')) for c in batch], error_type=type(e).__name__)
            # 批次失败时，保留原始分数
            for c in batch:
                result = {**c}
                result["rerank_score"] = None
                result["rerank_status"] = 'UNKNOWN'
                result['content'] = result.pop('_original_content')
                result.pop("_tokens", None)
                all_results.append(result)

    observe_candidates("rerank.batch_output", all_results)
    # 4. 按 rerank_score 排序
    before_sort = [r.get("id", r.get("chunk_id")) for r in all_results] if enabled() else []
    all_results.sort(key=lambda x: (x.get("rerank_score") is not None, x.get("rerank_score") or 0), reverse=True)
    if enabled():
        after_sort = [r.get("id", r.get("chunk_id")) for r in all_results]
        event("rag.candidate_transition", operation="sort", input_ids=before_sort, output_ids=after_sort, input_candidate_ids=before_sort, output_candidate_ids=after_sort, sort_key="rerank_score_desc", removed=[], rule_version="rerank_sort_v1")

    # 5. 过滤低于阈值的结果
    filtered_results = [
        r for r in all_results
        if r.get("rerank_score") is None or r["rerank_score"] >= score_threshold
    ]

    observe_candidates("rerank.filtered", filtered_results)
    if enabled():
        before = [r.get("id", r.get("chunk_id")) for r in all_results]
        after = [r.get("id", r.get("chunk_id")) for r in filtered_results]
        event("rag.candidate_transition", operation="threshold_filter", input_ids=before, output_ids=after, input_candidate_ids=before, output_candidate_ids=after, threshold=score_threshold, score_kind="rerank_score", rule_version="rerank_threshold_gte_v1", removed=[{"candidate_id": r.get("id", r.get("chunk_id")), "reason_code": "below_threshold", "rank_before": i, "threshold": score_threshold} for i, r in enumerate(all_results, 1) if r.get("rerank_score") is not None and r["rerank_score"] < score_threshold])
        selected = after[:top_k]
        event("rag.candidate_transition", operation="topk", input_ids=after, output_ids=selected, input_candidate_ids=after, output_candidate_ids=selected, top_k=top_k, score_kind="rerank_score", rule_version="rerank_topk_v1", removed=[{"candidate_id": cid, "reason_code": "outside_topk", "rank_before": i} for i, cid in enumerate(after, 1) if i > len(selected)])
    # 6. 返回 top_k
    observe_candidates('rerank.selected', filtered_results[:top_k], top_k=top_k)
    return filtered_results[:top_k]


@observe("retrieval.expand_context")
def expand_context(top_chunks, session, window_size=None, max_parent_length=None,
                   include_source_members=False, allowed_kb_ids=None, max_context_chars=None):
    """Resolve actual parents once per request; legacy invalid links fall back to seeds."""
    from app.services.parent_context import expand_parent_context
    return expand_parent_context(top_chunks, session, window_size=window_size,
        max_parent_length=max_parent_length, include_source_members=include_source_members,
        allowed_kb_ids=allowed_kb_ids, max_context_chars=max_context_chars)

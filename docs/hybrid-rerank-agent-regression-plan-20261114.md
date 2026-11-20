# Async Rerank + Agent Regression Plan (2026-11-14)

## Scope and evidence posture

Read-only comparison of current local sources, archived ECS source bundles/manifests, prior HTTP/Agent acceptance runners, and the frozen 10-KB QA500 scope. No tests or provider calls were run; no ECS access was performed. This is a plan, not an acceptance result.

## Source identity: current local and archived ECS

The current local files match the saved 104-file final ECS acceptance manifest (`var/server-chat-history-acceptance-20261114/final-source-manifest.json`), and the corresponding files in `ecs-source-a1.tar.gz`:

| File | SHA-256 | Current local vs accepted ECS snapshot |
|---|---|---|
| `backend/app/services/rerank.py` | `71c6530410de7d5b12d89a766f1482a81c85a4c89bcaad7012d1b3fece44e1cf` | exact match |
| `backend/app/services/agent_workflow.py` | `6fb4c5e16a02ca64eb718bb785be0431496147a2190362dfef79bbf8d87ef076` | exact match |
| `backend/app/services/chat.py` | `e93d24450b1664487eda2598178ed26bd0db1c90c2564e4afc42bd3941946266` | exact match |

The archive `var/hybrid-concurrency-20261114/ecs-baseline-source.tar.gz` is an older snapshot: its `rerank.py` hash is `54fb20bc…`, and its `agent_workflow.py` / `chat.py` also differ from the accepted source. Do not use that archive as the R7 comparator. The saved `ecs-r7-delta.tar.gz` changes `retrieval.py` and `retrieval_budget.py`; its candidate manifest records the same current `rerank.py` hash. It does not establish that async reranking was exercised in R7.

## Actual async rerank path and blockers

- Normal Chat: [`chat.py`](../backend/app/services/chat.py), `ChatService._retrieve_with_new_service` calls `RetrievalService.search`, awaits `async_batch_rerank` when `rerank_enabled`, then expands context. This is the intended nonblocking route for the actual `/chat/completions` request.
- Agent: [`retrieval_tool.py`](../backend/app/tools/retrieval_tool.py), `search_knowledge_base` calls `RetrievalService.search`; with reranking enabled it requests `top_k * 2` seeds (20 for top-k 10), awaits the same sequential batch helper, then expands context and returns citations/context to the workflow.
- [`rerank.py`](../backend/app/services/rerank.py), `RerankService.arerank` uses `provider_request` and async HTTP for a remote `CustomModel`; remote path is `base_url.rstrip('/') + '/rerank'`, with JSON `model`, `query`, `documents`, `top_n`, and `return_documents=false`. It expects `results` containing `index` and `relevance_score`/`score`. `_batch_rerank_steps` uses `RERANK_MAX_CONTEXT` (default 8196 estimated tokens) and sequentially awaits each batch; oversized individual candidate text is truncated for the rerank request and original text is restored for output.
- Concrete integration blocker: the saved 10-KB manifest and prior acceptance requests identify Qwen and the ten KB IDs, but do not identify an active BAAI rerank `CustomModel` ID, URL, API key availability, or the provider's exact `/rerank` contract. The local SQLite read-only check did not find a BAAI model binding. Resolve the existing binding read-only before sending anything; require `model_type=RERANK`, the intended model name, and an API base URL that makes the appended `/rerank` path correct. Do not guess an endpoint or copy credentials into test artifacts.
- Code-level failure behavior matters for interpreting a run: `_arerank_via_api` does not call `response.raise_for_status()` before parsing JSON. A non-200 JSON error without `results` can flow into `rerank.chat_fallback`; that fallback sends a chat completion to the same base URL with a 5-second timeout. Require a recorded 200 response and nonempty, index-valid rerank result for each batch; if fallback is observed, mark the run as fallback/invalid for the real-reranker comparison rather than counting it as a rerank success. A normal empty/unusable response also becomes `UNKNOWN` rows, while unknown provider errors can propagate.
- The actual BAAI endpoint, model binding and response schema remain unverified in these local receipts. Provider-side latency, quotas and cost are also unknown. Resolve these in a bounded preflight before the full case matrix.

## Agent tool, grader and final answer are separate stages

The workflow has one declared knowledge tool, `search_knowledge_base` (`agent_workflow.py` initializes `self.tools = [search_knowledge_base]`). `agent_node` asks the Qwen model for a tool call or a final answer. `run_tools` applies the server-selected KB scope and retrieval parameters; the model supplies the query, not the allowed KB IDs or rerank configuration. Useful events include `agent.actual_query` (query, scope hash, tool call ID, retry count and actual parameters), `rerank.batch_output`, and `rerank.selected`.

After each tool result, `grade_documents` makes a separate non-streaming Qwen call (`stage='quality'`) over at most the first 2,000 characters of the current tool-round context. Scores below 0.8 can schedule up to three additional retrieval attempts; an invalid score/provider failure yields `UNKNOWN` and does not schedule retry. The next `agent_node` call may request another tool search or compose the final answer. Thus Agent HTTP/model call counts are higher than Chat: for a clean single search there is an Agent tool-selection call, one grade call, and a separate final Agent call; each actual retry adds another tool/search/rerank and grade cycle. Record grader output/status and final generation status independently. A successful `grading_status=VALID` is not an answer-quality pass; the final response reports `validation_status=NOT_RUN` unless a separate evaluator is used.

Prior local evidence also has an Agent top-k context-budget failure at top-k 10 (`docs/server-chat-history-acceptance-20261114.md`, “未关闭项”); the successful six-turn comparison used top-k 3. Reranking may narrow the Agent evidence but is not proof that the top-k 10 prompt will fit. Keep the requested top-k 10 config fixed and report a budget failure as a failure; do not silently lower the value to force a pass.

## What prior acceptances do and do not prove

- Frozen QA500 10-KB scope: [`run-manifest.json`](../var/qa500-scope-20261114-020006-f6c60c36/run-manifest.json) defines 60 turns across six sessions, an explicit ten-ID allowlist, Qwen `Qwen/Qwen3.5-4B` / model ID `a8753099-fe56-403b-a7db-ff457a6403f5`, Hybrid 0.7/0.3, top-k 50, rewrite enabled, and reranking disabled. Its results are not a rerank baseline.
- R7 real Chat HTTP/SSE captures in `var/hybrid-concurrency-20261114/r7-chat-real` used top-k 10 and the same Qwen ID and 10 KB IDs, but `rerank_enabled=false`; answer quality was `NOT_RUN`.
- [`run_conversation_live_acceptance.py`](../scripts/run_conversation_live_acceptance.py) exercises actual Chat/Agent sessions and HTTP/SSE, but configures `rerank_enabled=false`. The reported final ECS run used top-k 3. It does not test async reranking or the requested top-k 10 Agent budget.
- [`run_p0_agent_http_acceptance.py`](../scripts/run_p0_agent_http_acceptance.py) uses a rerank-disabled state and sets an explicit rerank-deny monkeypatch/call budget of zero; it invokes `AgentWorkflow().app.ainvoke` directly rather than the Chat HTTP/SSE route. It cannot prove the API/async rerank path.

No saved receipt demonstrates a real cloud call to `BAAI/bge-reranker-v2-m3` through this async path.

## Proposed bounded regression

Use the latest source-only app snapshot in the planned isolated ECS directory and the existing runtime environment; keep the approved read-only DB/index and explicit ten-KB allowlist. Do not copy the DB/index. Run both normal Chat and Agent through the actual HTTP/SSE API with the same inputs and fixed configuration. Keep R7 code unchanged so ranking causality remains clear.

Fixed ChatRequest fields:

```json
{
  "kb_ids": [
    "f29cc026-03f5-516f-a716-9bd4fc8416dc",
    "b98ec9ee-3310-530f-86c6-1b35cfd607d1",
    "7cdb1ee4-ae8e-5cb6-b2ee-4f42a385cf00",
    "52c1a74f-c4dd-5731-bdbd-750c22b30ceb",
    "df847eb7-135e-54ca-99a6-ee8263aade80",
    "37f912bb-ea0d-4f66-82eb-fd01426f5992",
    "9c11b808-386d-4943-b719-7c907a90b589",
    "ba255077-8e23-4d3e-845d-bc0fedcc7a4f",
    "e959f353-9497-468e-bcf7-1a9dcdaeb8c4",
    "06524ce7-7513-48c1-b90a-a96378ca8e7a"
  ],
  "mode": "<chat|agent>",
  "model_id": "a8753099-fe56-403b-a7db-ff457a6403f5",
  "search_mode": "hybrid",
  "top_k": 10,
  "score_threshold": 0.0,
  "vector_weight": 0.7,
  "bm25_weight": 0.3,
  "rerank_enabled": true,
  "rerank_model_id": "<existing BAAI/bge-reranker-v2-m3 RERANK binding ID, resolved read-only>",
  "rerank_top_k": 10,
  "rerank_score_threshold": 0.0,
  "context_window": 1,
  "use_rewrite": false,
  "web_search_enabled": false,
  "stream": true
}
```

The `kb_ids` above are the exact ten IDs in `run-manifest.json`; keep this allowlist unchanged on every request. Keep automatic HTTP/provider retries at zero; the Agent's built-in relevance retry policy remains observable and may execute up to three retries. Capture actual response model identity, request counts by `chat`/`embedding`/`rerank`, rerank status/latency/batch indexes, tool calls/retries, grade output/status, final answer status, citations, prompt budget result, and API/SSE terminal status. Redact credentials.

Three representative cases, at most, per mode:

1. CN direct printer question `terra-ecs-b1-024` (existing development qrel; same ten-KB IDs). It was previously a synthetic response-capture case, not a real answer-quality run.
2. EN time-limit question `qst_0032` (“What is the default time limit for the short-lived cross-account debugging access sessions in the proposal for an auditable broker role for SREs across AWS and GCP?”). Existing qrel and prior capture use the explicit ten-KB scope. Do not send its Gold to the application.
3. Structured-PDF follow-up from the frozen QA500 history: seed with `legacy-session5-v3-s1-t01` (Pew 2016 sampling-error table), then ask `legacy-session5-v3-s1-t02` (the corresponding Clinton-voter figures). Preserve the exact frozen user text. For stateless Chat, include only the real assistant response captured from the first live turn as the second request's prior assistant message; do not synthesize history or insert Gold. This checks stateless history compatibility, not server-side history migration.

Compare Chat vs Agent retrieval candidate IDs/order and rerank scores for matching questions, while preserving the distinction that Agent overfetches 20 candidates before reranking and normal Chat starts with top-k 10. Evaluate retrieved evidence and final answers after capture against qrel/answer points offline; do not treat the Agent's in-workflow grader score as final-answer correctness.

## Stop conditions / unresolved items

- No matching active BAAI `RERANK` binding or unconfirmed `/rerank` route/schema: stop before external calls.
- Non-200 response, empty/invalid indexes, `rerank.chat_fallback`, unknown provider state, or request-budget overflow: preserve the receipt and mark that case failed/unknown; no silent retry/reconfiguration.
- Any change to the explicit KB allowlist, Qwen identity, top-k 10, rerank top-k 10, Hybrid weights, context window, rewrite flag, or session scope invalidates this comparison and must be reported as a separate config.
- This task performed no execution: regression status is `NOT RUN`.

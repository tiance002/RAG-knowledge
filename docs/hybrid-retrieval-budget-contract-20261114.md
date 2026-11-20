# Hybrid 检索预算与并发合同

2026-11-14。本文按当前本地候选源码核对，描述配置和实际执行边界；不把本地实现、隔离服务测试和正式 ECS 部署混为一项。运行版本及最终验证状态由 `docs/hybrid-concurrency-implementation-and-validation-20261114.md` 记录。

## 1. 请求总量与默认策略

入口为 `RetrievalService._hybrid_search()`，预算由 `RetrievalBudget.from_environment(target_count, base_query_count)` 创建。

- `target_count` 是请求范围内去重后的 KB 数量。
- `base_query_count` 是 `distinct_queries([query, original_query])` 的数量；只去除空白/大小写等价的重复项，保留实际查询文本。
- 以下 depth 都是**本次请求总量**，不是每个 KB 各领一份；初始词法额度还要由全部 base query × KB 组合共享。

| 环境变量 | 未设置时的值 | 显式配置范围 |
|---|---|---|
| `RAG_RETRIEVAL_VECTOR_DEPTH` | `min(150 × target_count, 500)` | 1～1000，总量覆盖默认值 |
| `RAG_RETRIEVAL_BM25_INITIAL_DEPTH` | `min(50 × target_count × base_query_count, 500)` | 1～1000，总量覆盖默认值 |
| `RAG_RETRIEVAL_BM25_SUPPLEMENT_DEPTH` | 50 | 0～1000，所有补充组合共享 |
| `RAG_RETRIEVAL_MAX_VARIANTS` | 5 | 0～5 |
| `RAG_RETRIEVAL_MAX_VARIANT_TARGET_PAIRS` | 8 | 0～64 |
| `RAG_RETRIEVAL_TARGET_INFLIGHT` | 2 | 1～8，同请求目标读取并发 |
| `RAG_RETRIEVAL_DEADLINE_SECONDS` | 30 秒 | 0.01～300 秒 |

非法值明确报错。两路默认上限分别继承旧的 Vector 150/目标和 BM25 50/base query/目标，再各自封顶；不是要求两路必须取相同值。直接构造 `RetrievalBudget()` 的 dataclass 默认仍为 150/50。

| 去重 KB 数 / base query 数 | 默认 Vector 总量 | 默认 BM25 initial 总量 |
|---|---:|---:|
| 1 / 1 | 150 | 50 |
| 2 / 1 | 300 | 100 |
| 1 / 2 | 150 | 100 |
| 2 / 2 | 300 | 200 |
| 10 / 1 | 500 | 500 |
| 10 / 2 | 500 | 500 |

`fair_depths()` 按已排序的唯一 KB 列表、base query 顺序做整除分配，余数分给前面的组合。补充组合按 variant 顺序、KB 顺序排列后取配置上限，再共享一个 supplement depth。最终返回数量 `top_k` 与这些额度分开；本地 `search()` 缺省 `RETRIEVAL_TOP_K=100`，真实 Recall 对照固定 `top_k=10`。

## 2. 一次 Hybrid 的实际流程

1. `_lexical_execution_scope()` 决定是否可使用独立只读 SQLite reader。可并行时先提交首轮 BM25；调用方所属任务顺序准备模型绑定、目标维度、查询向量及向量范围，再提交不访问调用方 Session 的目标读取。
2. 首轮向量只使用 `query`；词法使用去重后的 `query` 和可选 `original_query`。初始查询不会在补充阶段重做；词法变体不会增加 embedding 调用。
3. `_vector_scope()` 在 Chroma 查询前限定有效文件、活动 build 或 legacy SQL ID；BM25 `search(live_only=True)` 在评分/TopK 前过滤其可检查的来源范围、删除文件、父块及失效历史版本。
4. 两路回收后，`_authorized_route_rows()` 用每批最多 500 个候选 ID 做轻量 SQL join：排除删除 KB/文件、父块、失效版本、KB 标签不一致及孤立来源。活动 build 必须属于该文件/KB、状态 `ACTIVE`、manifest hash 正确且候选存在于其 `chunk_ids`。这里不读取候选正文、生成 provenance 或加载文件 lineage。manifest 在本请求内缓存。
5. 对**有效且按 chunk ID 去重的初始并集**计数，仅当数量小于本次 `top_k`、预算未耗尽时，调用已有本地关键词扩展。默认最多五个变体、八个 variant × KB 组合、补充候选请求总深度 50；发生局部补充错误时保留已成功的首轮/补充结果，停止后续变体。UNKNOWN 提供商结果不自动重试。
6. 所有词法查询先折叠成一个 lexical 列表，再与一个 vector 列表融合。`_hybrid_search()` 取融合 TopK，`_enrich_results()` 在读取最终正文前再次验证来源，`search()` 随后执行原有 ID/同来源内容去重。因此交付数量可能小于 `top_k`；初始“数量足够”不代表语义质量足够，也不保证内容去重后补满。

## 3. 排名与投票

`aggregate_bm25_results()` 对每个查询的跨 KB 结果按 BM25 原始分数降序、chunk ID 升序排列；同一 chunk 命中多个变体时，取最佳变体名次，再以原始分数和 ID 稳定排序。保留 `bm25_query_matches` 便于追查，**不累加命中次数或变体票数**。

`weighted_rrf()` 的向量列表按原始相似度降序、ID 升序；每个 chunk 在每条路由最多贡献一次。RRF 常数为 60，默认权重 0.7/0.3，最终按 RRF 分数降序、ID 升序，再对融合分数做 Min-Max；分数全相等时归一为 1。路由原始分数不在 RRF 前做 Min-Max。本次没有更改权重、阈值、RRF 或同来源去重算法。

## 4. 并行、线程容量与取消

`RAG_HYBRID_PARALLEL_ENABLED` 缺省 `true`，但只有受管 lexical executor 存在、调用方为真实 SQLModel Session、SQLite 文件数据库/独立 Engine、没有待提交修改且驱动未处于实际事务时，才启用独立 reader。内存库、共享连接池、未管理的 CLI、非 SQLite 和未提交写入均保留串行。串行与并行使用**相同预算、范围、有效性检查及排名流程**。

FastAPI `lifespan()` 管理 `managed_lexical_executor()` 和可选的 `managed_chroma_executor()`：

| 容量层 | 当前代码默认与边界 |
|---|---|
| 单请求目标读取 | `RAG_RETRIEVAL_TARGET_INFLIGHT=2`，BM25/Chroma 共享该请求 gate；滑动窗口不一次创建全部目标任务 |
| lexical 进程执行器 | 两个线程；工作项自己创建 Session，独立 `NullPool` 连接设置 `PRAGMA query_only=ON` |
| Chroma 进程执行器 | 开关缺省 `false`；启用后 `RAG_CHROMA_QUERY_WORKERS` 源码缺省 2，范围 1～16；`backend/.env.example` 写的是 1，不能把示例值当运行值 |
| HTTP 请求准入 | 独立的 `request_admission.get_gate()`；源码默认 active 2、queued 4、排队 5 秒，分别由 `RAG_CHAT_MAX_ACTIVE/MAX_QUEUED/QUEUE_TIMEOUT` 配置。它不等同于线程数或模型并发 |

两个线程执行器都先获取容量再 `pool.submit()`；使用 `shield` 等待，容量由底层 concurrent Future 真正完成后的回调释放。请求取消/超时不会提前归还仍在运行的线程名额；关闭时先等待真实工作结束，再释放自有连接/客户端。

30 秒是检索预算，异步等待到期会停止新调度并取消等待包装；已完成的有效证据可以降级返回。无完成证据且检索未完成时不能冒充正常空结果；已开始的付费 embedding 被截止时间取消时按 UNKNOWN 处理，不重放。它不是“整条 Chat 必在 30 秒结束”的保证。

同步的调用方 SQL、未启用 executor 的直接 Chroma，以及已运行的线程不能被 Python 协程即时终止。Chroma REST worker 另有 `RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS=30` 的单次 HTTP I/O 限制，适配器只支持实际检查过的 `chromadb==1.3.7`；PersistentClient 没有该 HTTP 限制。SQLite 的连接 timeout 是锁等待参数，不是整条查询执行上限。

## 5. Embedding 绑定与请求内复用

`RAG_QUERY_EMBEDDING_REUSE_ENABLED` 缺省 **false**。启用时使用本请求自己的 `RequestQueryVectors`；不跨请求、会话或 KB 授权范围保存缓存。目标准备顺序执行，避免同一 identity 并发 miss。

`_known_target_dimension()` 先从所选目标的有效 ACTIVE manifest 验证文件/KB 归属、hash 和一致的正整数 `dimension`。legacy、缺失、损坏、维度未知或混合维度均不推测，记录 UNKNOWN 并跳过跨目标 memo；返回向量长度不符时也不缓存/复用。

`_query_embedding_reuse_key()` 包含精确查询、binding ID/source/type/context length、原始 base URL、实际 endpoint、model、credential、目标维度与排序后的授权范围；现有请求没有 dimensions 参数，键同时明确记录该请求选项未指定。**含凭证的键不得输出或持久化**。仅支持已检查的单文本 `/v1/embeddings` 合同；活动 effect scope、图片查询、Doubao/Volces 或身份不完整等情况绕过复用。KB 模型绑定使用 `use_default=False`，不能拿另一默认模型替代既有向量空间。

隔离候选测试通过明确设置 `RAG_QUERY_EMBEDDING_REUSE_ENABLED=true` 启用该能力；不能据此说代码默认开启或正式服务已经开启。

## 6. 候选、上下文、Token 与费用是不同边界

| 边界 | 当前实际规则 |
|---|---|
| 候选深度 | 上述请求总 depth，限制目标返回/保留深度；BM25 精确评分仍遍历匹配 postings。Chroma 在有限 scope page 中各请求目标 depth，再按该目标归并截断，物理工作量不能直接等同于一次总 depth |
| 父块/邻居扩展 | `parent_context.expand_parent_context()` 不再设置单父块字符上限；父块按来源/子块包含关系校验，并受 `CONTEXT_MAX_CHARS=24000` 聚合扩展额度约束。总预算不足时回退到完整命中 seeds 和可验证邻块；真实 seeds 必保，必要内容可能导致 `mandatory_overrun`。该额度不是绝对输出字节/Token 上限 |
| 辅助选择器：`NOT_WIRED` | `context_delivery.bound_context_delivery()` 已实现按可验证源成员保留 seeds/已知结构、选择完整可选内容和标记未知映射；当前 `backend/app` 中仅有定义，没有主路径调用，不能把它当作本次实际生效的额外额度门 |
| 可选 Rerank | `_batch_rerank_steps()` 缺省每批 8196 **估算 Token**，默认返回 20；查询也计入估算，超长单候选有现存前缀截断逻辑。它不是供应商 tokenizer 的精确上限 |
| 最终模型请求 | `grounded_generation.enforce_request_budget()` 对完整 messages/tools 的 JSON 序列化计算 Unicode 字符；缺省 `GROUNDED_REQUEST_MAX_CHARS=32000`，超限在付费发送前报错。供应商 Token 限制仍是额外条件 |

检索线程/候选额度不替代模型 Token、提供商限流或费用核算。上述字符与 Token 规则在本次保持原样。

## 7. 可核查状态与源码入口

源码合同定向测试：`var/hybrid-request-budget-20261114/scope-default-final.xml` 为 91 项通过；根集成 `var/hybrid-concurrency-20261114/integrated-tests-v3.xml` 为 229 项通过，两者均 errors/failures/skipped 为 0。

已下载的 R6 真实 Recall 产物 `ab500-ready/summary.json` 记录显式 500/500、每臂 40 次全部完成，两臂完整引文覆盖均 44/66；该测试关闭 Rerank，没有生成答案。`serialpair/summary.json` 是同预算的串行/并行比较。它们支持相应隔离运行结论，**不构成 Rerank/答案生成质量验收，也不能自动证明 R7 新默认版本已启动或已发布正式服务**；后者须看最终报告与实际版本证据。

| 源文件 | 本文对应函数 |
|---|---|
| `backend/app/services/retrieval_budget.py` | `RetrievalBudget.from_environment/metrics/wait_for`、`fair_depths`、`RequestQueryVectors` |
| `backend/app/services/retrieval.py` | `_hybrid_search`、`_budgeted_hybrid_routes`、`_lexical_execution_scope`、`_vector_scope`、`_authorized_route_rows`、`_enrich_results`、`_known_target_dimension`、`_embed_query_for_kb`、`aggregate_bm25_results`、`weighted_rrf` |
| `backend/app/services/query_expansion.py` | `distinct_queries`、`expand_queries` |
| `backend/app/services/vector_store.py` | `query`、`query_async`、`_prepare_query_timeout_adapter` |
| `backend/app/services/bounded_chroma_executor.py` / `bounded_lexical_executor.py` | 各 executor 的 `run/close` 与 lifespan context manager |
| `backend/app/main.py` / `backend/app/services/request_admission.py` | `lifespan`、`get_gate`、`acquire_request`、`AdmissionLease` |
| `backend/app/services/parent_context.py` / `context_delivery.py` / `rerank.py` / `grounded_generation.py` | `expand_parent_context`、`bound_context_delivery`、`_batch_rerank_steps`、`enforce_request_budget` |

历史 150/50 总量候选的证据损失保留在 [原始失败复核](hybrid-quality-loss-review-20261114.md)，不能用后续 500/500 成功结果改写当时观察。

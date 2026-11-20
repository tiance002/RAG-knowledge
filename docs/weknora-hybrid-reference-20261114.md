# WeKnora 混合检索与多会话并发参考（固定源码快照）

日期：2026-11-14
用途：为 `D:\RAG knowledge`（RAG-knowledge）的 Hybrid 与并行会话方案提供上游实现对照。本文记录上游事实与适配提醒，不是本项目已采用的实现决策。

## 来源、范围和复核边界

- 官方仓库：[`Tencent/WeKnora`](https://github.com/Tencent/WeKnora)。以 GitHub API 的 `main` commit 与 `git ls-remote` 核对，均为 `cbdcebabf48c5ee2d6868b370326aa8341703f62`，2026-11-14；本地浅克隆 `var/hybrid-concurrency-20261114/reference` detached at 此 SHA。仓库根目录的 `HEAD` 可能继续前进，本文代码结论仅对应此固定 SHA。
- 官方源码快照：[commit](https://github.com/Tencent/WeKnora/commit/cbdcebabf48c5ee2d6868b370326aa8341703f62)。主要证据路径均以该 commit 固定链接，避免引用可变 `main`。
- 官方架构文档：[RAG pipeline](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/website-docs/02-architecture/04-rag-pipeline.md)、[retrieval engines](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/website-docs/03-features/05-retrieval-engines.md)、[installation / Lite](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/website-docs/01-getting-started/02-installation.md)。实现细节以下以 Go 源码为准。
- 本次读取 D 项目根目录的 `AGENTS.md`、`docs/bm25-jieba-required-20261114.md` 和 `docs/hybrid-parallel-20261114.md`。该仓库没有根目录 `progress.md`、`docs/adr/README.md` 或 `docs/decisions/v1-retrieval-decision.md`；未从缺失文件推断项目决策。此处只记录上游源码比较，不改产品代码。

## 1. 请求参数和真实候选深度

### `embedding_top_k` 如何进入检索

- 普通 Knowledge QA 从租户 `RetrievalConfig` 取 `EmbeddingTopK`、`VectorThreshold`、`KeywordThreshold`、`RerankTopK`。配置的有效 TopK 默认值是 **50**，向量阈值默认 0.15，关键词阈值默认 0.3，RRF `k=60`、权重默认 vector=0.7 / keyword=0.3。见 [`internal/types/retrieval_config.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/types/retrieval_config.go) 与 [`session_knowledge_qa.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/session_knowledge_qa.go)。
- 搜索 API 的 `MatchCount` 或 rerank `TopK` 可覆盖 rerank 返回数；源码随后强制 `EmbeddingTopK = max(EmbeddingTopK, RerankTopK)`，保证候选召回深度不低于末端所需结果数。Search pipeline 将该值作为 `SearchParams.MatchCount` 传入。见 [`applyKnowledgeSearchOverrides`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/session_knowledge_qa.go) 和 [`chat_pipeline/search.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/chat_pipeline/search.go)。
- HybridSearch 入口先把 `MatchCount <= 0` 归一为 50，并把极大值限到 500。接着计算一次过召回深度：`max(MatchCount * 5, DefaultRetrievalTopK=50) * len(searchKBIDs)`，整个检索池上限 500。该 `matchCount` 被同值赋给每个 vector / keyword `RetrieveParams.TopK`；不是分别给 keyword 更大的 K。只有阈值不同：vector 使用 `VectorThreshold`，keyword 使用 `KeywordThreshold`。一组同时含 document/FAQ KB 时可产生 document-vector、FAQ-vector、document-keyword 三种检索参数，其中 FAQ 只走向量，document 可走关键词。见 [`knowledgebase_search.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/knowledgebase_search.go)。
- 重要计数边界：公式中的 `len(searchKBIDs)` 是该次 `HybridSearch` 的 KB scope 数，不是整个聊天 SearchTargets 数。全 KB targets 可合并为一次多 KB HybridSearch；指定 document/tag 的 targets 各自走调用。后者由 `search.go` 并发发起，因此每个单 KB 调用自己的按 KB 扩大计算为 `max(MatchCount*5,50)`，然后再跨 target 合并。不要将这一公式简化成“全请求的 target 数倍数”。
- 自定义 Agent 配置把 `embedding_top_k` 缺省为 **10**，而 tenant RetrievalConfig 的默认有效值是 50；因此讨论具体请求时，需先说清请求来自普通 Quick/Knowledge QA、Agent 配置还是独立 search API，不能把一个默认值当成全局默认。见 [`custom_agent.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/types/custom_agent.go) 与 [`retrieval_config.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/types/retrieval_config.go)。

### 多 KB 分组深度和 FAQ 迭代

- 多 KB scope 按绑定检索 store 与 owner tenant 分组。`HybridSearch` 前只对 query text 计算一次 query embedding 并放进 params 传给各 store group；如果调用者已预先携带 embedding，也会复用。Chat pipeline 另按“实际模型身份”分组多个 SearchTarget，组内只 embed 一次，再把 query embedding 传给组合 KB 或单 target 检索。模型身份不一致的跨 KB scope 会校验并拒绝，避免比较不兼容的向量空间。见 [`knowledgebase_search.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/knowledgebase_search.go)、[`knowledgebase_search_storegroup.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/knowledgebase_search_storegroup.go)、[`chat_pipeline/search.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/chat_pipeline/search.go)。
- store-group 数为 1 时直接调用 engine；大于 1 时用 `errgroup` 并发、单请求最多 4 组、每组默认 30 秒超时。空结果伴随失败的 group 会使全请求失败并取消兄弟组；若 engine 返回了部分结果和错误，则保留可用结果并记录 warning。混合 engine 类型时，仅在服务层按 engine/retriever 归一化向量分到 [0,1]。见 [`knowledgebase_search_fanout.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/knowledgebase_search_fanout.go)。
- FAQ primary KB 的特殊迭代：若融合、去重后少于 MatchCount，且某个 vector list 已达当前深度，可从 `max(matchCount*3, initialDepth*2)` 深度开始重查，最多 5 轮、TopK 逐轮翻倍、全程上限 500，跨 store group 同步增长；命中负向问题的 FAQ 排除后再判断是否够数。此迭代不是所有普通 document 搜索都会运行。见 [`knowledgebase_search_faq.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/knowledgebase_search_faq.go)。

## 2. 并行检索、过滤、融合与截断顺序

### 并行边界

1. `PluginSearch.OnEvent` 把 KB 搜索与可选 web search 并发运行。
2. KB SearchTargets 按 embedding model identity 并发成组；同模型组复用 query embedding。每组内无 tag 的完整 KB scope 可合并为一个多 KB HybridSearch；文档级/tag scope 按 target 并发单独 HybridSearch。
3. 每个 HybridSearch 内，多 store groups 并发上限 4、每组默认 30 秒；单个 CompositeRetrieveEngine 里每个 `RetrieveParams`（通常 vector/keyword，混合 document/FAQ 时更多）由 goroutine 并发调用。Lite SQLite repository 的一次 `RetrieveParams` 单独执行对应的 FTS5 或 sqlite-vec 路由，因此两路并行由 Composite 层产生，而不是 SQLite `Retrieve()` 的单次调用同时跑两路。
4. `CHUNK_SEARCH_PARALLEL` 又可将 chunk search 与 graph entity search 并发；这是检索与实体查找的并发，不代表向量库本身一个查询被切为并行 shard。

代码：[chat_pipeline/search.go](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/chat_pipeline/search.go)、[search_parallel.go](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/chat_pipeline/search_parallel.go)、[composite.go](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/retriever/composite.go)。

### 结果顺序

1. 后端对各自召回结果应用 KB/document/tag/is_enabled 等 scope filters，并使用自己的 TopK、阈值支持。多-store 混合 engine 结果必要时先做 score normalization。
2. 服务层将每个 `RetrieveResult` 保留为独立列表，再按 retriever type 分为 vector lists 和 keyword lists；列表从并发任务 append，列表次序/跨列表拼接位置不用于全局名次。
3. 如果只有向量：按 chunk ID 去重，保留最高分、按分数降序。只有关键词：先对大于 1 的 BM25 分数在各自列表内按最大值缩放至 [0,1]，再按 chunk ID 去重、排序。两路都有：按每个原始列表独立排序/去重后取 chunk 在同类 lists 中最好 1-based rank，进行加权 RRF：`vectorWeight/(k+vectorRank) + keywordWeight/(k+keywordRank)`，再以理论最大值 `(vectorWeight+keywordWeight)/(k+1)` 归一到 [0,1]；同 chunk 优先保留 vector 元数据，按 RRF score 降序输出。默认 `k=60`, weights 0.7/0.3。见 [`knowledgebase_search_fusion.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/service/knowledgebase_search_fusion.go) 与 [`retrieval_config.go`](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/types/retrieval_config.go)。
4. 若适用，FAQ negative-question filter / iterative recall 在融合后处理。随后 `processSearchResults` 最多检查前 500 个融合候选，回查 knowledge 状态；取消解析状态的候选不占名额，直到选足 primary MatchCount 或候选用尽；再批量查 chunk 元数据，并按调用选项补 parent/related/nearby context。`SkipContextEnrichment=true` 时跳过这类扩展。检索输出此处截到 MatchCount；pipeline 后续还可 rerank/filter 到 `RerankTopK`。

### 图谱/查询扩展额外路径

- chunk search 与 entity search 并行，完成后先按精确 chunk ID / 内容签名去重，再进入后续合并。查询扩展只在配置启用且首轮结果少于 `EmbeddingTopK` 时启动，采用关键词偏向路径；具体候选扩大另有实现。这个分支不应和 vector+keyword 两路 hybrid 当成同一个召回池来计数。

## 3. embedding identity、会话隔离、取消与 SSE

- Chat 检索按 `ResolveEmbeddingModelKeys` 得到的真实模型 key（说明含模型名称、endpoint、配置维度）分组，不只按 display name 或模型数据库 ID 字符串判断相同。每个 text model group 计算一个 query embedding；未能解析 identity 的 image-query 目标按 target 分别计算，因为 image vector space 不能共用 text embedding。HybridSearch 也把每次调用的 query embedding 预先计算一次并传给每个 store group。共享 embedding 的前提是实际模型/endpoint/dimension 表示同一向量空间。
- 每个 SSE QA 请求建立独立 `EventBus`、`context.WithCancelCause` async context 和消息流处理器。StreamManager 的事件按 `(sessionID, assistantMessageID)` 存储、继续流请求显式要求 `session_id` + `message_id`，通过 offset 读取/重放事件。Memory manager 的 map 以 session ID 再按 message ID 分层，并对事件 slice 加锁；这是进程内隔离/并发保护，不是跨进程共享保证。
- Stop API 验证消息属于指定 session 与 owner tenant，再对该 session/message append stop event。运行中的 watcher 消费 stop event 并取消该请求 async context。取消传播边界是该 turn 的 context；不能把它理解成进程级 cancellation token。不同 session 独立键控，因此可以独立发起会话请求；同 session 的 Agent live run 有 `SetLiveRun` 防重检查。源代码未在此任务中做并发压测，也未验证所有数据库后端在任意并发负载下的写入吞吐或锁行为。
- StreamManager 通过 factory 支持 memory / Redis 等不同策略；memory stream backend 明确是 single-process backend，不能用作多副本共享流。Lite 文档注明其运行时为单进程 + SQLite + 内存队列，不需要 Redis/Asynq。此部署形态决定跨节点续流不成立；它不意味着不能有多个不同 session 在本进程发起请求。

源码：[handler/session/qa.go](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/handler/session/qa.go)、[handler/session/stream.go](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/handler/session/stream.go)、[stream/memory_manager.go](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/stream/memory_manager.go)、[installation / Lite](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/website-docs/01-getting-started/02-installation.md)。

## 4. Lite SQLite 与其他检索 engine 的差异

- Lite 配置使用 `DB_DRIVER=sqlite`, `RETRIEVE_DRIVER=sqlite`, build tag `sqlite_fts5`, `STREAM_MANAGER_TYPE=memory`; FTS5 承担 keyword 检索，sqlite-vec 的 cosine vec0 表承担 vector 检索。见 [Lite installation docs](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/website-docs/01-getting-started/02-installation.md) 与 [SQLite repository](https://github.com/Tencent/WeKnora/blob/cbdcebabf48c5ee2d6868b370326aa8341703f62/internal/application/repository/retriever/sqlite/repository.go)。
- SQLite vector query 以 `TopK` 作为 sqlite-vec 的 `k`，cosine distance 转成 `1-distance` 后在 Go 层应用 `Threshold`。先取 K 再过滤，不会在这个方法里为低于阈值的空位补查。SQLite FTS5 query 以 `TopK` 限定 SQL `LIMIT`；本仓库该 keyword 方法没有读取 `params.Threshold` 做阈值过滤，而是输出经缩放/融合前的 BM25 score。不要假设 SQLite 的 keyword threshold 跟其它 backend 完全同义。
- Qdrant 例：vector `ScoreThreshold` 直接传入服务端 query；keyword 使用 TopK 搜集 token 命中再合并/截断，不能仅凭共同 DTO 就假定阈值语义等同 SQLite。OpenSearch 例则把阈值分别送入 vector/keyword query builder。其它插件后端也可能有自己的 score 标度、topK 调整和过滤位置。
- SQLite 是单机嵌入式 vector/FTS 索引 + SQLite 文件 DB，适合 Lite 单进程形态；PG/pgvector、Qdrant、OpenSearch、Milvus 等各有服务端索引/查询语义与并发特征。WeKnora 的 RRF 在应用服务层共用，但各 engine 产出的候选、过滤、阈值位置和原始 score 不应视为完全一致。架构可借鉴接口/管线结构，具体 TopK、阈值、score normalization、并发上限须按目标后端本地实现和评测确认。

## 对 RAG-knowledge 的可借鉴点与禁止直接搬用项

- 可借鉴：清楚地区分 request-level 并行、同一次 hybrid 内 vector/keyword 并行、跨 store fan-out；按完整 embedding identity 减少重复 embedding；为每个会话+消息独立留存 SSE 事件，并让 stop 只取消对应 turn；设定候选深度上限，记录 filter → fuse → dedupe → truncate 的确定顺序。
- 不直接搬用：`MatchCount*5*KB_count`、500 hard cap、默认 `EmbeddingTopK=50`、阈值 0.15/0.3、RRF k/权重、四路/30 秒 store 限制以及 FAQ 五轮倍增均为 WeKnora 特定缺省/架构决策。特别是 SQLite 的关键词 threshold/向量阈值实现位置不同于其它 engine；不得当成自研 SQLite 的默认阈值设计依据。
- 应以 EviRoute 当前的 [Hybrid 首轮双路并行说明](hybrid-parallel-20261114.md)、[固定 Jieba 说明](bm25-jieba-required-20261114.md) 和当前数据库/索引实现为准；WeKnora 实现细节只是一组上游参考事实，不能替代项目自己的接口、索引语义与离线评测。

## 本次核查结果

| 项 | 结果 |
|---|---|
| 官方仓库身份 | PASS；API 和 `git ls-remote` 的 HEAD SHA 相同 |
| 固定 commit | PASS；`cbdcebabf48c5ee2d6868b370326aa8341703f62` |
| 本地快照 | PASS；`var/hybrid-concurrency-20261114/reference` detached checkout |
| 源码路径核验 | PASS；检视 search pipeline、hybrid service/fusion、SQLite retriever、SSE/session stream 与 Lite 文档 |
| WeKnora 上游测试/模型/数据库 | NOT RUN；本次是只读上游实现比较 |
| 未知项 | 无任何专门测试证明 WeKnora 上述具体版本在高并发/SQLite 写入场景下容量或质量；本参考不作此类断言 |

## 2026-11-15 补充：SQLite 数据库连接层的串行化

重新核对同一固定 commit 的 internal/container/container.go：SQLite DSN 启用 WAL、busy_timeout=5000；随后 SQLite 分支设置 sqlDB.SetMaxOpenConns(1)。因此 Composite 层 goroutine 并发发起 vector/keyword，不等于两条 SQLite SQL 同时运行；数据库操作排队等待同一连接。repository.go 中关键词/向量读使用 r.db.WithContext(ctx)，写入事务使用局部 tx，不是将调用方未提交事务共享给并行查询。事务占用唯一连接时，另一路读等待连接释放；外部连接引发锁冲突时则由 SQLite busy timeout 和错误处理约束。busy_timeout 不代表连接池等待上限，更不保证看到未提交数据。以上为源码分析，未进行 WeKnora 实际数据库压测。

# 混合检索与多会话并发：实施与隔离验证报告

日期：2026-11-14。项目：`D:\RAG knowledge`。本轮不提交、不推送，不更新或重启正式 API/Worker，不改模型、Prompt、Gold、切块及索引。测试先汇总问题，再依据可复现证据修复；集中问题见 [问题账本](hybrid-concurrency-issues-20261114.md)。

## 1. 结论及适用范围

**PASS：本地预算、准入、取消与会话隔离定向回归；ECS 只读隔离环境的检索、两会话及四会话测试。正式部署 NOT_RUN。**

最终建议保留单进程、2 活跃请求、4 排队、最长等待 5 秒、Chroma 1 查询线程、BM25 2 线程、单请求目标并发 2。不把四会话的排队当成四个活跃模型请求。四会话没有显著吞吐收益，尾延迟明显增加，故不继续八会话；未提高 Chroma 内存上限。

整体候选相对原 ECS 基线的检索平均耗时下降约 **52.83%**，P95 下降约 **49.57%**。相同新预算下仅切换串行/并行，平均耗时下降约 **5.22%**，P95 下降约 **4.28%**。不能把整体收益全部归因于双路并行。

20 道开发题两次重复的完整引文覆盖，基线和最终 500/500 候选均为 **44/66**，每一单元的状态相同。这是指定 quote 在交付文本中的匹配，**不是回答正确率或完整 recall@10**。同预算串/并行中 7/40 对候选有变化；因此不宣称输出完全等价。

## 2. 版本、环境与现状核查

- 本地起点：分支 `codex/bm25-stats-write-optimization-20261112`，HEAD `7c7415f85a063e3f7645f676bd22f7c8f766a732`。大量已有未提交修改保留；没有 reset/clean/切换分支。
- 任务开始时冻结 98 份本地源码/相关输入，合计 1,234,141 字节，见 `var/hybrid-concurrency-20261114/local-baseline-manifest.json`。本轮差异以该快照比较，不能用整个 dirty Git diff 冒充本轮修改。
- ECS 正式 `/opt/amrag/backend` 没有 Git 元数据，按 81 份实际源码哈希冻结。正式 API PID **728757**、Chroma PID **567489**，结束时均保持不变；81 份正式源码无漂移。
- 本地 Python 3.13.0 / httpx 0.28.1 / chromadb 1.3.7 / sqlmodel 0.0.27 / SQLAlchemy 2.0.55 / openai 3.40.0。ECS 复用既有 venv，httpx 0.28.1 / chromadb 1.3.7 / sqlmodel 0.0.27 / SQLAlchemy 2.0.45 / openai 1.109.1。两边 jieba 均 **0.42.1**；没有安装或升级依赖。

| 能力 | 本轮前真实状态 | 本轮处理/最终状态 |
|---|---|---|
| Hybrid 初始双路并行 | 本地和 ECS 已有条件并行；不能只依据开关认定生效 | 复用既有 executor，增加请求总预算、多目标有界调度、实际 worker 计时；保留串行诊断 |
| Chroma、BM25 有界执行器 | 已存在并被 lifespan 管理；ECS Chroma 1 worker | 复用，不建立第二套线程池，不跨线程共用 SQLAlchemy Session |
| 查询向量复用 | 本地已有，正式 ECS 未开启；身份/维度覆盖需要完善 | 完整身份、授权范围和可信维度核验；默认仍 OFF，隔离测试明确 ON；十库绑定零外调等价检查通过 |
| 共享 Provider HTTP 池 | 本地已有，正式 ECS 缺对应模块/接线 | 隔离兼容补丁仅移植必要生命周期和借用 transport；不更改模型、超时或供应商重试策略 |
| 扩展触发及融合 | 已有本地关键词扩展、Weighted RRF | 有效来源过滤提前到初始并集统计前；总量/组合/deadline 受限；继续单路 lexical 聚合，不给变体重复投票 |
| 会话持久化/CAS/引用 | 最新本地已有，正式 ECS 尚不是该完整版本 | 复用已有接口；本轮补准入及取消漏洞，Chrome 验证独立会话；未把整套本地会话链覆盖到正式 ECS |
| 前端多会话同时生成 | 原单一 controller/busy 会干扰切换/提交 | 按会话保存 controller/submitting，并校验异步路由归属 |
| 普通 Recall 的异步重排 | 本地已有 async 实现，API 仍用同步调用；ECS 缺 async 依赖链 | 本地接既有 async 实现；ECS 兼容补丁保留原同步版本，本轮显式关闭重排，真实重排性能 NOT_RUN |
| `bound_context_delivery()` | 定义存在、无生产调用 | 标记 NOT_WIRED；不因本任务顺手接入新的上下文策略 |

## 3. WeKnora 参考与适配边界

官方固定 commit：[`cbdcebabf48c5ee2d6868b370326aa8341703f62`](https://github.com/Tencent/WeKnora/commit/cbdcebabf48c5ee2d6868b370326aa8341703f62)。源码克隆及 commit 已实际核对。完整路径和调用关系见 [官方源码对照](weknora-hybrid-reference-20261114.md)。

- `session_knowledge_qa.go` / `chat_pipeline/search.go`：普通检索有效默认 EmbeddingTopK 50，Agent 自定义配置可为 10；pipeline 用 max(EmbeddingTopK, RerankTopK)，不是一个全局统一 TopK。
- `knowledgebase_search.go`：每次 HybridSearch 在该次 scope 内计算 `min(max(MatchCount*5,50)*KB数,500)`。它不是跨全部 SearchTarget 的严格总预算；不能照搬后认为整个请求不超过 500。
- `knowledgebase_search_storegroup.go` / `retriever/composite.go`：分组与双路分别并发、参数独立；实际模型身份可共享时复用 embedding。组并发 4、组合上限 16 等不直接成为这台 ECS 默认值。
- `chat_pipeline/merge.go` 等保留顺序、融合及后续处理；SQLite Lite 是 FTS5 + sqlite-vec，其 keyword 阈值/分数语义不同于本项目的自建 SQLite 倒排。
- 会话 EventBus、独立取消上下文、session/message 流归属值得借鉴；本项目复用自己的后端会话和前端请求归属，没有引入第二套 StreamManager。

本轮保持自建 BM25、jieba 0.42.1、Weighted RRF k=60、权重 0.7/0.3；不引入 bigram、FTS5、PostgreSQL、pgvector，不复制上游 BM25 原始阈值。

## 4. 最终预算与执行合同

详见 [预算、执行及来源校验合同](hybrid-retrieval-budget-contract-20261114.md)。

| 层级 | 最终规则 |
|---|---|
| 用户目标候选 `top_k` | 独立于两路深度；本次固定 10，不修改正式默认值 |
| Vector 总深度 | 默认 `min(150*唯一KB数,500)`；`RAG_RETRIEVAL_VECTOR_DEPTH` 可覆盖全请求总量 |
| BM25 初始总深度 | 默认 `min(50*唯一KB数*基础查询数,500)`；`RAG_RETRIEVAL_BM25_INITIAL_DEPTH` 可覆盖全请求总量 |
| 多目标分配 | 排序后的唯一 KB/查询组合确定性均分、余数稳定分配；不让每个 KB 重领总额度 |
| 关键词补充 | 先完成初始有效 ID 并集；不足目标才扩展；最多 5 变体、8 个变体×目标组合、共享 50 补充深度 |
| 并发与截止时间 | 单请求目标并发 2，总检索 deadline 30 秒；停止新调度，已运行线程仍持有容量 |
| Rerank 输入/输出 | 保持既有估算 Token 分批及 `rerank_top_k`；本轮实际测试关闭，不推断质量或性能 |
| 父子扩展 | 保留原来源/ACTIVE manifest/父子类型检查与种子；原 4000/24000 字符策略不改，强制种子可能 `mandatory_overrun` |
| 最终模型请求 | 既有完整请求 32000 字符发送前检查不改；无可靠模型 tokenizer 时不能把字符写成精确 Token |
| 图片与说明 | 保留来源、区域/成员映射与“模型生成图像描述”标记，不冒充原文；本轮未调整图片预算 |

原 150 是每知识库向量查询深度，BM25 原 50 是每基础查询×知识库深度；本次改为不同路线各自的请求总量。对于十库单查询，总值恰好都是 500，是两条独立缩放规则分别触顶，不是要求两路相等。单库仍是 150/50。

有效性过滤 → 初始 ID 去重并集计数 → 必要时关键词补充 → 所有词法变体折叠为一个列表 → Vector/lexical Weighted RRF → TopK → 正文/来源再次校验及原有去重 → 可选重排/父子扩展。共同 ID 对每路最多一次投票，同分用稳定 ID 排序。父块合并数量不用于决定关键词扩展。

兼容向量复用键包含查询、真实模型/端点、凭据身份、兼容参数、授权 scope 以及经 ACTIVE manifest 确认的维度；身份不全、维度不明/冲突、effect scope 等情形保守绕过。凭据原值和内部身份指纹不输出。十库实际绑定均 BAAI/bge-m3，网络返回维度实测 1024。

Chroma 的深度约束不代表物理只读一次：有限过滤分页分别请求后归并。现有有限分页/deadline 避免无限补查；不能把逻辑 depth 当作磁盘读量上界。BM25 仍需遍历实际匹配 postings，候选额度也不等于扫描成本。

## 5. 修改文件与版本恢复

相对任务开始的源码快照，已有产品文件修改 **7 份**：

- `backend/app/api/chat.py`：改写/聊天准入，SSE 生命周期和许可归属。
- `backend/app/api/conversations.py`：服务端会话准入、重放兼容、排队后重新核对会话状态。
- `backend/app/api/recall.py`：单库/多库召回准入；多库空 scope 显式 400；本地复用 async 重排。
- `backend/app/services/conversation_runner.py`：取消终态先落库，后台 Provider 结束前继续持有准入许可。
- `backend/app/services/retrieval.py`：请求预算、分组有界调度、来源授权、向量身份及维度复用、可追踪指标。
- `backend/app/services/vector_store.py`：有效 scope 的有限分页与检索 deadline 约束。
- `frontend-weknora/src/pages/Chat.tsx`：按会话独立请求及路由归属，取消不误伤其他会话。

新增共用模块 `request_admission.py`、`retrieval_budget.py`；没有新建第二套 Chroma/BM25 executor。相关定向测试、浏览器验收、只读 ECS 探针及兼容补丁构建脚本一起保留。`scripts/conversation_acceptance_app.py` 仅修测试环境变量污染；`test_server_conversation_api.py` 增加唯一产物目录支持，以及主动取消/直接断线的参数化真实 ASGI 回归。

本轮源码哈希差异：`var/hybrid-concurrency-20261114/task-source-changes.json`。完整 Git dirty diff 含历史成果，不应用作本轮补丁。

ECS R7 兼容候选：`var/hybrid-concurrency-20261114/candidate-20261114-200248-118107/`，含 `candidate.diff`、`candidate-manifest.json` 和完整兼容源码。基于 ECS 81 份原始源码生成，**11 个文件变化**，不覆盖整份本地目录；正式 Rerank、融合等差异保留。R6→R7 仅预算构造两文件变化；主要性能表来自 R6 显式 500/500，R7 新默认在五题实际请求中验证同样生效，不能把它写成 R7 重新跑过全部容量测试。

恢复：本地原任务快照+各候选 diff/完整配置可重建；ECS 正式源码从未修改，故正式服务无需回滚。隔离候选停止后不接流量。后续若发布，须重新核对正式文件哈希、当前负载与备份，不直接把本轮整个工作区覆盖过去。

## 6. 测试输入与统计口径

- 只用 QA500 **开发集**，冻结选择 20 道：中文 10、英文 10。题目选择在看检索结果前完成；没有使用冻结 300 题或修改 Gold/qrel。
- `development20.json` 的 SHA256 `6496c8d219447a290afeff4aa612683da2bbb8a771d02c929cd7041dc84daa08`；上游 QA500 文件 SHA256 `141c46825db096a236c34bf3458dc1e0f5608550f7e1140b57a0e54feecdca0a`，本地/ECS 一致。
- 十个明确 KB ID 保存在该文件和每次原始 HTTP 请求中，始终非空。没有把空 scope 退化成全库；没有复制数据库/Chroma。
- 20 题含 33 个引文单元，21 可按来源哈希和 ACTIVE 子块精确映射，10 仅在父块确认、2 无完整同文，后 12 保留 UNKNOWN。它们不构成完整 chunk 级 qrel。
- Quote 指标仅移除 Unicode 空白并 casefold 后做完整子串匹配。引文缺失不能推出必要事实缺失，例如 `qst_0001` 完整 Gold 引文不在请求中，但请求有“billing activation — target: <=7 business days”，真实模型仍正确回答。
- 重复两次仍只有 20 道独立题；44/66 是 33 单元×2，不是 66 道题。图像派生说明仍标为模型生成，不把其引文当原始像素事实证明。

## 7. 实测结果

### 7.1 真实只读 Recall：质量与单请求耗时

固定 Hybrid、top_k=10、score_threshold=0、vector/BM25=0.7/0.3、context_window=1、rerank=false。真实 Embedding+Chroma+SQLite，未调用回答模型；每组按题交替/平衡顺序，不直接复用另一组结果。

| 对照 | 每臂请求 | 平均秒 | P50秒 | P95秒 | 完整引文覆盖 | 失败/UNKNOWN |
|---|---:|---:|---:|---:|---:|---:|
| 原 ECS 基线（ab500-ready） | 40 | 2.5745 | 2.5507 | 2.8019 | 44/66 | 0/0 |
| 候选500/500（ab500-ready） | 40 | 1.2143 | 1.2035 | 1.4130 | 44/66 | 0/0 |
| 新预算串行（serialpair） | 40 | 1.2937 | 1.2695 | 1.4666 | 44/66 | 0/0 |
| 新预算并行（serialpair） | 40 | 1.2261 | 1.1997 | 1.4038 | 44/66 | 0/0 |

前两行平均下降 1.3602 秒，P95 下降 1.3889 秒；后两行平均下降 0.0676 秒，P95 下降 0.0627 秒。各配对的引文覆盖均逐项相同。基线首次冷请求曾约 12.38 秒，已单独保留，不混入稳态结论；表中是后续有限样本，不代表长期线上 P95。

**复用的实际收益**：同一40请求，基线发出400次Embedding，候选40次，均成功返回1024维向量；只在兼容的十库绑定内减少90%重复调用。单次Embedding均值178.97→131.32ms、P95 251.30→191.80ms，但后两项受供应商/连接状态影响，不当作模型本身变快；没有usage/账单就不能换算节省金额。

阶段记录以实际时间线解释，不把并行耗时相加：

| 同预算40请求的阶段 | 串行均值/P95 ms | 并行均值/P95 ms | 口径 |
|---|---:|---:|---|
| Vector路线 | 875.73 / 987.22 | 1037.87 / 1180.40 | 包含该路线准备/排队，不是纯Chroma执行 |
| BM25路线 | 237.12 / 335.53 | 750.53 / 967.00 | 并行共享目标gate时会等待，不能拿相加结果解释请求总耗时 |
| 搜索返回 | 1284.59 / 1453.48 | 1216.94 / 1395.03 | 服务端search墙钟 |
| 实际Chroma/BM25 worker重叠区间并集 | 0 / 0 | 88.68 / 172.65 | 39/40并行请求有真实跨路worker重叠，串行0/40 |
| 已记录目标执行器队列 | 0 / 0 | 0.35 / 1.02 | 各400条被记录工作项，不是整机队列 |

单独融合、父子扩展耗时和完整Chroma内部队列在这版隔离探针中未全部拆出，记UNKNOWN；没有用“总耗时减两路之和”伪造它们。重排关闭、关键词补充均未触发，不能拿0当它们真实开启时的性能。完整阶段数组及真实worker区间保留在receipts，独立统计见 `audit-aggregate.json`、[证据复审](hybrid-concurrency-evidence-audit-20261114.md)。

**失败候选未隐藏**：最早总150/50候选20对平均3.11→1.62秒，但引文22/33→19/33，三项丢失，所以不采用该总深度。仅对一套预先声明的通用500/500替代配置复测，未按题号调预算。见 [三项损失复核](hybrid-quality-loss-review-20261114.md)。

独立复核还计算了**可映射子集**的证据 Hit@K：将同一引文单元的多个可替代子块 ID 作 OR，使用 `RetrievalService.search()` 返回的融合后Top10、父子扩展前的 ID。两臂各40次均未触发关键词补充，结果如下，不是全题集召回率：

| 指标 | 基线 | 候选500/500 | 分母 |
|---|---:|---:|---|
| 证据单元Hit@1 | 18/42（42.86%） | 18/42（42.86%） | 21个已映射单元×2 |
| 证据单元Hit@5 | 30/42（71.43%） | 30/42（71.43%） | 同上 |
| 证据单元Hit@10 | 34/42（80.95%） | 34/42（80.95%） | 同上 |
| 至少命中一项已映射证据的问题Hit@10 | 18/22（81.82%） | 18/22（81.82%） | 11道有映射的题×2 |

另12个UNKNOWN单元（重复后24项）不塞入已映射分母，也没有从总运行记录中删掉。全33单元的完整quote覆盖仍是44/66。上述两种指标的取样对象/阶段不同，不能互换。

**排名限制**：serialpair 33/40 有序 ID+正文完全相同；7/40 不同（2 仅次序，5 替换一个搜索返回 Top10 成员）。7 对都在父子/邻块上下文扩展前的 `RetrievalService.search()` 返回时已发生，共同块正文哈希和 BM25 分相同、向量分变化。该返回已包含查询扩展/融合，不能冒充融合前完整路由候选。现有证据不能区分 Embedding 数值变化、ANN、浮点或并发影响；没有保存查询向量和完整路由池，原因 UNKNOWN，不修补没有证据证明损坏的 RRF。详见 [排名差异审查](hybrid-serial-parallel-ranking-review-20261114.md)。

### 7.2 多会话容量

每档20题×2次；同一 active=2/queued=4/5秒配置，不增加 Uvicorn worker。这里是客户端并发检索，不是长回答完整对话容量。

| 同时客户端请求 | 请求数 | 平均秒 | P50秒 | P95秒 | 吞吐请求/秒 | 最低MemAvailable | 引文覆盖 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 2 | 40 | 2.2472 | 2.2571 | 2.5964 | 0.8405 | 2011447296字节 | 44/66 |
| 4（最多2活跃） | 40 | 3.6318 | 3.7734 | 4.9307 | 0.8439 | 2004987904字节 | 44/66 |

两档80次全部完成，无错误、UNKNOWN、内存/磁盘越线。四会话吞吐只增约0.42%，P95增加约89.90%；不建议通过放开背压追求 CPU100%，8会话 NOT_RUN。吞吐以真实墙钟计算，单请求两路耗时不相加冒充请求时间。混合 A/B 批次整体 QPS 不拆成每臂吞吐。

准入等待也已实际出现：2会话平均24.85ms/P95 75.79ms，4会话平均1141.81ms/P95 2699.91ms，能解释大部分四会话额外时延。隔离API设置MemoryMax600MiB、CPUQuota100%（最多约1个CPU）、TasksMax96来保护同机正式业务；两档分别有约8.52/7.62秒cgroup CPU throttled时间。这是保守配额下的负载实验，**不是整台2CPU ECS的极限吞吐**，也不能将所有尾延迟归咎网络。

### 7.3 实际模型请求及会话隔离

1. **五题×两臂实际 SDK 请求捕获**：只拦截模型 HTTP 网络出口，检索仍真跑；模型响应明确 SYNTHETIC_CAPTURE_ONLY。十次均完成，真实 Chat 外调0。每臂10个引文单元中7个完整引文进入实际 messages，逐项相同；4/5题消息正文完全相同。打印机题9009→8940字符、22286→22487 UTF-8字节，4项引文全部在。字符减少不代表字节必减少。`qst_0032` 的30分钟引文已进入实际请求。未把“扩展选中”冒充“已送达”。
2. **真实云端 Qwen/Qwen3.5-4B 三题冒烟**：打印机位置、7个工作日、30分钟；主代理依据冻结Gold及实际返回证据复核，三项核心答案吻合。这不是独立校准Judge或负责人确认，脚本未实施质量打分，仍记`answer_quality=NOT_RUN`。显式 `enable_thinking=false`、输出4096不改，三次 finish 正常。延迟10.77/6.27/12.94秒，Input共12652、Output共606 Token。只作三题冒烟，不宣称统计正确率或长期性能。
3. 其中打印机题第二结论引用[2]不支持，真实依据在[1]；核心答案正确不等于引用通过。保留该问题，不改Prompt/校验阈值。三题 `expansion_delivery_status` 均为 PARTIAL，未伪装 DELIVERED。
4. 当前生成是**非流式生成后发送 SSE**。首答案增量接近全部耗时，不能当底层模型 TTFT；模型首 Token 延迟 UNKNOWN。
5. 探针原来把 SDK 外层和借用连接池内层的 `AsyncClient.send` 都记录；前两题各有两条边界，不能算两次付费请求。修正仅探针后，用第三道新题验证 depth0 `ProviderBorrowingTransport` + depth1 `AsyncHTTPTransport`，**一次网络尝试**，不重发旧题。前两题原记录保留；当时没有层次字段，物理网络计数依实现关系复核，未声称有独立 wire 计数。费用 UNKNOWN，不估算账单。
6. **真实 Chrome 27项通过**：真实 API/store/BM25，唯一替换 Provider HTTP 为 MockTransport。两个会话同时生成，SPA切换、取消A、B继续、刷新及新浏览器上下文回读均正常；真实保存A=CANCELLED、B=COMPLETED，未串消息。7请求过载验证中2活跃+4排队，另1请求在SSE响应头前得到503 CHAT_BUSY+Retry-After。不得据替身延迟宣称真实LLM容量。

## 8. 命令、退出码和证据

统一根目录 `var/hybrid-concurrency-20261114/`；原始请求正文/引用仅在获准本地和 ECS 实验目录，不提交Git。记录无密钥。

| 验证 | 实际命令/入口 | 退出与关键产物 |
|---|---|---|
| 改前定向基线 | 同一venv，6模块pytest | 0；100 passed，`baseline-tests.log/xml` |
| 最终本地集成 | 下列PowerShell命令 | 0；229 passed，0 failures/errors/skips，`integrated-tests-v3.log/xml` |
| 预算专项 | pytest预算/并行/向量复用等指定模块 | 0；91 passed，`var/hybrid-request-budget-20261114/scope-default-final.*`；不是与229不重叠的额外样本 |
| Chrome | `& 'E:\RAG quention\.venv\Scripts\python.exe' scripts\check_hybrid_session_browser.py`（自动创建唯一目录） | 驱动0；27 passed。复用E盘Python仅为已安装浏览器自动化依赖，受测API/源码仍D项目 |
| 前端构建 | `D:\RAG knowledge\frontend-weknora` 中 `npm run build`（tsc --noEmit && vite build） | 0；3210 modules transformed，24.59秒；本轮工具回执及dist产物存在，未单独保存build.log |
| ECS兼容构建 | `.\.venv\Scripts\python.exe scripts/build_hybrid_ecs_patch.py --build` | 0；R7 compile/import及1KB150/50、10KB500/500零网络检查通过 |
| 真实基线/候选 | `run_hybrid_concurrency_benchmark.py --cases development20.json --out ab500-ready --arm baseline=http://127.0.0.1:18331 --arm candidate500=http://127.0.0.1:18332 --concurrency 1 --repetitions 2` | 独立systemd job ExecMainStatus0；80完成，`ab500-ready/`、同名log |
| 同预算串并行 | 同入口，`--out serialpair --arm parallel=http://127.0.0.1:18332 --arm serial=http://127.0.0.1:18333 --concurrency 1 --repetitions 2` | ExecMainStatus0；80完成 |
| 容量2/4 | 同入口，单parallel arm，`--out cap2 --concurrency 2`或`--out cap4 --concurrency 4`，均`--repetitions 2` | 两个job ExecMainStatus0；各40完成，raw资源JSONL保留 |
| 最终请求捕获 | `check_hybrid_chat_delivery.py --cases development20.json --out r7-chat-capture --base http://127.0.0.1:18332 --model-id a8753099-fe56-403b-a7db-ff457a6403f5 --indices 0 6 10 13 15 --capture-only` | 0；5/5；基线相同参数另目录5/5 |
| 真实Qwen | 同入口去掉capture-only；两题`--indices 0 10 --out r7-chat-real`，另新题`--indices 15 --out r7-chat-boundary` | 两条命令均0；2/2及1/1，无自动重试 |

可复制本地回归（只使用已安装venv，不启动生产服务）：

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH='backend;.'
$env:DATABASE_URL='sqlite:///:memory:'
$env:PYTHONUTF8='1'
$env:RAG_OBSERVABILITY_ENABLED='false'
$env:CONVERSATION_TEST_ARTIFACT_DIR=('var/hybrid-concurrency-20261114/recheck-'+[guid]::NewGuid().ToString('N'))
& '.\.venv\Scripts\python.exe' -m pytest backend/tests/test_hybrid_request_budget.py backend/tests/test_hybrid_parallel_routes.py backend/tests/test_query_expansion_rrf.py backend/tests/test_query_embedding_reuse.py backend/tests/test_bounded_chroma_executor.py backend/tests/test_bounded_chroma_bootstrap.py backend/tests/test_request_admission.py backend/tests/test_server_conversation_api.py backend/tests/test_server_conversation_store.py backend/tests/test_evidence_repairs.py backend/tests/test_provider_http_pool.py backend/tests/test_provider_startup.py backend/tests/test_bm25_jieba_required.py -q --tb=short --disable-warnings
```

旧失败均保留：第一次集成25失败为验收app导入污染环境，修测试隔离；第二次1失败为Windows80ms调度边界，修合成fixture，产品deadline不变；R4缺helper导致向量分支失败，不计作性能成功；`ab500`第一次启动socket未就绪，原UNKNOWN保留，独立复核无服务端receipt、连接未发出，新增健康GET后用新run ID开始，没有重发未知Provider请求。

## 9. ECS资源、停止线及收尾

实际为2 CPU、3803394048字节内存，无Swap。开始云盘12636045312字节可用；结束约12596924416字节。全盘变化约37.3MiB，不全部归于本任务；本任务远端目录约25MiB，含源码、逐题结果及压缩归档，没有整库复制、新容器、新venv或重新索引。

Chroma `MemoryHigh=671088640`（640MiB），`MemoryMax`未设置；全过程未改变。AB/串并行最低MemAvailable约1.44GiB，容量档最低约1.86GiB，均高于800MiB；磁盘始终高于8GiB。资源采样包括/proc CPU、磁盘读写、PSI、各服务cgroup和后期RSS，不以“未OOM”代替资源评估。

容量2/4档：Chroma RSS采样峰值分别约483.1/487.5MiB，cgroup memory.current峰值约609.2/613.5MiB；隔离API RSS约352.0/359.7MiB。两档内存PSI avg10峰值0，IO full PSI avg10峰值0，Chroma没有新增high/oom/oom_kill事件。AB500窗口IO full PSI avg10峰值2.94，早期失败预算AB20窗口最高34.93，不能只引用最后低压窗口声称磁盘无瓶颈。

早期AB20曾有IO压力，因此等待回落后才继续；最终只读清点4856058条postings计数又产生短时磁盘压力（收尾full PSI avg10约54.65），该窗口不混入容量测试结论。随后不再扫大表，20:14采样IO/内存PSI avg10回到0，MemAvailable约2.19GiB。只读SQL也有资源成本，后续清点优先复用已有计数。

正式表计数与开始一致：17KB、23787文件、117507块、12017索引build、102229索引记录、4856058 postings。正式源码无漂移，API PID728757、Chroma PID567489未变，Chroma心跳HTTP200。此计数是整库现状，不等于十库测试scope大小。

本轮创建的18331/18332/18333隔离API均已停止，不留后台压测，浏览器验收API也已停止。R6/R7、原基线、失败回执和日志保留用于复核/恢复。自动审批审核以“blocked by policy”拒绝删除本轮闲置五份中间backend源码目录（约5.5MiB），此前也拦截小型Chrome合成SQLite夹具删除；未使用其他方法绕过，见 `intermediate-cleanup-plan.json` 和 `cleanup-policy-status.json`。

## 10. 已完成、未知及后续最小步骤

**PASS**：229定向测试、Chrome27、十库模型绑定身份、80次基线A/B、80次串并行、两档各40容量、10次实际模型请求捕获、3道真实Qwen核心答案冒烟、资源下限与正式源码/逻辑数量保护。

**UNKNOWN**：7对排名差异的精确数值来源；12引文单元的完整子块qrel；真实模型首Token延迟；真实费用；未采集窗口的全量资源峰值。

**NOT_RUN**：正式发布/重启、8会话、4活跃请求容量、线上真实Rerank及长Agent容量、完整答案正确率/Citation Precision与250/500题、R7新默认整套容量重跑、独立留出集。局部五题及三题只作接线/生成冒烟，不能替代上述验收。

剩余瓶颈仍包含同步的调用方SQL、Chroma串行物理读取、较大postings扫描、父子扩展及非流式回答；本轮没有盲目全异步重构。下一步优先在保持2活跃上限下采集完整route排名与查询向量哈希（不外传向量正文），隔离7对排名变动来源，再决定是否开展单因素排名实验。单独修生成引用要有专门方案，不与本轮检索调度混改。

**发布建议而非发布决定**：可以审查这份最小兼容补丁；如负责人决定替换正式服务，发布前重新确认实时负载和文件身份、备份原文件、空闲重启，并保留旧参数/文件作为回退。未经该授权，本轮已停止于隔离验证。

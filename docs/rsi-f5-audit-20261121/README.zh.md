# F5-A 与 F5-B 审查

审查依据为 RSI.zip，SHA-256 为 `be4632f2fe39115a49fa5e2319654c3c053ce890f7d1076a4177579d0044bee0`。基线为远端 `main e242e91a`、`codex/rsi-s2 f3779d78` 和草稿 PR #11 `c286bf95`。F0–F4 复用。F5 为待验收，DEV60 与 DEV200 尚未完成正式配对。

## F5-A 数据资格

- [200 题资格清单](dev200-eligibility.json)逐题绑定 QA500 开发题身份、来源、语言、题型、Gold 和证据哈希、Rubric 哈希、历史曝光、现有基线和 qrel 状态。ECS 冻结原题与 QA500 原问题逐题一致。20⊂60⊂200，无重复 qid，三个分组中英各半。200 道均为单轮开发题，独立 Holdout 为 0。
- 139 道当前无额外资格标记，49 道为 DEV 且 QA500 留有人工评分政策提示，12 道暂列 BLOCKED，需核查来源或 Gold。原 QA500 共 52 道有人工政策提示，其中 3 道与这 12 道重叠。人工评分提示与本轮历史问题标记分列。12 道全部在新增 140 中。如果全部维持 BLOCKED，DEV200 有效双侧样本上限为 188，低于 190 的门槛。
- Qrel 的已验证覆盖仅原 20 中的 10 道。其余 190 道在本报告中为 UNKNOWN。Gold 文本或源引用的存在无法替代当前冻结索引的相关块身份映射。Recall、Precision、MRR 仅用已绑定的分母。
- V7 同题取旧 cheap 回复，原 60 标签为完全正确 30、部分正确 27、错误 3。旧 expensive 为 58、1、1。V9 冻结上下文中，`baseline_whole` 和 `routing_only_whole` 分别为 31、26、2、未判 1，`prompt_only_whole` 和 `combined_whole` 分别为 53、5、1、未判 1。V9 的同题 60 条新 cheap 中 26 条新生成，34 条复用 V8。历史版本间有 5 题标签变化。旧标签保留版本来源，供诊断和重放。
- V7 先实际检索并保存上下文，随后另外调用模型生成。V9 沿用冻结上下文作固定输入比较，正式 E 验证包含 route-only 回放。历史报告有真实生成 Token、生成耗时、旧评分和检索准备记录，缺少本轮正式源码和索引下的同请求、同路由、同期限完整回执。尤其是检索、重排与端到端时延，无法直接用于正式配对。
- [60/200 基线缺失清单](dev60-200-baseline-gaps.json)给出两种口径。保持旧 30 秒，合格旧基线可复用 20，道数缺 40/180。严格全部使用新 20 秒，当前缺 60/200。旧 20 仍可用于原设置的配对筛选和历史对照。可以保留两个配置的分析分层，统一 20 秒验收则要求同配置证据。新运行只在隔离环境固定 `RAG_RETRIEVAL_DEADLINE_SECONDS=20`。本地已验证该参数读取和默认 30 秒，无生产变更。

### 12 道待核查项

| QID | 原评分归因摘要 |
|---|---|
| terra-ecs-b4-019 | 化学源材料有疑点，cheap 答案另有扩链剂和软段多元醇混淆 |
| terra-ecs-b5-022 | 来源可能简化了尼罗河上游流向，两份答案历史标签为正确 |
| terra-ecs-b7-005 | 染色体材料互相矛盾，回答还有矛盾或无支持概括 |
| terra-ecs-b8-002 | 区域气候地理边界在来源间冲突，回答遗漏或误述冲突 |
| terra-ecs-b1-009 | cheap 把都塔尔和手鼓混为一谈，expensive 未带该问题标记 |
| terra-ecs-b1-027 | 来源对风扇或转子位置的说法不一致 |
| terra-ecs-b7-001 | 牛津数学历史来源有疑点，cheap 额外写入导师或学位错误 |
| terra-ecs-b7-007 | 旗帜颜色史与来源存在差异 |
| qst_0335 | 匿名化后的签名完整性仅由来源声称，缺少独立证明 |
| qst_0928 | 日期与星期冲突，cheap 还断言了有争议的星期 |
| qst_0920 | 日期范围与声称的 90 天冲突 |
| qst_0799 | cheap 将其他事故手册的调参归到本次事故，另一臂也有来源标记 |

原因来自 V7 `dev200-canonical-grades.json`，SHA-256 为 `383b800e5911f4cd6ca3d6a71994aecca080b675a027f6237b6eb6a13ead1b85`。其中既有来源争议也有历史回答错误，清单保留待核查状态。旧 V9 标签来源为 `evaluation-e_three_slots.csv`，SHA-256 为 `6e97157255d7aab44f07a0263f6eda126de7758c65b3f0dc7b0ff2e92d195455`。

## F5-B 全链路观测

[机器可读矩阵](full-chain-coverage.json)包含按臂计数和逐题回执哈希。每臂原 20 条 E 问答原始回执已核验。应用端 SSE 首个非空答案的计时已在新隔离宿主接好，旧 80 条无该字段。

| 阶段 | 原 20 的档案证据，每臂 | 主要缺口 |
|---|---|---|
| Query Processing | 查询输入及检索入口 20/20 | 独立处理起止与每次改写链路不全 |
| Vector Retrieval | 数量、耗时 20/20 | 独立候选 ID、排名、分数快照 0/20 |
| BM25 Retrieval | 数量、耗时 20/20 | 独立候选 ID、排名、分数快照 0/20 |
| Fusion | 合并数量及方法 20/20 | 独立融合耗时和 ID 快照 0/20 |
| Rerank | 数量 20/20 | 独立耗时缺失，Provider 有未回传用量 |
| Parent / Neighbor Expansion | 已交付引用血统及扩展标记 20/20 | 独立耗时缺失 |
| ContextBuilder | 最终正文、预算和裁剪字段 20/20 | 独立耗时和逐事实前后快照缺失 |
| Final Prompt | 消费和 Provider 请求哈希 20/20 | 完整组装的 Prompt 快照 0/20 |
| Generation | 答案块、done、请求用量回执 20/20 | 旧 ASGI TTFT、客户端 TTFT、Provider finish reason 0/20 |
| Citation / Evidence | 交付引用及映射 20/20 | 检索 UUID 关联和逐事实 PRESENT/LOST/RECOVERED 0/20 |
| Answer Evaluation | BASELINE/PROMPT/CODE 各 19/20 已判，TOP_K 20/20 | 三个未知保留，qrel 有效分母为 10/20 |

当前有稳定 `attempt_id`、case/arm 标签、Provider wire ID，以及候选侧的 Candidate/Parent ID。实验和 run 身份、独立的 qid/repeat/request/stage 及精确检索 UUID 尚需在 RSI 接线补齐。根据同一题或同一 query 文本拼接不能证明请求关联。没有同一事实前后快照，因此证据流失和恢复为 UNKNOWN。

## 本轮试跑回执

加入严格资料包之前启动了一次旧 30 秒基线补跑。第 21 题耗时约 83.706 秒，检索超时返回 PARTIAL，生成 NOT_RUN，E 路由没有决策记录。基线 39 道随后被该版 runner 取消，四组最终退出码均为 1，基线有一份失败正文回执，三个候选经过隔离准备后均在启动闸门处结束，新增候选问答 0。Judge 接收 40 份基线行回执，产生 0 笔评分调用，0 题获得新增评分。全局 QA 账本包括取消时仍完成写账的请求，因此不能用该题持有的 12 条观测替代账本新增 16 条。另存 QA 和 Judge 档案供恢复。F5-A/B 审查新增付费模型调用为 0。

## 开发阶段逐组件建设

最新补充覆盖资料包第四节里由 RSI 自行判断并扩大代码范围的设计。Codex 在第一阶段负责核对实际接缝，补齐测试、观测、隔离及恢复，再逐组件扩大允许的文件、函数和参数，并保留修改前后证据。Scope manifest 仍绑定精确边界。RSI 在运行时消费已批准范围，并独立完成诊断、Patch、测试、并行问答、异步评分、裁决、经验、父代选择和 Git PR。运行时范围由已建设的接缝控制。

目标组件覆盖 Context Selection、Query Processing、Vector Retrieval、BM25、Hybrid/Fusion、Rerank、Parent/Neighbor Expansion、ContextBuilder 和 Grounded Generation 的证据与回答逻辑。模型路由和 Agent Workflow 冻结。授权、预算、证据、裁决、EventLog 和恢复、生产环境、main 合并保持保护。单测、静态检查、替身实验和孤立 PR 各自作为工程证据记录。组件真实优化能力仍须用同题完整链路和隔离恢复证明。

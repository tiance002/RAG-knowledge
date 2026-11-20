# Hybrid AB20 三个证据单元丢失复核

2026-11-14。只读复核本地已下载的 ECS AB20 产物；本次没有调用模型、访问 ECS、改代码、改 Gold、重建索引或重新运行真实检索。

本轮候选更快，但证据交付指标退步：20 题、每臂一次、并发 1，共 40 次请求全部 `COMPLETED`；完整 Gold 引文在返回上下文中的覆盖由 **22/33 降至 19/33**，新增丢失 3 个单元，没有新增覆盖单元。这里的 `COMPLETED` 仅表示 HTTP/检索流程成功，不能作为质量通过；该指标也不是最终答案正确率。

## 1. 证据范围与统计核对

数据目录：`var/hybrid-concurrency-20261114/`。

- 冻结输入：`development20.json`；qrel：`development-qrels.json`。
- 请求、返回正文、逐单元覆盖：`ab20/<request_id>.json`。
- 扩展前检索结果及 metrics：`base-receipts/<request_id>.json`、`candidate-receipts/<request_id>.json`。
- 汇总：`ab20/summary.json`。
- 已检查 qrel 的 `cases_sha256` 等于冻结输入文件实际 SHA256；同一题两臂请求 JSON 完全相同；40 个 AB20 请求均有同 ID receipt。
- receipt 目录另有预热/探测记录，本报告仅按 AB20 的 40 个 ID 关联，未把额外记录计入分母。

固定范围为明确的 10 个 KB；固定请求参数为 `hybrid`、`top_k=10`、`score_threshold=0`、权重 `0.7/0.3`、`rerank_enabled=false`、`context_window=1`。

| 已观测指标 | Baseline | Candidate |
|---|---:|---:|
| 请求/COMPLETED | 20/20 | 20/20 |
| FAILED / UNKNOWN | 0 / 0 | 0 / 0 |
| 平均 HTTP Recall 耗时 | 3.113638 秒 | 1.616609 秒 |
| p95 | 3.631987 秒 | 2.838680 秒 |
| 完整引文在返回上下文中出现 | 22/33 | 19/33 |

平均耗时约减少 48.1%，p95 约减少 21.8%；引文覆盖下降 9.09 个百分点。只有一轮 20 题，不能据此证明总体性能分布或全部检索质量。

引文检测沿用冻结口径：移除 Unicode 空白并 `casefold` 后做完整子串匹配。33 个 qrel 单元中，21 个 `MAPPED`、10 个 `UNKNOWN_PARENT_ONLY`、2 个 `UNKNOWN_NO_EXACT_TEXT`。后两种状态不能伪造为明确的可检索 child ID；父块中的完整引文仍可通过返回正文独立核实。

## 2. 如何解释“最早可证明的丢失阶段”

receipt 字段虽然叫 `raw_candidates`，实际记录的是 `RetrievalService.search` 交付给后续上下文扩展的 Top-10；它不是每条 Vector/BM25 路由截断前的完整原始池。当前产物没有完整路由池、目标内原始排名、全部 RRF 候选排名、逐项授权剔除记录和父块扩展事件。

因此可以证明某个 ID 是否已经在“检索 Top-10 → 上下文扩展”的边界消失，可以证明最终返回哪些源成员及正文；**不能**仅凭最终缺失就断言该 ID 恰在某个目标 TopK、授权过滤、去重或 RRF Top-10 的哪一行被删。

候选三题均实际分配为每 KB **Vector 15、BM25 initial 5**，其请求总预算为 150/50；三题初始有效并集分别为 191、192、183，均大于 `top_k=10`，所以没有触发关键词扩展，deadline 也均未耗尽。原 baseline 则每 KB 最多 Vector 150、BM25 50，三题实际向量候选数均为 1454。

| 单元 | Baseline 可观测边界 | Candidate 可观测边界 | 最早可证明结论 |
|---|---|---|---|
| `qst_0017#sol-e2` | 映射 child 在检索 Top-10 第 7 位，随后进入源成员和正文 | 映射 child 不在检索 Top-10，也不在返回源成员，完整引文缺失 | 已在交付检索 Top-10 时丢失；更早的具体剔除步骤未知 |
| `qst_0032#sol-e1` | 无精确 child qrel；首位结果扩展成完整父块，包含引文 | 同一首位 seed 仍在，但最终仅交付六个 child 片段，缺失引文所在区间 | 源文档未整体漏检；可证明上下文交付从完整父块变为不覆盖该段的片段集。触发此变化的准确事件原因未留存 |
| `terra-ecs-image1-002#e2` | 映射 child 在检索 Top-10 第 8 位；原 PDF 结果和描述派生结果均交付引文 | 两个映射 ID 均不在检索 Top-10/返回成员，相关描述 seed 也消失 | 已在交付检索 Top-10 时丢失；目标剪枝与融合排序的具体分界未知 |

## 3. qst_0017：辅助 rubric 定义丢失，数值答案线索仍在

问题原文：

> In a rolling investigation of a model regression on policy-related prompts after a recent deploy, what was the average triage rubric score change between the older baseline build and the newer optimized build in the first comparison run?

冻结答案：baseline 2.7、optimized 1.9，下降 0.8 rubric points。

本次丢失的是 `qst_0017#sol-e2`：

```text
- Plan: rolling bisection across deploy shards and tokenizer artifacts; capture example outputs; use simple ad-hoc rubric for quick triage.

Ad-hoc rubric (quick, for nightly triage):
- 0 = clear fail (hallucination/biased endorsement present)
```

来源：`dsid_72ec4a9962ba43e88acd61abbba1052d__rolling-bias-bisection-log-jared.txt`；qrel child 为 `47aee401-9e6a-5e60-bfb8-b1c526bce69d`，状态 `MAPPED`。

- Baseline request：`48e0b3b9f27948c792e03ec95e45b86b`。该 child 为原始 Top-10 第 7 位，Vector score `0.53584905`，BM25 score `21.57866254`。首个交付 group 包含该 child，其父内区间为 `[530,914)`，完整 rubric 引文出现于返回正文。
- Candidate request：`88ed10f3763e4795a2c9ee7398935307`。该 child 不在原始 Top-10；同一父块 `0a643b8b-a1ff-5535-aecf-11b40ce0aa7b` 仅交付 `[1421,1716)`、`[1668,2040)`、`[1992,2288)` 三个 child。完整定义段确实不在这些区间内。
- 两臂返回正文都保留 `baseline average rubric 2.7, new average 1.9`。因此本题新增丢失的是 rubric 定义的完整证据单元，不能把它直接换算为“数值答案错误”。本轮没有生成答案，也没有进行语义答案评分。

## 4. qst_0032：首位文档仍命中，但真正的 30 分钟条款消失

问题原文：

> What is the default time limit for the short-lived cross-account debugging access sessions in the proposal for an auditable broker role for SREs across AWS and GCP?

冻结答案：默认 TTL 30 分钟，说明理由后可配置至 1 小时。

Gold `qst_0032#sol-e1`：

```text
Expiry policy and audit retention:
- Default session TTL: 30 minutes; configurable up to 1 hour with justification.
- Audit records retained according to compliance policy (90 days hot, 1 year cold).
```

来源：`dsid_12621dfa773b444e9b1854457ee4fe0f__INT-27840-provision-ephemeral-iam-broker-for-cross-env-debugging.txt`。qrel 状态是 `UNKNOWN_PARENT_ONLY`，`expected_chunk_ids=[]`；不得为它编造一个独立命中的 child ID。

- Baseline request：`9d92813d105d4dfd8238a71452b4c5ad`。首位 seed 为 `ae66cb8f-8cca-576a-a6e9-33b8e36d00c2`，交付完整父块 `93b4b916-c58a-554e-b479-e0cfe5f82bdd`，长度 3957 字符。完整 Gold 从父块字符位置 **1915** 开始，长度 199。
- Candidate request：`883edc3d93534baeb88487d622d1d7c8`。同一首位 seed 保留；原始 Top-10 与 baseline 仍共有七个 ID。首组却仅交付六个 child，共 2039 字符，父内区间为 `[0,328)`、`[280,572)`、`[845,1151)`、`[1103,1477)`、`[2737,3121)`、`[3381,3731)`。**Gold 的 `[1915,2114)` 落在未交付区间中**。
- Candidate 所有返回正文中都没有 `30 minutes` 或 `Default session TTL`；目标文档仅保留 `short-lived session (max 1 hour)`。另一个来源还保留 `TTL default 1 hour`。这构成实际的关键条款证据缺失，不能解释成仅空白、大小写或格式不一致。
- Baseline 总上下文 9921 字符，candidate 为 33993 字符；candidate 另引入一个 16680 字符的非目标表格结果。更多上下文字节没有保住所问的 TTL 段。

归因边界：已核对本地与冻结 baseline 的 `parent_context.py` SHA256 相同：`7743fbd7c92dd9c9bcf4928c33073d3c01cce7d397f8bec779ac109c03b30ccd`。该规则会为其他真实 seeds 预留正文空间；父块超预算或 seed 不被父块原文包含时，会退回完整 seeds。此次“父块 → 稀疏 child 集”的观察与这种回退相符；**receipt 没有保存该 group 的 `degradation_reason`、实际 `future_seed_cost`、完整 seed 正文长度和运行时总字符额度，不能确认具体触发的是 `PARENT_OVER_BUDGET` 还是其他分支**。顶层 `expansion_delivery_status=DELIVERED` 本身不能证明 Gold 段已交付。

## 5. terra-ecs-image1-002：所问页面的明确限定丢失

问题原文：

> 猫狗检测讲义“main Libraries”页面中，Caffe当天使用的是怎样的网络，而非重新训练一个？

冻结答案：加载一个预训练的深度神经网络。

本次丢失的 Gold `terra-ecs-image1-002#e2`：

```text
(for today: loading a pre-trained one)
```

来源 PDF：`catvsdogdlpycon15se-150512122612-lva1-app6891_95.pdf`，原图第 4 页左中区域；qrel 映射 ID 为 `32f7b5e0-9bdb-455c-8445-1aa88d90a45f` 和 `640d06f9-5c0d-5623-99e3-26d35c223f3f`。

- Baseline request：`e0d459deab0042fc8c0cda30abb1b7d1`。`32f7b5e0...` 为原始 Top-10 第 8 位，Vector score `0.56325575`、BM25 score `20.98867842`；扩展后第 6 个 PDF 结果包含该句。第 5 个描述派生结果 `1b5f4522-0636-4a7e-a0cb-35afd505a16d` 也明确交付：`绿色文字补充说明：**(for today: loading a pre-trained one)**，即“今天课程中：加载一个预训练好的模型”`。
- Candidate request：`0c9fe925cdef4819bc3a9411481f4dca`。两个 qrel ID 均不在原始 Top-10，也不在返回源成员；上述描述派生 seed 亦不在 Top-10。完整限定句在所有返回正文中缺失。
- Candidate 仍命中同一本讲义的 Caffe 框架、模型库和其他页面。相关页面不等于所问第 4 页的明确限定，不能把“命中同一文件”判为这个单元通过。
- 原 PDF 交付内容包含“图片描述（模型生成，需核对原图）”标记；本报告核查的是既有文本交付，没有重新查看原图或验证描述真实性。

## 6. 预算剪枝的直接证据与尚未证实的因果

共有 Top-10 ID 的 `content_sha256` 在各题两臂完全相同：qst_0017 共有六个、qst_0032 七个、image1-002 六个。与此同时，一些仍被选中的 ID 从“双路贡献”变为“仅向量贡献”：

| 题目 | 同一被保留 chunk | Baseline BM25 score | Candidate BM25 score |
|---|---|---:|---|
| qst_0017 | `b89bf96a-6ef4-56d7-b34f-87aeb9ef7817` | 19.01189308 | `null` |
| qst_0032 | `d372e72e-3dfe-5031-b91a-1b51feb536a3` | 17.48397664 | `null` |
| qst_0032 | `15acd5e2-d3f0-51d8-8824-385d73c1396e` | 18.73100569 | `null` |
| image1-002 | `81a620b6-0d5a-4b14-b0d5-4c1716a8a8ec` | 16.27433143 | `null` |

这证明候选并非只把相同候选池调度得更快，路由参与及融合输入确实改变；15/5 的每 KB 浅预算是需要优先验证的通用因素。它不能单独证明每个缺失 Gold ID 的确切目标内名次，也不能把所有收益都归于缓存/并行：本轮同时改变预算、复用和调度，部分共同 ID 的 vector score 也存在细微差异，未保存全部向量供因果隔离。

三题均未执行关键词补充，因此不能把损失归因于扩展查询失败、变体重复投票或 8 组合上限；没有 deadline 耗尽或请求失败的证据。这里只排除了这些已记录路径，未排除未记录的早期排名/过滤差异。

## 7. 下一步只建议一个通用候选配置

建议对**同一整套冻结 20 题**验证下列配置，不设置 qid/来源专用规则，不修改 Gold、模型、Prompt、阈值、权重、上下文策略或最终 Top-10：

```text
RAG_RETRIEVAL_VECTOR_DEPTH=500
RAG_RETRIEVAL_BM25_INITIAL_DEPTH=500
RAG_RETRIEVAL_BM25_SUPPLEMENT_DEPTH=50
RAG_RETRIEVAL_MAX_VARIANTS=5
RAG_RETRIEVAL_MAX_VARIANT_TARGET_PAIRS=8
RAG_RETRIEVAL_TARGET_INFLIGHT=2
RAG_RETRIEVAL_DEADLINE_SECONDS=30
RAG_QUERY_EMBEDDING_REUSE_ENABLED=true
```

这两个 500 有各自的边界理由，并非为了两路数字相同：

- **BM25 500**：对本轮固定 10 KB、单 base query 恢复每 KB 50 的原 baseline 词法深度；两个 base query 时仍由整个 initial 请求共享 500，不能假称始终每 KB 每 query 50。补充阶段继续共用独立的 50。
- **Vector 500**：对 10 KB 给每 KB 50，较当前 15 深，仍小于旧每 KB 150，即请求最多约 1500 的旧上限；是一个通用的、有界的中间探针，**未证明 50 足以恢复全部 Gold**。

该配置目前 **NOT RUN**。需要同时看 33 单元逐项覆盖、完整候选交付、父块/child 源成员、尾延迟和资源，而不是仅要求总数回到 22 就宣布质量恢复。它用于检验“过强浅预算是否是主要因素”；由于向量深度仍不等于 baseline，单次比较仍不能完全分离缓存、调度与剪枝的因果收益。

下一次至少保留丢失候选所在目标的路由内排名/截断标记、融合前后排名，以及父块 group 的实际回退原因和字符预算账本，才能把上述区间进一步缩小；不能补写本轮不存在的日志。

## 8. 执行与状态

实际执行了本地 PowerShell 下 `.\.venv\Scripts\python.exe -` 的只读 JSON/哈希检查，退出码 0；输入为前述冻结 JSON、40 个响应、40 个配对 receipts 和冻结 baseline 源码。检查结果：40 `COMPLETED`、请求配对相同、qrel 哈希匹配、3 个新增丢失、0 个新增命中。

本报告状态：**PASS（产物一致性与上述交付差异已核实）**；**UNKNOWN（未留存阶段的精确剔除原因、最终答案正确率）**；**NOT RUN（500/500 下一配置及后续答案评测）**。152/158 项单元测试属于此前实现验证，不能替代本轮真实质量结论。

## 2026-11-14 后续记录（保留前文失败历史）

本节是追加记录，不改写前文 150/50 总量候选的三个损失，以及当时 500/500 尚未执行的状态。之后下载的 R6 `var/hybrid-concurrency-20261114/ab500-ready/` 已记录显式 500/500、每臂 40 次 Recall 全部完成，两臂引文覆盖均为 44/66；本地复核 40 个配对请求相同，逐题、逐重复、逐证据单元没有差异。该测试未开启 Rerank、未生成答案，仍不能作为答案正确率或正式部署结论。

当前本地默认预算合同见 [Hybrid 检索预算与并发合同](hybrid-retrieval-budget-contract-20261114.md)。后续完整实验、运行版本与交付状态统一由 `docs/hybrid-concurrency-implementation-and-validation-20261114.md` 记录；R6 显式配置的结果不能自动证明 R7 新默认版本已实际运行。

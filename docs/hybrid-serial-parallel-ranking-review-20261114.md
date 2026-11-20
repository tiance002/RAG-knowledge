# 同预算串行/并行排序差异复核

2026-11-14。只读检查本地 `var/hybrid-concurrency-20261114/serialpair/` 响应及其 `quality-receipts/`（parallel）、`serial-receipts/`（serial）。未修改产品代码、配置或 Gold，没有外部请求或模型重跑。

**结论：不能报告真实返回结果完全等价。** 40 个 `(qid, repetition)` 配对中，7 对返回的有序 `(chunkId, content)` 序列不同，33 对这两个字段的序列相同；这不表示其余响应 JSON/分数字节完全相同。所有 40 对的逐证据引文覆盖相同，只能证明本题集这一交付指标未变。

## 最早可观察边界

- 七对差异全部已经出现在 receipt 的 `raw_candidates` Top-10。该字段是 `RetrievalService.search()` 在本地 query expansion、RRF、enrichment 及检索去重后交付的结果，仅位于父块/邻居 **context expansion 之前**；不是 query expansion 之前，也不是原始 Vector/BM25 路由各自的 Top-10。
- 其中两对只是顺序变化：raw/final ID 集合相同，最终内容 hash 多重集合也相同；另外五对替换一个 raw Top-10 成员，最终 ID/内容集合随之改变。
- 七对中，共有 raw ID 的 `content_sha256` 完全相同，`bm25_score` 也相同；但 `vector_score` 已不同，每对共有 ID 的最大绝对变化约为 `0.000537～0.0014218`。这不是“对完全相同评分输入作排序却得到不同输出”的观测。
- 两臂均实际记录 Vector/BM25 initial 总深度 500/500；parallel 的执行原因为 `managed_independent_reader`，serial 为 `disabled`；七对都没有 deadline 耗尽。以下三个例子也都没有触发关键词补充。

## 三个代表配对

### 1. terra-ecs-b1-008，repetition=1：向量相对分数翻转，Top-10 边界替换

- parallel：`178f24053d4543c1810a44b9d951053b`；serial：`92fa9ffdb190452e818048c244df3bcc`。
- 两臂这十个 raw 候选都没有 BM25 贡献，排序差异与其向量相对分数变化一致：

| chunk ID | parallel 向量分数 / raw 名次 | serial 向量分数 / raw 名次 |
|---|---|---|
| `2aa46a92-c206-41c5-b042-b5b06e0bc596` | 0.40257575 / 4 | 0.40205675 / 5 |
| `9ce53761-f89c-49d0-8da9-d85736963363` | 0.40224480 / 5 | 0.40256635 / 4 |

parallel 分数中前者较高，serial 分数中后者较高；这两行并非等分。parallel raw Top-10 独有 `056acc23-e31f-48b3-8c6d-1ab14f30487e`，serial 独有 `973cc81b-c69b-4777-8703-b1109f0c5494`。原始有效并集计数也已从 serial 642 变为 parallel 643，因此不只是最终列表展示顺序变化。

最终总正文为 parallel 16574、serial 20256 字符。逐证据覆盖相同，但上下文集合和长度确实不同。

### 2. terra-ecs-b1-006，repetition=0：相同成员的尾部顺序变化

- parallel：`60236483ce394573858581660cf1c148`；serial：`2350c23ef27946baa9634ff7445517d1`。
- raw Top-10 ID 集合相同，前八名顺序相同；最后两名在融合结果中互换：

| chunk ID | parallel raw 名次 / 归一化融合分数 | serial raw 名次 / 归一化融合分数 |
|---|---|---|
| `e89dfdd3-c974-4dd0-9fc4-5ebf9b535cb4` | 9 / 0.7455162489 | 10 / 0.7455162489 |
| `ef1c7a73-4a65-45de-a9d9-5fb1cd4f3036` | 10 / 0.7416116292 | 9 / 0.7475136501 |

两臂对应 chunk 的 BM25 分数分别保持 `18.92509605`、`18.27313474`；向量分数有所变化。完整 500 项路由排名未保存，无法仅由上述十个结果重建具体哪几个全局向量名次改变。可确认 raw 名次按当前融合分数正确排列，未观察到相等融合分数被随机换序。

扩展后有九个结果，尾部两项相应互换；两臂正文总长均为 3064，ID 集合、内容 hash 多重集合相同。这里的“有序内容不同”是相同内容块的位置变了，不是这些共同 chunk 正文被修改。

### 3. terra-ecs-b1-024，repetition=0：近边界候选替换，等分项次序仍稳定

- parallel：`72d481a898a949e0a3763b850107e32e`；serial：`85389ff6dde24c97a278ee483f99005e`。
- raw 前九个 ID 的顺序相同；parallel 第十名为 `00d52d65-be30-4586-ad00-d6f3e0cbe93b`，serial 第十名为 `4e0c666f-a022-48e3-895f-35ad45f08590`。两者最终也成为各臂第六个交付结果；正文总长分别 8592、8518。
- 共同的 `edcc0048-081e-4708-8368-62dcd28a223a` 与 `ff620cbe-92e0-463a-9933-4be98c6d414b` 在每一臂内部的向量分数相等、BM25 原始分数也相等；两臂均保持前者在前、后者在后的 ID 次序。两臂之间这些向量分数由 `0.586464165` 变为 `0.58576975`。
- 缺少完整池，不能证明上述第十名是在哪个目标内 cutoff 或 RRF 边界胜出；但差异确实早于父块/邻居 context expansion，不能归为上下文扩展器凭空增加不同 seed。

## 是否违反稳定 RRF

检查 R6 冻结候选 `candidate-20261114-192459-190728/backend/app/services/retrieval.py` 的 `weighted_rrf()`：

1. 向量路由按 `(-score, id)` 排序；每个 chunk 每路仅贡献一次。
2. `weight / (60 + rank)` 相加后，按 `(-rrf_score, id)` 排序。
3. 最后才做 Min-Max，归一化不改变已确定的排序。

因此其“稳定”是**给定相同路由候选与分数时，规则和 tie-break 确定**；不是保证多次真实 Embedding/Chroma 请求获得相同评分输入。代表配对已存在原始向量分数差异，并有一例明确相对大小翻转；现有证据没有证明 RRF 的相等输入排序合同被违反。受控相同输入测试通过，也不能覆盖或抹去这七对真实差异。

## 尚不能确定的原因与可安全表述

三个例子均记录一次成功的 `BAAI/bge-m3` embedding、返回维度 1024，但记录没有查询向量本身或其 hash、完整逐目标 KNN 响应/距离、全部路由排名、ANN 参数/状态和计算精度信息。**不能把差异锁定为 Chroma ANN、浮点舍入、提供商 embedding 输出变化或并行本身中的某一项。** 已证实的是 Vector 路由交付分数/候选存在变化；其产生位置和具体机制仍属 UNKNOWN。

本次可报告：同预算串行/并行的 40 对请求在逐证据引文覆盖上相同；33 对有序 ID/正文序列相同、7 对不同；这七对在检索服务交付给父块/邻居 context expansion 之前已有差异，未发现稳定 RRF tie-break 被违反的直接证据。不得表述为“完整返回结果等价”“真实排序完全一致”或“已证明只有 ANN 随机性”。

本地只读检查命令为 PowerShell 下 `.\.venv\Scripts\python.exe -`，退出码 0；读取 80 个响应及其配对 receipts，按 `(qid, repetition)` 比较有序 ID/正文、内容 hash 多重集合、raw ID/分数和逐证据覆盖，并核对冻结候选排序源码。本次没有重放任何模型或检索请求。

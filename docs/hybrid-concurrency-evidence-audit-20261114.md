# 混合检索并发证据审计（2026-11-14）

## 审计范围

本审计只读取 `var/hybrid-concurrency-20261114` 下已经保存的日志、JUnit XML、JSON 汇总及归档收据；未重跑测试、未访问 ECS、未修改产品代码或测试。机器可读聚合与输入 SHA-256 清单见 [`audit-aggregate.json`](../var/hybrid-concurrency-20261114/audit-aggregate.json)。归档收据的 SHA-256 也记录在该 JSON 的 `provenance.inputs_sha256` 中。

本目录内的混合检索性能收据来自真实 Recall HTTP、Embedding、Chroma 与 SQLite；这些 Recall 运行没有生成答案。浏览器场景用隔离 SQLite、合成 Atlas 和 `httpx.MockTransport`，不产生外部模型流量。两类证据回答的问题不同，不能互相替代。

## 测试收据

| 收据 | 用例数 | 失败 | 错误 | 跳过 | 说明 |
|---|---:|---:|---:|---:|---|
| `baseline-tests.xml` | 100 | 0 | 0 | 0 | 冻结本地基线 |
| `integrated-tests.xml` | 221 | 25 | 0 | 0 | 首轮集成测试，失败保留 |
| `integrated-tests-v2.xml` | 221 | 1 | 0 | 0 | 只剩 deadline/无候选 fixture 失败，收据为 `test_hybrid_request_budget::test_deadline_returns_completed_valid_evidence_and_keeps_real_lexical_capacity` |
| `integrated-tests-v3.xml` | 229 | 0 | 0 | 0 | 最终收据：229 passed，耗时 60.434 秒；用例集合较 v2 多 8 项，不能说成同一分母的 221 项全修复 |

首轮失败分布包含混合路由与 RRF/扩展预期、BM25 postings、embedding memoization，以及 evidence repair 状态等测试。失败细节保存在原始 XML/log；本审计保留它们作为迭代历史，不把最终通过倒推为首轮已通过。

## 检索结果和证据单元覆盖

开发 qrel 有 20 个问题、33 个证据单元：21 个 `MAPPED`、10 个 `UNKNOWN_PARENT_ONLY`、2 个 `UNKNOWN_NO_EXACT_TEXT`。因此 12 个 UNKNOWN 单元不纳入映射单元命中率；两次重复后，映射分母为 42 个单位重复，UNKNOWN 共 24 个单位重复。

在 40 组 baseline/candidate500 成对 Recall 请求中，每臂均完成 40/40、无失败或未知结果，精确引文字符串出现数均为 44/66。基于 `expected_chunk_ids` 的 OR 匹配，映射单元排名如下：

| 指标 | Baseline | Candidate500 |
|---|---:|---:|
| mapped-unit Hit@1 | 18/42 (42.86%) | 18/42 (42.86%) |
| mapped-unit Hit@5 | 30/42 (71.43%) | 30/42 (71.43%) |
| mapped-unit Hit@10 | 34/42 (80.95%) | 34/42 (80.95%) |
| question Hit@10 | 18/22 (81.82%) | 18/22 (81.82%) |

question Hit@10 的 22 是 11 个至少有一个映射单元的问题乘两次重复；它不是全体 20 个问题的 Recall@10。候选范围是 `RetrievalService.search` 返回的 top-k（已完成查询处理、融合、去重和增强），在 parent/neighbor context expansion 之前；配对收据均记录 `expansion_triggered=false` 且 `expansion_query_count=0`。因此不要把这组数据称作向量路由原始 Top-k、融合前结果或答案正确率。

较早的 aggressive budget 试验为每臂 20 次请求、每题一次重复：baseline 22/33 个精确引文单元，candidate 19/33，少 3 个单元；耗时均值 3.114 秒降至 1.617 秒，p95 从 3.632 秒降至 2.839 秒。它是单次重复的小样本结果，展示预算变化可能损失证据覆盖，不能只依据速度选择配置。

## 串行与并行对照

Serial-pair 为 20 题、两次重复、每臂 40 次请求；两臂均 40/40 完成。精确引文覆盖均为 44/66，按每个映射单元逐项比较的覆盖布尔值 40/40 对相等。

但返回内容不是完全相同：40 对里有 33 对的有序候选 ID、候选内容 hash 和返回 context SHA-256 均一致；另 7 对至少一项有序候选 ID/内容 hash/context hash 不同。40 对中 context 字符数和字节数分别有 35 对相等。故“qrel 覆盖相同”不代表“排名或上下文等价”。差异原因未在此审计中定论，代表性样本另行审查。

平均/尾延迟：parallel 均值/p50/p95 为 1.226/1.200/1.404 秒；serial 为 1.294/1.270/1.467 秒。这些是收据中的 Recall 请求时间，不含答案生成或语义质量评估。

独立 worker 区间收据证明执行线程确实重叠，而非只看逻辑阶段跨度：parallel 组 40 请求中 39 个观测到 Chroma 与 BM25 实际区间交叠，交叠区间并集均值 88.681 ms、p50 82.706、p95 172.652、最大 232.709 ms；serial 组是 0/40，交叠为 0。其它组：candidate500 38/40，cap2 30/40，cap4 36/40。这里报告的是实际区间重叠；日志里的 `initial_routes_overlap_ms` 不能单独证明工作线程重叠。

## 客户端并发容量和资源观察

| 客户端并发 | 完成数 | 平均 / p50 / p95 | 吞吐量 | admission wait p50 / p95 |
|---|---:|---:|---:|---:|
| 2 | 40/40 | 2.247 / 2.257 / 2.596 秒 | 0.84045 req/s | 0.077 / 约 75.8 ms |
| 4 | 40/40 | 3.632 / 3.773 / 4.931 秒 | 0.84395 req/s | 989 / 2700 ms |

两组的服务端 admission gate 都限制活动工作为 2；cap4 是客户端同时提出 4 个请求，不是 4 个请求同时活动。该容量收据的服务 CPUQuota 为 1 核，因而不能将曲线解释成不受 CPU 配额影响的扩展性结果。吞吐量几乎持平，cap4 排队和 p95 更高。客户端并发 8 没有运行（`NOT RUN`）。

cap2/cap4 各有 48 个资源样本。最小 `MemAvailable` 分别约 1.87 GiB / 1.86 GiB；采样期间 memory PSI 最大值均为 0。Chroma `memory.current` 峰值约 639/643 MB，进程 RSS 峰值约 507/511 MB、VmHWM 约 522 MB；该服务 `memory.events high` 是累计 913，但两组起止差值为 0，OOM/OOM-kill 差值均为 0。质量 runner RSS 峰值约 369/377 MB，cgroup 峰值约 346/355 MB；CPU throttled 计数增量分别 189/216。

ab500-ready 有 152 个样本，最低 `MemAvailable` 约 1.44 GiB，memory PSI 为 0；ab20 有 95 个样本，最低约 1.66 GiB，观测到 memory/io PSI 峰值（memory some/full avg10 最大约 3.99/3.63，IO some/full avg10 最大约 36.17/34.93）。这些是当时的采样与归档计数，不能外推为没有压力或长期容量保证。`resource_violations=0` 仅表示对应收据没有触发预设违规条件。

## 浏览器会话与聊天边界

最终 Chrome 本地收据 [`browser-results.json`](../var/hybrid-concurrency-20261114/browser-20261114-193457-7c6a9ee1/browser-results.json) 为 27/27 检查通过。真实浏览器 UI 创建 Alpha/Beta 两个会话并让两请求重叠；通过 SPA 切回 Alpha 时未整页重载；只取消 Alpha，Alpha 最终为 `CANCELLED`，Beta 仍完成；刷新与新浏览器 context 后两会话仍可读取。请求体只提交 question 与 receipt 标识相关字段；真实 API admission 场景验证 active=2、queue=4 与第七项拒绝（`CHAT_BUSY`）。受控 MockTransport 和合成 Atlas 使其成为会话/UI/取消/准入契约验证，不是生产模型或答案质量验证。

5 组 baseline 和 5 组 R7 provider-message capture 都是 capture-only：每臂 5/5 完成，10 个 qrel 引文字符串中均有 7 个精确字符串出现在实际 provider message；5 对中 4 对消息 hash 相同。qst_0001 的完整引文字符串两臂均未出现，但短语 `7 business days` 两臂均出现，故完整字符串缺失不代表事实短语缺失。capture-only 的 provider response 是合成内容，不能评估回答质量。

另有真实 Chat HTTP/SSE 冒烟收据：`r7-chat-real` 2/2 完成，`r7-chat-boundary` 1/1 完成；它们验证请求/流式集成状态，`answer_quality=NOT_RUN`。本审计不提出模型回答正确、事实可靠或用户体验质量结论。

## 结论与限制

证据支持：最终本地集成回归收据通过；串行/并行检索都在该开发 qrel 上达到相同的映射单元覆盖布尔结果；并行组实际 worker 区间有交叠；cap4 请求等待 gate 队列，未提高吞吐且 p95 变差；浏览器双会话取消隔离和刷新持久性契约通过。

证据不支持：7 个有序候选/上下文差异已解释或等价；UNKNOWN 单元已被完整评测；有答案生成质量提升；生产流量下的长期容量或无压力保证；客户端并发 8 的效果。所有上述限定以原始收据和分母为准。

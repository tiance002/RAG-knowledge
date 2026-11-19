# RAG 项目复盘与面试复习索引

建议按「遇到什么问题 → 为什么选这个方案 → 做了哪些改动 → 如何验证 → 还有什么边界」来回顾。下表是入口，执行命令、原始结果和阶段性结论以各报告为准。

| 主题 | 复盘重点 | 报告入口 |
| --- | --- | --- |
| 异步入库和任务恢复 | 为什么 SQLite 记录任务状态，Redis/Celery 负责投递。租约、重复通知、迟到响应和旧索引保护分别解决什么问题。 | [Worker 运行说明](file-task-workers.md)、[单请求补发实测](../var/ecs-file-failure-states-20261119/isolated.log)、[发布回执](../var/ecs-file-failure-states-20261119/deploy.log) |
| BM25 倒排和写入优化 | 为什么把查询从旧 JSON 词频记录切到倒排表。写入统计少做了哪些工作，SQL 次数、WAL 和总耗时如何权衡。 | [倒排索引改造](bm25-inverted-index-20261107.md)、[统计写入改造](bm25-statistics-write-optimization-20261112.md)、[ECS 对照记录](../.planning/2026-11-18-bm25-write-publish-60-validation/findings.md) |
| 混合检索并行与容量 | 向量与 BM25 两路怎样并行，线程和 I/O 约束为何会抵消并行收益，如何对照单题延迟和并发吞吐。 | [混合检索并行验证](hybrid-concurrency-implementation-and-validation-20261114.md)、[Chroma 异步改造](chroma-async-ecs-20261114.md) |
| 统一分块、父子块和旧索引复用 | Parser 与 Chunker 如何分工。命中子块、父块正文和引用定位分别承担什么责任。哪些旧索引经过保留，哪些格式进行了重解析。 | [统一分块与 ECS 验证](unified-document-chunker-ecs-20261116.md)、[最终重放结果](unified-chunker-ecs-final-results-20261116.md)、[父块来源身份](parent-source-identity-20261115.md) |
| 上下文超限和错引 | 为什么分块小仍可能超出最终请求预算。如何处理历史消息、父块扩展、证据引用和最终提示词。 | [总请求预算复测](total-request-budget-ecs-results-20261116.md)、[引用偏差诊断](agent-citation-partial-investigation-20261115.md) |
| 模型路由和评测 | 为什么默认 Qwen、按风险升级 GLM。怎么定义可修正题，如何衡量遗漏、误升级以及开发集与冻结集的变化。 | [路由分析](quality-first-router-analysis-20261117.md)、[路由迭代结论](router-v9-current-conclusion-20261117.md)、[RSI 可复用评测资产](RSI-TEST-ASSETS.md) |
| Langfuse 与可观测性 | 怎样记录一次回答的检索、重排、生成和异常。如何区分模拟结果、真实调用与原始证据。 | [Langfuse 基线](p0-langfuse-baseline.md)、[接入补充](e1-1-langfuse-only-supplement.md) |

本地生成的两份 HTML 测试报告和两份执行输出归档在 `var/repository-closeout-20261119/`，归档清单记录原路径、大小和 SHA-256。历史设计报告仍位于 `docs/`，任务过程和实验原始记录仍位于 `.planning/`、`var/`。其中部分材料仅存在当前工作区，单独克隆 Git 仓库时需要同时保留本地副本。

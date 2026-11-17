# P0 测试记录（2026-10-28）

## 版本与范围

工作目录 `D:\RAG knowledge`，分支 `codex/pdf-parser-migration`，基线 HEAD `42bf2ab3bfd3b84dda56510d96e0afa9e506c6c2`。P0 改动尚未提交。origin 指向上游 futureYYY/Agentic-Multimodal-RAG，不把它误记为用户仓库 tiance002/RAG-knowledge。未推送本轮改动，未切正式服务。

目标目录不存在 AGENTS.md、progress.md、docs/adr；遵从本次用户提供的执行规范和明确 P0 范围，不臆造里程碑。保留其他会话/用户的 archive、output.json、pytest_html_report.html 等文件。密钥文件 .env.langfuse.local 被 Git 忽略，不输出其内容。

## 本地命令与证据

以下命令在目标仓库执行，必须以实际退出码为准；未实现的命令不得报告通过。

```powershell
& .\.venv\Scripts\python.exe -m pytest backend/tests/test_observability.py backend/tests/test_langfuse_sdk_export.py backend/tests/test_evaluation_baseline.py backend/tests/test_observation_stages.py backend/tests/test_index_lineage.py backend/tests/test_llm_answer_limit.py backend/tests/test_score_publication.py -q
& .\.venv\Scripts\python.exe scripts/summarize_p0_trace_run.py --runtime var/p0-observability/live10
```

第一项 15 passed，1 个既有 Pydantic 弃用警告，退出码 0；日志 var/p0-observability/tests-final.log。第二项退出码 0，10 条派生记录、1 空、0 跳过、1 截断；摘要 var/p0-observability/live10/p0-validation-summary.json。模型/Vector 替身测试与实际 SDK 内存导出均属于本地验证，不冒充 Cloud 或模型端到端。

## ECS 与 Cloud 实际验证

| 项目 | 结果 | 证据与限制 |
|---|---|---|
| SDK 配置与 US Cloud auth_check | PASS | 隔离配置已认证，不输出密钥 |
| live10 preflight | PASS / 退出 0 | 冻结输入与模型身份核验；0 模型调用 |
| live10 第一轮启动 | FAIL | 新隔离 venv 缺原应用 openai，导入前失败，0 模型调用；保留日志 |
| live10 第二轮 | 10/10 请求返回 | ECS 日志 FINISHED，结果 10 条；未保存主进程 OS 退出码，不能称该进程退出 0 |
| Cloud 10 条 Trace 回读 | PASS / 退出 0 | cloud-readback-final.json，与 results trace_id 全部匹配 |
| PDF 页级 Gold 派生评分 | PASS / 退出 0 | ECS 评分脚本；p0-evidence-scores.jsonl，4 题可评，6 题 UNKNOWN |
| 入库首轮 | FAIL / 退出非零 | 未找到完全匹配缓存，未回退付费 Embedding；保留 ingest-run.log |
| 入库修正轮 | PASS / 退出 0 | 真上传/解析/BM25/Chroma，0 模型调用；ingest-run-r2.log、ingest/result.json |
| 同文件真实向量查询 | PASS / 退出 0 | 回读相同 chunk；lineage-run.log、ingest/lineage-check.json |
| 入库/查询 Cloud 读回 | PASS / 退出 0 | cloud-ingest-readback.json，两条 Trace 可关联 |
| 正式 HTTP/SSE、真实 Agent 重试与引用 | NOT RUN | live10 直接调用真实检索、重排、扩展与非流式生成服务 |
| 全 250 题重跑 | NOT RUN | 遵守边界 |
| Cloud Score 写入与读回 | PASS / 最终读回退出 0 | 10 题 124 个确定性分数全部匹配；首次立即读回退出 1（尚未可见），保留记录，之后只读核验成功，无重复模型调用 |
| 完整答案判分 | NOT RUN | 核心正确/忠实度/引用/最终补救缺充分依据，保留 null |

ECS live10 原目录：`/opt/amrag/experiments/pdf-table-hardcases-20261027/p0-live10-20261028-161431-24488f8e`，代码快照 code-r1。后续入库验证代码 code-r2，运行目录 `p0-ingest-20261028-163931-3c0d3d1a`。两个实验与生产配置、DB、索引隔离，既有历史材料未删除。

实际调用 19 次 rerank、10 次生成、0 新 Embedding。生成 usage：100775 输入、15610 输出、116385 总 Token；这是生成用量，不是所有解析/重排成本。价格未知，金额 UNKNOWN。smoke Prompt 和输出上限 4096 独立版本，不与旧评测混算。

4 个 PDF 题前三题 context 覆盖 1.0，第四题 0.5；不是总体 recall@10，也不是回答正确率。后者丢失范围为阈值候选到 Top-K/上下文之间；当前事件无法再细分。后加 Top-K 和保留兄弟块事件用于后续实验，不能反填本轮。6 题来源 Gold 不充分、10 题正式引用未执行，对应 UNKNOWN。

## 验收判断

已验证：默认关闭、观测故障不重试业务、不吞原异常；真实 Cloud Trace 接线、10 题核心检索生成链、真实入库/检索关联；独立未知评分、实验版本与历史回放。

未完成：正式 HTTP/SSE 与真实 Agent 重试全链、完整答案忠实度/引用判分和补救成功率。本地真实 Agent 导入检查因 langgraph 缺失退出 1，没有用伪造 StateGraph 测试冒充真实图验证，也没有另装依赖。当前不宣称 P0 全项验收通过，不打 Tag。最小后续为 ECS 已有依赖环境中的少量正式路径代表题，不需要先重跑 250 题。

Score 执行命令（退出码分别 0、1、0）：

```powershell
& .\.venv\Scripts\python.exe scripts/publish_p0_scores.py --input var/p0-observability/live10/p0-scored-records.jsonl --output var/p0-observability/score-publish-dryrun.json
& .\.venv\Scripts\python.exe scripts/publish_p0_scores.py --input var/p0-observability/live10/p0-scored-records.jsonl --config backend/.env.langfuse.local --output var/p0-observability/score-publish-cloud.json --publish
& .\.venv\Scripts\python.exe scripts/publish_p0_scores.py --input var/p0-observability/live10/p0-scored-records.jsonl --config backend/.env.langfuse.local --output var/p0-observability/score-readback-cloud.json --verify-only
```

只读独立代码审查未发现明确 P0/P1 功能或隐私缺陷，但未独立运行模型、Agent 和取消分支；不把代码审查代替运行验收。

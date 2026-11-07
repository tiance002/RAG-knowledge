# M2 Agent 引用与偏好 A/B：审查文件索引

本文供独立模型/开发者审查结果与代码。原始题目、资料、上下文和回答只在本地共享；不要上传到公开仓库或未获授权的模型服务。路径均为本机绝对路径。

## 重要的评分更正

原始独立复核 JSON 不改写；`currency-correction-v1.json` 是后续复核覆盖层，应优先应用：Excel T07 原文件汇总行的显示格式包含 `$`，因此不能将美元视为事实错误。Agent 6 题的全文忠实度修正为 PASS 5、PARTIAL 1，引用支持为 PASS 4、MISSING 1、SOURCE_LIST_ONLY 1，行内引用 4/6 不变。解析送达文本仍遗漏单元格货币格式。Excel 普通 B 的全国汇总脚注范围歧义另行保留。

## 报告和逐题复核

- 本轮中文报告（末尾含更正）：`D:\RAG knowledge\docs\m2-agent-citation-preference-ab-20261104.md`
- M2 总报告（末尾含更正）：`D:\RAG knowledge\docs\m2-preference-review.md`
- Agent 逐题复核，包含回答、Gold、证据与请求引用：`D:\RAG knowledge\var\preferences-acceptance\m2-agent-preference-ab-20965c41264e4759b79309df82d74b60\independent-agent-answer-review.json`；同目录 `.md` 为摘要。原 JSON 的币种判定须应用下方更正。
- 普通模式逐题复核：同目录 `independent-chat-review-v1.json` 和 `.md`；原 JSON 的币种判定须应用更正。
- 币种更正：同目录 `currency-correction-v1.json`。
- 两次运行的调用、参数、Token、来源和 A/B 可比性核对：同目录 `independent-combined-operational-audit-v1.json` 和 `.md`。

## 冻结题目、Gold 和原始材料

- 基础冻结清单（题目、Gold、80 个片段、来源路径与 SHA256）：`D:\RAG knowledge\var\preferences-acceptance\m2-manifest-v3.json`
- 补充流程题及 Gold：`D:\RAG knowledge\var\preferences-acceptance\m2-current-supplement-real-english-v1\dataset.json`
- Word 原件：`D:\RAG knowledge\var\preferences-acceptance\m2-source\408f7757b382f46d41fd7a8b89fa1cd6bcebeef1bd784fd372dabd8373abc514.docx`
- Excel 原件：`D:\RAG knowledge\var\preferences-acceptance\m2-source\excel-2.xlsx`；检查 T07 的 C5、C7:G7 显示格式、C9。
- Hotpot 原始材料：`D:\RAG knowledge\var\preferences-acceptance\m2-source\hotpot-selected8.jsonl`
- 基础清单另有 PDF：`D:\RAG knowledge\var\corpus\8f49f93f1665fd5ff604e66ffed9649d8b18ce7f114ab43463aeac46cd3d00cb.pdf`；但本轮 3 道 A/B 题不包括 PDF 题，不应混入本轮分母。

## 原始回执与实际模型请求

首轮目录：`D:\RAG knowledge\var\preferences-acceptance\m2-agent-preference-ab-1f095dfd520e4378a266f98cb32bfd8b`

续跑目录：`D:\RAG knowledge\var\preferences-acceptance\m2-agent-preference-ab-20965c41264e4759b79309df82d74b60`

两个目录中：`plan.json` 为冻结配置，`started.jsonl` 为已启动身份，`trials.jsonl` 为身份状态和耗时，`events.jsonl` 为实际输出事件与回答，`policies.jsonl` 为偏好解析，`observations.jsonl` 为本地检索与工具轨迹，`usage.jsonl` 为调用和 Token，`summary.jsonl` 为终态。`calls/` 中每次请求有 `*.started.json`、`*.request.json`、`*.finished.json` 和有响应时的 `*.response.body`；每个逐题审查 JSON 中有对应路径或哈希。目录中的 `isolated.db`、`chroma/` 是隔离测试副本，不应当成生产数据库。

首轮的普通流程 B 为 HTTP 503、生成 UNKNOWN，续跑严格排除已启动的两个身份。两轮合计 12 个身份启动、11 个回答、1 个 UNKNOWN；5 对 A/B 有回答。供应商实际账单 UNKNOWN，HTTP 200 或 run 退出 0 不等于答案正确。

运行输出日志：`D:\RAG knowledge\var\preferences-acceptance\m2-agent-preference-ab-1f095dfd520e4378a266f98cb32bfd8b-execute.log` 和 `D:\RAG knowledge\var\preferences-acceptance\m2-agent-preference-ab-20965c41264e4759b79309df82d74b60-execute.log`。

## 源码与测试

- 本轮 Agent 提示修复：`D:\RAG knowledge\backend\app\services\agent_workflow.py` 的 `agent_node` 生成指令；没有新增严格答案校验。
- 上下文、来源与真实生成相关的既有调用：`D:\RAG knowledge\backend\app\services\chat.py`、`D:\RAG knowledge\backend\app\tools\retrieval_tool.py`、`D:\RAG knowledge\backend\app\services\grounded_generation.py`。
- 本轮实验入口：`D:\RAG knowledge\scripts\m2_agent_preference_ab.py`；依赖复用 `m2_hybrid_postfix_continuation.py`、`m2_hybrid_free_run.py`、`m2_hybrid_free_app.py`。入口和定向测试为工作区未跟踪文件，不在 Git HEAD。
- 测试：`D:\RAG knowledge\backend\tests\test_m2_agent_citation_regression.py`、`D:\RAG knowledge\backend\tests\test_m2_agent_preference_ab.py` 和 `D:\RAG knowledge\backend\tests\test_agent_grounded_answer.py`。
- 最终测试日志：`D:\RAG knowledge\var\preferences-acceptance\m2-agent-citation-ab-final-tests.log`；164 passed，退出码 0。完整命令见中文报告。

当前 HEAD 为 `57f9972b8477b0c4aaf7fac329b911420fafc95e`，分支 `codex/pdf-parser-migration`。工作树存在本轮以前及本轮的未提交修改，`git diff` 相对 HEAD 会混入以前的工作，不能把整个 diff 当成本轮改动。审查本轮提示时，可将历史运行的实际最终请求中的 system 消息与当前源码和本轮请求比较；没有保存完整本轮前 `agent_workflow.py` 源码副本，不能声称取得完整精确的单轮补丁。

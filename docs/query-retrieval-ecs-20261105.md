# Query Rewrite、Query Expansion 与 Weighted RRF：ECS 实施记录

日期：2026-11-05。任务依据为负责人提供的文字规则和 Weighted RRF 截图。当前本地分支 `codex/pdf-parser-migration`，HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e`，保留全部原有未提交修改。本轮没有提交、推送或安装依赖。

## 1. 更新范围与运行身份

本轮代码已同步到 ECS 的 **`/opt/amrag/dev-runtime`**，其中 61 个最终源码／新增测试文件的 SHA256 与本机逐项一致。该目录为可复用的单一开发运行目录，约 1.2 MiB，不包含语料库、数据库副本、依赖或索引副本。

ECS 的正式服务仍为 PID `1760402`、`127.0.0.1:8200`、`/opt/amrag/backend`，原生 Uvicorn 单进程。它的源码明显早于当前本地版本，例如正式 `llm.py` 只有约 5.9 KB，缺少当前接口和观测／故障合同；本轮没有把局部新文件强行混入旧服务，也没有替换该正式进程。**“已在 ECS 更新并测试”指开发运行目录，不表示正式 8200 已启用这些改动。** 正式版本迁移与重启为 NOT_RUN。

测试复用 `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv`，pytest 复用既有 `/opt/amrag/experiments/pdf-parser-20261027-r1/offline/test-tools`。没有新建虚拟环境或容器；没有操作其他项目 PostgreSQL/Redis 服务。

## 2. 实际改动

### Query Rewrite

`LLMService.rewrite_query(query, history=None)` 的默认 Prompt 现在明确：

- 保留原语言、含义、实体、技术术语、标识符、数值、否定及约束。
- 不自动翻译中英文或生成双语关键词，当前请求明确要求翻译时才翻译。
- 仅从真实历史补全指代和省略，歧义不明时保留原文，不猜实体。
- 保留总结、比较、解释和追问目的，不改成“请在知识库中查找……”的泛化操作指令。
- 历史为引用数据，不将旧助手回答当作已验证事实；不直接回答问题。

普通模式按最后一个真实 user 消息定位当前问题，把之前的 user/assistant 历史传给改写，排除 system、偏好注入和尾随草稿。独立 rewrite API 的 `RewriteRequest.history` 为可选字段，旧调用仍可用。没有新增答案字符串门禁或独立 LLM 审查模型。

原问题通过 `original_query` 继续参与 BM25，向量路只对当前完整检索问题执行一次查询。相同原问题／改写去重后只执行一次词法查询。空改写、截断改写继续保留原问题；Provider UNKNOWN 仍终止，不自动再次收费请求。

### 意图与 Agent

复用现有意图分析调用，补入真实历史以理解追问，保留现有闲聊／明确资料请求快捷路径。内部仍使用 `chat`、`qa` 两条图边：文档总结、比较进入 qa，普通对话总结可进入 chat。联网搜索仍由显式 `web_search_enabled` 单独路由；本轮没有让改写自动触发联网搜索或额外增加每轮分类模型。

Agent 及其初始化现在使用最后一个真实 Human/user 请求，排除尾随助手草稿。工具检索的 `original_query` 由服务端 state 注入，模型不能伪造替换；知识库范围、权重、重排参数仍由服务端决定。

### Query Expansion

新增轻量 `query_expansion.py`，复用现有 Jieba／英文 tokenizer、停用词过滤、词频和词序抽取，不调用模型、不翻译。最多生成 **5 条不同的扩展查询**，与基础查询分开计数。

触发条件：初始向量＋基础词法的去重候选总数少于请求 `top_k`。这是明确的候选数量规则，尚不是“正确答案证据不足”的语义判断。大库向量路通常可返回较多候选，因此该规则较保守；没有为触发扩展而修改正式相关性阈值。

基础 BM25 查询包括原问题和上下文完整问题。扩展仅补充 BM25，**不会为每条扩展重新执行向量或 Embedding**。这里的“一次”指一次检索请求、每个不同 KB 一次；Agent 主动发起的新一轮检索另计。

各词法变体先合并为一条 BM25 候选列表，按最佳变体排名、原分和稳定 ID 排序。同一 chunk 多次命中不会累加成多个融合投票。保留变体来源，避免关键词扩展数量放大 BM25 权重。

### Weighted RRF

本轮发现旧 Hybrid 实际采用“先归一化两路原分，再加权相加”。已按截图改为：

```text
rrf(d) = 0.7 / (60 + vector_rank(d))
       + 0.3 / (60 + bm25_rank(d))
```

排名从 1 开始，某路未命中则该项为 0。先融合排名，再对最终 RRF 分数做 Min-Max 归一化；全部相等时保留原有归一化约定，分数为 1。实际传入的两路权重仍可使用，默认 0.7／0.3，K=60。记录原分、两路排名、贡献、融合原分、归一化结果和词法变体来源。

没有修改解析器、Embedding/Rerank 模型、Agent 的 0.8 评分阈值、重试上限或正式默认相关性阈值。

## 3. ECS 测试结果

所有测试在 ECS 执行。本机只编辑、打包、传输和复核回执。局部测试中的 Provider 替身仅用于验证调用与故障合同；下表真实检索均为 Hybrid。

| 验证 | 结果 | 原始退出码／证据 |
| --- | --- | --- |
| 改写历史、语言规则、扩展、RRF、故障与兼容回归 | PASS；63 passed | 0，`targeted-r1.log` |
| Agent 原问题与 Scope 接线、尾随草稿、相关改写与清理回归 | PASS；16 passed | 0，`targeted-r2.log`。与 63 项有重叠，不相加。 |
| 已有普通问答证据保护与偏好回归 | PASS；64 passed | 0，`normal-memory-regression.log`；复用既有两份测试，不将各阶段数字相加。 |
| SQLite 清理独立回归 | PASS；真实临时 SQLite，正常／异常两个分支 | 0，`cleanup-r2.log`，2 tests。也与上一组清理用例重叠。 |
| 真实模型改写 | 4 次请求均 HTTP 200，4 条语义复核符合预期 | Qwen3.5-4B，关闭思考，max_tokens=200 |
| 真实 Hybrid 主测试 | 4 题、6 次检索，每次目标片段均召回 | BGE-M3，6 次 Embedding，每次仅一次；无降级 |
| 受控候选不足分支 | PASS；两个基础查询＋5 条扩展，仍仅一次 Embedding | 0，`controlled-expansion-command.log`、`controlled-expansion.json` |
| 本轮最终源码身份 | PASS；61 文件逐项一致 | 0，`source-verification-cleanup.log` |
| 正式 8200 服务切换 | NOT_RUN | 旧进程、源码与正式入口保持原状 |

真实材料来自已有 `prod-hotpotqa` KB，ID `c2202c4d-5eaf-4fec-bfe7-16fe10f2506a`。直接只读使用 `/opt/amrag/backend/local.db` 与原有 Chroma，未重新入库。Embedding 使用库中绑定的 `BAAI/bge-m3`，没有更换模型或重新生成文档向量。

| 问题 | 实际改写 | Hybrid 检索情况 |
| --- | --- | --- |
| What man is credited with inventing both the Leyden jar and the Tribometer? | 保留原英文问题 | 改写组和原问题组目标片段均召回，两路有结果 |
| 哪位科学家被认为发明了莱顿瓶和摩擦计？ | 保留中文，没有自动翻译 | 改写组和原问题组 BM25=0，向量路召回目标片段 |
| Which tribological quantities does a Tribometer measure? Keep the term Tribometer unchanged. | 保留英文及 Tribometer | 目标片段召回 |
| Which device did he invent in 1746?；历史已明确 Pieter van Musschenbroek | Which device did Pieter van Musschenbroek invent in 1746? | 目标片段召回，BM25 实测同时使用原问题与补全问题 |

主测试共六次检索（4 题，其中 2 题另有原问题对照），Hit@10 为 6/6，每次冻结目标 chunk Recall@10 均为 1。它们涉及少量相关的公开科学资料，**不是整体准确率或统计显著提升结论**；本轮没有生成最终问答，不能报告回答正确率提升。

六次主测试候选充足，未触发扩展。另补一次 Hybrid 受控请求，将该次 `score_threshold` 设为 0.99，用于制造候选不足，明确不更改正式配置。实测 1 次 Embedding、7 次本地 BM25 查询（2 基础＋5 扩展），最终有真实词法结果。它验证调用机制，不作为阈值或效果优化证据。

本轮合计 **4 次改写 Chat＋7 次 Embedding**，没有请求 UNKNOWN、没有自动重发历史不明请求、没有生成回答或调用 OpenAI Judge。凭据只由服务器已有模型配置在进程内读取，不写入 manifest、日志或本报告。

### 原始验收脚本的收尾故障

`live-r1` 的四次改写和六次 Hybrid 查询均已返回且记录完整，但退出时在 Session 关闭前执行了 engine.dispose，导致 `Cannot operate on a closed database`。其**原退出码 1 和 STOPPED 回执保留**，不能把脚本总结果改为退出 0。

已修正为先关闭 Session，再 dispose；真实临时 SQLite 的两个分支检查通过。没有重跑十次 Provider 请求。复核程序基于原始 results/attempts 记录确认各阶段结果，统一状态为 `VERIFIED_STAGE_RESULTS_WITH_FIXED_RUNNER_CLEANUP`，不伪造原运行 COMPLETED。修复后完整云调用重跑为 NOT_RUN。

## 4. 执行命令与证据

ECS 工作目录 `/opt/amrag/dev-runtime/backend`，复用已有依赖：

```sh
PYTHONPATH=/opt/amrag/experiments/pdf-parser-20261027-r1/offline/test-tools \
LANGFUSE_ENABLED=false RAG_OBSERVABILITY_ENABLED=false \
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python -m pytest -q \
 tests/test_query_rewrite_context.py tests/test_query_expansion_rrf.py \
 tests/test_e11_candidate_transitions.py tests/test_retrieval_outcomes.py \
 tests/test_retrieval_cache_safety.py tests/test_rewrite_completion.py \
 tests/test_provider_unknown.py tests/test_agent_request_parameters.py
```

第二轮只运行受影响的测试：

```sh
PYTHONPATH=/opt/amrag/experiments/pdf-parser-20261027-r1/offline/test-tools \
LANGFUSE_ENABLED=false RAG_OBSERVABILITY_ENABLED=false \
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python -m pytest -q \
 tests/test_query_agent_context.py tests/test_agent_request_parameters.py \
 tests/test_query_rewrite_context.py tests/test_query_retrieval_acceptance_cleanup.py
```

最后的普通模式关联回归使用相同环境运行 `python -m pytest -q tests/test_memory_chat.py tests/test_normal_evidence_guard.py`，退出码 0，64 passed。

真实验收使用 `scripts/query_retrieval_acceptance.py --manifest results/query-20261105/manifest.json --live --output results/query-20261105/live-r1`，原调用额度已消费，以上仅记录历史命令，不默认再次发送。默认不带 `--live` 为零调用预检。

统一 [已审查结果 JSON](<D:/RAG knowledge/var/query-retrieval-20261105/reviewed-results.json>)；[ECS 原始回执目录](<D:/RAG knowledge/var/query-retrieval-20261105/ecs-results>)；[最终源码清单](<D:/RAG knowledge/var/query-retrieval-20261105/source-manifest-final.json>)。ECS 原始证据在 `/opt/amrag/dev-runtime/results/query-20261105`，包含 `manifest.json`、`gold-source-proof.json`、`live-r1/results.jsonl`、`attempts.jsonl`、原日志、原退出码、受控测试与清理回归。

## 5. 文件、资源与遗留

产品修改：`llm.py`、`chat.py`、rewrite API/schema、`retrieval.py`、新增 `query_expansion.py`、`agent_workflow.py`、`retrieval_tool.py`。定向测试新增改写、扩展、Agent 接线与验收清理用例；已有 `test_e11_candidate_transitions.py` 仅更新受影响的旧融合公式预期，保留其已有修改。新增服务器验收脚本。所有本轮修改前源码保存在本机 `var/query-retrieval-20261105/local-before`，ECS 增量前源码也有小型回滚包。

复用原有 venv、pytest 工具、数据库和索引；新建开发源码目录约 1.2 MiB，保存必要报告、源码清单、增量前回滚材料及原始请求结果。删除了 9 个本任务字节码目录和两个已解包传输包，共 258,171 字节的传输中间件；未删除源码、原始材料、历史题集、Gold、模型记录或其他项目资源。磁盘运行前后均约剩 18 GiB（53% 使用），没有新增数据／索引副本或临时容器。

遗留与限制：

- 正式 8200 是旧版，未做不兼容的局部覆盖或自动替换。开发目录可复用，但正式迁移仍需单独核对当前旧版接口与完整依赖。
- “首次不足”目前由候选数量判断，不能保证识别语义证据不足；此规则有显式指标，后续可作为 RSI 候选独立比较。
- Prompt 保真只有这四个真实样例的证据，不承诺任何模型都不会改错实体、时间或意图；未新增严格运行时答案门禁。
- 多轮意图复用现有一次分类调用；没有新建四条独立图分支或自动开启联网搜索。联网搜索仍需要显式开关。
- 本轮验证查询与证据召回，不包含最终回答质量 A/B、正式 HTTP 服务切换或大规模评测。

本轮停止于代码实施、ECS 开发运行目录同步、定向回归和真实 Hybrid 检索验证完成。

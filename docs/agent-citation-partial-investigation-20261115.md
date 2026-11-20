# 打印机引用与 PARTIAL 送达：定位、修复及隔离回归

日期：2026-11-15；项目 `D:\RAG knowledge`；HEAD `7c7415f85a063e3f7645f676bd22f7c8f766a732`，当前分支 `codex/bm25-stats-write-optimization-20261112`。所有既有未提交成果保留；没有提交、推送或替换正式服务。

## 结论

1. **发现并修复一个明确工程缺陷**：父块因长度超限回退为完整命中子块后，邻块循环仍把“存在 parent 对象”当成“父块已送达”，错误地跳过同父块邻块。评分题的“第一次比较”限定语就在相邻子块中，旧请求确实没有这句话；数字 2.7、1.9 和版本名当时已经送达，并未丢失。
2. **PARTIAL 本身不是误报**：现有单父块上限 4,000 字符、总扩展上限 24,000 字符。部分原父块长 4,038～4,093 字符，回退后没有送整份父块，继续标 PARTIAL 正确。没有为消掉这个状态提高限额或改为 DELIVERED。
3. **打印机缺行内引用是模型输出问题，未发现工作流删掉编号或映射错位。** 实际请求已有来源编号及逐事实引用要求；本地 finalizer 保留有编号和无编号文本。新隔离运行直接捕获 SDK 原回复：与最终 SSE 正文完全相等。这次它改为末尾“参考来源：[1][2]”，但仍无事实旁引用，且 `[2]` 不支持两项操作。引用质量保持 PARTIAL，不能宣称已修好。
4. 两题真实 HTTP/SSE 均正常结束、核心数值/操作位置正确，完整源映射与实际网络请求检查通过。**来源映射 PASS 与模型引用质量 PARTIAL 可以同时成立。** 未加严格答案校验、补编号、自动修答案或额外 Judge。

## 一、历史输入和原文核查

复用前轮 `var/agent-model-view-20261114/model-view/` 两题回执：

- `qst_0017`：`52768093e4be40a68868fa0c5d8eb8ae`。
- `terra-ecs-b1-024`：`5d710d28769d4c04bc5b6ea02968fa32`。

新增 `scripts/audit_agent_partial_delivery.py` 在 ECS **只读**读取原候选涉及的文件、Chunk、父块和相关子块；逐项校验原 20 个候选正文哈希，使用已保存 Rerank 响应，通过真实 `batch_rerank → expand_context → build_chat_context → _register_sources` 回放，不重新调用检索或模型。两题恢复的编号、正文和 context 与历史回执一致，退出码 0。小型来源快照保存在 `var/agent-citation-partial-20261115/source-replay-v1/`，不是复制整库或新建索引。

| 题目 | 造成 PARTIAL 的父块 | 原必要信息是否送达 | 本次发现 |
|---|---|---|---|
| 评分变化 | 主来源父块 4,062 字符；另一候选父块 4,038 字符，均超过 4,000 | 2.7→1.9、版本名已在请求；问题中“第一次比较”的原文限定语未在请求 | 该句在子块 `3dc2b31b-b91c-5d5a-8e2f-318bc0bec2e2`，原顺序 index=5，紧邻命中 index=6，window=1 本应可补；被错误 parent 分支跳过 |
| 打印机两个位置 | 四个非完整父块 4,087 / 4,093 / 4,093 / 4,093 字符 | “打印机属性/端口”、Print Spooler、“登陆”页及取消选项的四项冻结引文全部已在来源 `[1]` | 答案所需的操作位置未丢；PARTIAL 涉及其他候选的完整父块，不能推断核心答案没送达 |

评分题漏掉的限定语：

> 2026-03-10 22:05 - Start first pass: compare v2026-02-16-quant (baseline) vs v2026-03-01-opt (current).

旧来源 `[2]` 从 `-quant (baseline) ...` 开始，保留了数值句，却缺少前述“Start first pass”起始句。这是**证据限定信息不完整**，不是原数值答错，也不是全部 Gold 证据都已完整送达。Gold 的长段落和评分量表全文未作为运行时选择词；只在离线审阅中使用，不注入模型。

两题各有 10 项独立种子，原回放中既有文本渲染后全部保留。最初诊断用原字符串逐字包含检查时，有六处因首尾空白被既有 `evidence_text(...).strip()` 去掉而失败；已保留退出码 1 的 `local-replay-v1.json/log`。复核确认是空白，非事实丢失，改为调用真实渲染函数并同时保留原字节差异记录；没有为这个诊断去修改产品。其后基线回放 `local-replay-v2.json` 退出码 0。

## 二、最小修复

唯一产品变更：`backend/app/services/parent_context.py::expand_parent_context`，邻块循环附近。

- 新增 `parent_delivered`，根据**已实际选入 members 的父块**判断，不能只看 parent 对象存在。
- 只有 `PARENT_OVER_BUDGET` 回退、邻块属于同一可信父块、且邻块正文确实存在于父块时，按原 window 和剩余预算补充。
- 文件/KB 范围、不同父块、标题边界、未来种子预留、父块/总字符上限保持原样。
- `INVALID_PARENT_LINK`、`SEED_NOT_IN_PARENT` 不借此扩大扩展，沿用只保留已验证种子的处理。
- 不改 Rerank、检索权重、Prompt、模型或投影模块。修复对调用该共用扩展器的普通模式和 Agent 都有效，但本轮真实请求只复验 Agent。

本轮前后哈希：

```json
{
  "path": "backend/app/services/parent_context.py",
  "before": "7743fbd7c92dd9c9bcf4928c33073d3c01cce7d397f8bec779ac109c03b30ccd",
  "after": "f90cd2e5d4fbaa86bfe3fb1123e78702faa217f6e905fc7359f78de6f71b0e75"
}
```

其他改动：`backend/tests/test_parent_expansion_wiring.py` 增加三组有预算/无窗口/零预算的针对性反例及跨来源保护；两个只读回放脚本；`scripts/agent_model_view_probe.py` 仅在隔离环境显式开启时捕获扩展输入、Context Builder 摘要和 finalizer 之前的 SDK 原回复；已有资源保护脚本仅增加本轮隔离服务白名单。没有增加产品答案门禁。

## 三、本地验证和反证

缺陷复现命令：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_parent_expansion_wiring.py -k oversize_parent -q -o addopts='' --junitxml=var/agent-citation-partial-20261115/repro-before.xml
```

修复前实际退出码 **1**：1 failed、2 passed、6 deselected；失败是同父块单位/表头邻块未进入上下文。

修复后最终命令：

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe -m pytest backend/tests/test_parent_expansion_wiring.py backend/tests/test_parent_context_v2.py backend/tests/test_context_delivery_budget.py backend/tests/test_agent_evidence_projection.py backend/tests/test_agent_grounded_answer.py backend/tests/test_agent_tool_observation.py -q -o addopts='' --junitxml=var/agent-citation-partial-20261115/focused-final.xml
```

最终退出码 **0**：**80 passed，0 failed/errors/skipped，230 条弃用警告**。初版修复曾导致 `SEED_NOT_IN_PARENT` 场景多取邻块，原有反例实际报错（79 passed / 1 failed）；据此把可扩展分支收窄为纯预算回退，未放宽旧断言，原始 `focused-after.log` 保留。

同一冻结来源的修复后回放：

```powershell
.\.venv\Scripts\python.exe scripts/replay_agent_partial_sources.py --sources var/agent-citation-partial-20261115/source-replay-v1 --receipts var/agent-model-view-20261114/model-view/receipts --out var/agent-citation-partial-20261115/local-final.json --allow-context-change
```

实际退出码 **0**，外部调用 **0**。输出已存在时脚本拒绝覆盖；再次执行须换新输出名。

| 同一冻结输入 | 旧 canonical context 字符 | 修复后 | 新增成员 | 证据变化 |
|---|---:|---:|---:|---|
| 评分题 | 9,685 | 13,707 | 9 | “Start first pass”恢复；原数值、版本及全部原已送达正文保留 |
| 打印机题 | 5,809 | 9,069 | 8 | 原核心证据继续保留；增加允许窗口内邻块，不代表这些邻块都与答案有关 |

回放比较的是相同来源和排序输入，足以定位修复效果；新增可见上下文也带来无关文字和长度成本，不能把“取更多”直接算作答案质量提升。两题仍为 PARTIAL。当前投影保留新 context 与 source_ids，旧归档的实际请求也能原样复核。

## 四、ECS 隔离真实回归

在本地验证后，只有 `parent_context.py` 增量进入既有 `followup/current-source/backend`；旧文件备份于本任务输出目录。探针新增的诊断字段不改变业务返回。复用既有 venv、只读正式 SQLite、Chroma 和十库非空范围。未运行生产 lifespan/迁移或解析 worker。

实际任务：`amrag-agent-partial-test-20261115.service`，ExecMainStatus **0**；API 仅监听 `127.0.0.1:18336`。命令：

```text
python -u check_hybrid_chat_delivery.py --cases development20.json --out /opt/amrag/experiments/hybrid-concurrency-20261114-r1/partial-audit/live-v1/live-agent --base http://127.0.0.1:18336 --model-id a8753099-fe56-403b-a7db-ff457a6403f5 --indices 13 0 --mode agent --rerank-model-id fe787636-d155-42aa-848a-24d177f417d9 --rerank-top-k 10
```

仍使用 Hybrid 0.7/0.3、top_k=10（工具内部过召回 20）、Rerank top_k=10、context_window=1、Qwen/Qwen3.5-4B、实际 `enable_thinking=false`、BAAI/bge-reranker-v2-m3；固定模型、关闭改写和联网。没有修改这些参数或通过扩大字符上限让请求通过。

| 题目 / 运行 ID | 实际最终请求字符（投影前→后） | 耗时 | 实际回答与检查 |
|---|---:|---:|---|
| qst_0017 / `3e814a21dad64650866117232dd95d35` | 62,637→25,062 | 21.59s | COMPLETED；2.7→1.9、-0.8 正确；恢复的第一次比较原句实际送达为 `[3]`，分数字段在 `[4]` |
| terra-ecs-b1-024 / `e51f7829fcc74c0bb9cdf193ef1e1258` | 38,932→17,284 | 12.36s | COMPLETED；两个设置位置正确；末尾“参考来源：[1][2]”，`[1]` 支持，`[2]` 不支持；没有逐事实行内引用 |

两次都有真实检索/评分/最终生成，无空答案、截断或 UNKNOWN；无检索重试。每题实际 3 次 Chat 网络请求（工具选择、评分、最终回答），共 **6 次**，另有 **2 次 Embedding、2 次 Rerank HTTP**。6 条捕获 SDK usage 均存在，Chat 合计输入 **20,177**、输出 **485**、总计 **20,662 Token**；不包含 Embedding/Rerank，不从 SSE 的 `AGENT_NODE_CALLS` 误推全链路用量。费用 **UNKNOWN**。

新一轮两题各有 14 项来源，共 **28 项**：完整 registry/工具消息投影前后不变，所有注册正文存在于投影 context，最终实际网络工具消息等于投影视图；20 项原始命中种子的既有文本渲染均在最终请求。打印机四项冻结证据仍完整，评分题数值与“Start first pass”都在请求；状态 PARTIAL 保留。

**对新回答的限制：**评分题核心数值正确，但它将引用中的 `Hypothesis` 扩写为较确定的“主要归因于”解释；不能仅因核心正确就把补充内容与引用全部记 PASS。打印机题新增的 `[2]` 实际是另一文档中商业经济试题/像素等文字，不支持那两项操作；已有正确来源 `[1]` 没有丢。模型 SDK 原回复与 SSE 完全相等，证实没有后端静默删除编号或错换文本；原始的“文章2”和系统编号并存是可能干扰，但未做单因素提示词实验，**具体生成失误原因仍 UNKNOWN，不能认定就是这一因素**。本轮不为此加校验或替模型重写引用。

新旧真实评分题的工具查询发生变化，候选集合也有变化；上表实际耗时不是同输入性能 A/B，不能把 14.90→21.59s 的差值全部归因于此修复。严格同输入对照只来自前一节的零模型回放。

## 五、状态、资源及证据

- 本地工程修复和定向回归：**PASS**。
- 第一次比较限定信息恢复、原种子及核心事实送达：**PASS（所检查两题）**；不宣称其他任务所需事实全部受保护。
- 父块整段送达：**PARTIAL，符合现有限额**，未伪改为完整送达。
- 两次真实完成/核心答案：**2/2 PASS，开发小样本**。
- 打印机行内引用：**未修复/PARTIAL**；映射机制 PASS，模型选择引用仍有错误。
- 评分题补充因果解释与逐事实引用：**PARTIAL / 待进一步限定性复核**；没有用严格答案校验拦截。
- Langfuse Cloud、普通模式真实模型回归、长期容量/质量统计、正式发布：**NOT_RUN**。

35 个资源样本：最低 MemAvailable **1,905.1 MiB**、最低磁盘 **11.66 GiB**，未触及 800MiB/8GiB；隔离 API cgroup 峰值 **268.3 MiB**。正式 81 个源码文件 **0 漂移**，API PID 778457 / Chroma PID 567489 和 MemoryHigh 640MiB 未变；Chroma v2 心跳 200。隔离 API/任务/保护器全部 inactive、MainPID=0、退出码0。

任务结束磁盘剩余约 **11.66 GiB**；当前 ECS 本任务诊断/快照/原始回执约 **1.93 MiB**。没有新容器、数据库副本或索引副本。保留必要来源快照、修复前后回执和脚本供追溯，停止了本任务隔离进程，没有删除历史原始资料。归档包是停止 API 后的快照，之后写入的 `final-state.json` 单独保存。

证据根目录：`D:\RAG knowledge\var\agent-citation-partial-20261115`。

- `manifest.json`：版本/文件哈希/实际命令结果索引。
- `changes.patch`、`before/`：本轮单产品文件增量及修改前工作现场，恢复不能用 HEAD 整体覆盖已有 dirty 成果。
- `local-final.json`、`local-fact-review.json`：同输入实际函数回放和字段检查；事实检查只在离线评审使用，不进入选择器或答案门禁。
- `reviewed-live.json`：逐题问题标记、实际回答、被引用的完整来源、成本/状态与原始回执关联（开发者复核，不冒充负责人手工标签）。
- `live-v1/live-agent/`、`live-v1/receipts/`：真实 SSE、扩展输入/输出、模型输入、finalizer 前原回复和完整注册表。它们含正文，仅保留本机/ECS 获准位置，不上传 GitHub/Cloud。
- `live-v1/final-state.json`：正式服务未替换及资源收尾证据。

本轮停止于明确的工程修复和隔离验证。对模型引用习惯的改进应另做小型、固定上下文的生成提示实验；它不应混入父块邻域修复，也不应通过严格规则或自动补引用掩盖。

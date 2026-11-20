# 六父块上限与综合重排本地验收

日期：2026-11-15。工作区 HEAD：7c7415f85a063e3f7645f676bd22f7c8f766a732；保留原有未提交工作。

## 实际行为

综合排序默认权重：0.6 模型分 + 0.3 检索基础分 + 0.1 来源权重，权重除以总和。原始 rerank_score 和 score 保留。原模型阈值仍过滤 rerank_score，综合分决定排序与 Top-K。来源权重 web_search=0.95，当前内部来源=1；内部来源之间没有虚构可靠性差异。

检索分都在 [0,1] 时保留；候选池出现范围外数值时，对该池的有效检索分 min-max 归一化，等值池为1。模型分限制到 [0,1]。这是一项适配，尚未证明质量收益。缺模型分的候选 composite_score=null，保留 UNKNOWN_MODEL_SCORE，排在已评分候选后，不冒充重排成功。未开启 rerank 时没有新增模型调用。

父块以最强命中子块的最终分数排序，同分保持输入顺序。默认最多选六个父块，不足六个不补齐；未获父块名额的独立命中子块仍保留。聚合字符预算仍可令入选父块回退子块。selected_parent_id 表示实际送达父块；parent_selection 表示名额选择。六父块不是整个请求只有六条证据，历史与独立子块仍占用请求空间。

普通聊天的后置排序复用综合排序键，防止覆盖。上下文摘要保留选择依据；Agent 模型投影剥离新增后台评分字段；候选观测区分 composite 与原模型分。

## 测试与复核

命令（PowerShell，仓库根目录）：

```powershell
$env:PYTHONPATH='backend'
.venv\Scripts\python.exe -m pytest backend/tests/test_parent_context_v2.py backend/tests/test_parent_expansion_wiring.py backend/tests/test_parent_context_compatibility.py backend/tests/test_evidence_repairs.py backend/tests/test_e20_context_delivery.py backend/tests/test_image_caption_parent_child.py backend/tests/test_async_rerank.py backend/tests/test_rerank_unknown.py backend/tests/test_rerank_composite.py backend/tests/test_agent_evidence_projection.py -q --tb=short -p no:warnings
```

最后联合运行：134 passed / 1 failed，退出码1。唯一失败为新增测试遗漏必填 model 参数，修正测试调用后单跑 test_rerank_composite.py：2 passed，退出码0。联合运行中的另外133个通过项与该文件两项合计135个独立用例通过；没有将联合命令退出码伪报为0。日志：var/parent-six-composite-final-tests.txt、var/rerank-composite-corrected-tests.txt。

早期失败保留：var/parent-six-composite-tests.txt。实现阶段 result 构造自引用错误已修正；旧字符预算测试显式给8个父块名额，使其继续覆盖24k预算而不是先触发6个上限。

测试覆盖八父块按最强子块选六个、同父多命中归并、落选父块保留种子、4096完整父块、真实普通/Agent扩展接线、合成 Provider 请求、引用映射、异步/同步重排一致性、UNKNOWN无重复调用、综合分改变Top-K与阈值独立性。业务外部模型调用0。

## 限制

ECS部署、真实模型质量对照、60题新回归：NOT_RUN。未宣称正确率提升。全局 git diff --check 有工作区 CRLF/空白告警，未清洗其他任务文件。

参考 WeKnora 官方 RAG pipeline 的 compositeScore 与父块合并最高分机制：https://github.com/Tencent/WeKnora/blob/main/website-docs/02-architecture/04-rag-pipeline.md 。六父块为本项目约束；没有声称上游固定六块。

## ECS 隔离回归：Agent + Hybrid + 综合重排（2026-11-15）

本节更新前文的“ECS / 60 题 NOT_RUN”。隔离回归已经运行，但因生成请求字符预算连续触发保护停止，**60 题未全部完成**；以下结果为部分运行的实测，不代表正式服务切换或完整 60 题验收。

### 运行身份与隔离

- 本地基线：`7c7415f85a063e3f7645f676bd22f7c8f766a732`。ECS 以此前父子上限隔离快照为底，仅覆盖 `config.py`、`rerank.py`、`parent_context.py`、`context_builder.py`、`agent_evidence_projection.py`、`agent_workflow.py`、`isolated_eval_capture.py`、`retrieval_tool.py` 八个源码文件，并加入一次性隔离 API/Runner；覆盖文件 SHA256 见本地 `var/agent-composite6-20261115/ecl6-de2c7183/manifest.json`。
- 冻结输入：ECS `/opt/amrag/evaluation/ecs-live60-20261112/input-qwen-v4.jsonl`，60 题，SHA256 `0b5de53ddb88ae9c157d3f4ff039a51d43d212b6760fe73030a93bd4a6bf3f46`。Gold 只在本地。
- 隔离运行：`agent-composite6-ecl6-de2c7183`；Agent 模式、Hybrid、Qwen/Qwen3.5-4B、Rerank 开启，Top-K 20，Rerank 综合权重 `model=0.6 / retrieval=0.3 / source=0.1`，父块上限 6，历史上限 5。未单独调用 Recall 接口。
- 只读复用 ECS SQLite 与在线 Chroma；未复制数据库或索引。隔离 API 只监听回环端口 `18339/18340/18341`。正式 API 始终为 PID `778457`，未切换或重启。
- 最终只读核验命令退出码 0：正式服务 `active`、PID 仍为 `778457`、隔离监听端口均已释放；`/opt` 可用磁盘约 12 GiB、`MemAvailable=2,181,216 kB`，超过 8 GiB/800 MiB 保护线。
- 隔离包 `preflight.sh`、`preflight-segment40.sh`、`preflight-segment50.sh` 均退出码 0；三个 Agent Runner 状态分别为 5、5、5，这是连续 5 次预算拒绝后的预设停止码，不是完整测试通过。三条隔离 API 健康检查均返回成功，结束后端口已释放。
- 本地定向测试命令见本文件前文：联合命令曾因新增测试调用缺必填参数退出 1；修正测试后单文件退出 0。合计 135 个独立定向用例通过，不把联合命令退出码写成 0。
- 下载归档 `var/agent-composite6-20261115/ecl6-de2c7183/results-ecl6-20261115.tar.gz`，SHA256 `4bb615b6ce3a324d99e1e98421e8c023bd33a36c1c9042c4a6e1b7050d4cad05`。原始 SSE、请求、逐题回执和服务日志均保留在该目录及 ECS 原归档目录。

### 执行结果与停止原因

| 项目 | 实测 |
|---|---:|
| 计划题数 | 60 |
| 已尝试 | 45 |
| 正常返回 | 27 |
| 应用字符预算失败 | 18 |
| 未运行 | 15 |
| HTTP/SSE 尝试 | 63（18 个失败题各重试一次） |
| Provider 请求 / 已知错误 | 159 / 0 |
| Provider 输入/输出 Token | 495,726 / 8,413（包含 Agent 内部请求及重试） |
| 模型侧 `finish_reason=length` 截断 | 0 |

18 个失败回执均为 `FINAL_REQUEST_OVER_CHAR_BUDGET`，阶段是 `generation_input_budget`：最终生成请求在发给 Provider 前被应用拒绝。它们不是模型返回长度截断，也不是 HTTP 429/Provider 错误。每题只重试一次，仍失败。三段运行均在连续 5 题触发该拒绝后按保护条件停止；后续 15 题都是这些会话的追问，未伪造缺失的前序答案继续跑。18 个失败题仍有检索/Rerank 记录，但没有最终答案可评。

失败 QID：`legacy-session3-v5-s1-t09`、`legacy-session3-v5-s1-t10`、`legacy-session5-v3-s1-t06`、`isolated-session6-v4-s1-t01` 至 `t05`、`forward-session10-v2-s1-t01` 至 `t05`、`forward-session14-v7-s1-t01` 至 `t05`。未运行 QID：`isolated-session6-v4-s1-t06` 至 `t10`、`forward-session10-v2-s1-t06` 至 `t10`、`forward-session14-v7-s1-t06` 至 `t10`。

### 综合重排及六父块的实际接线

- **综合重排：PASS（机制执行证据）**。66 次 Rerank 执行、6,565 个候选，全部得到模型分数；每次实测权重都是 `0.6/0.3/0.1`，输入 Top-K 20、输出 20。综合分改变了 66/66 次候选顺序；相对于模型原始 Top-20，65/66 次成员集合发生变化。因此不是只配置了权重，而是综合分实际影响了排序与筛选。该结果证明接线生效，**不证明答案质量提升**。
- **六父块上限：PASS（实际 Provider 请求计数）**。66 次扩展的父块选择数量为 6 块 48 次、5 块 14 次、4 块 4 次，超过 6 为 0。进一步按 `source_id` 将 30 条含工具证据的实际 Provider 请求与同次 `rag_result.citations` 映射，30/30 成功；这些实际请求中的父块数量为 6 块 26 次、5 块 2 次、4 块 2 次，最大 6、超过 6 为 0。
- 六父块限制的是扩展父块数量，不是整个模型请求只有六条消息/证据；独立种子子块、历史消息及其他提示仍会占用输入。哈希方式无法逐字节重建工具消息（服务端序列化与展示内容格式不同），因此父块计数采用真实请求里的来源 ID 与同次引用 ID 关联；不声称内容字节级完全一致。

### 逐题答案与引用复核

逐题判断按 Gold 核心答案与原始引用片段分开记录。完整问题、回答、Gold 和引用正文保留在 `var/agent-composite6-20261115/ecl6-de2c7183/analysis/ecl6-question-review.jsonl`；60 题的判定、状态、引用结论和实际父块计数索引在同目录 `analysis/ecl6-review-decisions.jsonl`。

| 序号 / QID 后缀 | 核心答案 | 引用 | 复核说明 |
|---|---|---|---|
| 00 `legacy-session3-v5-s1-t01` | PASS | PASS | 日期正确；`[1][2]` 的来源正文均含 2020-08-27，精确页码未提供。 |
| 01 `legacy-session3-v5-s1-t02` | 无答案 | 不适用 | 请求含上一轮回答；未出现检索工具结果，终态 `retrieval_status=null / UNKNOWN`。 |
| 02 `legacy-session3-v5-s1-t03` | PASS | PASS | 扩展名 3 位及引用示例可在 `[1][2][3]` 正文中核对。 |
| 03 `legacy-session3-v5-s1-t04` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 04 `legacy-session3-v5-s1-t05` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 05 `legacy-session3-v5-s1-t06` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 06 `legacy-session3-v5-s1-t07` | PASS | PASS | 625 万元与补充的 113.9 万吨均在所引政府工作报告正文。 |
| 07 `legacy-session3-v5-s1-t08` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 10 `legacy-session4-v6-s1-t01` | PASS | FAIL | UTC+8 答案符合 Gold，但附带的 7 条知识库引用没有一条支持该结论，且无行内引用。 |
| 11 `legacy-session4-v6-s1-t02` | 无答案 | 不适用 | 上一轮 UTC+8 结论及换算式在请求历史中；未调用检索工具，仍返回 UNKNOWN。 |
| 12 `legacy-session4-v6-s1-t03` | PASS | PASS | `[1]` 的 X-Men 来源片段明确写出 Hope 与绯红女巫制服镭射眼。 |
| 13 `legacy-session4-v6-s1-t04` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 14 `legacy-session4-v6-s1-t05` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 15 `legacy-session4-v6-s1-t06` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 16 `legacy-session4-v6-s1-t07` | PASS | PASS | `[1]` 正文含 1956 年设立图书室；来源页位置未知。 |
| 17 `legacy-session4-v6-s1-t08` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 18 `legacy-session4-v6-s1-t09` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 19 `legacy-session4-v6-s1-t10` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 20 `legacy-session5-v3-s1-t01` | PASS | PARTIAL | PDF 第 45 页含 533 / 5.1；正确来源在引用列表中，但答案没有行内 `[n]` 指针，且附带 17 条来源。 |
| 21 `legacy-session5-v3-s1-t02` | FAIL | FAIL | 回答 540 / 4.6，Gold 为 587 / 4.9。正确子块含答案，但在 99 个 Rerank 候选中模型分排第 42、综合分排第 54，未进 Top-20；所以页 45 未送入最终请求。 |
| 22 `legacy-session5-v3-s1-t03` | PASS | PASS | `[1]` 支持一个物种，额外的 Azores 与少于 1000 株成熟植株陈述也在该来源。 |
| 23 `legacy-session5-v3-s1-t04` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 24 `legacy-session5-v3-s1-t05` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 26 `legacy-session5-v3-s1-t07` | PASS | PASS | `[1]` 原文明确写出高出 9 vertical feet。 |
| 27 `legacy-session5-v3-s1-t08` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 28 `legacy-session5-v3-s1-t09` | 无答案 | 不适用 | 有历史消息；未调用检索工具，返回 UNKNOWN。 |
| 29 `legacy-session5-v3-s1-t10` | PASS | PARTIAL | PDF 第 45 页支持 1,254 / 3.4；正确来源在引用列表中但无行内指针，另加了一句与问题无关的图片说明。 |

**Clinton 数值题的损失阶段已进一步核实。** 用 ECS SQLite `mode=ro` 只读读取 `document_chunks`：正确子块 `bd6732bf-6fbf-4499-bac1-9e54bd31715c` 属于 PDF 第 45 页父块 `4c7d37ce-400e-4072-877c-963727f1cd02`，正文 328 字符，包含 `Clinton voters 48% / 587 / 4.9 percentage points`。该子块进入了该题的 99 个 Rerank 输入候选，但 `rerank_score=0.3159`、`retrieval_score=0.2225`、`composite_score=0.3563`，模型分名次 42、综合分名次 54，未被 Top-20 选中；父块因此没有进入 Agent 最终工具证据。纯模型分排名本来也是第 42，综合分把它再降 12 位，但两种排序都不会进入 Top-20，所以这次错误不能归因于综合分单独造成的成员出局。**首个可证实的损失点是 Rerank Top-20 选择；Vector 与 BM25 在融合前各自是否命中该子块，当前归档没有候选 ID 列表，不能再往前细分。** 这同时排除了该题的 PDF 解析缺行、索引缺失与六父块名额挤占。

27 个已返回答案中：10 个核心正确、1 个核心错误、16 个拒答/UNKNOWN。16 个拒答题均在请求体中带有历史消息，但对应 SSE 没有 `rag_result` 或知识库工具结果；这不是“真实检索后 NO_RESULTS”，也不能归因于前端漏传历史。可见缺陷是 Agent 没有触发工具，也没有利用足够的已给历史回答可回答的追问。11 个实际生成的回答中，7 个有正确行内引用；2 个正确答案只在独立来源列表里带有支持证据、没有行内指针；1 个通用事实答案缺少支持来源；1 个错误数值答案既没有正确表格证据也没有引用。

### 与同一题集旧结果的比较

旧记录是普通 Chat + Hybrid、Rerank 关闭；本次是 Agent + Hybrid + Rerank 开启，模式与重排配置不同，不能把差值归因于综合重排或六父块单因素效果。

| 指标 | 历史普通 Chat（60 题） | 本次 Agent + Hybrid（已尝试/完成） |
|---|---:|---:|
| 执行数 | 60 题；59 COMPLETE、1 UNKNOWN | 45 尝试；27 COMPLETE、18 FAILED、15 NOT_RUN |
| 核心完整正确 | 38/59 = 64.4% FULL | 10/27 = 37.0%（完成回答分母）；10/45 = 22.2%（已尝试题分母） |
| 完成题内状态 | 21 FAIL、1 UNKNOWN | 10 PASS、1 错误、16 无答案 |
| 输入 Token | 59 个回答总计 964,816；均值 16,352.8；中位数 17,363；P95 20,373 | 27 个 COMPLETE 的所有 Agent Provider 调用合计均值 6,474、中位数 1,967、P95 17,137；11 个实际回答子集均值 13,210.6。45 个尝试含重试总计 495,726（均值 11,016/题） |
| 耗时 | n=60；均值 19.69s；中位数 17.02s；P95 45.04s；最大 61.09s | 27 个 COMPLETE；均值 11.52s；中位数 6.00s；P95 65.01s；最大 80.02s。18 个预算失败含重试均值 25.23s |
| 超限 / 截断 | 冻结复核账本没有专门的应用字符预算拒绝字段，历史超限数 UNKNOWN（不能当 0） | 18/45 题发生应用字符预算拒绝，均重试一次；模型长度截断 0 |

Token 口径不同：旧字段是 Chat 的 `prompt_tokens`，新字段是一次用户题目内所有已观察 Agent 节点请求的输入 Token。因此不能宣称新方案节省 Token。新组均值偏低的主要可观察原因是 16 道追问以短输入直接 UNKNOWN，另有 15 题未运行；这不代表有用回答成本下降。耗时也有同样的早停/拒答偏差。

P95 使用排序后最近秩 `ceil(0.95*n)` 计算。质量差异只作当前链路警报，不能做单因素因果结论。

### 本轮判定

- 综合重排真实执行并改变排序：**PASS（机制）**；答案质量收益：**NOT ESTABLISHED**。
- 六父块在扩展结果和实际 Provider 工具证据中均不超 6：**PASS（机制）**。
- 同一 60 题完整 Agent 回归：**PARTIAL**，45 尝试、15 未运行。
- 已完成题的核心答案质量：**FAIL / 需处理**，10/27 正确，16 个没有发起检索工具，1 个数字错误。
- 引用：有 2 个答案明显缺少证据支持，2 个只在来源列表提供支撑但无行内引用；其余明确引用片段支持核心答案。多条文本来源的精确页码仍不可用。
- 正式服务切换：**NOT DONE**。没有修改正式服务、数据库、Chroma 索引或 Gold；隔离端口已释放。
- 本轮后续代码修复与重跑：未执行。结果表明应先修复 Agent 对追问的工具触发/历史利用，并解决最终请求字符预算问题，再做完整对照；否则继续比较会把功能失败误当作模型质量或重排效果。

## 原因定位后补充范围（2026-11-15）

详见 `docs/agent-composite6-root-cause-20261115.md`。前文“六父块PASS”只适用于单轮扩展和已成功发出的30条含工具证据请求；多轮累计后被字符预算阻断的请求，可能累计12个不同父块，或重复发送同样4个父块两次。当前实现还没有按最终整个请求统一限制和去重，因此不能将前文机制PASS解释为整个Agent生命周期满足请求级六父块。此处只更正结论范围，原始回执保持不变。

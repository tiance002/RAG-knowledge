# M2 最小验收反馈修复与独立测试交接

日期：2026-11-04。工作区：`D:\RAG knowledge`，分支 `codex/pdf-parser-migration`，HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e`。

**2026-11-05 更新：已接收并只读复核独立测试回执，三项修复的定向工程验收 PASS，详见第 6 节。** 下文的 NOT RUN 是 11 月 4 日开发交接时状态，保留历史语义；开发代理本次也没有重新运行测试。全量套件尚未通过，云端回答质量和浏览器渲染仍未验证。

**2026-11-05 继续任务：第 7 节记录该次交接。** 已补 Agent 生成终态字段、资料检索引导和普通 RAG 的来源提示，共 5 个源码文件；交接时运行验收 **NOT RUN**，不能套用第 6 节旧版本的 PASS。测试继续由负责人安排的其他模型执行。

**2026-11-05 后续更新：第 8 节为当前结论及下一步交接。** 第 7 节的独立运行回执已审查：终态与定向回归通过，P1 业务答出 1/2，P2 核心/引用 2/2、整份回答的资料支持 1/2。随后仅修改 EMPTY 前端提示和失败 Rerank 的分数来源标记；这两处新增修改运行验收 **NOT RUN**。历史报告和原始回执保持原样。

**状态：代码修改完成，运行验收 NOT RUN。** 按负责人最新分工，本轮由开发代理修改代码、审查差异并提供测试方案，测试交由负责人安排的另一模型执行。没有运行 pytest、构建、解析、模型或 HTTP 服务；没有提交、推送、部署或切换正式服务。原有未提交修改保留。

## 1. 依据和结论边界

依据 `docs/m2-mini-acceptance-20261104.md` 及其 `var/acceptance-mini-20261104/` 原始记录。旧报告中的 6 题、13 次调用、核心 5 PASS / 1 UNKNOWN 是**独立测试方的旧版本结果**，不是本轮修改后的验证结果。旧结果不覆盖或重新计分。

源码确认了三处需要修改的行为：

1. `_parse_excel` 原来只输出 pandas 数值表，不输出 Excel 数字显示格式。
2. 检索服务已经记录单路降级，但聊天 `done` 没有向客户端透传。
3. `grade_documents` 原来使用 `get_chat_model` 的默认流式调用，未配置评分专用的短超时。

对第 3 点的限定：本机 OpenAI SDK 默认超时是 600 秒，不应描述为“完全没有超时”。现有日志不足以定位“同步读阻塞事件循环”的具体原因；其中 `stall-evidence/step5g.log` 还包含正常结束和 `RC=0`。本轮修复明确的调用配置缺口，不声称已证实或解决所有卡顿原因。

## 2. 修改内容

| 文件与入口 | 本轮变化 | 保持的行为 |
|---|---|---|
| `backend/app/services/parser.py` → `FileParser._parse_excel` | 利用已有 openpyxl workbook，一次扫描每张工作表的货币符号数字格式；按块记录真实坐标、列号、原始格式，正文和 metadata 都保留。相邻同格式单元格合并成范围。 | 数值、20 行切块和原有定位不变；不把 `$` 翻译成 USD，不自动套到其他单元格。 |
| `backend/app/services/chat.py` → `_chat_stream_normal` / `_chat_stream_langgraph` | 普通模式 `done` 透传 `degraded` / `fallback_used`；Agent 累计各轮降级事实并送到检索状态事件与 `done`。 | 有可用证据仍允许 `SUCCESS` 和生成；不增加评分门槛。 |
| `backend/app/tools/retrieval_tool.py` → `search_knowledge_base` | 将实际 retrieval metrics 中的两个标志加入工具 JSON。 | 不变更检索、重排、扩展、引用选取。 |
| `backend/app/services/agent_workflow.py` → `AgentState` / `process_tool_output` | 保存各工具轮已观测到的降级标志；后续健康轮不会擦掉先前的降级事实。 | 不改变检索成功/失败的判定。 |
| 同文件 → `grade_documents` | 评分专用 `streaming=False`、配置化请求超时及总等待期限。 | 评分 Prompt、0.8 重试阈值、最大检索重试次数、模型和输出上限不变。 |
| `backend/app/services/llm.py` → `get_chat_model` / `invoke_provider_model` | 增加可选 `timeout` / `timeout_seconds`；等待期限放在现有 provider 调用记录内部，超时沿用 UNKNOWN 终止路径。 | 不传新参数的调用保留原行为；SDK `max_retries=0`。 |
| `backend/app/core/config.py`、`backend/.env.example` | 新增 `AGENT_GRADING_TIMEOUT_SECONDS=60`，只作用于相关性评分。 | 普通回答和其他 Agent 模型阶段不改默认超时。 |
| `frontend/src/types/chat.ts`、`frontend/src/pages/Chat/index.tsx` | 可选降级字段；仅实际收到 `degraded=true` 时展示“本次检索部分通道不可用，结果可能不完整。” | 旧消息没有字段时不显示提示；不改回答文本及引用。 |

`backend/app/api/chat.py` 和 `frontend/src/services/chat.ts` 已经完整转发 SSE 数据，无需另加适配层，本轮未改。

### Excel 证据范围

- 正文会包含类似 `C7:G7 (列 3-7): "$"#,##0` 的原始格式记录；这是**设计示例，尚非本轮执行输出**。
- 格式仅记录其实际所在单元格；`C9` 数值依然是 `108149.892`，不会改写为带美元符号的值。模型可结合表结构理解，但解析器不制造币种断言。
- 每块收集本块范围和原有 pandas 表头行的格式，不将其他表格/其他位置的币种无条件套过来。不会自动把早期汇总行扩展到后续所有块。
- 已处理 read-only openpyxl 的空单元格：不从 `EmptyCell` 读取不存在的行号。格式只扫描一次，避免每个块重复读取工作表 XML。
- `currency_format_status=AVAILABLE` 表示扫描成功，不保证该块一定有货币格式。未支持的引擎或格式提取异常为 `UNKNOWN`，保留原数值解析。
- 第一版识别 `$ / € / £ / ¥ / ₹` 及含这些符号的格式；排除 `[$-409]` 这类纯区域标记。其他币种文字格式、条件格式、多表结构推断不在本轮完成范围。
- 旧索引不会凭空获得新增证据；独立复测需要从原件重新解析测试用片段。**本轮未重新入库，也未修改历史索引。**

### 超时与调用记录

评分同时配置 SDK 请求时限和 `asyncio.wait_for` 等待期限。期限到达后，现有 `provider_attempt` 会保留本地请求 ID 和 UNKNOWN，沿现有 SSE error 路径结束，不能自动再发一次评分/回答请求。

这不是对任意阻塞 Python 代码的强制终止机制；若底层存在同步阻塞或不响应取消，仍需独立验证实际网络入口。没有为解决等待而关闭全部模型思考、改变最终回答 Prompt 或增加严格答案判定。

## 3. 本轮执行与未执行

| 动作 | 实际结果 |
|---|---|
| 读取 AGENTS、报告、相关源码、安装包元数据、Git 状态 | 已执行，只读 |
| 核对当前分支和 HEAD | 已执行：上述分支及 SHA |
| 审查相对本轮开始时源码的差异 | 已执行；`git diff --no-index` 有差异时返回 1 是正常语义，不是测试失败 |
| pytest / 前端构建 / Excel 解析 / HTTP 或 SSE 验证 | **NOT RUN** |
| 云端 Qwen / GLM / Langfuse Cloud 回读 | **NOT RUN**；本轮模型调用 0 |
| 旧 5 项验收的所有结论 | 沿用历史记录，不宣布本轮通过 |

本机读取到的依赖版本：`openpyxl 3.1.5`、`langchain-openai 1.7.3`、`openai 3.40.0`、`httpx 0.28.1`、`httpx2 2.16.0`。没有安装或升级依赖。

官方配置参考：[LangChain 模型连接配置](https://docs.langchain.com/oss/python/langchain/models)、[LangGraph 关闭指定模型的流式输出](https://docs.langchain.com/oss/python/langgraph/streaming#disable-streaming-for-specific-chat-models)。实际参数同时对照了本机安装源码。

## 4. 给独立测试模型的任务

以当前实际工作树为准，先检查下面的改前快照与当前代码。不要因为文字形式、提示语、标点或同义表达不同判失败。测试焦点是实际模型请求拿到的事实、SSE 状态、调用次数和用户能否正常拿到有依据的回答。

### 先保护旧证据

旧 `step1_excel_ingest.py`、`step234_behavior.py`、`step5_real_regression.py` 都有固定输出目录和删除旧目录的逻辑。**不要直接在原目录重新执行这些脚本。** 在新的验收目录复用其逻辑，输出写到唯一目录，数据库、audit 和索引也必须隔离；只读复用原件，不复制整份语料或大索引。

旧 step5 覆盖了整个 `LLMService.get_chat_model`，旧签名不接收 `timeout`。复测时保留真实工厂，让 `streaming`、`timeout`、`max_retries` 来自产品代码；云端 Qwen 的 `enable_thinking=false` 如需固定，应只增加该模型参数，不替换掉产品的工厂/调用边界。不得让测试替身吞掉超时配置，再声称完成验收。

### 最小测试范围

| 编号 | 输入/执行入口 | 应观察的实际结果 |
|---|---|---|
| F1 | `excel-2.xlsx` → `FileParser.parse` → 测试入库 → 真实检索/上下文 → 捕获客户端请求，先拦截网络 | T07 数值、`In thousands`、工作表/原文件引用和 `C7:G7` 的 `$` 格式证据共同到达。`C9` 不被改值，百分比列不被补美元。请求有 `$` 还不够，要确认它来自该范围。 |
| F2 | 一个小表含不同币种列、百分比列、空 A 列，以及第二张表 | 格式坐标能区分来源，不跨表或跨列赋币种；无货币格式的文件仍正常解析。无需逐字符串比较整张 Markdown。 |
| D1 | 复用“向量路明确失败，BM25 有有效证据”情形，普通/Agent 各一条，经过实际 HTTP/SSE 入口 | 有证据可正常回答；`done` 是 `SUCCESS` 并带 `degraded=true`、`fallback_used=true`；引用保持原来源。 |
| D2 | 健康检索；Agent 第一轮降级、后轮健康；旧消息未含字段 | 健康请求无误报；多轮 done 保留“曾降级”的事实；旧消息无误报。前端提示不替换答案、不重复添加。 |
| G1 | 真实模型工厂和 `grade_documents`，仅在网络层提供完整评分响应 | 捕获请求为非流式，评分正确解析；只调用一次，阈值和重试判断不变。 |
| G2 | 本地隔离 HTTP 端点收到请求后保持连接但不回答；将评分时限临时设为短值 | 真实网络请求到达后在合理宽限内结束；provider 本地 ID 对应 UNKNOWN；SSE error 可见；没有重发、没有新增 fallback 付费调用。端点延迟/系统调度可留容差，不卡毫秒精度。 |
| R1 | 复用无证据、明确检索错误、部分证据、截断及用户偏好的既有代表用例 | 原行为未退化，不因新增字段误拒答案。无须重跑全套评测。 |

F1 的原件路径：`var/preferences-acceptance/m2-source/excel-2.xlsx`。旧证据可对照 `var/acceptance-mini-20261104/step1/report.json` 和 `step1/final_provider_request.json`。不能只读旧冻结 chunk 来验收新解析器。

已有定向测试可供独立测试方选择复用，以下命令**本轮没有执行**；若测试替身签名过时，应核对真实入口后调整替身，不改生产逻辑迎合测试：

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& '.\.venv\Scripts\python.exe' -m pytest -q `
  backend/tests/test_provider_unknown.py `
  backend/tests/test_agent_quality_outcomes.py `
  backend/tests/test_retrieval_outcomes.py `
  backend/tests/test_agent_request_parameters.py
$LASTEXITCODE
```

这些旧单元测试不能代替 F1/D1/G2 的真实入口验证。新增入口验证脚本由独立测试方实现并执行，本轮未声称它们已经存在或通过。

必要时再用云端 `Qwen/Qwen3.5-4B` 做 Excel 普通/Agent 两条成功路径复核。不要启动 Ollama，不需要为验证这三处修改重跑全部 A/B。旧 UNKNOWN 不重发；新请求留新运行 ID，实际返回、截断和 UNKNOWN 分开记录。不能根据“程序退出 0”推断答案正确。

回传一个报告即可，包含：实际代码哈希、命令、退出码、逐项结果、最终请求证据、SSE 和请求次数；使用 PASS/FAIL/PARTIAL/UNKNOWN/NOT_RUN/BLOCKED 区分结果。存在业务失败则给出可复现输入及影响，不为格式差异增加硬校验。

## 5. 遗留与恢复

- 季度报告 Agent 未调用工具的问题继续保留，未添加新的强制工具调用门禁，也未改 Prompt。
- Hotpot 普通回答额外补充 Coulomb 的来源问题继续保留，未新增逐句拒答/重写规则。
- 新格式摘要会改变对应 Excel 的索引文本，新增字符与检索影响待独立测试；不宣称回答正确率已经改善。
- 评分短超时只能让失败有界、状态明确；不能保证供应商一定在期限内完成。
- `degraded` 描述当前请求中曾发生的部分通道降级；不是答案错误判定，也不表示最终一次检索仍然降级。

本轮开始时的源码副本、哈希和 Git 状态位于 `var/m2-mini-followup-20261104/before/`、`before-hashes.json`、`before-status.txt`；它们只用于区分本轮修改与此前未提交修改，不含凭据或评测正文。结束时的 `after-hashes.json` 和 `changes.patch` 记录本轮差异。若需回退，按该差异逐项处理，不能 `git checkout` 整个文件覆盖此前/之后的本机成果。

复用了既有源码、依赖、验收记录；新增内容只有本报告、内部任务记录及上述小型源码差异档案，没有创建新数据库、Chroma 索引、容器、模型缓存或语料副本。没有删除任何历史材料。

源码差异档案目录合计 294,959 字节（包括改前副本、状态、前后哈希和补丁），其中 `changes.patch` 单文件为 19,439 字节；两个数字口径不同。文件哈希/差异归档命令退出码 0（仅文件操作，不是测试通过）。D 盘可用空间读数从 143,233,417,216 到 143,233,048,576 字节；差值包含同期文件系统活动，不能全部视作业务开销。该小型档案保留用于独立复核及避免误回滚前序修改。

## 6. 2026-11-05 独立测试回执接收与只读复核

独立报告：`docs/m2-mini-followup-test-20261105.md`。原始证据：`var/acceptance-followup-20261105/`。本节只补充验收结论，不修改独立报告、原始回执或测试标准。

### 版本与证据

开发代理重新计算交接清单中的 10 个文件哈希，全部与 `after-hashes.json` 匹配；其中 9 个是本轮修改文件，另 1 个是未修改的 `frontend/src/services/chat.ts`。当前分支和 HEAD 未变。测试方调整的两个替身签名均已看到 `**kwargs`；本次没有再改产品代码或测试。

逐项读取全部 7 份 JSON：共 **110/110 个检查项为 true**，其中 D2 有 5 项静态检查。该数字不是 110 道真实模型问答，也不是浏览器实测数量。

| 项目 | 回执及只读复核结果 | 验收范围 |
|---|---|---|
| F1 | PASS，24/24；实际最终 system 请求 29,584 字符，8 个 `$` 全在格式记录行中，含 T07 `C7:G7`；数值保持 `108149.892` | 实际解析、检索、提示组装，网络返回为替身 |
| F2 | PASS，20/20 | 多币种位置、空列、区域标记和无货币格式文件 |
| D1 | PASS，15/15；普通/Agent 降级 done 均保留 SUCCESS 和两个 true 标志 | 真实回环 HTTP/SSE，模型返回来自本地端点 |
| D2 | PASS，10/10（运行时 5、静态 5） | 多轮状态与前端源码接线；浏览器 NOT RUN |
| G1 | PASS，11/11；`stream=false`、`timeout=60`，评分阈值行为未变 | 真实产品工厂/SDK，本地端点 |
| G2 | PASS，14/14；评分 3.16 秒终止；完整 SSE 7.77 秒收到 UNKNOWN error、无 done、评分到达一次 | 实际网络超时及调用次数，不代表云端性能分位值 |
| R1 | PASS，16/16 | 已有代表行为，固定模型返回 |

各组退出码 0 依据独立报告；本次没有重新执行这些命令。源码可见部分验收脚本只打印失败列表而不强制非零退出，因此此次判断依据是逐项 JSON 和实际请求/事件，不只看退出码。

`pytest-plan.log` 原始末行确认 **62 passed**；追加偏好用例后的 **109 passed** 和替身修复后的 **38 passed** 来自独立报告，所列目录未见这两次的单独日志，不合计或声称已独立核对其全部执行记录。62 已包含在 109 的范围中。

### 全量测试与限制

原始 `pytest-full-after.log` 末行确认：**1155 passed、54 failed、37 errors、6 skipped、33 deselected**。独立报告记录退出码 1；全量套件判为 **FAIL（未通过）**。定向修复 PASS 与全量未通过可以同时成立。

不能把 `Source code snapshot changed` 的 133 次文本出现次数当成 133 个独立失败用例。源码身份检查失败与业务失败应分别归因；没有改前对照的 `test_e11_*`、观测和 metrics 等失败继续记 **UNKNOWN**。文件本轮未修改也不能证明不存在间接回归。不为使全量变绿而覆盖历史哈希、旧预算或修改冻结材料。

云端 Qwen 回答质量、Langfuse Cloud 回读、浏览器渲染、前端构建均为 **NOT RUN**。本次没有发起模型调用，没有复跑测试，没有启动服务、提交或部署。

### 两项表述澄清及遗留

1. D1 原始引用是每条请求 **4 条 T07 + 1 条 T10**，不是 5 条全为 T07。报告正文“5 条、含 T07”成立；各条仍指向真实来源，这一口径澄清不改变 D1 结果。
2. 294,959 字节是整个源码差异档案；19,439 字节是单个补丁，已在第 5 节明确区分。内容不存在因此缺失的问题。
3. Agent 的 `generation_status` 在测试摘要中为 null，仍需保留为终态字段接线待办；此次评分超时依靠明确 error 结束，不能因此扩称 Agent 所有终态字段均验收通过。
4. 季度报告工具选择、Hotpot 无来源补充仍是原有待办。此次没有新业务失败证据，不继续添加答案校验规则或扩张修复范围。

**本阶段决定：三项修复的定向工程验收收尾；全量故障归因及真实回答质量独立保留，等待负责人决定后续范围。** 资源只读复用，无新数据/索引副本，无删除和远端操作。

## 7. 2026-11-05 继续任务：剩余接线及统一测试交接

### 7.1 本次范围、版本与分工

负责人要求继续后，在同一工作区、同一分支和同一 HEAD 上实施；保留全部此前未提交内容。主代理负责 Agent 终态和 SSE 接线，6.1 Terra 子代理负责生成提示及工具描述，6.1 Sol 子代理只读分类全量日志中的指定失败。最后由主代理审查合并后的差异。

本次没有执行测试、构建、产品模块导入、模型调用、服务启动、Cloud 回读或远端操作。命令仅用于读取、修改和归档文件。两个由独立测试方修改的测试替身继续保留，本次未改任何测试文件。

### 7.2 修改及效果边界

| 文件 / 函数 | 实际修改 | 尚待验证 |
|---|---|---|
| `backend/app/services/agent_workflow.py` → `AgentState`、`_answer_generation_metadata`、`_finalize_knowledge_answer`、`agent_node`、`generate_chat` | 保存最终回答模型的 `generation_status` 和原始 `generation_finish_reason`；在证据兜底修改回答正文前取值。无生成的预算/检索终止分支为 `NOT_RUN`。`qa.answer` 同步记录状态。 | 实际 StateGraph → HTTP/SSE 的完整传递及引用不变。 |
| 同文件 → `agent_node` | 在原工作流程中补一句：依据所选资料回答时先检索，工具返回前不要声称资料缺失。 | 是否改善季度报告未调工具的问题；提示不等于强制执行或验收通过。 |
| `backend/app/services/chat.py` → `_chat_stream_langgraph`、`chat_stream` | 只从 `agent` / `generate_chat` 节点读取最终生成状态，送入 `done`；明确无调用的范围预检和纯记忆命令标 `NOT_RUN`。 | 评分输出不会覆盖答案终态；已有异常仍通过 error 结束。 |
| `backend/app/services/grounded_generation.py` → `generate_grounded_candidate` | 允许与问题有关的综合解释和有依据的推断；提示新增事实、单位、归因需要资料依据，关键结论引用对应来源编号。 | Hotpot 补充内容是否仍混入无来源事实，正常综合回答是否保持。 |
| `backend/app/tools/retrieval_tool.py` → `search_knowledge_base` 工具描述 | 明确所选资料的检索用途、Agent 中服务端提供范围/配置，以及查询前不能断言无资料；文档中的阈值默认值修正为函数现有的 `0.1`。 | 实际工具选择。函数签名、运行阈值和检索实现未变。 |
| `frontend/src/types/chat.ts` → `DoneData` | 增加上述两个可选终态字段的类型。 | 前端构建与浏览器状态展示。 |

生成状态与答案质量分开：`finish_reason=length` 为 `TRUNCATED`；空字符串为 `EMPTY`；非空字符串且原因是 `stop` 或缺省为 `COMPLETED`，与现有普通 grounded 路径一致；未识别状态为 `UNKNOWN`；没有生成调用为 `NOT_RUN`。这是结果描述，不是新的答案校验器。

模型完整返回但未取得资料时，可以同时出现 `generation_status=COMPLETED`、`answer_mode=UNKNOWN`；不能据前者宣布答案正确。评分、工具调用和意图分析也不算最终回答完成。Provider 超时仍沿已有 UNKNOWN/error 终止，不转换成正常 done、不自动重发。

现有前端 `frontend/src/services/chat.ts` 已将 `TRUNCATED` / `UNKNOWN` 终态交给错误提示回调；本次 Agent 填入字段后也会走这条路径。独立测试需确认已送达的安全摘录仍可见、提示不重复、不会自动重发。这个前端文件本次没有修改。

没有增加逐句、字符串、数值或引用的硬拒绝规则，没有强制 `tool_choice`，没有新增模型审查调用；保留现有无证据保护、检索/重排参数、评分阈值、记忆优先级和路由配置。**Prompt 修改是否改善回答，当前为 UNKNOWN，运行验收为 NOT RUN。**

### 7.3 历史证据：只读复用，不算新结果

- 季度报告：`var/acceptance-mini-20261104/step5/calls/004-request.json` 及对应 `004-response.json`。旧请求已经存在工具及检索工作流描述，返回却没有工具调用而声称缺少资料。此次只强化“先查再判断”的提示；没有把旧请求伪装成新版本验证。
- Hotpot：`var/acceptance-mini-20261104/step5/hotpotqa_5a729fe55542991f9a20c53b--chat.json`，原始请求 `step5/calls/002-request.json`。核心人物答案之外加入了 Coulomb 归因，实际参考资料没有支持该补充。这里只判断它缺少本次请求中的依据，不将其外部历史真伪混入本轮判定。
- Agent 终态缺字段：独立回执 `var/acceptance-followup-20261105/d1/` 及 `docs/m2-mini-followup-test-20261105.md`。旧 done 不含可用 `generation_status`，不是证明模型没有完成。
- 第 6 节的 110 项定向检查和全量日志继续作为旧版本记录保留；本次没有追加新通过数量。

### 7.4 全量失败的定向只读分类

仅核对下列 7 个失败节点，未重跑或全面清理 54 failed / 37 errors。日志路径统一为 `var/acceptance-followup-20261105/pytest-full-after.log`。下表解释该次失败机制，**不证明它们何时首次出现**。

| 失败节点 / 原始日志位置 | 源码与日志对应关系 | 处理意见 |
|---|---|---|
| `test_e11_candidate_transitions.py::test_hybrid_to_rerank_transitions_and_observability_equivalence`；1829–1837 | 测试第 71 行用两参数 lambda 替换 `_enrich_results`；当前 `retrieval.py` 第 539 行传入第三个 `kb_ids`，实际函数接受该范围参数。日志为 TypeError 后结果空。 | 过期替身；独立测试方适配现有签名。不删除生产 KB 范围校验。 |
| `test_observation_stages.py::test_vector_observation_preserves_results`；3852–3859 | 同类两参数替身，当前 vector 第 336 行调用三参数。 | 同上；不是证明向量业务返回丢失。 |
| `test_e11_context_delivery.py::test_expanded_neighbor_does_not_reach_final_model_message`；1889–1894 | 假 Session 只提供 seed，不能提供 `rerank.expand_context` 第 505–510 行要求的 `FileDocument` 归属。扩展返回空后测试索引越界。 | 补可信文件/知识库关系的测试夹具，保留来源校验；原送达断言尚未执行到。 |
| `test_e12_native_observability.py::test_failed_rerank_batch_marks_observed_score_origin_without_changing_results`；2071–2074 | 测试期待失败 Rerank 的分数为 `.6`；当前 `rerank.py` 第 419–424 行明确保留 `rerank_score=None` / UNKNOWN，原检索分数另在 `score`。 | 旧断言与现有真实分数语义不一致，不能为了通过恢复伪 Rerank 分数。 |
| `test_e12_native_observability.py::test_actual_stategraph_tool_retrieval_descriptor_parent_is_sdk_tool`；2093–2125 | `run_e12_acceptance.py` 第 87–88 行将 Windows whoami 输出按 UTF-8 解码失败，随后对 None 调用 splitlines；图执行前就退出。 | 验收脚本的 Windows 编码兼容问题；测试方修 SID 读取后再跑该节点，保留 ACL 检查。不能据此断言 SDK 父子链错误。 |
| `test_grounded_candidate_snapshot.py::test_candidate_snapshot_is_opt_in_and_events_do_not_contain_raw_text`；2164–2169 | 当前 `trace_schema._private_root_fd` 明确拒绝未验证 ACL 的 Windows 私有快照，测试却假定可以得到 snapshot_id。 | 区分 POSIX 成功和 Windows 安全拒绝；不关闭快照保护、不伪造内容。 |
| `test_metrics.py::test_context_reaches_handler_through_starlette_base_http_middleware`；3824–3833 | 测试用普通 Starlette Route，没有生产 FastAPI 路由提供的 `scope['route']`，`metrics._route` 因此返回 unmatched。 | 换成代表真实 FastAPI 的夹具再验证；200 请求及该断言不能证明上下文传播失败。监控不重新启用。 |

另有一项需独立观察的元数据疑点：`rerank.fallback` 发出 `fallback_score_origin='none'`，`observability.py` 的 `event` / `observe_candidates` 仍把非空字符串视为存在原始分数回退，并标 `score_kind='original_retrieval'`。这需要查看实际导出的失败候选描述，不能把它扩大为排名或答案错误。本次没有改观测子系统；将它加入下述定向核验，保留为未关闭项。

冻结源码哈希失配不能通过批量刷新旧基线解决。其余 WinError、Redis/WSL 等环境失败及未逐一检查的用例继续保留原 FAIL/ERROR 记录，归因为 UNKNOWN；不声称全部是既有问题。

### 7.5 交给独立测试模型的最小方案（本次均 NOT RUN）

先读取本节、`var/m2-remaining-20261105/before-hashes.json`、`after-hashes.json` 和 `changes.patch`；确认待测实际工作树是否又有变化。保留此前两轮证据目录，新输出使用独立目录。复用原件和冻结样例，不重建整库或复制全部索引。

| 编号 | 必要执行路径 | 判据与宽松边界 |
|---|---|---|
| S1 终态 | 真实 Agent 图、产品模型工厂、HTTP/SSE，仅替换 Provider 网络返回；分别完整文本、缺 finish_reason 的完整文本、length、空文本、无生成的范围/预算阻断 | done 的终态与实际返回一致，截断兜底仍是安全摘录，不能因摘录非空把原生成改成 COMPLETED；缺 finish_reason 不误拒正常回答。评分/tool_calls 不能覆盖最终状态。 |
| S2 异常/前端 | 复用 UNKNOWN 超时单次请求；前端接收 Agent 截断和正常 done、历史无字段消息 | 异常只有 error，无伪 done/重发；已收到的摘录保留并提示；普通和 Agent 正常完成可见。实际浏览器与构建分别记录，不用源码存在冒充渲染通过。 |
| P1 季度报告 | 复用原季度题、资料、检索参数，通过当前真实 Agent HTTP/SSE；先本地返回验证接线，再按负责人授权做少量云端 Qwen 请求 | 最终请求能看到新工具说明及检索引导；真正检索后按证据回答。没有调工具则如实记录本题未改善，不靠强制工具门禁或指定结果做假通过。 |
| P2 Hotpot | 原人物题，普通模式，固定证据、模型参数和题目；捕获实际最终请求与回答 | 核心答案、行内引用及补充内容逐项核对。允许同义表达、合理解释及说明依据的推断；不允许把无原文支持的额外发明归因当成资料结论。 |
| R2 最小回归 | 沿用部分证据、首轮空后续成功、无证据、Excel 格式/单位、显式偏好及第三轮检索代表路径 | 正常可用答案、Scope、实际检索参数、来源编号、单位和重试预算不变；不要以字数或固定句式判错。无需重跑全量。 |
| T1 失败分类复核 | 只选第 7.4 节必要失败节点；修测试签名/文件归属/Windows SID 读取，保留现有安全契约 | 通过真实对应入口重现/排除各原因；失败 Rerank 实际导出中 score 与 rerank_score、score_kind、UNKNOWN 不混淆。不得把未执行节点记 PASS。 |

现有可复用的窄范围命令如下，**本次未执行、无新退出码**。它们不能代替 S1/S2 和真实云端的行为验证。测试方根据实际影响挑选必要节点，避免无关 live 测试与重复全量回归：

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& '.\.venv\Scripts\python.exe' -m pytest -q `
  backend/tests/test_agent_grounded_answer.py `
  backend/tests/test_agent_engineering_repairs.py `
  backend/tests/test_provider_unknown.py `
  backend/tests/test_memory_chat.py::test_agent_generation_and_auxiliary_inputs_unchanged `
  backend/tests/test_memory_chat.py::test_grounded_actual_provider_prompt_and_router_separation `
  backend/tests/test_memory_chat.py::test_standalone_and_combined_commands_use_actual_stream
$LASTEXITCODE
```

云端效果验证只用已获准的 `Qwen/Qwen3.5-4B`，不启动 Ollama；固定已验证的 `enable_thinking=false` 及其他条件。季度报告和 Hotpot 每题可做 2 次新请求作为小样本，记录新 run_id；不得重发旧 UNKNOWN。保留真实工厂和调用记录，不在返回后改答案。不得将此小样本说成稳定正确率提升；若重复结果不一致，按逐次结果报告。开发代理本次实际调用为 0。

回传**一份统一报告**：当前源码哈希、命令及退出码、实际请求/响应证据、状态/引用对照、失败原因和未执行项。不要生成多个碎片报告或覆盖旧证据。未跑 Cloud 回读时明确写 NOT RUN。

### 7.6 归档、恢复与下一步

本次 5 个源码文件的改前副本保存在 `var/m2-remaining-20261105/before/`，另有 `before-status.txt`、`before-hashes.json`。结束时 `after-hashes.json` 和 `changes.patch` 只表示本次增量，不能把这些文件的整个 Git diff 当成本轮修改。需要恢复时先对照后续工作区变化，按补丁逆向逐项处理，不覆盖他人改动。

本次实测：**没有产品运行测试**；完成的是源码/日志只读审查与文件差异归档。沿用历史：第 6 节的工程验收和 `step5` 旧模型结果。尚未完成：本节新代码的运行验证、Prompt 实际效果、浏览器/构建及 Cloud 回读。全量套件仍为历史 FAIL，不能宣称已全绿。

只新建 5 个小型源码副本及其差异和哈希，复用既有环境、历史证据和原件。没有新建/删除语料、数据库、索引、容器或服务，没有读取凭据、提交、推送或切换生产。资源统计和归档命令结果见同目录 `archive-summary.json`；其退出码是文件操作结果，不是产品测试通过。

实际执行的归档命令为 `.venv/Scripts/python.exe -X utf8 -` 接收仅含标准库 `pathlib/hashlib/difflib/json/shutil` 的文件归档代码，**退出码 0**：5/5 改前源码哈希与开始时清单一致，5 个文件存在本次差异，补丁 10,700 字节，档案 153,162 字节（不含 `archive-summary.json` 自身）。D 盘可用从 143,144,448,000 到 143,144,415,232 字节，受同期文件系统活动影响，不据此估计业务成本。保存这些小型恢复材料，无临时服务或语料副本待清理。

**当前结论：必要接线修改已交付，运行验收 NOT RUN；等待独立测试回执后按具体产品问题修正。** 不因全量旧测试红灯新增严格答案校验，也不凭 Prompt 文本宣布季度/Hotpot 质量问题已关闭。

## 8. 独立回执复核与下一步：EMPTY 提示、分数来源和检索诊断

### 8.1 本次只读复核的范围与结论

输入是 `docs/m2-remaining-20261105-test.md`、`var/acceptance-remaining-20261105/` 原始材料及实际工作树。HEAD 仍为 `57f9972b8477b0c4aaf7fac329b911420fafc95e`，分支未变。上轮 5 个产品文件的当前哈希全部匹配 `var/m2-remaining-20261105/after-hashes.json`。本轮使用 6.1 Sol / Terra 子代理分别只读审查云端证据和测试侧修正，由主代理合并判断；这是开发代理证据复核，不冒充人工标注或新一次运行。

| 项目 | 核对到的原始结果 | 审查结论 |
|---|---|---|
| S1 | `s1/report.json` 18/18 为 true，20 次本地 Provider 请求；空范围预检 0 次 | PASS：状态在兜底前获取；低评分不把模型完成改成未完成 |
| S2 服务端 | `s2/error-path.json` 6/6；5.15 秒收尾、一次 UNKNOWN error、无 done、评分一次 | PASS：评分超时边界；不是最终生成超时验收 |
| S2 前端 | `s2/frontend-stream.json` 17/17 | PASS：产品 transport 的 Node 运行时。旧测试也确认 EMPTY 没提示，这是观测到的缺口，不是该行为已满足新要求 |
| P1 | 两次都有真实 tool_calls；第一次 10 条引用、答出；第二次三次 NO_RESULTS | 接线 PASS；业务效果 PARTIAL，答出 1/2。不能说稳定恢复 |
| P2 | 两次人物及 [4]/[8] 对应正确；第一次另加 Benjamin Roy / 1784 / Kleist；第二次无该类补充 | 核心及关键引用 PASS 2/2；整份回答资料支持 PARTIAL：1 PASS / 1 FAIL。现有 8/8 检查没有覆盖所有补充事实 |
| R2 | `r2-behavior/checks.json` 16/16、`r2-excel/report.json` 24/24、`r2/pytest-plan.log` 77 passed | PASS：相应范围，不与其他 pytest 范围简单相加 |
| T1 | `t1-selected.log` 7 failed → `t1-selected-after.log` 7 passed；`t1-files-after.log` 35 passed | PASS：7 节点及对应 6 文件。全量非 live 没有重跑，不能据此更新为全量通过 |

上述独立执行的退出码来自回执；pytest 日志可核对收集/通过数量，未另见所有命令的 shell 退出码文件。开发代理此次没有重跑命令。P1/P2 的原进程 **退出码 1 保留**；当前 report.json 的 14/14 是事后拆分接线与效果后的检查清单，不代表原命令变为 0。

TypeScript 两份日志均为相同的 45 条错误，未出现 `types/chat.ts` / `services/chat.ts` 诊断；支持“这两份日志未见新增诊断”，不等于构建通过。`vite build`、浏览器渲染、Langfuse Cloud 回读仍是 NOT RUN。E12 的 PASS 是本地合成验证，原结果 `cloud_export=NOT_RUN`，不算 Cloud 验收。

### 8.2 对独立报告的限定与纠正

1. **低分场景送达的是摘录。** `s1/grading_low.json` 的 `summary.answer` 以“现有证据不足以完整回答，以下是相关原文”开始；报告 S1 表格中的“模型原文”不准确。`COMPLETED + EXTRACTIVE_FALLBACK` 结论成立。
2. **P2 整体支持性没有全通过。** 两份最终请求 `010-request.json` / `011-request.json` 相同，核心来源 [4]/[8] 足够，但 Benjamin Roy、1784、Kleist 不在请求材料中。将无引用的额外历史事实放在 Note 仍不能算有依据；“一个来源未提及另一个发明”也不足以说明资料互相矛盾。允许合理综合推断，不据同义表达判错。
3. **概率变化 UNKNOWN。** 本轮 Coulomb 未复现，但 1/2 次换成了其他资料外事实；两个样本不足以支持“提示降低了幻觉概率”。本轮不再改 Prompt 或加答案拒绝器。
4. **跨语言解释是待验证假设。** 已证明 fulltext、禁用 embedding、一次英文查询成功和三次中文查询无结果同时存在；没有完成同输入的直接召回对照，不声称已证明唯一原因，也没有验证生产 Hybrid 能修复。
5. **11 次调用是最终成功运行小计。** 保存的 11 个成功 response 与 proxy-records 一致，共 17,656 输入 / 1,212 输出 Token，包含意图、评分和最终生成。`001-response.txt` 至 `004-response.txt` 还残留早期 Not Found，而对应编号的 JSON 已属于后续成功运行。测试网关每次从 seq=0 开始且复用文件名，早期失败身份不完整，不能把残留数当实际失败调用数。早期次数、usage 和费用 UNKNOWN，最终小计不能当整个测试会话账单。保留原文件，不为了补账重发请求。
6. **最终生成超时缺口应定位到正确函数。** `agent_workflow.py` 的 533 行是 intent；知识 Agent 调用在 624/659 行，闲聊在 1080 行（本轮源码位置），没有传 `timeout_seconds`。SDK 仍有自己的超时；本轮没有最终生成挂起实测，不能写成无限等待已复现。保留产品级等待期限的后续设计任务，不随意给全部回答套评分的 60 秒上限。

T1 的 6 个测试文件和 1 个脚本修改已核对：补签名/文件归属、平台条件、FastAPI 路由和隔离 Redis 没有关闭产品 Scope、ACL 或快照保护。不过，正向归属夹具本身不证明所有跨库拒绝路径，35 passed 也不是全套安全验收。测试修正完整保留，本轮未改测试。

### 8.3 已继续实施的两处最小修改

| 文件 / 函数 | 原始证据与改动 | 保持的行为 | 状态 |
|---|---|---|---|
| `frontend/src/services/chat.ts` → `StreamFailure` / `createChatStream` | 旧前端实测收到 EMPTY 后仍 onDone、无提示。将 EMPTY 纳入现有终止提示路径，文案“模型未返回回答内容，本次未生成完整答案。” | 已收到正文及引用不删除；复用现有页面 onError；无额外 fetch、无自动重试、正常/旧 done 逻辑不改 | 实现完成，NOT RUN |
| `backend/app/services/rerank.py` → `batch_rerank` 的明确失败分支 | 失败候选实际导出 `score_kind=original_retrieval`、`score_value=.6`，但来源写 none；将来源改为 `original_score`，对应被导出的原始检索分 | `rerank_score=None`、`rerank_status=UNKNOWN` 不改；不将 .6 伪装成 Rerank 分，不改变候选排序、过滤、阈值、正文或引用 | 实现完成，NOT RUN |

第二处仅修正遥测字段语义。“原分来源”不表示 Rerank 成功或实际使用了一个替代 Rerank 分。最新 T1 测试将 origin=none 固化进断言；下一次独立测试应据此明确字段含义，继续验证未知 Rerank 分和结果等价性，不只改断言使其变绿。

### 8.4 给独立测试方的下一步顺序

**先做两个零模型验证，输出一份统一回执。** 本次开发代理不执行，以下状态均 NOT RUN。

1. 复用 `s2_frontend_stream.mjs` 的真实 transport 打包方式，改成新的输出目录，不能覆盖旧 17 项回执。EMPTY 有摘录：正文/引用保留、提示一次、onDone 不再冒充完整生成、fetch 一次。EMPTY 无正文也能结束且不捏造摘录；正常、NOT_RUN、旧 done、TRUNCATED、UNKNOWN 保持。可自行测试浏览器呈现，未做就注明 NOT RUN。不要以提示文案逐字一致作为验收条件。
2. 复用 `test_failed_rerank_batch_marks_observed_score_origin_without_changing_results`，保留实际服务替身和 native 导出。失败行原分 .6 仍在 `score` / `score_value`，`score_kind=original_retrieval`、来源为原检索分；`rerank_score=None`、UNKNOWN 不变；成功行仍为真实 .95，观测开/关结果相同。不需要 Cloud 或真实 Rerank 请求。这里只新增语义一致性要求，未修改 Gold 或答案评分。

已有 E12 节点的命令如下，由测试方适配相应 origin 断言后执行，**开发代理本次未运行**：

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH = Join-Path (Get-Location) 'backend'
& '.\.venv\Scripts\python.exe' -m pytest -q `
  backend/tests/test_e12_native_observability.py::test_failed_rerank_batch_marks_observed_score_origin_without_changing_results
$LASTEXITCODE
```

**接着优先做一次检索诊断，不继续增加提示词。** 只复用现有冻结 Word 资料/索引和 Recall/search 入口，将归档中的 1 条英文查询及 3 条中文查询分别送入同一 fulltext 配置；固定 KB、top_k、阈值及语料/代码身份，禁用生成、Embedding、Rerank 外部调用。记录实际参数、候选顺序、分数、正文，以及 `word-paragraph-6` 在前 10 条中的命中和季度/表头字段覆盖。这个目标片段的 hit@10 不冒充完整 qrel 的 recall@10。资料或索引版本不匹配就标清限制，不重建整库。

查询直接取自 `p1p2/proxy/calls/002-response.json`、`006-response.json`、`007-response.json`、`008-response.json` 的 tool_calls，避免手动改写造成新变量。新诊断保留独立运行 ID；已归档的模型响应只读复用，**不重新调用 Agent 生成查询**。若无匹配的 Hybrid 索引，Hybrid 对照为 NOT RUN；不临时外发 Embedding 或宣称多语向量一定能补救。

只有这项诊断确认查询改写导致可恢复的证据损失，才提出“复用原始查询 q0 的后备检索”的单因素候选；当前不改检索算法或在线策略。P2 补充事实继续列为未解决的质量问题，不把核心答案正确扩成全文忠实度通过；后续与上下文呈现/生成策略实验分开，不为追求小样本全绿连续改提示。

**归档要求先修在测试侧。** 新云端/本地运行使用独立 run_id 目录，Provider 记录以独立调用 ID 或不可覆盖文件保存；记录实际进程退出码。此要求针对已证实的网关序号复用问题，不是新业务校验。不要清除旧 404、UNKNOWN、Gold、快照或原回执。

### 8.5 交付、资源与版本恢复

只读审查汇总：`var/m2-remaining-20261105/receipt-followup/receipt-review.json`，含原 JSON 的哈希、逐组检查数量与开发代理复核口径，不覆盖原报告。两处产品改动的改前副本、前后哈希及 `changes.patch` 位于同目录；不复制原始请求、正文、数据库或索引。原工作区其余修改及独立测试方改动保留。

本轮业务模型/提案模型调用 0，测试、构建、产品导入、服务、Cloud 回读均 NOT RUN；未提交、推送或部署。来源审查、文件归档命令退出码见本次执行记录及 `archive-summary.json`，这些退出码不表示产品验证通过。恢复时按本轮补丁逐项处理，不用整个文件回退覆盖其他改动。

实际标准库文件归档命令 `.venv/Scripts/python.exe -X utf8 -` 退出码 **0**：两个改前文件哈希一致，本次补丁 2,113 字节；恢复档案及审查元数据共 50,582 字节（不含 summary 自身）。D 盘可用从 143,136,477,184 到 143,136,444,416 字节；同期磁盘活动参与差值，不算模型成本。没有新语料/索引副本、服务或待清理容器。

**本轮决定：接收已证实的工程通过项；纠正质量与调用分母口径；两处最小实现交付测试。下一项质量工作优先验证原查询与改写查询的证据差异。** 最终生成超时、全文补充事实、Cloud/浏览器及全量构建仍单独保留，不宣称全部 M2 已完成。

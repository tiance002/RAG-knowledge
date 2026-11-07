# 修复后定向测试报告（2026-11-05）

对象：`docs/m2-mini-followup-20261104.md` 所述三项修复。
工作区 `D:\RAG knowledge`，分支 `codex/pdf-parser-migration`，HEAD `57f9972b`。
本轮只做测试，未改产品代码；仅调整了两处过期的测试替身签名（见 §9）。

## 0. 结论

| 编号 | 内容 | 结果 | 退出码 | 证据 |
|---|---|---|---|---|
| F1 | Excel 货币格式证据送达最终请求 | **PASS**（24/24） | 0 | `var/acceptance-followup-20261105/f1/` |
| F2 | 多币种/空列/多表坐标隔离 | **PASS**（20/20） | 0 | `f2/report.json` |
| D1 | 单路降级经真实 HTTP/SSE 透传 | **PASS**（15/15） | 0 | `d1/report.json`、`d1/*.json` |
| D2 | 多轮累积、健康不误报、旧消息 | **PASS**（运行时 5 + 静态 5）；浏览器渲染 **NOT_RUN** | 0 | `d2/report.json` |
| G1 | 评分请求非流式、单次、阈值不变 | **PASS**（11/11） | 0 | `g1/report.json` |
| G2 | 评分超时有界、UNKNOWN、SSE 可见 | **PASS**（14/14） | 0 | `g2/report.json`、`g2/sse_events.json` |
| R1 | 既有代表用例未退化 | **PASS**（16/16） | 0 | `r1/checks.json` |
| — | 方案指定 4 个单元测试 | **PASS**（62 passed） | 0 | `pytest-plan.log` |
| — | 非 live 全量套件 | **PARTIAL**（1155 passed / 54 failed / 37 errors） | 1 | `pytest-full-after.log` |

**一句话判断**：三项修复在本次覆盖的入口上都按预期工作；没有发现本轮引入的产品行为缺陷。全量套件的红色项几乎全部来自「冻结基线哈希」「本机权限/WSL/Redis 夹具」，不是这三处改动造成的（§10 给出分类与无法完全归因的部分）。

## 1. 代码与差异核对

- 10 个文件的 SHA-256 与 `var/m2-mini-followup-20261104/after-hashes.json` **逐项一致**，说明交接后产品代码没有被再次改动。
- `changes.patch` 覆盖 9 个实际变更文件；用 `difflib` 将「before 快照 → 当前文件」的真实差异与 patch 的增删行逐行比对，**9/9 完全一致**（44/23/15/14/6/2/2/3/2 行）。
- `frontend/src/services/chat.ts` 在 after-hashes 中列出但内容与 before 相同（未改），与文档「已完整转发、本轮未改」一致。
- 文档 §5 写「源码差异档案 294,959 字节」，实际 `changes.patch` 为 **19,439 字节**。内容完整（逐行核对通过），字节数描述不准确，记为文档问题。

## 2. F1：Excel 原件 → 解析 → 检索 → 最终请求

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-followup-20261105/f1_excel_format.py
```

退出码 **0**。链路为 `openpyxl` 读原件 → `FileParser.parse`（产品入口）→ SQLite + BM25 → `ChatService.chat_stream`（真实引用组装）→ 捕获 provider 请求；唯一替身是 provider 返回值。输出写到新目录 `f1/`，旧 `var/acceptance-mini-20261104/step1/` 未被触碰。

| 检查 | 结果 |
|---|---|
| 原件 `C7:G7` 五格格式均为 `"$"#,##0` | 通过 |
| 原件 `A1` 标题格式含 `$` | 通过 |
| 原件 `C9` 自身格式为 `#,##0`（不含 `$`） | 通过 |
| 解析 `metadata.currency_format_status` | `AVAILABLE` |
| 解析 `currency_formats` | 仅 2 条：`A1:A1`、`C7:G7 (列 3-7)`；合并范围正确 |
| 解析文本含 `C7:G7 (列 3-7): "$"#,##0` | 通过 |
| 数值 `108149.892` 未被改写 | 通过（无 `$108149`） |
| 百分比列未被补 `$` | 通过 |
| 最终请求（29,584 字符）含格式证据 | 通过 |
| `$` 共 8 处，全部落在「原始货币显示格式」行内 | 通过 |
| 请求中 `C7:G7` 证据只来自该范围 | 通过 |

关键原文（`f1/final_provider_request.json`）：

```
[原始货币显示格式（仅适用于列出的单元格）] A1:A1 (列 1-1): "$"#,##0;[Red]"$"#,##0; C7:G7 (列 3-7): "$"#,##0
```

观察：T07 共 4 个分块，`A1` 的表头格式按设计重复出现在每一块；`C7:G7` 只出现在覆盖第 7 行的那一块（page 1）。这符合「每块收集本块范围 + 表头行」的约定，未观察到跨块误用。

## 3. F2：多币种、空 A 列、第二张表

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-followup-20261105/f2_multi_currency.py
```

退出码 **0**，20/20 通过。测试输入为临时生成的两个小工作簿：

- `mixed.xlsx`：Sheet1 `$ / € / % / ¥ / 普通数字` 各一列，A 列除标题外全空；Sheet2 含 `[$-409]`、`[$$-409]`、`£` 三种格式。
- `plain.xlsx`：纯文本 + 普通数字，无任何货币格式。

| 检查 | 结果 |
|---|---|
| 3 个币种列 × 3 行 = 9 条格式记录，列号为 2/3/5 | 通过 |
| 百分比列（4）、无格式列（6）未进入列表 | 通过 |
| 无跨列赋值（B 列不带 `€`、C 列不带 `$`） | 通过 |
| `[$-409]` 纯区域标记不被当成美元 | 通过 |
| `[$$-409]` 字面美元被识别 | 通过 |
| 第二张表的 `£` 不污染第一张表，反之亦然 | 通过 |
| 空 A 列未塌缩：表头仍 6 列，`B2` 坐标仍为第 2 列 | 通过 |
| 无货币格式文件正常解析，状态 `AVAILABLE`、列表为空 | 通过 |

## 4. D1：单路失败经真实 HTTP/SSE

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-followup-20261105/d1_http_sse.py
```

退出码 **0**，15/15 通过。`uvicorn` 真实监听回环端口，客户端用 `httpx` 走真实 TCP；`EMBEDDING_BASE_URL` 指向 `127.0.0.1:1` 制造向量路失败，BM25 有结果。模型由本地假端点提供响应，其余全部是产品代码。

| 案例 | `retrieval_status` | `degraded` | `fallback_used` | `answer_mode` | 引用 | provider 调用 |
|---|---|---|---|---|---|---|
| chat + hybrid（向量路失败） | `SUCCESS` | `true` | `true` | `GROUNDED_GENERATION` | 5 条，含 `工作表: T07` | 1 |
| chat + fulltext（健康） | `SUCCESS` | `false` | `false` | `GROUNDED_GENERATION` | 5 条 | 1 |
| agent + hybrid（向量路失败） | `SUCCESS` | `true` | `true` | `GROUNDED_GENERATION` | 5 条 | 4（意图/工具/评分/回答） |

- Agent 的 `retrieval_status` 事件同样带 `degraded=true`、`fallback_used=true`，不只是 `done`。
- 三条请求的引用都保留了原来源（`excel-2.xlsx`、`工作表: T07`），未因降级改变映射。
- 三次请求合计 6 次 provider 调用，与预期一致。

## 5. D2：累积语义与前端提示

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-followup-20261105/d2_degraded_state.py
```

退出码 **0**，10/10 通过。

运行时（真实 `process_tool_output`）：

| 场景 | 结果 |
|---|---|
| 第一轮降级 | `degraded=true`、`fallback_used=true` |
| 第一轮降级 + 第二轮健康 | 仍为 `true`（保留“曾降级”事实），`retrieval_status` 仍 `SUCCESS` |
| 全程健康 | `false`，不误报 |
| 只有降级标志、无引用 | 不会凭空产生引用，状态仍 `NO_RESULTS` |

前端静态核对：

- `DoneData` 声明 `degraded?: boolean`、`fallback_used?: boolean`；
- `done` 处理把 `data.degraded` 写入 `retrievalDegraded`；
- 提示仅在 `msg.retrievalDegraded` 为真时渲染，文案为「本次检索部分通道不可用，结果可能不完整。」；
- 提示块不修改答案正文；旧消息缺字段时为 `undefined`，不渲染。

**未覆盖**：浏览器实际渲染、重复添加的视觉验证（NOT_RUN）。本轮没有启动前端开发服务器。

## 6. G1：评分请求形态

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-followup-20261105/g1_grading_request.py
```

退出码 **0**，11/11 通过。使用真实 `LLMService.get_chat_model`（只包一层记录参数，仍调用原方法），真实 `ChatOpenAI`，网络层由本地端点返回 `0.9` / `0.5`。

| 检查 | 结果 |
|---|---|
| 评分请求次数 | 1 |
| 请求体 `stream` | `false` |
| 工厂实参 | `temperature=0.1, streaming=False, timeout=60.0` |
| 默认 `AGENT_GRADING_TIMEOUT_SECONDS` | 60.0 |
| `0.9` 解析与状态 | `last_score=0.9`、`VALID` |
| `0.9` 的重试决定 | 不安排重试 |
| 评分范围 | `CURRENT_TOOL_ROUND_PREFIX`、上限 2000 字符 |
| `0.5` 的重试决定 | 安排重试，`retry_count=1`（阈值仍是 0.8） |
| `0.5` 的请求次数 | 1 |

## 7. G2：评分端点不返回

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-followup-20261105/g2_grading_timeout.py
```

退出码 **0**，14/14 通过。本地端点收到评分请求后读取请求体、保持连接但永不响应；评分配置临时设为 3 秒。

直接调用评分节点：

| 观察 | 值 |
|---|---|
| 结束耗时 | **3.16 s** |
| 异常类型 | `TimeoutError` |
| `provider_status` | `UNKNOWN` |
| 本地请求 ID | 有（`1fb637c0…`） |
| 评分请求到达次数 | 1；等待 2 秒后仍为 1（无重发） |
| `provider_error_payload(stage="sse")` | `status=UNKNOWN`、`local_request_id` 与异常一致、文案「模型请求未完成，请检查后再手动重试。」 |

真实 HTTP/SSE（`mode=agent`，`AGENT_GRADING_TIMEOUT_SECONDS=3`）：

| 观察 | 值 |
|---|---|
| 总耗时 | **7.77 s**（意图 + 检索 + 工具轮 + 评分 3 s） |
| 事件序列 | `agent_thought ×3 → rag_result → retrieval_status → agent_thought → error` |
| `error.status` | `UNKNOWN` |
| `error.local_request_id` | `fd928a58…` |
| `done` 事件 | **0 个**（没有把超时当正常完成） |
| 评分请求到达次数 | 1（无 fallback 重发） |

`provider_request_id` 为空是预期：端点从未返回响应头，无法取得供应商 ID。总耗时高于 3 秒也符合设计——超时只约束评分这一次调用。

## 8. R1：既有代表用例

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-followup-20261105/r1_behavior.py
```

退出码 **0**，16/16 通过。复用上一轮脚本逻辑，输出改到新目录 `r1/`，旧 `step234/` 保留。

- 部分证据：单路失败仍返回结果，`degraded=true`，正常生成。
- 评分文本：6 种输入（含 `8.5/10`、`No error, score 0.9`、`timeout code 0.5`）解析符合预期；缺失分数不阻断已生成答案；明确低分仍降级。
- 末轮空检索不否决已有证据。
- 无证据/故障：4 种文案互不相同，不含具体数字，普通模式 provider 调用 0 次。
- 终态：`stop → COMPLETED`；`length → TRUNCATED + 原文摘录`（不含被截断半句）；`finish_reason=None → COMPLETED`。
- Agent 截断同样降级为摘录。

## 9. 既有测试

方案指定命令（4 个文件）：

```bash
PYTHONPATH=backend ./.venv/Scripts/python.exe -m pytest \
  backend/tests/test_provider_unknown.py \
  backend/tests/test_agent_quality_outcomes.py \
  backend/tests/test_retrieval_outcomes.py \
  backend/tests/test_agent_request_parameters.py
```

**62 passed，退出码 0**。追加偏好相关用例 `test_memory_chat.py` 后为 **109 passed，退出码 0**。

首次全量运行时发现两条与本轮新增关键字有关的失败：

- `test_agent_engineering_repairs.py::test_real_graph_multiple_tools_delivers_stable_ids_to_answer_and_grader`
- `test_agent_tool_observation.py::test_provider_messages_observe_all_agent_calls_and_hide_reasoning`

原因：两处测试替身写的是 `async def invoke(model, messages, config, stage, model_name)`，而产品新增了 `timeout_seconds=` 关键字，替身抛 `TypeError`，被评分节点的兜底吞掉后表现为 `Grading failed: TypeError`。按方案「替身签名过时则调整替身，不改生产逻辑」，只在两处签名末尾加了 `**kwargs`：

- `backend/tests/test_agent_engineering_repairs.py:193`
- `backend/tests/test_agent_tool_observation.py:103`

修改后两文件 **38 passed，退出码 0**。产品代码未动。

## 10. 非 live 全量套件

```bash
PYTHONPATH=backend ./.venv/Scripts/python.exe -m pytest -m "not live" -p no:cacheprovider \
  --basetemp="var/.pt-$(date +%s)"
```

**1155 passed / 54 failed / 37 errors / 6 skipped，退出码 1，耗时 550 s。**

红色项的主要来源（91 个非通过项）：

- **冻结基线/快照类**：`Source code snapshot changed` 出现 133 次、`Application source differs from v3` 6 次，覆盖绝大多数 `test_m2_current_*`、`test_m2_hybrid_postfix_*`、`test_e12_*`。这类测试把某次运行的源码哈希写死，任何代码改动都会失效；本轮改了 9 个文件，它们必然变红。
- **本机权限**：`PermissionError [WinError 5]` 3 条（`os.replace`、子进程 `CreateProcess`）。
- **安全策略**：`wsl.exe` 被拦截 3 条。
- **Redis/启动夹具**：`Fresh process required...`、Redis 前置失败数条。
- **验收脚本子进程**：`scripts/run_e12_acceptance.py` 里 `who.stdout` 为 `None`，5 条。
- **少量单元级**：`test_e11_candidate_transitions`、`test_e11_context_delivery`、`test_observation_stages`、`test_metrics`、`test_grounded_candidate_snapshot` 各有 1 条，涉及检索观测/中间件/快照，均不在本轮改动文件内。

**归因边界**：上述「单元级」几条我没有用改前快照回放证明它们在改动前也失败，因此只能标为 **UNKNOWN（疑似既有）**，不能断言「本轮未引入」。可以确定的是：它们依赖的 `retrieval.py`、`context_builder.py`、`observability.py`、`grounded_generation.py` 都不在本轮 9 个变更文件里。

两次全量之间还出现了 3 条环境相关的抖动（`WinError 5`、`wsl.exe` 拦截），第二次比第一次多 1 条失败；这属于执行环境不稳定，不构成产品结论。

## 11. 需要知道的行为观察

1. **Agent 的 `done.generation_status` 仍为 `null`**（D1 实测）。这是既有结构：Agent 收尾只带 `validation_status` 等字段，不写 `generation_status`。本轮新增的 `degraded` 字段工作正常，但如果前端将来依赖 `generation_status` 判断截断，Agent 模式拿不到值。
2. **表头格式会在每个分块重复**。T07 的 `A1` 格式出现在全部 4 个分块里；这是文档写明的设计（每块带表头行），不是缺陷。若后续担心模型把表头格式套到远处数值，可考虑只在首块保留 `A1`。
3. **评分超时只约束评分这一次调用**。G2 的 HTTP 总耗时 7.77 s，其中 3 s 是评分等待；意图、检索、工具轮不受影响，符合「只作用于相关性评分」的约定。
4. **`changes.patch` 的字节数与文档不符**（19,439 vs 294,959），内容本身完整。
5. **未调用云端模型**。本轮所有模型响应都来自本地网络端点的固定文本，`M2_AB_API_KEY` 未使用，云端调用 0 次；因此本报告不对「真实 Qwen 回答质量」下结论。

## 12. 未执行 / 未覆盖

| 项 | 状态 | 原因 |
|---|---|---|
| 云端 Qwen 真实回答 | NOT_RUN | 本轮验证的是格式送达、状态透传与超时边界，不需要付费调用 |
| 前端浏览器渲染与去重 | NOT_RUN | 只做了源码静态核对，未启动前端 |
| 前端构建 / `tsc` | NOT_RUN | 未在本轮范围内，且工作区有大量既有未提交改动 |
| 真实 Redis / 正式服务 | NOT_RUN | 未启动，避免影响本机环境 |
| 旧 A/B 结果对比 | BLOCKED | 旧结果来自改前代码，模态已变，不可直接比较 |

## 13. 证据清单

```
var/acceptance-followup-20261105/
├── harness.py                    # 共用夹具（假模型、uvicorn、SSE、入库）
├── f1_excel_format.py  f1/       # report.json, t07_parsed.txt, final_provider_request.json
├── f2_multi_currency.py f2/      # report.json, mixed.xlsx, plain.xlsx
├── d1_http_sse.py      d1/       # report.json, chat_hybrid.json, chat_fulltext.json, agent_hybrid.json, uvicorn.log
├── d2_degraded_state.py d2/      # report.json
├── g1_grading_request.py g1/     # report.json
├── g2_grading_timeout.py g2/     # report.json, sse_events.json, uvicorn.log
├── r1_behavior.py      r1/       # checks.json, 2a/2b/2c/3/4 的原始输出
├── pytest-plan.log               # 4 个指定测试：62 passed
├── pytest-full.log               # 首次全量（替身修复前）
└── pytest-full-after.log         # 二次全量（1155 passed / 54 failed / 37 errors）
```

目录合计 3.6 MB。未复制语料或索引；测试数据库与 Chroma 目录都在各自输出目录内。本轮创建的 pytest 临时目录（`var/.pt-*`，约 3,200 个文件）已删除。旧验收目录 `var/acceptance-mini-20261104/`、`var/m2-mini-followup-20261104/` 未改动。

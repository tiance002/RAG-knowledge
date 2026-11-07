# 第 7 节最小方案 — 独立测试回执

日期：2026-11-05
分支 / HEAD：`codex/pdf-parser-migration` / `57f9972b8477b0c4aaf7fac329b911420fafc95e`
被测工作树：本轮 5 个源码文件哈希与 `var/m2-remaining-20261105/after-hashes.json` **完全一致**（测试前后各核对一次）。
证据目录：`var/acceptance-remaining-20261105/`（新目录，前两轮 `var/acceptance-mini-20261104/`、`var/acceptance-followup-20261105/` 未改动）。

## 1. 结论摘要

| 编号 | 范围 | 结果 | 退出码 | 关键证据 |
|---|---|---|---|---|
| S1 | Agent 生成终态（真实 StateGraph + 产品工厂 + HTTP/SSE） | **PASS 18/18**，6 个场景 | 0 | `s1/report.json`、`s1/*.json`、`s1/provider-requests.json` |
| S2 | 异常终止 + 前端终态处理 | **PASS 6/6 + 17/17** | 0 / 0 | `s2/error-path.json`、`s2/frontend-stream.json` |
| P1 | 季度报告 Agent（云端 Qwen，2 次） | **接线 PASS 6/6；效果 1/2** | 1（见 §4.3） | `p1p2/p1-attempt{1,2}.json`、`p1p2/proxy/calls/` |
| P2 | HotpotQA 普通模式（云端 Qwen，2 次） | **PASS 8/8；补充内容 1 次有瑕疵** | 同上 | `p1p2/p2-attempt{1,2}.json` |
| R2 | 最小行为 / Excel 格式 / 既有用例回归 | **PASS 16/16 + 24/24 + 77 passed** | 0 / 0 / 0 | `r2-behavior/`、`r2-excel/`、`r2/pytest-plan.log` |
| T1 | 7 个失败节点定向复核 | **7/7 PASS**（仅改测试替身/夹具/脚本，产品未改） | 0 | `t1-selected.log` → `t1-selected-after.log`、`t1-files-after.log` |

一句话：**本轮 5 处接线改动在真实入口上成立**；没有发现“终态字段错标、截断被当成完成、评分覆盖终态、异常伪造 done”这类缺陷。发现的是三个使用层面的问题（§7），均不否定本次接线。

## 2. S1：生成终态

命令：

```bash
./.venv/Scripts/python.exe var/acceptance-remaining-20261105/s1_terminal_states.py
```

退出码 **0**。每场景 4 次本地 Provider 调用（意图 / 决策 / 评分 / 回答），预检场景 0 次，共 20 次；只在网络返回处替换，Agent 图、`LLMService`/`ChatOpenAI`、检索、SSE 均为产品代码。

| 场景 | `generation_status` | `finish_reason` | `answer_mode` | 交付正文 | 判定 |
|---|---|---|---|---|---|
| 完整 + `stop` | COMPLETED | stop | GROUNDED_GENERATION | 模型原文 + 5 条引用 | PASS |
| 完整、**无** `finish_reason` | COMPLETED | null | GROUNDED_GENERATION | 模型原文 | PASS（不误拒） |
| `length`（正文非空） | TRUNCATED | length | EXTRACTIVE_FALLBACK | 安全摘录，**不含**半截生成 | PASS |
| 空文本 | EMPTY | stop | EXTRACTIVE_FALLBACK | 安全摘录（带“无法确认…”说明） | PASS |
| 范围预检（空知识库） | NOT_RUN | null | INSUFFICIENT_EVIDENCE | 固定文案，**0 次 Provider** | PASS |
| 低评分（0.1） | **COMPLETED** | stop | EXTRACTIVE_FALLBACK | 模型原文 | PASS（评分未覆盖终态） |

关键点：

- **截断/空文本的终态取自兜底改写之前**。`_guard_knowledge_answer` 会把 `response.content` 换成检索摘录，但 `_answer_generation_metadata` 已在 `_finalize_knowledge_answer` 开头取值（`agent_workflow.py:334-356`），所以摘录非空没有把状态染成 COMPLETED。这是本轮最容易写反的一处，实测顺序正确。
- **工具调用轮不覆盖终态**。每次运行第一次 `agent` 节点返回 `content=null` + `tool_calls`，若被当作最终回答会得到 EMPTY；实测最终仍是 COMPLETED。
- **评分不覆盖终态**。低分那轮 `grading_status=VALID`、`answer_mode` 降为 `EXTRACTIVE_FALLBACK`，`generation_status` 仍为 COMPLETED，符合“生成状态与答案质量分开”。
- **范围预检真的没有调用模型**：`provider_calls=0`，`done` 带 `NOT_RUN`。

## 3. S2：异常终止与前端

### 3.1 服务端超时

命令：`./.venv/Scripts/python.exe var/acceptance-remaining-20261105/s2_error_path.py` → **RC=0，6/6**。

评分节点挂起（本地端点只收不回），`AGENT_GRADING_TIMEOUT_SECONDS=3`：

- 端到端 **5.15 s** 结束，未等 SDK 默认 600 s；
- 事件序列 `... rag_result → retrieval_status → agent_thought → error`，**只有一个 error，没有任何 done**；
- error 携带 `status=UNKNOWN`、`local_request_id`；
- 评分请求只发 **1 次**（`grading_calls=1`），无自动重发；
- 未产生任何回答正文。

### 3.2 前端运行时（真实源码，非源码存在性检查）

用 esbuild 打包 `frontend/src/services/chat.ts`，在 Node 里用假 `fetch` 注入 SSE 字节，直接调用产品 `createChatStream`：**17/17 PASS，RC=0**（`s2/frontend-stream.json`）。

| 场景 | 结果 |
|---|---|
| 正常 done（COMPLETED） | onDone 一次、无错误、正文完整、**fetch 1 次** |
| TRUNCATED | 一次 `TRUNCATED` 提示、**不调用 onDone**、已送达摘录保留、fetch 1 次 |
| 旧 done（无 `generation_status`） | 按正常完成回调，正文保留 |
| error(UNKNOWN) | 一次错误回调、无 done、fetch 1 次 |
| 流中断 | 提示 INTERRUPTED、无 done |
| EMPTY | 摘录送达，但**不触发任何提示**（见 §7.1） |

### 3.3 类型检查

`frontend/node_modules/.bin/tsc -b` → **RC=1，45 条错误**。
把 `types/chat.ts` 换回本轮改前副本再跑一次：**同样 45 条**，错误数与集合一致，且**没有一条**落在 `types/chat.ts` 或 `services/chat.ts`；文件已还原，哈希 `AF87794708B6…` 与归档一致。
结论：本轮类型改动**未新增**错误；`vite build` 被既有 `tsc` 错误阻断，**NOT RUN**。浏览器渲染 **NOT RUN**。

## 4. P1 / P2：云端 Qwen 真实请求

命令：`./.venv/Scripts/python.exe var/acceptance-remaining-20261105/p1p2_cloud.py`
模型：`Qwen/Qwen3.5-4B`（SiliconFlow），凭据仅在测试进程内从 ECS `/opt/amrag/backend/local.db` 读取，文件用后即删（已确认 `ABSENT`）。
链路：uvicorn 真实 HTTP/SSE → 产品 `LLMService`/`ChatOpenAI` → 本地透传网关 → 云端。网关只补 `enable_thinking=false`（与冻结 A/B 一致），**没有替换模型工厂**，`timeout`、`streaming`、工具绑定均为产品原样。
最终一轮共 4 次用户请求 / 11 次 HTTP 调用，**17,656 prompt + 1,212 completion tokens**。

### 4.1 P1 季度报告（Agent）

接线检查 **6/6 PASS**（每轮 3 项：新工具说明、检索提示、实际调用工具）：两次最终请求都含新工具说明（1,424 字符，含 `Do not claim documents are missing`）和新系统提示（“先检索获取证据；工具返回前…”）。

| 次数 | 工具调用 | 检索查询 | 结果 | 引用 |
|---|---|---|---|---|
| 1 | 1 次 | **英文原句** | `GROUNDED_GENERATION`，覆盖季度/百分比/2–3 句/列全任务/即使无工作 | 10 条，`[1]` 命中 `word-paragraph-6` |
| 2 | 3 次 | **模型自行译成中文**（“如何准备…”“季度进度报告 模板…”“季度报告 进度汇报…”） | 3 次均 `NO_RESULTS`；回答“未在所选知识库中找到…”，`answer_mode=INSUFFICIENT_EVIDENCE`，`generation_status=COMPLETED` | 0 |

判断：**上一轮 `tool_calls=0` 的问题已改善**（两次都真正调用了工具，第二次还自主重试 3 次）。第 2 次没答出不是“无证据硬编”，而是全量检索配置（`fulltext`、embedding 关闭）+ 英文语料 + 中文查询导致的零命中；生产默认 hybrid（bge-m3 多语）不一定复现，本次不据此判定产品回归。按方案“没有调工具则如实记录未改善”，这里记为 **1/2 次答出，逐次保留，不宣称稳定提升**。

### 4.2 P2 HotpotQA（普通模式）

| 次数 | 核心答案 | 行内引用 | 补充内容 |
|---|---|---|---|
| 1 | Pieter van Musschenbroek ✅ | `[4]`=Leyden、`[8]`=Tribometer，编号与证据正文逐条对应 | 末尾“Note”加入 **Benjamin Roy / 1784 / Kleist**，这些词在最终请求中出现 **0 次** |
| 2 | 同上 ✅ | `[4][8]` 对应正确 | 全部来自证据，无额外归因 |

两次最终请求都含冻结证据（5,789 字符 JSON），`Musschenbroek`、`Leyden`、`Tribometer` 均在；回答中 `Coulomb` 出现 **0 次**（上一轮的问题未复现）。
第 1 次的“Note”明确标注为“历史记录”而非资料结论，核心答案也没被污染；但它仍是一条**无原文支持的事实陈述**，按“补充内容不得把无依据归因当资料结论”只能判 **部分达成**，第 2 次则干净。

### 4.3 关于该命令的退出码

最终一次执行的进程退出码是 **1**：当时脚本把两条“模型是否答出”也当成门槛，P1 第 2 次零命中就拉红了整次运行。逐项核对后，这两条已改为**逐次记录**（`report.json` 中 `checks=14 / failed=[]`，另有 `outcomes=4`），工程接线 14/14 全部通过；为避免多花云端调用，没有为此重跑，退出码按原始记录保留。

## 5. R2：最小回归

| 命令 | 结果 | 覆盖 |
|---|---|---|
| `r2_behavior.py` | **RC=0，16/16** | 单路失败仍答、降级标记、评分 6 种写法、评分缺失不阻断、明确低分降级、末轮空检索不否决、无证据/故障文案与零编造、四种终态 |
| `r2_excel_format.py` | **RC=0，24/24** | 原件 `$` 在 `C7:G7`/`A1`、`C9` 自身无 `$`、百分比是 `0.0`、解析保留 `108149.892` 与 `In thousands`、最终请求 8 处 `$` 全在格式行、未给 `C9` 套 `$`、引用 `工作表: T07` |
| 方案给出的 pytest | **RC=0，77 passed** | `test_agent_grounded_answer` / `test_agent_engineering_repairs` / `test_provider_unknown` + 3 个记忆与偏好节点 |

Scope、检索参数、来源编号、单位与重试预算均未变；没有按字数或固定句式判定。

## 6. T1：7 个失败节点

先复跑：**7 failed**（`t1-selected.log`）。逐项定位后只改测试替身 / 夹具 / 验收脚本，**产品代码 0 改动**；复跑 **7 passed**（`t1-selected-after.log`），六个文件整体 **35 passed**（`t1-files-after.log`）。

| 节点 | 实际原因 | 处理 | 结果 |
|---|---|---|---|
| `test_e11_candidate_transitions…hybrid_to_rerank` | 替身 `_enrich_results` 只收 2 参，产品传第 3 个 `kb_ids` → TypeError → 结果空 | 替身加 `kb_ids=None` | PASS |
| `test_observation_stages…vector_observation` | 同上 | 同上 | PASS |
| `test_e11_context_delivery…expanded_neighbor` | 假 Session 只给 chunk，不给 `FileDocument`；产品按 `kb_id` 校验归属后拒绝扩展 → 取 `[0]` 越界 | 补一个 `kb_id='kb'` 的 FileDocument 替身，**保留**来源范围校验 | PASS |
| `test_e12…failed_rerank_batch` | 旧断言要求 `rerank_score=.6`、origin=`original_score`；产品实际是 `rerank_score=None`、`rerank_status=UNKNOWN`、原分留在 `score` | 改为断言真实契约；导出实测 `score_kind=original_retrieval`、`score_value=.6`、`rerank_score=None`、`fallback_score_origin='none'` | PASS |
| `test_e12…descriptor_parent_is_sdk_tool` | ① `whoami` 输出是 GBK，被按 UTF-8 解码 → 读线程 `UnicodeDecodeError` → `stdout=None` → `splitlines` 崩溃；② 修好后产品偏好层连 `localhost:6379`，被“零网络”守卫记 1 次拒绝 | ① 改为按 utf-8/mbcs/gbk 回退解码，保留 SID 与 ACL 校验；② 测试子进程设 `REDIS_URL=memory://`，隔离与链路无关的本机 Redis 依赖 | PASS（脚本 `status=PASS`、`network_denials=0`） |
| `test_grounded_candidate_snapshot` | Windows 上 `_private_root_fd` 按设计拒绝未验证 ACL 的私有快照，测试却假定有 `snapshot_id` | 按平台分支：POSIX 校验文件内容；Windows 断言 `snapshot_id=None`、目录为空、事件不含原文 | PASS |
| `test_metrics…context_reaches_handler` | 用普通 Starlette `Route`，不写 `scope['route']`，`_route` 只能得 `unmatched` | 换成真实 FastAPI 路由（`add_api_route`） | PASS（`route=/chat`、`outcome=DONE`） |

关于 §7.4 的元数据疑点：失败批次导出为 `fallback_score_origin='none'`，`observability.py:501` 把非空字符串一律当“存在回退分”，于是 `score_kind` 被改成 `original_retrieval`。本次实测**排名与分数没有错**（`.6` 是原检索分，`rerank_score=None`），只是字段名读起来像“回退来源是 none”。未改产品，保留为**未关闭的命名问题**。

## 7. 发现与影响判断

### 7.1 EMPTY 有摘录、没有提示（前端）

服务端 `generation_status=EMPTY` 时已交付带“无法确认以下资料是否足以完整回答”的安全摘录；但前端 `services/chat.ts:88-93` 只把 `TRUNCATED/UNKNOWN/CANCELLED/FAILED` 当失败，`EMPTY` 直接走 `onDone`。用户看到的是一段摘录，没有任何“模型没有返回内容”的提示。
影响：不误导事实（摘录自带说明），但**终态对用户不可见**。若要修，只需把 `EMPTY` 加入提示分支，不必改后端。

### 7.2 中文查询 + 纯全文检索 = 零命中（P1 第 2 次）

系统提示是中文，4B 模型把英文问题译成中文再检索；冻结配置只有 `fulltext` 且 embedding 被禁用，英文语料自然 0 命中。产品如实回“未找到”，没有编造。这不是本轮改动引入的，但说明“工具被调用”不等于“检索能命中”；如果线上以中文提问英文资料为主，应依赖 hybrid/多语向量，而不是只加提示词。

### 7.3 P2 仍会出现“资料外补充”

第 1 次末尾的 Benjamin Roy / 1784 在证据中出现 0 次，虽标注为“历史记录”，仍属无依据事实。新提示（“新增具体事实、单位或归因应有资料依据”）**降低了它冒充资料结论的概率，但不能消除**。按方案不追加硬性内容校验，只记录。

### 7.4 其他

- Agent 最终回答调用（`agent_workflow.py:533`、`:1073`）仍**没有产品级超时**，只有评分节点有 `timeout`；本次没有用 600 s 的默认超时去做挂起实验。属于既有风险，不是本轮回归。
- 普通模式“未选知识库”的分支（`chat.py:226`）`done` 不带 `generation_status`；不在本轮范围，未判错。
- 全量非 live 套件 **NOT RUN**（方案明确“无需重跑全量”）；本轮回执只覆盖上表节点与文件。
- 云端费用 **UNKNOWN**：没有供应商账单；只能给出 token 量（17,656 / 1,212），不换算金额。
- Langfuse Cloud 回读、浏览器实际渲染：**NOT RUN**。

## 8. 改动清单（本轮测试侧）

产品 5 个文件未改（哈希一致）。为完成 T1 的“测试方适配”，修改了 6 个测试文件与 1 个验收脚本：

| 文件 | 改动 |
|---|---|
| `backend/tests/test_e11_candidate_transitions.py` | `_enrich_results` 替身补第 3 参 |
| `backend/tests/test_observation_stages.py` | 同上 |
| `backend/tests/test_e11_context_delivery.py` | 补 `FileDocument` 归属替身 |
| `backend/tests/test_e12_native_observability.py` | 失败重排断言对齐真实分数语义；E12 子进程 `REDIS_URL=memory://` |
| `backend/tests/test_grounded_candidate_snapshot.py` | Windows 安全拒绝分支 |
| `backend/tests/test_metrics.py` | Starlette Route → FastAPI 路由 |
| `scripts/run_e12_acceptance.py` | `whoami` 按控制台代码页解码，修 `stdout=None` |

没有改 Gold、冻结样例、原始模型结果或评测口径；没有为了让测试变绿而放宽产品的来源/ACL/快照保护。

## 9. 证据索引

```
var/acceptance-remaining-20261105/
├─ s1/                  report.json、stop|nofinish|length|empty|preflight|grading_low.json、provider-requests.json
├─ s2/                  error-path.json、frontend-stream.json、tsc.log、tsc-before.log、uvicorn.log
├─ p1p2/                report.json、p1-attempt{1,2}.json、p2-attempt{1,2}.json
│  └─ proxy/calls/      NNN-request.json（实际发给 Provider 的完整请求）、NNN-response.json、NNN-meta.json
├─ r2-behavior/         checks.json 及运行产物
├─ r2-excel/            report.json、最终请求与解析文本
├─ r2/pytest-plan.log   77 passed
├─ t1-selected.log      修复前 7 failed
├─ t1-selected-after.log 修复后 7 passed
├─ t1-files-after.log   六个文件 35 passed
└─ t1-e12-verify/       E12 脚本独立复跑：status=PASS、network_denials=0、generation_status=COMPLETED
```

本轮云端调用共 4 次用户请求（11 次 HTTP），全部记录在 `p1p2/proxy/calls/`；更早两次因网关路径拼接错误（`/v1/v1` → 404）作废，未计入结果，也未用其回答做任何判断。

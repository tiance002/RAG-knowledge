# 最小验收执行记录（2026-11-04）

针对上一轮修复，按 5 项最小方案实测。只记录实际执行结果；模型替身与真实调用分开标注。
原始证据全部在 `var/acceptance-mini-20261104/`。

| 项 | 内容 | 结果 | 退出码 |
|---|---|---|---|
| 1 | Excel 原件经产品解析入口入库 | 数值/千为单位/来源位置送达；**美元符号未送达** | 0 |
| 2 | 有部分证据时回答 | 3 类场景均能用已有证据作答；**降级状态未暴露给客户端** | 0 |
| 3 | 无证据 / 检索失败 | 4 种状态文案互不相同，不含具体数字，不调用模型 | 0 |
| 4 | 生成终态 | 正常/截断/无 finish_reason 均按预期；截断内容不被冒充完整回答 | 0 |
| 5 | 小规模真实回归 | 6 个案例（13 次 provider 调用）；核心答案 5 PASS / 1 UNKNOWN | 0 |

---

## 1. Excel 原件入库

**命令**

```bash
cd "/d/RAG knowledge"
./.venv/Scripts/python.exe var/acceptance-mini-20261104/step1_excel_ingest.py
```

退出码 **0**。日志 `var/acceptance-mini-20261104/step1.log`，结构化结果 `step1/report.json`。

**链路**：`openpyxl` 读原件真相 → `FileParser.parse()`（产品入口）→ 写 SQLite → `BM25Index` → `ChatService.chat_stream()` 真实检索与引用组装 → 捕获最终 provider 请求。
唯一替身：provider 返回固定文本；**被捕获的 messages 是产品代码真实拼装的**（`step1/final_provider_request.json`，29,286 字符）。

### 1.1 原文件有什么

| 位置 | 值 | 数字格式 |
|---|---|---|
| `T07!C5` | `(In thousands)` | `#,##0` |
| `T07!C9` | `108149.892` | `#,##0` |
| `T07!C7:G7` | 汇总行数值 | `"$"#,##0`（5 格全部） |
| `T07!A1` | 表标题 | `"$"#,##0;[Red]"$"#,##0` |
| `T07!H9` | `0.8210391998291016` | `0.0` |

**结论 1**：原文件支持「千美元」。但美元来自 **表级格式**（`C7:G7` 与 `A1`），`C9` 自身格式是 `#,##0`，不含 `$`。
**附带修正**：同一张表的百分比列格式是 `0.0` 而不是 `0.0%`，所以「解析丢了百分号」在这张表上不成立，不能算缺陷。

### 1.2 解析文本有什么

`step1/t07_parsed.txt`，首行 `[工作表: T07]`。

| 检查 | 结果 |
|---|---|
| `108149.892` | 保留 |
| `(In thousands)` | 保留 |
| 工作表名 `T07` | 保留（作为文本前缀） |
| `$` | **0 处** |

原因可复算：`parser._parse_excel()` 用 `pd.read_excel()` 取值 + `DataFrame.to_markdown()` 渲染，全仓没有任何代码读取 `number_format`。

### 1.3 最终模型请求收到什么

`step1/final_provider_request.json` 的 system 消息（29,286 字符）：

```
[1] 来源：excel-2.xlsx 工作表: T07
[工作表: T07]
| ... Alabama | 219 | 108149.892 | ... |
| ... State | libraries | (In thousands) | ... |
```

| 检查 | 结果 |
|---|---|
| 数值 `108149.892` | 收到 |
| `(In thousands)` | 收到 |
| 来源位置 `工作表: T07` | 收到（文件名带 `.xlsx` + 清单定位器同时生效） |
| `$` | **0 处** |

### 1.4 判定

1. **原文件是否有千美元依据**：有。
2. **解析文本是否保留美元显示格式**：没有。
3. **模型回答的事实是否正确**：本次未让模型作答（替身返回固定文本），事实正确性由第 5 项的真实调用回答。
4. **是否构成独立的送达缺陷**：构成。缺的不是「千」，是「币种」。模型只能看到 `(In thousands)`，无法从文本判断是美元、人民币还是其他货币。这是解析问题，不是模型问题。

---

## 2. 有部分证据时回答

**命令**

```bash
./.venv/Scripts/python.exe var/acceptance-mini-20261104/step234_behavior.py
```

退出码 **0**，16 项检查全部通过。证据：`step234/2a_hybrid_degraded.json`、`2b_score_parse.json`、`2c_success_then_empty.json`、`checks.json`。

### 2a 混合检索：向量路失败、BM25 有结果

做法：把 `RetrievalService._embed_query_for_kb` 替换为抛 `RuntimeError`，`search_mode=hybrid` 走真实融合与真实生成链路。

| 观察 | 值 |
|---|---|
| 检索结果 | 1 条 |
| `retrieval_status` | `SUCCESS` |
| `answer_mode` | `GROUNDED_GENERATION`，模型确实被调用 1 次 |

**结论**：能利用已有证据回答，不再因为单路降级丢掉答案。

**但**：客户端 `done` 事件只有 `usage / retrieval_status / answer_mode / generation_status / validation_status / source_mapping_status / expansion_delivery_status`，**没有 `degraded` 或 `fallback_used`**。降级事实只进了 `metrics` 与观测事件（`retrieval.outcome`），用户在回答里看不到任何「本次检索有一路失败」的提示。

→ 「利用已有证据」**通过**；「如实暴露降级状态」**只做到不撒谎，没做到主动告知**，判为部分达成。

### 2b 相关性评分格式异常

`_relevance_score()` 实测：

| 输入 | 解析结果 |
|---|---|
| `得分 0.85，理由：大部分相关` | 0.85 |
| `0.85/1.0` | 0.85 |
| `8.5/10` | 0.85 |
| `No error, score 0.9` | 0.9 |
| `timeout code 0.5` | `None`（故障词优先，不误判） |
| `1` | 1.0 |

生成门槛实测：
- `last_score=None` + 有证据 → `GROUNDED_GENERATION`（不再把格式异常当证据不足）；
- `last_score=0.2` + 有证据 → `EXTRACTIVE_FALLBACK`（明确低分仍降级）。

### 2c 前一轮成功、后一轮无结果

`statuses = ["SUCCESS", "NO_RESULTS"]` + 有引用 → `GROUNDED_GENERATION`，答案内容为模型原文。末轮空检索不再否决前面几轮。

---

## 3. 无证据 / 检索失败

| 状态 | Agent 模式文案 | 普通模式文案 | `answer_mode` |
|---|---|---|---|
| `NO_RESULTS` | 未在所选知识库中找到可用于回答的相关资料。 | 同左 | `INSUFFICIENT_EVIDENCE` |
| `RETRIEVAL_ERROR` | 知识库检索发生错误，无法确认是否存在相关资料，请稍后重试。 | 同左 | `RETRIEVAL_ERROR` |
| `UNKNOWN` | 知识库检索状态不确定，无法确认是否存在相关资料，请稍后重试。 | 同左 | `UNKNOWN` |
| `LOW_RELEVANCE` | 找到的候选资料相关性不足，无法依据现有证据回答。 | 未测（同分支） | `INSUFFICIENT_EVIDENCE` |

- 4 种文案互不相同，「资料不足」与「检索故障」可区分。
- 7 个空结果场景的回答中都不含 `219` / `108149` 等具体数字，没有编造事实。
- 普通模式三种空状态下 **provider 调用次数均为 0**，没有先让模型自由发挥再兜底。

证据：`step234/3_no_evidence.json`。

---

## 4. 生成终态

普通模式用真实 `_chat_stream_normal` + 固定证据 + 可控 provider 返回；Agent 模式用真实 `_finalize_knowledge_answer`。

| 场景 | 客户端 `generation_status` | `answer_mode` | 实际内容 |
|---|---|---|---|
| 正常完成（`stop`） | `COMPLETED` | `GROUNDED_GENERATION` | 模型答案原文 |
| 明确截断（`length`） | `TRUNCATED` | `EXTRACTIVE_FALLBACK` | 检索原文摘录，**不含**被截断的半句 |
| 无 `finish_reason`（`None`） | `COMPLETED` | `GROUNDED_GENERATION` | 模型答案原文 |

Agent 模式 `length`：`EXTRACTIVE_FALLBACK`，回答为「无法确认以下资料是否足以完整回答，以下为检索到的原文…」，**不含**被截断内容。

**结论**：截断不会冒充完整回答；不返回 `finish_reason` 的非空响应按完成处理，符合修复预期。

证据：`step234/4_terminal_states.json`。

---

## 5. 小规模真实回归

**凭据**：测试启动时经 SSH 从 ECS `120.55.115.162:/opt/amrag/backend/local.db` 的 `custom_models` 读取 SiliconFlow 配置，只写入本地受限文件 `step5_credentials.json`（`chmod 600`，`var/` 已 gitignore），未进入代码或提交。

**命令**

```bash
./.venv/Scripts/python.exe var/acceptance-mini-20261104/step5_real_regression.py
```

固定配置：`Qwen/Qwen3.5-4B`、`temperature=0.01`、`max_tokens=4096`、
`{"search_mode":"fulltext","top_k":10,"context_window":0,"rerank_enabled":false,"use_rewrite":false,"score_threshold":0}`。

语料：冻结 `m2-manifest-v3.json` 的 80 个片段 / 4 个来源（与上轮 A/B 同一份）。
差异说明：`FileDocument.name` 用原文件名（`excel-2.xlsx`、`*.docx`、`hotpot-selected8.jsonl`），上轮 A/B 用的是标题；这只影响 `location` 显示，不影响检索内容。

（逐题结果见 §5.2，原始证据在 `var/acceptance-mini-20261104/step5/`。）

**复跑前提**：凭据文件已在验证结束后删除。要复跑，先重新从 ECS 取一次（不落库、不提交）：

```bash
ssh -i ~/.ssh/study-plan-dev.pem root@120.55.115.162 'python3 -' \
  > var/acceptance-mini-20261104/step5_credentials.json <<'EOF'
import json, sqlite3
con = sqlite3.connect("/opt/amrag/backend/local.db")
row = con.execute("""select base_url, api_key from custom_models
                     where model_type='LLM' and base_url like '%siliconflow%' and is_active=1
                     order by created_at limit 1""").fetchone()
print(json.dumps({"base_url": row[0], "api_key": row[1], "model": "Qwen/Qwen3.5-4B",
                  "credential_source": "ecs:/opt/amrag/backend/local.db:custom_models"}))
EOF
chmod 600 var/acceptance-mini-20261104/step5_credentials.json
```

### 5.1 执行概况

- 命令退出码 **0**（`step5-chat.log` / `step5-agent.log`）。
- provider 调用 **13 次**（chat 各 1 次；Agent 分别 2 / 4 / 4 次）。
- 用量合计：prompt **41,654** / completion **1,327** tokens（provider 返回值，非估算）。
- 费用：**UNKNOWN**。没有供应商账单，不能用历史 manifest 的单价推算。
- 每次调用都留了原始请求与响应：`step5/calls/NNN-request.json` / `NNN-response.json`。

### 5.2 逐题结果

| 题目 | 模式 | `answer_mode` | 核心答案 | 引用 | 全文忠实度 |
|---|---|---|---|---|---|
| 季度报告 | chat | `GROUNDED_GENERATION` | **PASS**（6 个 Gold 点，5 个明确 + 1 个隐含） | **PASS**（10 条，`[1][2][5][10]` 已核对） | **PASS** |
| 季度报告 | agent | `UNKNOWN` | **UNKNOWN**（未作答） | **MISSING** | N/A |
| Excel T07 | chat | `GROUNDED_GENERATION` | **PASS**（108,149.892） | **PASS**（`[1]` 已核对） | **PARTIAL**（自行补了 `$`） |
| Excel T07 | agent | `GROUNDED_GENERATION` | **PASS**（108,149.892） | **PASS**（`[1]`，分项数值与 IMLS 均核对到） | **PARTIAL**（自行补了「千美元」） |
| HotpotQA | chat | `GROUNDED_GENERATION` | **PASS**（Pieter van Musschenbroek） | **PASS**（`[4][8]` 已核对） | **PARTIAL**（补了 Coulomb 说法，来源无支撑） |
| HotpotQA | agent | `GROUNDED_GENERATION` | **PASS**（Pieter van Musschenbroek） | **PASS**（`[5][3]` 已核对） | **PASS** |

#### 季度报告（chat）

回答覆盖：任务进度状态、每任务完成百分比、2–3 句话的当前状态、accomplishments/problems、即使未开展工作也要列出、报告期间。
6 个 Gold 点里 5 个明确写出；「每个日历季度」只以「需确认报告所属的季度」隐含，没有点明「项目存续期间的每个日历季度」。核心仍判 PASS，但这一句不算完整覆盖。
引用核对：`[1]` 确实同时含 `even if no work` / `percentage completion` / `2 or 3 sentences` / `accomplishments and problems`；`[2]` 含 `Progress this Quarter`；`[9]` 含 `budget`/`problems`；`[10]` 含 `Quarter 2 (April 1 – June 30) X`。引用的内容与被引条目一致。

#### 季度报告（agent）

模型**没有调用检索工具**：`calls/003-response.json` 的意图分类为 `qa`，`calls/004-response.json` 直接给出「知识库中未找到相关信息」，`finish_reason=stop`、`tool_calls=0`。
系统没有放行这段无证据回答，而是替换为「知识库检索状态不确定，无法确认是否存在相关资料，请稍后重试。」，`answer_mode=UNKNOWN`、引用 0 条。
→ 安全性正确（没编造、没把无证据内容当答案），但用户拿不到答案。这是工具选择失败，不是引用映射失败。

#### Excel T07（chat / agent）

- chat：`108,149.892`，单位「(in thousands)」，命中 Gold；引用 `[1]` 含该数值与 `In thousands`。
- agent：额外给出 Federal/State/Local/Other 四项分项（`887.953` / `4,064.11` / `94,831.341` / `8,366.488`）和 `IMLS` 出处，均能在最终请求（`calls/008-request.json`）中找到。
- **两处补充的币种都没有来源**：两次最终请求里 `$` 出现 **0 次**、「千美元」**0 次**。chat 写了 `$108,149.892`，agent 写了「单位：千美元」。原文件确实是美元（见 §1.1），所以事实正确，但这是模型自己补的，不是证据送达的结果。
- 这条正好把 §1 的缺陷接到了下游：币种格式丢失 → 模型只能靠常识补。若换成非美元的表，同样的补法就会变成静默错误。

#### HotpotQA（chat / agent）

- 两模式都答 `Pieter van Musschenbroek`，与 Gold 一致；引用的 `[4]/[8]`、`[5]/[3]` 分别支撑莱顿瓶与摩擦仪。
- chat 版本多了一句「历史上摩擦仪常被归给 Coulomb」。最终请求里 `Coulomb` 出现 **0 次**，属来源未支持的补充；它没有改变最终答案，但按「补充内容单独核对」的口径要标出。
- 没有出现上一轮那种「开头说不是同一人、结尾又说是同一人」的自相矛盾。

### 5.3 一个执行层发现（待验证假设）

Agent 的评分节点（`agent_workflow.py:920`，`get_chat_model(temperature=0.1)` 默认流式）如果部署层没有关闭思考模式，会长时间不返回：
- 关掉思考（`enable_thinking=false`，与上轮 A/B 冻结配置一致）后，3 道 Agent 全部在正常时间内完成；
- 不传该参数时，两次独立尝试都卡在同一个 2.2k 字符的评分请求上，**5 分钟以上没有任何响应字节**，且外层 480 秒超时没有中断（阻塞读不在事件循环里）。
证据：`stall-evidence/012-request.json`、`stall-evidence/015-request.json`，以及 `stall-evidence/step5f.log`、`step5g.log`。
这不是本轮改动的回归，但属于「评分节点缺超时 + 默认流式」的实际可用性风险。

---

## 6. 判定口径

- 「通过」= 影响实际使用的事实或来源正确，且没有把不确定性说成确定。
- 「部分达成」= 核心功能可用，但用户可见信息缺少必要限定。
- 「不构成问题」= 结合源文件或调用链可解释，不影响事实与来源。

---

## 7. 结论与建议动手顺序

1. **Excel 币种送达（优先）**：`parser._parse_excel` 目前完全不读 `number_format`。本次实测里模型自己补了 `$`/「千美元」，恰好猜对；换成非美元报表就是静默错误。最小改法是解析时把表内出现过的货币符号与单位并入表头行，而不是逐格渲染。
2. **单路降级要让客户端看得见**：`retrieval_status` 仍为 `SUCCESS` 是对的，但 `done` 里应增加 `degraded: true`（或等价的 `fallback_used`），否则「利用已有证据」做到了，「如实暴露降级」没做到。
3. **Agent 评分节点加超时、改非流式**：本次两次独立卡死都在同一处（2.2k 字符的评分流式请求）。评分只要一个数字，没有必要流式，也不需要无限等待。
4. **Agent 工具选择失败只记录，不加硬校验**：季度题 1/3 没触发检索，是模型行为；系统已经正确地拒绝放行无证据回答。这类问题适合改工具描述/提示，不适合再加一层「必须调用工具」的硬门禁。
5. 旧的 A/B 结果仍不能与新代码直接对比：本轮 chat 全部 `GROUNDED_GENERATION`，而旧结果里有大量 `EXTRACTIVE_FALLBACK`，模态已经变了。

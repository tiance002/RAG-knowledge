# M2 第 8 节独立测试回执

日期：2026-11-05
依据：`docs/m2-mini-followup-20261104.md` 第 8.4 节
证据目录：`var/acceptance-receipt2-20261105/`（新目录，旧 `var/acceptance-remaining-20261105/` 未改动）

## 1. 范围、版本与产品哈希

- 分支 / HEAD：与交接文档一致，HEAD `57f9972b`。
- 本轮只验证第 8.3 节两处“实现完成、NOT RUN”的改动，另做一次检索诊断。
- **产品代码零改动**：两处改动文件的 SHA-256 与 `var/m2-remaining-20261105/receipt-followup/after-hashes.json` 完全一致：

| 文件 | 当前 SHA-256（前 16 位） | 与归档 |
|---|---|---|
| `frontend/src/services/chat.ts` | `E2A6786F6FF64D5B` | 一致 |
| `backend/app/services/rerank.py` | `CDAEE354680ECE45` | 一致 |

- 本轮唯一源码改动在测试侧：`backend/tests/test_e12_native_observability.py` 的 origin 断言由 `'none'` 改为 `'original_score'`，并把 `rerank_score is None`、`score == .6` 拆成独立断言。
- 云端模型调用 **0 次**，未读取凭据，未启动 Ollama，未做 Cloud 回读。

## 2. 命令与退出码

| 步骤 | 命令 | 退出码 |
|---|---|---|
| E1 打包 | `./frontend/node_modules/.bin/esbuild frontend/src/services/chat.ts --bundle --format=esm --platform=browser --define:process.env.NODE_ENV='"production"' --outfile=var/acceptance-receipt2-20261105/e1/chat.bundle.mjs --alias:@=./frontend/src` | **0** |
| E1 运行 | `node var/acceptance-receipt2-20261105/e1_frontend_empty.mjs` | **0**，17/17 |
| E2 改前 | `pytest backend/tests/test_e12_native_observability.py::test_failed_rerank_batch_marks_observed_score_origin_without_changing_results` | **1**（预期失败，见 §4.1） |
| E2 改后 | 同上 | **0**，1 passed |
| E2 字段证据 | `./.venv/Scripts/python.exe var/acceptance-receipt2-20261105/e2_export_evidence.py` | **0** |
| E2 整文件 | `pytest backend/tests/test_e12_native_observability.py` | **0**，15 passed |
| E1 类型检查 | `cd frontend && ./node_modules/.bin/tsc -b --pretty false` | **1**（既有 45 条，改动文件 0 条，见 §6） |
| E3 检索诊断 | `./.venv/Scripts/python.exe var/acceptance-receipt2-20261105/e3_retrieval_diag.py` | **0** |

所有 pytest 均使用 `--basetemp="var/.pt-*-$(date +%s)"` 的全新目录。

## 3. E1：前端 EMPTY 终止提示（17/17 PASS）

重打包当前 `chat.ts`，以假 `fetch` 注入 SSE 字节流，直接调用产品 `createChatStream`。原始结果：`e1/e1-frontend-empty.json`。

| 场景 | 结果 |
|---|---|
| EMPTY + 安全摘录 | 正文保留；`rag_result` 2 条引用（`word-paragraph-6`、`excel-sheet-T07`）保留；错误回调恰好 1 次且 `status=EMPTY`；**不调用 onDone**；fetch 1 次 |
| EMPTY 无正文 | 错误 1 次且 `status=EMPTY`；正文为空、无 `answer_chunk`（不捏造摘录）；不调用 onDone；不重发 |
| 正常 `COMPLETED` | onDone 1 次、无错误、正文完整 |
| `NOT_RUN`（纯记忆/范围预检） | onDone 1 次、无错误、正文保留（未被当成失败） |
| 旧 done（无终态字段） | onDone 1 次、无错误、正文保留 |
| `TRUNCATED` | 错误 1 次、摘录保留、不调用 onDone、不重发 |
| `UNKNOWN` | 错误 1 次、无 done、不重发 |

结论：第 8.3 节第一处改动的**接线正确**——EMPTY 进入既有终止提示路径，但不吞已送达正文/引用、不冒充完成、不自动重发；其余五类终态未被波及。

浏览器实际渲染 **NOT RUN**（按方案允许）；`tsc` 对该文件零诊断，见 §6。

## 4. E2：失败重排 `origin=original_score`（1 passed + 字段证据）

### 4.1 改前断言确实因产品值变化而失败

改前日志 `e2/before-assert.log`：

```
assert failed['rerank_score'] is None and failed['fallback_score_origin']=='none'
AssertionError: assert (None is None and 'original_score' == 'none')
E       - none
E       + original_score
```

说明不是“改断言凑绿”：产品实际导出的就是 `original_score`，旧断言固化了 `none`。

### 4.2 改后语义逐项核对

`e2/export-evidence.json`（同一测试替身、native 导出）：

| 字段 | 失败候选 `failed` | 成功候选 `scored` |
|---|---|---|
| `score`（原检索分） | **0.6** | 0.2 |
| `rerank_score` | **null** | **0.95** |
| `rerank_status` | **UNKNOWN** | — |
| `score_kind`（导出） | **original_retrieval** | **rerank** |
| `score_value`（导出） | **0.6** | **0.95** |
| `fallback_score_origin` | **original_score** | 无 |

- 观测开 / 关两次 `batch_rerank` 返回完全相等（`observability_on_equals_off=true`），最终顺序均为 `scored → failed`，未因遥测字段改变排序、过滤或正文。
- `frontend`/`rerank` 的排序逻辑本轮未改；失败候选仍按 `rerank_score=None` 排在成功候选之后，原分只用于展示/导出，不伪装成 Rerank 分。
- 注：导出里的 `rank` 是 `rerank.batch_output` 排序前的输入顺序（`failed` 在前），这是既有观测语义，不是本轮改动。

E12 整文件 **15 passed**，没有其他用例依赖该 origin 字段。

## 5. E3：检索诊断（1 英文原查询 vs 3 中文改写）

### 5.1 条件

- 复用 `var/acceptance-remaining-20261105/p1p2/app.db` 的冻结 KB/索引；运行期临时复制到 `e3/app.db`，SHA-256 完全相同（`29AE01FC…72DB6C`），未重建、未重建整库。运行后按第 8.5 节“不保留数据库/索引副本”删除临时副本（`e3/copy-note.json`），原件哈希与大小未变。
- KB `m2-ab-kb`，80 chunks，`embedding_model=disabled`；`search_mode=fulltext`、`top_k=10`、`score_threshold=0`、`rerank_enabled=false`、`context_window=0`。
- 真实入口：`POST /api/v1/knowledge-bases/m2-ab-kb/recall`；LLM 指向关闭端口，未调用生成；uvicorn 0 条 ERROR。
- 查询直接取自归档 tool_calls：`p1p2/proxy/calls/002/006/007/008-response.json`，未手改写。

### 5.2 结果

| 查询 | 词数 / 命中词表 | 候选数 | `retrieval_status` | `word-paragraph-6` 排名 |
|---|---|---|---|---|
| 英文原句（002） | 11 / **10** | 10 | `SUCCESS` | **第 1 名**（42.925） |
| 中文 1（006） | 25 / **0** | 0 | `NO_RESULTS` | 无 |
| 中文 2（007） | 13 / **0** | 0 | `NO_RESULTS` | 无 |
| 中文 3（008） | 17 / **0** | 0 | `NO_RESULTS` | 无 |

英文查询前 5 名：

```
1 word-paragraph-6      42.925
2 word-table-4-row-0    17.153   Progress this Quarter
3 word-paragraph-1      15.572   QUARTERLY PROGRESS REPORT
4 word-paragraph-9      11.219   Project schedule status
5 word-paragraph-10      7.086   On schedule / Behind schedule
```

- 目标片段自身包含 `quarterly / progress report / percentage / schedule / status / task` 六项。
- 英文 top-10 覆盖 7/8 个季度与表头字段（缺 `next quarter`），并含 4 条 `word-table-*` 表头/表体片段（`word-table-4-row-0`、`word-table-0-row-4`、`word-table-6-row-1`、`word-table-0-row-0`）。
- 中文 3 条全部 0 候选，**不是“目标排在 11 名以后”**，而是整个查询与英文词表零重叠。分词实际输出为单字（`季/度/进/度/报/告…`）。

### 5.3 机制与限制

- 这是 **fulltext-only + 英文语料** 的结果：2,348 个词表 token 中中文词为 **0**，中文 query token 与词表交集为 0，因此三种问法都直接 `NO_RESULTS`。
- 环境观察：`requirements.txt:60` 声明 `jieba==0.42.1`，当前 venv **未安装**，中文按单字降级切分。即使安装 jieba，中文词（如“季度”“进度”）也不在英文词表内，本组零命中的结论不变；该缺包是独立环境问题，本轮未安装，以免改变冻结运行条件。
- **hit@10 只针对 `word-paragraph-6` 一个目标片段，不是完整 qrel 的 recall@10**；没有 Gold 标注，不能外推为整体召回率。
- Hybrid 对照 **NOT RUN**：KB embedding 被禁用、无可用向量索引；不声称多语向量能修复，也不临时外发 Embedding。

### 5.4 对“复用原查询 q0”候选的判断

在同一 fulltext 配置、同一 KB、同一 top_k 下：

- 英文原查询把目标片段送到 **第 1 名**；
- 模型实际使用的 3 条中文改写均为 **0 候选**；
- 损失发生在**查询文本与词表的匹配层**，不是排名靠后，也不是 Gated 阈值。

因此，本次诊断**确认了“查询改写导致可恢复的证据损失”**：若在改写查询零命中时用原问题 q0 再检索一次，本配置下能恢复目标证据。该“q0 后备检索”仅作为**单因素候选**提出，本轮未实现、未改变检索算法或在线策略；生产 hybrid/多语向量是否同样失败仍为 UNKNOWN。

## 6. 未执行项与限制

| 项 | 状态 | 说明 |
|---|---|---|
| 浏览器实际渲染 | **NOT RUN** | 只验证产品 transport 的 Node 运行时；未启动浏览器 |
| `vite build` | **NOT RUN** | `tsc -b` 仍为既有 45 条错误，与上轮**同数同集合**（逐行比对一致）；`services/chat.ts`、`types/chat.ts` **0 条诊断**，`pages/Chat/index.tsx` 的报错为既有项 |
| Hybrid 检索对照 | **NOT RUN** | 无向量索引，embedding 禁用 |
| 全量非 live 套件 | **NOT RUN** | 按方案无需重跑；本回执不更新全量 PASS/FAIL |
| Cloud 回读 / Langfuse | **NOT RUN** | 本轮零云端调用 |
| E3 qrel recall@10 | **不可计算** | 只有一个目标片段，按 hit@10 记录 |

费用：本轮 **0**（无模型调用）。资源：只新增 `var/acceptance-receipt2-20261105/`，旧证据目录未覆盖；临时数据库副本已删除，未复制原始请求或索引。

## 7. 结论

1. **EMPTY 提示接线通过**：有摘录时保留正文/引用、提示一次、`onDone` 不再冒充完成、不重发；无正文时不捏造内容；正常、`NOT_RUN`、旧 done、`TRUNCATED`、`UNKNOWN` 均保持原行为。
2. **失败重排字段语义通过**：失败候选 `rerank_score=None`、`UNKNOWN`、原分 `.6` 保留在 `score`/`score_value`，`score_kind=original_retrieval`，来源改为 `original_score`；成功候选仍 `.95`；观测开关结果一致，未改排序/过滤/正文。
3. **检索诊断给出可执行方向**：英文原查询 hit@1，三条中文改写 0 候选，属于可恢复的证据损失；下一步值得做“改写零命中时用原问题 q0 后备检索”的单因素实验，但本轮不实现、不宣称生产可修复。
4. P2 的补充事实、最终生成超时、全量红灯仍为未关闭项，本回执不改变其状态。

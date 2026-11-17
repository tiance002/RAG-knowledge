# E1 Langfuse Dataset 与历史答案回放验收（2026-10-29）

本轮 Dataset、SDK Experiment 和 Cloud 正文回读 **PASS**；UI 正文显示及 Evaluator 变量选择操作 **BLOCKED**，不能将 API 回读代替 UI 验收。未创建 Judge、Evaluator、Evaluation Rule 或 Score，未调用 RAG 或评分模型。

## 已创建对象

- [Dataset：rag/e1-judge-calibration-v1](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/datasets/cmvtnd1s701ndad0ee0fb3j73)，5 个 ACTIVE Item。
- [Experiment：e1-historical-replay-20261029-v2](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/experiments/results?baseline=807706243468622c)，ID `807706243468622c`，5 个 Item、0 Scores。
- [中文 UTC 问题的评测 Observation](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/41966b51db8027c6edd0f4778cdd84fe?observation=3d7cf93996d607ec)。
- [人工 FAIL：反射面题](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/7b405d4b353846c48f50a36ec2db4575?observation=48be531809a77cac)。
- [人工 FAIL：人脸神经网络层数题](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/a6b7731858b1536e2a0a8300e027f4c4?observation=28555436b758b0d2)。

## 样本、授权与冻结证据

| qid（长 ID 以下用唯一后缀展示） | 冻结 Gold | 本轮有效人工结论 | 来源 |
|---|---|---|---|
| office50:001 | Stormwater Management to Address Highway Runoff Toxicity Due to 6PPD-Quinone from Tire Rubber | PASS | DOCX，table 0 / row 1 / cell 0 |
| office50:011 | 7% | PASS | DOCX，table 1 / row 1 / cell 2 |
| mmlongbench-doc / row-0958 | 原值 `["['shiny object', 'mirror']"]` | FAIL | PDF 物理第 9 页 |
| mmlongbench-doc / row-0431 | 5（包含输入层） | FAIL，新补充人审 | PDF 物理第 29 页 |
| longbench-zh / 9938c97e6ccf006c5fc66ea3294b9835e1584d95713576a9 | 原冻结 UTC+0800 / 北京时间换算文字 | PASS | 冻结 Dureader corpus 对应记录 |

共 3 人工 PASS / 2 人工 FAIL，没有把 AI 复核冒充人工结论。原 `ledger-v4.jsonl` 中 row-0431 的人工 UNKNOWN 原样保留；用户本轮回复“人工判 FAIL”保存在 `supplemental-human-review.json`，以 qid 和 answer_hash 绑定；Cloud Dataset 与回放 metadata 同时保留原记录及新补充审核。

用户本轮明确回复“明确允许这 5 题及上述字段上传”，授权范围是这 5 道原问题、冻结 Gold、完整历史答案、来源和审核元数据，目的地为现有美国区项目 `cmuido3u800zyad0con8gaqw1`。`authorization.approved.json` 绑定本地冻结文件哈希及新增人审；`authorization.pending.json` 和原 dry-run 保留为授权前快照，原 BLOCKED 字样不代表当前授权仍缺失。

授权前 dry-run：5 个 Dataset Item 的规范化 UTF-8 JSON 合计 5,914 bytes；5 个评测 Input/Output 的规范化 JSON 合计 4,565 bytes；这不是 HTTP/OTLP 的线路字节数。逐项大小、来源身份、答案 SHA-256 在 `dry-run.json`。扫描凭据、邮箱、身份证号、手机号和本地绝对路径，候选内容上述模式均为 0。人工检查发现历史答案包含报告项目负责人的姓名和项目金额，已在授权问题中明示。模式扫描不等于完整隐私审计或来源授权；上传依据是用户本轮明确授权。

本地独立回放账本 `historical-answers.jsonl` 记录 qid、run_id、原 Trace ID、完整原答案、answer_hash、SSE/result/events 路径及哈希、历史模型、代码版本和 Prompt 快照身份。没有重新生成或修订 Gold，没有改写答案。

## 版本与证据边界

- 本轮代码 HEAD：`722f000bd874d1fa17bfe94385612f045218f74e`；原本就存在未提交的正式服务文件改动，本轮没有编辑这些文件。
- 历史应用版本：`app-sha256:521b765ceb407c2be5b7b4166f482b16c0c4685d913dd024128071592ce080d8`，来源为原 preflight 与 E1 报告；不是声称当前 HEAD 等于历史运行源码。
- 历史模型：`zai-org/GLM-5.3-Flash`；原配置为 fulltext、grounded generation on、rewrite false；报告记录 top_k 20、未启用 Rerank。本轮 task 不运行该模型或检索配置。
- E1 候选文件 SHA-256：`391fa027948b01b9291b992b1a8d91e812a6934a0241e0a2f137e20a0853d175`。
- 原 ledger-v4 SHA-256：`a4bf3478616fe2a2c4d2cf8818cf33d31f3996c3dffdebd77b5094774b60a09a`，交付前重核保持一致。
- 原 30 道 SSE 和 result 文件共 60 个哈希全部复验；前 24 道的 events 哈希也与旧账本一致。后 6 道未在旧账本记录 events 哈希，本次所选 row-0431 的原事件文件只在新回放账本新增冻结，明确标记 `NEW_FREEZE_OF_EXISTING_EVENTS`。
- PDF 两份原文件本轮重新计算 SHA-256，与既有页审查身份一致。DOCX/text 依赖已有冻结来源审查，不声称本轮重新读取远端原文件或重新验证全部真实世界事实。UTC 样本的人工 PASS 仅指核心答案，旧答案中其他补充断言仍按原样保留。
- 历史 Langfuse Prompt 版本未捕获，记为 `NOT_CAPTURED`；保存并回读了原 Prompt 快照、完整 messages 和 template prefix 的 SHA-256，没有猜测版本号。

## 官方 SDK 回放与零模型调用

使用当前隔离环境已安装的 Langfuse **5.0.4** / OpenTelemetry SDK **1.45.1**。读取新建 Cloud Dataset 后，对全部 5 道 input、expected_output、metadata 做严格比较，再调用官方 `DatasetClient.run_experiment`，并将 `evaluators=[]`、`run_evaluators=[]`。

task 只读取独立历史答案账本，返回 `{"answer": 原历史答案}`，不导入正式 RAG app 或供应商客户端。为每题新建 `eval.review_case`（SPAN），Input 是真实 `question` 与原冻结 `reference_answer`，Output 是原始 `answer`。Gold 只用于 replay Observation，不调用被测 RAG。

每个回放 Trace 的三个节点是 `experiment-item-run`、`experiment-item-task`、`eval.review_case`。metadata 明确标记 `execution_type=HISTORICAL_REPLAY`、`real_model_run=false`、`external_model_calls=0`；保留原 Trace ID、历史模型、Prompt 快照哈希、answer_hash、来源审核身份。Cloud 会把嵌套 metadata 展平为点分键，例如 `historical_prompt.prompt_version`、`review.supplemental_human_review.human_core`；已按此实际形态验证。

回放进程的 DNS 和 socket 守护仅允许 `us.cloud.langfuse.com:443`，其余外部端点阻止；Windows asyncio 的内部 socketpair 仅供本地唤醒，不是外部模型通信。成功 v2 进程仅对自身设置 NO_PROXY 直连 Langfuse，没有修改系统代理或正式服务。阻断次数 0。Cloud 逐题回读也确认没有 Generation、Embedding 或 Evaluator 节点；Experiment/Item Scores 均为空。结合代码路径、出站守护和 Cloud 证据，本轮 Chat、Embedding、Rerank、VLM、Judge 调用均为 **0**。

创建 Dataset 和每次回放前，项目 Evaluation Rules 查询返回空列表。本轮没有创建 Judge、Evaluator、Rule 或 Score，也没有调用评分测试。

官方资料：[Experiment Runner](https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk)、[Observation 评分输入与映射](https://langfuse.com/docs/evaluation/evaluation-methods/llm-as-a-judge)。

## Cloud 回读和 UI 状态

| 检查 | 结果 | 证据 |
|---|---|---|
| Cloud Dataset 内容与授权后本地结构一致 | PASS，5/5 | dataset-readback.json |
| Experiment 实际存在、Item 数正确 | PASS，1 个 Experiment / 5 个 Item | cloud-cli/experiment-final.json、experiment-items-final.json |
| eval.review_case 正文存在并逐字一致 | PASS，5/5，含中文 | cloud-cli/replay-observations-*.json |
| question / reference_answer / answer 不是 undefined 或只有哈希 | PASS，5/5 API 完整正文与冻结内容一致 | final-evidence-audit.json |
| 原 Trace 的本地事件关联 | PASS，5/5 | 历史 SSE/result/events 与新回放账本 |
| 原 Trace 的当前 Cloud 身份查询 | 4/5 返回；office50:011 UNKNOWN | original-trace-verification.json |
| Langfuse UI 中实际阅读正文 | BLOCKED | Computer Use 工具停止，见下文 |
| UI 中实际选择三个变量 | BLOCKED | 未完成 UI 验收，不以 API 可用冒充点击成功 |
| 自动 Judge 评分 | NOT RUN | 用户要求保留 UI 自行配置；0 Scores |

`office50:011` 的原 Trace ID `f55f96da152944348b755015b948deba` 在旧本地事件和结果中一致，但本轮 Cloud 查询 HTTP 200、data=[]，原因 UNKNOWN。没有伪造或替换 Trace ID；新回放 Trace 与原本地历史答案关联完整。其余四道原 Trace 身份在当前 Cloud 返回。原 Cloud Trace 内容经常是脱敏引用；旧答案真实性仍以原始 SSE 和冻结哈希证明。

UI 工具先返回 `Debugger unattached`，随后明确停止：`Computer Use has been stopped for this turn because it could not determine the current browser URL on Windows with enough confidence to enforce policy.` 因而停止 UI 自动化，未继续绕过该检查，也没有操作用户已有的 Evaluator 草稿。

用户在 UI 完成验收时，选择 Observation 名 `eval.review_case`，映射如下。字段已在 Cloud 正文中验证存在，具体 UI 下拉框操作尚未验证。

| 变量 | 数据源 | 字段 |
|---|---|---|
| `{{question}}` | Input | `question` |
| `{{ground_truth}}` | Input | `reference_answer` |
| `{{answer}}` | Output | `answer` |

若界面使用 JSONPath 输入，路径分别为 `$.question`、`$.reference_answer`、`$.answer`。保留 reference_answer 的原 JSON 数组形态，不把多字段 Gold 拍平成重写答案。建议使用环境/metadata 限定本次历史回放，先选代表题人工核对映射预览；本报告没有替用户保存或运行评分器。

## 命令退出码、失败和证据审查

| 执行 | 退出码 | 实际结果 |
|---|---:|---|
| prepare（最终 r2） | 0 | 本地 dry-run、冻结导入文件；授权前 BLOCKED 是正确业务状态 |
| cloud-import | 0 | Dataset + 5 Items 创建；各写入 CLI 退出 0 / HTTP 200 |
| 第一次 cloud-replay | 1 | OTLP 导出经本机代理被守护拦截；原 stderr、退出码保留 |
| cloud-replay --attempt v2 | 0 | 5 个新 Trace；API 回读证实正文到达 |
| unittest test_e1_historical_replay.py | 0 | 8/8 passed；0 skipped；无失败 |
| SDK 最终离线验收 sdk-check | 0 | 真实 SDK Runner、内存 Dataset/Exporter；5 个评测 Observation、15 spans、0 外网 |
| verify_saved_evidence.py | 0 | 审查已保存的真实 CLI 响应、SDK 输出、逐字段内容、Scores 和退出码 |
| 最终原始 60 文件哈希复核 | 0 | 60/60 PASS |

首次导出失败保留在 `cloud-replay.stdout.log`、`cloud-replay.stderr.log`、`cloud-replay.exit.json`；不覆盖成 PASS。按 v1 名查询 Cloud Experiment 返回 0 个，独立成功 v2 返回 1 个；没有将 v1 失败称为真实模型 UNKNOWN，也没有重发旧模型 UNKNOWN 请求。

已检查最终测试逐项输出、实际退出码、日志是否为空、是否有 Traceback/错误或截断、统计分母、版本与证据来源。unittest 正常结果位于 stderr（8 条 ok、Ran 8 tests、OK），不是异常；成功 SDK/Cloud v2 stderr 为空属预期，stdout 非空且完整。原始 CLI envelope 和各命令 exit.json 保留在 cloud-cli/；Dataset 预先不存在的 GET 退出 5 / HTTP 404 是创建前证据，不作为上传失败。探索阶段一次 `projects get` 命令不受支持、退出 2，之后按 CLI schema 使用 `projects list` 验证项目身份，原诊断文件保留。

本地离线 SDK 验收使用的只有 Project GET 的内存 HTTP 替身和内存 Dataset 对象，不能替代 Cloud 验收；Cloud Dataset、Experiment、5 个 Observation 和 5 个 Experiment Item 均已另外真实读回。没有将执行成功当作评分正确。

本轮新增 `scripts/e1_historical_replay.py` 和 `backend/tests/test_e1_historical_replay.py`，其余产物位于 `var/e1-historical-replay-20261029-r2/`。var 按仓库既有规则被 Git 忽略。没有修改正式 RAG 算法、Prompt、检索配置、Trace 正文脱敏策略或线上服务；没有覆盖旧 Trace、Scores、E1 账本，没有提交、推送或部署。

整体交付状态：**数据及回放链路 PASS；完整 UI 验收 BLOCKED；office50:011 原 Cloud Trace 当前可用性及历史 Prompt 版本 UNKNOWN / NOT_CAPTURED；Judge 评分 NOT RUN。**

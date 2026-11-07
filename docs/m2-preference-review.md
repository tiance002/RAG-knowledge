# M2 用户偏好记忆专项验收报告

审查日期 2026-11-04。GitHub `rag-knowledge/codex/pdf-parser-migration` 为 `23a0e05b5c90c3197f633efdd0a8ce3c73eda09d`，父代 `09a3146d04e9c0c9f2cd1feed842c86e8af2ef74`；该提交仍在远端分支。GitHub 主分支为 `42bf2ab3bfd3b84dda56510d96e0afa9e506c6c2`，上游 origin 主分支为 `b5ceb463a433193a908e1d814b221c855f8717d0`。本地功能分支 HEAD 为 `57f9972b8477b0c4aaf7fac329b911420fafc95e`，有大量未提交修改，本轮保留。当前工作区中的命令识别已包含开始前未提交的修改，需要与远端区分。

## 测试计数和口径（初次运行；后续新增用例见文末）

历史 114 项由 9 项服务、46 项聊天、7 项 RSI、52 项其他测试构成；其中专项 62 项恰为 9+46+7，不是额外 62 项。历史另有两项真实 Redis/HTTP/浏览器集成，不含在 114 项内。文件角色可能同时覆盖多个场景，下表按测试文件计数，不能再把普通模式与 Agent 场景重复相加。

| 范围 | 测试文件 | 收集 | 通过 | 失败 | 跳过 |
|---|---|---:|---:|---:|---:|
| 偏好服务和 API | test_memory_service_api.py | 9 | 9 | 0 | 0 |
| Explicit、普通和 Agent（同文件） | test_memory_chat.py | 46 | 46 | 0 | 0 |
| 新增明确指令和覆盖 | test_m2_explicit_acceptance.py | 33 | 33 | 0 | 0 |
| RSI 文件契约 | test_rsi_playbook_contract.py | 7 | 7 | 0 | 0 |
| 真实 Redis 和前端 | test_memory_live.py | 2 | 2 | 0 | 0 |
| 本轮 RAG 回归 | test_normal_evidence_guard、test_agent_quality_outcomes、test_agent_request_parameters、test_e20_context_delivery、test_retrieval_outcomes、test_retrieval_cache_safety、test_rerank_unknown、test_pdf_pipeline、test_pdf_table_validation、test_model_router | 134 | 134 | 0 | 0 |

离线命令和逐题结果来自 `var/preferences-acceptance/m2.log` 与 `m2.xml`，退出码 0，229 项通过。真实实例日志在 `m2-live.log`，退出码 0，2 项通过。以上是当前工作区，并非对远端提交的重新构建。前次隔离远端验证的 62 项结果在 `push-verify.log`。

## 发现与修复

| ID | 状态 | 位置 | 证据与影响 |
|---|---|---|---|
| M2-01 | 已修复 | memory_commands.py 的 resolve_display_preferences | 长期先讲原理与“本次直接给结论”同时产生冲突指令。修复前专项 32 通过、1 失败；识别明确的结论优先后全通过。新增的分步偏好仅在流程、操作和计算类问题适用 |
| M2-02 | 未完全验证 | agent_workflow.py 的 agent_node | 工具选择与回答可能共用模型请求；替身两轮图和真实意图请求对照未发现意图输入差异，不能证明真实模型工具选择完全不受影响。真实样本存在未检索路径，是否为既有路由行为需另行定位 |
| M2-03 | 部分修复 | chat.py、agent_workflow.py、grounded_generation.py 的观测事件 | 已增加 memory.delivery 事件，在普通流式、普通有证据生成、Agent 联合工具/回答、Agent 闲聊调用入口记录 preference_policy_applied。实际 Langfuse SDK 内存导出测试确认字段保留，SDK 本地导出和真实请求构造均有记录；合成事件已完成 Cloud 回读；真实模型链路的 Cloud 回读未完成，构造事件不等同供应商成功收到 |

明确写入、修改、删除、默认、混合请求、否定、引用及模糊表达由新增 33 例和原有测试覆盖。普通和 Agent 使用替身捕获请求，已有重复检索图检查，真实小样本语义结果见下文，不能证明所有真实模型路径均不会受影响。

## Redis

独立测试为 Redis 7.0.15，监听临时回环端口，配置为 appendonly yes、appendfsync everysec、save 900 1 300 10、maxmemory 0、noeviction、protected-mode yes。测试使用临时密码和数据目录。真实 AOF 重启、RDB 备份独立恢复、恢复后启用 AOF 再次重启均通过。服务测试覆盖不可连接、读超时、写失败、已写入但回读失败；聊天和 Agent 使用替身确认读失败后继续回答。应用默认复用 REDIS_URL，未指定 MEMORY_REDIS_URL 时可能与 Celery 共用同一实例，本地未见默认端口监听。ECS 实例配置与备份安排未验证。

## RSI 文件规范

只读校验器验证字段、ID、证据引用、修订链、独立回执格式、损坏行与可信摘要。它不验证事实真伪或真正独立性。规范引用现有 RSI-kit 的 registry、manifest、run_id，并未接入新引擎。进入 validated 需要可核查的独立回执，模型自评不能替代独立验证。

## 真实模型 A/B：限定语料、小样本和计费边界

状态 **PARTIAL**。负责人批准 GLM-5.3-Flash，总费用不超过 1 元。价格参照硅基流动公开价格：输入 0.8 元/百万 Token，输出 2.8 元/百万 Token；以下是按回执用量和该价格计算，不是供应商账单。截至本报告最初 A/B 和补充实验，共 221 次已完成供应商调用、0 次结果未知的正式受控调用；输入 605,945 Token、输出 75,714 Token，计算金额 **0.6967552 元**，实际账单 **UNKNOWN**。此前一次替身联调存在误发无效凭据并返回 HTTP 401 的事件，无成功推理，精确尝试数和账单未知，不隐瞒也不混入上述受控调用。未重试已有结果 UNKNOWN 的请求。

冻结计划为 6 题 × 两模式 × 四偏好组 × 两次，96 项。第一次汇总时已启动 91 项，其中 88 项有回答、3 项未完成，另 5 项 NOT RUN；其后的五项续跑结果见文末。三项没有答案不得计为正确拒答或普通模型错误。按原始答案逐项审查，88 个有回答样本中核心 PASS 82、PARTIAL 5、FAIL 1；这不是全面正确率的无条件估计。五题沿用原有 Gold，一题依据原文新建，精确身份见清单，不将模型自己的判断代替原文。以下表格是每组**已记录**样本，括号为未完成/UNKNOWN；每组计划 12，未开始另计。

| 模式 | 组 | 已记录 | 核心 PASS | PARTIAL | FAIL | UNKNOWN |
|---|---|---:|---:|---:|---:|---:|
| 普通 | A 默认 | 12 | 11 | 1 | 0 | 0 |
| 普通 | B 详细 | 12 | 11 | 0 | 1 | 0 |
| 普通 | C 初学者 | 12 | 12 | 0 | 0 | 0 |
| 普通 | D 分步 | 12 | 12 | 0 | 0 | 0 |
| Agent | A 默认 | 11 | 9 | 1 | 0 | 1 |
| Agent | B 详细 | 11 | 9 | 1 | 0 | 1 |
| Agent | C 初学者 | 11 | 8 | 2 | 0 | 1 |
| Agent | D 分步 | 10 | 10 | 0 | 0 | 0 |

计分按冻结标准：5 个 PARTIAL 因技术题的“磨损量”不能确认覆盖 Gold 的“磨损体积”；1 个 FAIL 在不可回答题先承认证据缺失，随后仍补充了无证据的 White/Black 比较。问题本身和已收到结果不能因不利表现排除。审查仅限此语料和记录。

以下是五道有答案题的两次重复，即每组 10 条；引用是**核心答案涉及的预期证据编号召回**，不是全文 Citation Precision。Context Recall 只对已能判定的实际送达证据列明，未覆盖项不能直接视作 0 或 1。统计为初步观察，毫无统计显著性结论。

| 模式/组 | 输入/输出 Token 平均 | 平均耗时秒 | 平均工具调用 | 平均检索重试 | 核心编号引用召回 | 核心证据送达 |
|---|---:|---:|---:|---:|---:|---|
| 普通 A | 3601 / 279 | 6.31 | 0 | 0 | 10/10 | 可判定 6/6，其余未知 |
| 普通 B | 3655 / 799 | 16.30 | 0 | 0 | 10/10 | 可判定 6/6，其余未知 |
| 普通 C | 3656 / 511 | 11.23 | 0 | 0 | 9/10 | 可判定 6/6，其余未知 |
| 普通 D | 3601 / 371 | 8.24 | 0 | 0 | 10/10 | 可判定 6/6，其余未知 |
| Agent A | 7907 / 889 | 22.78 | 0.8 | 0 | 0/10 | 可判定 2/2，其余未被识别为完整送达或未知 |
| Agent B | 8665 / 1186 | 28.89 | 0.7 | 0 | 0/10 | 可判定 2/2，其余未被识别为完整送达或未知 |
| Agent C | 9480 / 891 | 21.32 | 0.7 | 0 | 0/10 | 可判定 2/2，其余未被识别为完整送达或未知 |
| Agent D | 8928 / 938 | 21.52 | 0.7 | 0 | 0/10 | 可判定 2/2，其余未被识别为完整送达或未知 |

模型调用耗时与输出变长并不能单独证明质量变化。各类引用支持审查口径不完全一致，不能把编号匹配直接升级为全文引用精确率；全文 Citation Precision **UNKNOWN**。逐条审查显示部分补充事实未受所引片段支持。PDF 图表的原图支持正确答案，但提取到模型的扁平文本没有可靠行列对应，证据实际送达为 UNKNOWN，不能根据页号认定模型获得图表关联。Word 测试夹具把一个合并单元格当作四个物理单元格读出，个别补充内容因此失真；这是样本构造缺陷，须保留在解释中。

Hotpot 发明者题的 Agent 8 次中 7 次走闲聊路径，技术题 8 次全部走闲聊路径；两题相应意图阶段请求在四组相同，偏好未进入该阶段，但实际语义隔离不能仅凭一次小样本证实。碰巧答对不等于检索证据支持；Agent 缺少编号引用，不判成有证据正确。该现象尚未证明由本轮记忆改动引起，不借此改动正式检索或评分阈值。

六题中分步偏好适用条件均未触发；额外使用同原文的独立操作型开发问题做 A/D、普通/Agent、各两次，**不并入原 96 项**。8 条均出现三个指标，但其中三条为原文摘录回退，不等于完整操作回答；普通 D 的两条呈现步骤，Agent D 两条回退均未形成步骤。完整请求证实四条 D 均收到偏好，附加建议是否准确 UNKNOWN。这只说明本例送达及有限样本表现。

实验入口为 `ChatService.chat_stream` 的异步事件，**并未经过真实 HTTP/SSE 网络层**；另有独立替身 HTTP/浏览器测试。语料冻结为 `var/preferences-acceptance/m2-manifest-v3.json`（SHA256 `7b5524c8bfd1e8bbbef6298f6950fa4a6cc2462fe336789ad66639417e5e75d3`），包括完整 Word 段落/表格、Excel T07、一个 Hotpot 原始题全部候选段及 PDF 45 页抽取文本，共 80 片段。题目为 `office50:001`、`office50:031`、`hotpotqa:5a729fe55542991f9a20c53b`、`mmlongbench-doc:...:row-0684`、`mmlongbench-doc:...:row-0685`、`m2:tribometer-mechanism`。五题沿用 Gold，一题根据原文新建。局部开发语料不代表生产全库。真实零生成预检五道正题 BM25 前 10 命中分别 1/1、1/1、2/2、1/1、1/1；页命中不等于图表字段完整。

对照固定在 fulltext/BM25、top_k=10、context_window=0、不启用重排/改写、实验输出上限 4096、同模型同语料；这是隔离实验配置，不代表生产默认 Hybrid。每次请求、候选、Trace、本地审查、结果和 token 回执保存在 `var/preferences-acceptance/m2-real-20261104-r1`、`m2-cont-real-r1`、`m2-cont-real-r2`；不得提交私有正文。聚合 `m2-analysis-final-20261104/analysis.json`，逐题独立审查 `review-*.jsonl` 和 `independent-review-*.md`。真实模型 A/B 运行显式关闭了 Langfuse，因此其 Cloud 回读 **NOT RUN**；本地 SDK 实际内存导出 PASS。另有合成 Cloud 事件回读，见下节，不代表真实模型运行已上传。

首次受控运行第 73 题和首个续跑的第 17 题停止，保留真实错误、无最终答案。对首次停止现场的零网络请求逐个重放 155–159，发现下一请求的保守预留加累计费用超出 1 元；LangChain 将预算拦截包装为 `AttributeError: GuardStop ... generations`。该异常不能归因为业务问题。第二次续跑在首题后停止：无外网离线重放逐字匹配前五次请求后确认，第六次请求的保守预留使累计占用达到 1.0066464 元，超过 1 元，因而在发送前被预算门阻断，随后同样被包装为 AttributeError；证据在 `m2-r2-offline-replay-20261104-v1`。另一个中断保留 `GUARD_STOP` 原始状态，独立回执不单独证明原因。连续两次相同问题的受控样本均在大上下文处触发预算门，余下 5 项标记 NOT RUN，避免继续消耗费用而不能保证完成。保守预留以请求字节和最大输出估算，可能远大于真实使用；不能为跑完而突破用户的费用上限。三次停止分别以不同身份记录，未重复已经启动的题。

隔离补充实验第一次在调用模型前因导入顺序导致使用错误 Redis，并在工作树根目录新建 SQLite；实际 0 模型请求。新建数据库和零字节 WAL/SHM 已移入该次实验的 `accidental-root-local.db*` 留证，未覆盖既有 `backend/local.db`。修复后先确定隔离环境，再验证 SQLite 路径与 Redis 地址；17 项测试包括真实临时 Redis、既有数据库哨兵和禁止外网。此事件及本地临时证据均不进入 Git。

## 命令、状态和可复核证据

- 本轮离线 229 项：退出码 0，`var/preferences-acceptance/m2.log`、`m2.xml`；测试文件和分类见上表。该日志未记录原始完整命令行，完整原命令为 NOT AVAILABLE，不冒充已记录。
- 真实独立 Redis + 浏览器：2 项 PASS，退出码 0，`m2-live.log`；另用浏览器和独立模拟 API 验证故障状态：`$env:PYTHONPATH="E:\RAG quention\.venv\Lib\site-packages"; & .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra backend/tests/test_m2_frontend_faults.py`，退出码 0、1 passed。未设置此路径时该测试为 1 skipped（退出码 0），不等于通过。早期 `redis-first.log`、`redis-second.log` 和 `redis-ui.log` 存在失败，包括连接超时、Windows 临时目录占用和浏览器点击状态问题；后续独立实例复验以 `m2-live.log` 为准，保留前述日志。生产/ECS Redis 配置、备份、重启均 NOT RUN。
- 本地 Langfuse SDK/偏好送达及相关回归：96 项 PASS、退出码 0，`m2-final-observability.log`；Cloud 已完成纯合成事件回读；真实模型问答的 Cloud 回读仍为 NOT RUN。
- 最终定向回归：`& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra backend/tests/test_m2_explicit_acceptance.py backend/tests/test_m2_analyze_results.py`，退出码 0、50 passed。`git diff --check` 在本仓库 CRLF 文件报告行尾空格、退出码 1；使用 `git -c core.whitespace=cr-at-eol diff --check -- backend/app/services/chat.py backend/app/services/agent_workflow.py backend/app/services/grounded_generation.py backend/tests/test_memory_chat.py` 退出码 0，不擅自改动原有行尾。
- 续跑身份、预算、隔离定向测试：`& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/m2-continuation-chain-offline.xml backend/tests/test_m2_continue.py`，退出码 0、12 passed，`m2-continuation-chain-offline.log`。
- 实际零模型预检：`& .\.venv\Scripts\python.exe scripts/m2_preference_continue.py preflight --manifest var/preferences-acceptance/m2-continuation-manifest-v2.json --output var/preferences-acceptance/m2-cont-preflight-v2`，退出码 0，核实 90 条已启动、6 条未开始、剩余额度 0.316476 元。
- 第二次续跑：`& .\.venv\Scripts\python.exe scripts/m2_preference_continue.py execute --manifest var/preferences-acceptance/m2-continuation-manifest-v2.json --output var/preferences-acceptance/m2-cont-real-r2`，退出码 1（保留停止结果），记录 1 项、5 次完整供应商请求，5 项未开始。凭据通过只读配置传入子进程环境，未写入报告或源码。
- 第二次续跑的无外网诊断：`& .\.venv\Scripts\python.exe var/preferences-acceptance/m2-r2-offline-replay-20261104-v1/replay.py`，退出码 0；5 次请求逐字匹配归档，供应商实际调用 0 次，第 6 次预算阻断发生在发送前。证据目录中的运行锁保护现有输出，原始账本不被改写。
- 聚合：`& .\.venv\Scripts\python.exe scripts/m2_analyze_results.py --run-dir var/preferences-acceptance/m2-real-20261104-r1 --extra-run-dir var/preferences-acceptance/m2-cont-real-r1 --extra-review var/preferences-acceptance/m2-cont-real-r1 --extra-run-dir var/preferences-acceptance/m2-cont-real-r2 --extra-review var/preferences-acceptance/m2-cont-real-r2 --output var/preferences-acceptance/m2-analysis-final-20261104`，退出码 0；此命令只读取现有回执，不评分、不调用模型。

## 结论与遗留

| 项目 | 状态 | 依据与限制 |
|---|---|---|
| Explicit 明确写入、否定、引用、混合与删除 | PASS（离线和真实 Redis） | 初次 33 个用例；后续扩展至 48 项并复测通过，不能代表所有自然语言均可识别 |
| 当前轮优先、普通和 Agent 读取 | PASS（定向路径）；真实语义 PARTIAL | 修复结论优先冲突；Agent 同模型工具/生成路径仍存在语义耦合风险 |
| Redis 故障、持久化与恢复 | PASS（隔离实例） | Redis 7.0.15；生产/ECS 状态 NOT RUN |
| 前端读写、失败与重载 | PASS（隔离真实 Redis/浏览器和故障替身） | 未验证生产前端 |
| 不干扰 RAG 与引用 | **未通过完整验收** | 小样本观察到无检索回答、欠支持补充及缺编号引用；未证明由记忆导致，真实模型 HTTP/Hybrid 全路径未测 |
| 真实四组重复 A/B | PARTIAL | 88 有答案、3 次中断（两次通过离线重放确认预算门，一次保留 GUARD_STOP）、5 未开始；不能据此宣称无回归 |
| RSI 文件规范 | PASS（格式和引用契约） | 哈希不证明事实正确，经验独立性和真实结论需人工/真实证据核实 |
| 完整 M2 验收 | **BLOCKED / PARTIAL** | 真实模型链路 Cloud 回读、真实模型 HTTP/SSE、完整真实四组回归及 Agent 证据/引用问题仍未闭合 |

本轮相关文件为 `backend/app/services/memory_commands.py`、`chat.py`、`agent_workflow.py`、`grounded_generation.py`、`backend/tests/test_m2_explicit_acceptance.py`、`test_memory_chat.py`、`test_m2_frontend_faults.py`、`test_m2_ab_runner.py`、`test_m2_supplement.py`、`test_m2_continue.py`、`test_m2_analyze_results.py`、`scripts/m2_preference_ab.py`、`m2_preference_supplement.py`、`m2_preference_continue.py`、`m2_analyze_results.py` 及本报告。上述列表中部分文件原先已是未提交状态，不等同本轮从零创建。实际工作区还有大量原有改动，均保留；未提交、未推送、未切换服务。下一步优先单独复现 Agent 的无检索回答及引用行为，明确与记忆接入的因果关系；在另行核定模型费用和真实 HTTP 样本后再补充验证。无关技术债不在本轮重构。

## 临时目录清理状态

自动审批审查拒绝了递归删除本轮离线复现的六个临时目录，清理未执行；它们位于 `C:\Users\22088\AppData\Local\Temp` 下，名称分别为 `m2-replay-_j4ryx4g`、`m2-replay-17btbkm0`、`m2-replay-3xtkbkn0`、`m2-replay-itd6rj87`、`m2-replay-kwnlewwl`、`m2-replay-za3u8k66`，合计约 4.8 MiB。未删除历史评测资料或生产数据。

## 后续增量核对：Agent 路由和 Langfuse Cloud

对现有原始回执复核：Hotpot Agent 样本 7/8 与技术题 Agent 样本 8/8 经意图分类进入 `chat`；相应分类请求不包含用户偏好。`backend/app/services/agent_workflow.py` 的 `analyze_intent` 和图路由显示该路径直接进入 `generate_chat`，不执行知识库检索，且在本地基线 `57f9972b8477b0c4aaf7fac329b911420fafc95e` 也存在。由此可以确认偏好没有直接进入这些意图请求，不能排除同模型随机性或其他路径上的影响；特别是检索后的工具选择与最终回答存在共用模型调用。检索成功后的 `_finalize_knowledge_answer` 接受条件也没有单独检查回答的编号引用，该行为在本地基线存在。此轮不新增严格答案校验或改变既有检索策略。

Cloud 回读（**合成、零模型调用**）：用当前 `observability.span` / `event` 在 `development` 环境发送一条合成事件，并通过当前 Langfuse 的项目级 v2 Observation 接口回读到 `memory.preferences`、偏好值和来源、`redis_fallback`、`memory.delivery` 及 `preference_policy_applied`；合成隐私标记在回读中仅保留哈希。真实 Trace：[合成验证记录](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/e0614a5b1fee870eb0493514de24db4f?observation=045a7546056ae831)。本机 SDK 为 5.0.4。旧 `/api/public/traces/{id}` 查询返回 HTTP 410，不能因此认定导出失败；新版 Observation 回读成功。保留首个错误查询的 `result.json` 和后续 `cloud-readback.json`，不覆盖历史现场。此项只验证事件能到云端；不能证明真实模型或实际 HTTP/SSE 的 Prompt 送达、回答质量和完整链路。

Cloud 响应的 SDK scope 元数据包含 Langfuse **public key** 字段；该字段不是 secret key，但不应混入验收报告或原始问答。已安装的 Langfuse 5.0.4 在 `_client/resource_manager.py` 的 `get_tracer` 创建阶段显式设置 scope 的 `public_key`；项目当前脱敏钩子只处理 observation 属性，尚不能确认如何在不影响 SDK 项目路由的条件下消除 SDK 自动添加的 scope 字段，记为需进一步核查的技术债。本次回读不在报告中写出其值。

## 后续增量验证：本机真实 HTTP/SSE 与独立 Redis

新增 `backend/tests/test_m2_http_sse_live.py`，使用现有 Redis 7.0.15 运行文件、隔离 Redis 和回环 Uvicorn 真实 TCP，在正式 FastAPI chat 路由上实际请求 HTTP/SSE。覆盖普通与 Agent 两种模式，分别测试已保存简洁偏好、本轮详细要求覆盖、Redis 不可连接下的默认回退和写入失败；检查实际模型替身收到的证据与偏好、来源及引用传递、成功终态和失败终态。Python 进程禁止非回环连接并关闭 Cloud 导出；模型、检索和数据库均为可控替身，独立 Redis 为真实进程。

实际命令：

```powershell
$env:RAG_TEST_REDIS_CMD = '["wsl.exe","-d","Ubuntu","--exec","/var/tmp/rag-pref-runtime.1VqWrib6/run-redis"]'
$env:PYTHONIOENCODING = 'utf-8'
& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/m2-http-live-guarded.xml backend/tests/test_m2_http_sse_live.py *> var/preferences-acceptance/m2-http-live-guarded.log
```

退出码 **0**，6 passed、0 skipped、13 个弃用警告；可核查 `m2-http-live-guarded.log` 和同名 XML。第一次运行两条 Agent 失败，原因为测试替身未提供 `finish_reason`，保留在 `m2-http-live-initial.log`；补齐替身后通过。测试结束回收临时 Redis 和服务线程，复用现有运行环境；开始前磁盘使用量未记录，前后增量 UNKNOWN。

这项验证将“网络入口和偏好/Redis 接线”更新为 **PASS（真实 HTTP/SSE + 真实 Redis + 替身模型/检索）**；实际 GLM、Hybrid、生产数据库、正式服务及其 Cloud trace 均不在此次测试覆盖内。

## Office 补充内容抽查（不改变冻结评分）

对 `office50:031` 的普通 A/B、Agent A/B 各第一次回答，读取实际请求 41、42、52、56 及 SHA256 与清单一致的 Excel 原件 T07。原表可核对 2018 年、219 家、总数 108149.892、四项收入与四项百分比；65–69 行亦有来源及脚注说明。四条回答的主要数值可支持。表内仅写 `In thousands`，未独立证明币种是美元，因此币种仍为 UNKNOWN。普通 B 将统计范围概括为全部美国州及哥伦比亚特区，遗漏原表列出的三个排除类别；`Local` 是否特指地方政府亦未由表内明确说明。Agent 两条均送达了核心数值证据，但没有编号引用；Agent B 还把 219 家放入金额单位表头下，存在单位标签歧义。按原冻结评分，不能因此单方面把整条忠实度从 UNKNOWN 改为 PASS 或 FAIL。

对 `office50:001` 的普通 A1、C1、B2、D2 四条回答，核对 Word 原件、实际十段请求及引用。原件标题只在一个跨四列物理单元格中出现一次，实验片段因合并单元格提取重复为四次；原件延期项为 `na`，不能据此断言“没有延期”。C1、B2 的臭氧、水体和鲑鱼相关补充内容没有出现在送达材料或该 Word 原件中，其外部科学真假不在此处判定；C1 的某项项目目标虽存在原件另一表格，却未随本次请求送达。A1/D2 的项目编号虽由片段 [1] 送达，但答案附近的 [2] 只有标题，不能仅因正确片段曾出现就认定该引用支持编号。这些是逐项证据边界，不覆盖历史评分账本。

## 后续增量修复：否定、引号与本轮覆盖

二次复查确认，部分“不保存”表达在切分后仍可能执行偏好写入；另外否定当前轮格式、某些引号内示例以及包含无关引号的混合请求处理不正确。定向修复 `backend/app/services/memory_commands.py`，相应测试在 `backend/tests/test_m2_explicit_acceptance.py`：在提取长期偏好前先识别明确的不保存要求；统一屏蔽常见引号内容，保留英文缩写中的撇号；先处理当前轮明确的否定要求，再处理肯定格式要求。样例及具体边界见测试文件。

修复前定向测试 **11 failed、37 passed、退出码 1**，证据 `preference-fix-before.log/xml`；修复后同范围 **48 passed、退出码 0**，证据 `preference-fix-focused.log/xml`。相关 8 个文件的后续回归 **208 passed、0 skipped、退出码 0**：

```powershell
$env:PYTHONIOENCODING = 'utf-8'
& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/preference-fix-regression.xml backend/tests/test_m2_explicit_acceptance.py backend/tests/test_memory_m2_commands.py backend/tests/test_memory_chat.py backend/tests/test_memory_service_api.py backend/tests/test_m2_ab_runner.py backend/tests/test_m2_supplement.py backend/tests/test_m2_continue.py backend/tests/test_m2_analyze_results.py *> var/preferences-acceptance/preference-fix-regression.log
```

约束：明确不保存时暂不执行该条请求中的任何记忆变更，包括重置或删除；存在示例/假设或无法确认引号时仍保守保留原问题。对“不要简洁”等否定只撤销冲突的长期限制，不猜测相反风格；不能声称规则可识别所有自然语言变体。这次未调用外部模型，也未改变检索、RAG 阈值或 Agent 流程。此前真实模型 A/B 使用的是修复前版本，不能当作修复后真实模型验证。

修复后的本机真实 HTTP/SSE、独立 Redis 与模型/检索替身回归再次运行：`$bin=(Get-Content -Raw -LiteralPath 'var/preferences-acceptance/redis-test-runtime.txt').Trim(); $env:RAG_TEST_REDIS_CMD=ConvertTo-Json -Compress -InputObject @('wsl.exe','-d','Ubuntu','--exec',$bin); $env:PYTHONIOENCODING='utf-8'; & .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/preference-fix-http-sse.xml backend/tests/test_m2_http_sse_live.py`，退出码 0、6 passed、0 skipped；日志 `preference-fix-http-sse.log`。真实模型语义和 Hybrid 索引仍未由这项替身测试证明。

## 修复后真实模型 HTTP/SSE 小规模复验

在用户已批准的 1 元总限额内，对冻结 `office50:031` 创建两个独立的新测试身份，使用修复后的本机代码，通过实际回环 TCP HTTP/SSE 路由请求 GLM-5.3-Flash；普通和 Agent 均用默认偏好，沿用冻结开发语料、`fulltext`、top_k=10、context_window=0、不使用 Rerank/改写和输出上限 4096。普通请求和 Agent 请求各运行一次，不能替代六题 × 四组的重复 A/B，也不证明生产 Hybrid。

两条请求均获得 `done` 且没有 `error`；普通生成状态 COMPLETED、检索 SUCCESS，Agent 的 `done` 未提供 generation_status，应用生成状态 UNKNOWN，不能由 `done` 反推完成。5 次供应商调用均有 COMPLETED 回执，0 UNKNOWN，输入 39,392、输出 933 Token，按公开单价计算 0.0341260 元；此前累计 0.6967552 元，本次后共 **0.7308812 元**，实际供应商账单 UNKNOWN。

两条回答的核心数字 108149.892（千为单位）与冻结 Gold 和原 Excel T07 一致。实际模型请求 1、5 都包含对应表格和来源。普通回答的核心编号引用 [1] 可支持数字；Agent 虽有 SSE 来源对象和 `source_mapping_status=VERIFIED`，答案没有编号引用，不能将来源映射等同回答引用正确。两条回答均把单位扩展为千美元，而原表提供 `In thousands`，现有原表和单元格格式不证明币种；全文忠实度标为 PARTIAL。普通回答额外扩写 IMLS 机构名称，送达证据不支持该扩写。独立逐题证据在 `var/preferences-acceptance/m2-http-smoke-real-run/review-human.md`，未修改原始记录。

预检命令 `& .\.venv\Scripts\python.exe scripts/m2_http_real_smoke.py preflight --real --manifest var/preferences-acceptance/m2-http-smoke-real-manifest.json --output var/preferences-acceptance/m2-http-smoke-real-run` 退出码 0、0 模型调用；实际运行 `& .\.venv\Scripts\python.exe scripts/m2_http_real_smoke.py execute --real --manifest var/preferences-acceptance/m2-http-smoke-real-manifest.json --output var/preferences-acceptance/m2-http-smoke-real-run` 退出码 0；凭据仅以进程环境传入，不写入清单、请求或日志。执行器定向测试 24 passed，0 模型调用；它约束项目已核查的 AsyncOpenAI 与 ChatOpenAI 调用边界，不宣称能够拦截所有 Python 网络访问。此前检查另确认 Windows asyncio 的 `sock_connect` 可能绕开普通 socket 钩子。模拟测试的费用只是假设值，不计入真实账单。

为避免旧续跑器遗漏这次真实账本，执行器在供应商调用启动后保留共享活动锁；需要先验证并将这次账本纳入原有历史，再按锁所有者的租约进行安全释放，不能简单删除或重置。实际 Cloud 追踪在本次隔离运行中关闭，本轮 Cloud 仍仅有合成回读。

## 后续预算账本核对及临时副本

已扩展现有续跑校验器 `scripts/m2_preference_continue.py`，把真实 HTTP 额外运行作为独立历史记录核对：冻结身份、请求与输出哈希、供应商使用量、未知预留及重复请求 ID；这两条 repetition=3 记录不占用原冻结计划的五个未启动身份。`& .\.venv\Scripts\python.exe -m pytest backend/tests/test_m2_continue.py -q --disable-warnings` 退出码 0、17 passed。实际 `m2-http-reconciliation-spec.json` 的零模型预检退出码 0，预算占用 0.7308812 元、剩余 0.2691188 元、当时当时原计划 5 项未启动，输出在 `m2-http-reconciliation-preflight`。共享活动租约在完成下述归属检查与核算后已释放；持久 grant 防止当前续跑器遗漏新账本。

四个合成 HTTP 试验产生的 SQLite 副本各 405504 字节；原开发索引哈希均核对一致，回执保留且无对应测试进程。按资源收尾要求尝试逐文件删除四个合成副本，自动审批审查以 `blocked by policy` 拒绝了操作，删除未执行；拒绝前后的磁盘剩余量没有命令结果，记为 NOT AVAILABLE。真实试验数据库和所有原始材料、账本、日志仍保留。

账本校验加入真实 HTTP grant 后，组合回归首次出现 **10 failed、31 passed、退出码 1**，因为旧合成测试读取的是不含这次真实账本的历史；原失败日志为 `m2-http-reconciliation-regression.log`。现有测试将合成 grant 查询隔离到临时目录，另保留对真实 grant 拒绝漏计历史的单独测试，没有放宽真实预算检查。最终执行 `& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/m2-http-reconciliation-final.xml backend/tests/test_m2_http_real_smoke.py backend/tests/test_m2_continue.py`，退出码 0、42 passed、0 skipped，见同名日志与 XML。真实运行脚本哈希与其冻结清单一致。

补充限制：更新后的账本防漏计检查会阻止一个将来新建的 HTTP 真实运行在自己的账本产生前通过复核；当前已有的一次性 grant 也会阻止重跑。若将来需要更多真实 HTTP 测试，应先针对这一兼容性设计和验证，不得删除 grant 或跳过预算记录。现存活动租约按文件创建时间、每次发送前的租约校验和最终摘要，在正常本机运行条件下可合理归属于本次已完成试验；该关联不是密码学证明。已核对该文件 SHA256 与运行后的观测一致、相关进程已退出，并确认 grant 及新账本核算有效后，仅释放该活动租约；持久 grant 仍保留。释放后原续跑器预检退出码 0，仍正确显示累计 0.7308812 元及 5 个未启动身份，证据在 `m2-http-reconciliation-preflight-after-release`。

## 历史引用的增量逐项复核

复用修复前已保存的回答、模型请求与原件，按非严格的邻近句子或小节引用口径，分别判断编号引用是否被其对应来源支持；未附编号的回答单列，未引用的补充内容不进入引用分母。以下是对既有回答的复核，不是修复后新模型测试。

| 题目 | 有编号引用的实际出现次数 | 可确认支持 | 有歧义 | 明确不支持 | 有回答但无编号引用 |
|---|---:|---:|---:|---:|---:|
| Excel `office50:031` | 12 | 3 | 9 | 0 | 8 |
| Hotpot 发明者 | 32 | 30 | 2 | 0 | 8 |
| PDF 0684 | 15 | 1 | 14 | 0 | 8 |
| PDF 0685 | 10 | 7 | 2 | 1 | 0 |
| Tribometer 技术题 | 13 | 9 | 4 | 0 | 8 |

表格数据只覆盖 72 条既有回答的 82 处编号引用；不能作为全部 88 条回答的总体精确率。不同长度的回答会产生不同引用次数。四份逐题记录位于 `var/preferences-acceptance/m2-citation-office031-review.jsonl`、`m2-citation-hotpot-review.jsonl`、`m2-citation-pdf-review.jsonl`、`m2-citation-tribometer-review.jsonl`；可读的缩进合并版本为 `var/preferences-acceptance/m2-citations-readable.json`（含 80 个身份，其中 72 个有答案、3 个中断、5 个未运行）。Tribometer 的 8 条 Agent 回答没有编号引用或检索结果，不能因此断言回答的内容为假。Excel 的歧义主要是表格未明确标识美元；PDF 0684 原图能对应男/女数字，但模型收到的扁平文本不能可靠保留行列对应。PDF 0685 的 3 条已启动但无答案和 5 条未运行分别保留，不算作拒答或引用缺失。

## 二次增量修复：避免误存待处理文本和破坏提问

再次审查发现三种明确问题：`不要把这条请求保存为偏好`、`仅本次有效` 等要求可能仍写入偏好；翻译或分析冒号后需要处理的文本可能被识别并写入长期偏好；同时包含长期偏好和多行问题时，拼接会破坏换行、Python 缩进与标点。已在 `memory_commands.py` 对这三类情况作定向处理：保留明确的不保存要求，区分待处理文本与独立指令，按原始位置删除被识别的偏好指令并保留余下文本。中文/英文翻译及首行缩进均有覆盖；三个旧测试原先要求丢掉句号，已改为检查句号保留。

修复前新复现 5 failed、48 deselected、退出码 1；修复后运行如下关联测试，**241 passed、0 failed、0 skipped、退出码 0**：

```powershell
$env:PYTHONPATH='backend'
$env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python.exe -m pytest -q --tb=short backend/tests/test_m2_explicit_acceptance.py backend/tests/test_memory_m2_commands.py backend/tests/test_memory_chat.py backend/tests/test_memory_service_api.py backend/tests/test_agent_engineering_repairs.py backend/tests/test_agent_evidence_guard.py backend/tests/test_agent_grounded_answer.py backend/tests/test_agent_quality_outcomes.py backend/tests/test_agent_request_parameters.py backend/tests/test_agent_stream_guard.py backend/tests/test_agent_tool_observation.py
```

原始命令记录与测试日志分别为 `C:\Users\22088\AppData\Local\Temp\memory-safety-commands-20261104.txt`、`memory-safety-before-20261104.log` 和 `memory-safety-final-20261104.log`。测试覆盖实际 ChatService 边界，但模型与 Redis 为替身；本次没有真实模型调用。需注意：前面的真实 HTTP 两题运行早于本次二次修复，不能冒充最新版本的真实模型验证。

此次修复后，进一步复用独立 Redis 和现有 HTTP/SSE 替身测试：`$bin=(Get-Content -Raw -LiteralPath 'var/preferences-acceptance/redis-test-runtime.txt').Trim(); $env:RAG_TEST_REDIS_CMD=ConvertTo-Json -Compress -InputObject @('wsl.exe','-d','Ubuntu','--exec',$bin); $env:PYTHONIOENCODING='utf-8'; & .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/memory-safety-final-http.xml backend/tests/test_m2_http_sse_live.py`，退出码 0、6 passed，日志 `memory-safety-final-http.log`。为保持费用授权边界，本次没有重发真实模型请求。

## 当前版本真实对照的可执行性

最新解析器修复晚于此前全部真实模型请求，当前版本的真实四组 A/B 为 NOT RUN。现有续跑器仅允许原计划中五个未启动的不可回答 Agent 身份；现有真实 HTTP 一次性执行器仅支持此前的同题默认普通/Agent 两条新身份，冻结脚本和 grant 也不允许当作新的四组运行重新执行。不能为补齐结果删除 grant、混入修复前结果或复用 UNKNOWN 身份。

按已记录用量，目前预算占用 0.7308812 元，剩余预留空间 0.2691188 元，实际供应商账单 UNKNOWN；先前相同 Excel 题的普通请求预留 0.0740384 元、Agent 最后一请求预留 0.216064 元，实际新请求大小和调用轮数受模型行为影响，不能据此保证八次普通/Agent × 四组均能完成。原五条待运行题与一次离线重放的同题结果相近：该次第六次请求单次预留 0.3098912 元，已超过当前剩余预留空间，但这不证明五条必然采取相同路径。若后续继续，需为当前版本新增明确的不可重放实验身份，沿用同一费用账本和共享锁并先完成离线验证；不得借此扩展为正式系统或更改冻结检索和评分。

## 原计划最后五个身份的续跑

续跑时复用已有隔离开发 SQLite/BM25 索引的副本，核对源哈希、冻结文档片段和索引；源文件与原始 PDF 未改动。最初一次隔离副本准备失败（退出码 1），没有启动供应商调用，也没有写入永久授权锁；根因是副本 WAL 未 checkpoint 即被校验为未封存。修复 SQLite 连接关闭与 checkpoint 后，真实源的逐行核对及相关组合测试 **46 passed、0 skipped，退出码 0**，证据为 `m2-reuse-wal-final2.log/xml`；零调用预检 `m2-reuse-wal-preflight-final/preflight.json` 通过。

随后按原配置尝试原计划五个未开始的身份。**五条均在下一次模型请求发送前因预算预留不足而跳过，没有最终答案**；总共已经发出的 25 次供应商调用均 COMPLETED、0 UNKNOWN，使用输入 71016、输出 4785 Token，按公开价格计算 0.0702108 元。运行器退出码 0 仅表示五条都已记录并按配置跳过，不表示回答正确或验收通过。拒绝发送的下一请求及其具体预留金额未被归档，因此不能独立重算每次拦截。五条逐项审查在 `m2-reuse-real-run-r2/independent-trial-review-readable.json`，不把未完成记成拒答或错误。

原 96 个计划身份现均有记录：88 个有答案（此前修复前代码版本：核心 PASS 82、PARTIAL 5、FAIL 1），8 个没有最终答案（先前 3 个、本次 5 个）；无未启动身份。按有答案的 88 条计算，严格核心 PASS 为 82/88（约 93.2%），单独列出 PARTIAL 和无答案；这不是最新解析器版本的真实质量估计。`m2-original-96-final-accounting.json` 是可读逐项核对表，不能替换原回执。历史 A/B、补充实验、此前真实 HTTP 与本次续跑合计 251 次 COMPLETED 供应商调用，0 终态 UNKNOWN，按记录及公开单价计算约 **0.8010920 元**；此前 401 联调账单及实际供应商账单仍为 UNKNOWN。`m2-reuse-final-reconciliation-preflight/preflight.json` 已纳入全部真实账本，剩余预算 0.1989080 元。

执行命令：`& .\.venv\Scripts\python.exe scripts/m2_preference_continue.py execute --manifest var/preferences-acceptance/m2-reuse-spec.json --output var/preferences-acceptance/m2-reuse-real-run-r2`，退出码 0。凭据只传入子进程环境；生产服务和数据未改。运行前 D 盘空闲 143325532160 字节，运行后抽样 143319212032 字节，差额 6320128 字节不能排除其他进程影响；必要的隔离数据库和日志留在输出目录，保留原始结果和审查材料。

原计划按组的最终记录数如下，所有组各有 12 个计划身份；前 88 个有答案样本来自修复前版本，不能把八个无答案样本当作错误，也不能把本表当成最新代码的质量测试。

| 模式 | 组 | 核心 PASS | PARTIAL | FAIL | 无最终答案 |
|---|---|---:|---:|---:|---:|
| 普通 | A | 11 | 1 | 0 | 0 |
| 普通 | B | 11 | 0 | 1 | 0 |
| 普通 | C | 12 | 0 | 0 | 0 |
| 普通 | D | 12 | 0 | 0 | 0 |
| Agent | A | 9 | 1 | 0 | 2 |
| Agent | B | 9 | 1 | 0 | 2 |
| Agent | C | 8 | 2 | 0 | 2 |
| Agent | D | 10 | 0 | 0 | 2 |

## 新一轮实验前的评分口径补充

`m2:tribometer-mechanism` 的冻结 Gold 原文为 `coefficient of friction, friction force, or wear volume`；题目没有要求列举全部三项，原文将三项列为示例。历史人工复核要求三项全覆盖，这是更严格的解释，并非 Gold 中 `or` 的唯一可能含义。保留历史 Gold 和历史标签不变；新一轮除了按历史口径列出可比结果，还应预先单独列出“准确答出至少一项”的字面口径与“三项全列”的完整性，不把泛称磨损量直接等同磨损体积。该敏感性分析不替代历史评分标准，问题中是否要求穷尽本身仍可另议。

## 当前代码真实模型 A/B：逐题复核与对账（最终增量）

本节取代前文“当前版本真实四组 A/B 为 NOT RUN”的当时状态；前文历史结果保持原样。用户批准本轮累计费用上限 2 元，使用 GLM-5.3-Flash。最新解析器修复后的六道开发题、普通和 Agent、A/B/C/D 四组、各两次，共 **96 个身份均已记录**。本次复用已冻结的语料和 BM25 索引，通过隔离数据库和临时 Redis 运行，检索采用 fulltext、top_k=10、context_window=0、不启用重排或改写；它不代表生产 Hybrid。入口是 `ChatService.chat_stream`，不是实际 HTTP 网络入口；真实 HTTP/SSE 的最新代码验证仍为模型替身，早期真实 GLM HTTP 验证发生在二次解析器修复前。

| 模式 | 组 | 有最终回答 | 无最终回答 | 核心 PASS | PARTIAL | FAIL | UNKNOWN（含无答案） |
|---|---|---:|---:|---:|---:|---:|---:|
| 普通 | A | 12 | 0 | 12 | 0 | 0 | 0 |
| 普通 | B | 11 | 1 | 11 | 0 | 0 | 1 |
| 普通 | C | 12 | 0 | 11 | 0 | 1 | 0 |
| 普通 | D | 12 | 0 | 12 | 0 | 0 | 0 |
| Agent | A | 11 | 1 | 10 | 0 | 0 | 2 |
| Agent | B | 11 | 1 | 9 | 0 | 0 | 3 |
| Agent | C | 11 | 1 | 10 | 0 | 0 | 2 |
| Agent | D | 10 | 2 | 8 | 1 | 0 | 3 |

合计 **90 条有回答、6 条无最终回答**。90 条中核心 PASS 83、PARTIAL 1、FAIL 1、UNKNOWN 5；另 6 条无最终回答均为 UNKNOWN。83/90 仅是已有回答中可确认完全正确的比例，未知项仍在分母中，不是总体正确率的无偏估计。无答案中一次为供应商超时 UNKNOWN，一次在发送前被守卫中止但原因无法从原始诊断确定，四次有明确预算预留不足证据；均未重发。对不可回答题，既有答案也有回退摘录，不能简单视为正确拒答。

核心证据送达逐题状态为 PASS 48、FAIL 16、UNKNOWN 32：Office 是逐题二值检查，Hotpot 是证据单元比例，不能合成统一的 Context Recall 分子分母。核心编号引用召回可量化 70 条，合计 31/70；另外 14 条 UNKNOWN、12 条不适用，不能当作全文 Citation Precision。全文忠实性分布为 PASS 20、PARTIAL 39、FAIL 2、UNKNOWN 30、受来源对齐限制的 PASS 5。引用支持性为 PASS 19、PARTIAL 12、FAIL 16、无编号引用 16、UNKNOWN 33；这些是逐回答状态，不能作为逐引用精确率。PDF 0684 原图可确认数字，但扁平文本无法可靠验证行列对应，不能把页命中视为证据完整送达。Word 测试夹具重复提取一个合并单元格；Excel 原表仅标注千为单位，币种无独立证据。逐项口径、问题与来源见 `var/preferences-acceptance/m2-current-aggregate-v2.json` 和同名 Markdown 报告；它们是本地私有评测产物，不提交。

两次重复的 Hotpot 发明者及技术题中，Agent 实际走闲聊路径且没有知识库检索的情况均有记录；答案可能正确，但无知识库证据及编号引用。已有证据不能证明这一现象由偏好功能导致，也不能证明所有 Agent 工具选择与偏好相互独立。技术题按冻结 Gold 的字面 `or` 为 16/16 PASS；历史另行要求三项全部覆盖时 15 PASS、1 PARTIAL，两种口径分开保留。

四次当前代码运行合计 **220 次 COMPLETED、1 次 UNKNOWN**；已知用量输入 783917、输出 75685 Token，按硅基流动公开价格计算 0.8390516 元。UNKNOWN 请求另按 0.075968 元全额保守预留，不假定其账单为零，也不重发。连同此前历史试验，预算占用 **1.7161116 元**，剩余 **0.2838884 元**；供应商实际账单 UNKNOWN。独立对账 `var/preferences-acceptance/m2-current-reconciled-v3.json` 保留每次请求的身份和哈希；四个预算阻断中，后续三项的拒绝请求正文未保存，故不能从正文独立复算这三项的预留额。

六道主测试的问题均不满足 `step_by_step` 的适用条件，所以 D 组无法证明分步偏好有效。旧版补充实验的题目原文只列举摩擦仪测量指标，不包含实际操作流程；它只可作为历史送达观察。已找到既有 Word 语料 `word-paragraph-6`，其中包含季度报告的具体填写要求，可用于下一次独立 A/D 测试。现有旧补充执行器只核算旧预算且使用另一把锁；当前全局共享租约没有被其遵守，因此无法安全直接承接本轮余量。新补充测试标记 **BLOCKED / NOT RUN**，没有为了完成对照绕过预算门，也不声称所有独立路径已验证。

本轮复核命令：

```powershell
$env:PYTHONPATH='backend'
$env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python.exe -m pytest -q --tb=short -p no:cacheprovider backend/tests/test_m2_current_ab.py backend/tests/test_m2_current_resume.py backend/tests/test_m2_current_resume16.py backend/tests/test_m2_current_resume11.py backend/tests/test_m2_current_reconcile.py
```

实际退出码 0，46 passed，日志 `var/preferences-acceptance/m2-current-harness-final.log`。独立聚合命令 `& .\.venv\Scripts\python.exe scripts/m2_current_aggregate.py --output var/preferences-acceptance/m2-current-aggregate-v2.json` 退出码 0；聚合测试 `& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra backend/tests/test_m2_current_aggregate.py` 退出码 0、24 passed。独立对账命令 `python scripts/m2_current_reconcile.py --output var/preferences-acceptance/m2-current-reconciled-v3.json` 退出码 0；这些对账、聚合和测试不发起模型调用。完整 M2 验收仍为 **PARTIAL / BLOCKED**：最新版本真实 HTTP/SSE + GLM、真实 Hybrid 与 Rerank 对照、真实模型 Cloud 回读、适用的分步偏好、Agent 证据和引用问题仍未全部闭合。未部署、提交或推送；当前四个运行目录的隔离数据库保留供回执追查。D 盘最终抽样可用空间 143289905152 字节，缺少与本次四段运行对应的完整清理前快照，无法可靠归因磁盘差额。

## 最新代码 RAG 关联回归复验

在二次偏好识别修复后的当前工作区，重新运行此前的 10 个 RAG 回归文件：

```powershell
$env:PYTHONPATH='backend'
$env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/m2-final-rag-recheck-20261104.xml backend/tests/test_normal_evidence_guard.py backend/tests/test_agent_quality_outcomes.py backend/tests/test_agent_request_parameters.py backend/tests/test_e20_context_delivery.py backend/tests/test_retrieval_outcomes.py backend/tests/test_retrieval_cache_safety.py backend/tests/test_rerank_unknown.py backend/tests/test_pdf_pipeline.py backend/tests/test_pdf_table_validation.py backend/tests/test_model_router.py
```

实际退出码 **0**，**134 passed、0 failed、0 skipped**，有 129 条依赖弃用提示，完整日志 `var/preferences-acceptance/m2-final-rag-recheck-20261104.log`、JUnit `m2-final-rag-recheck-20261104.xml`。这是本地测试而不是生产或真实模型 Hybrid 验证，不与此前 134 项重复相加，也不改变以上真实模型回执的版本和分母。

当前代码的独立 Redis 与浏览器也已重新复验：

```powershell
$bin=(Get-Content -Raw -LiteralPath 'var/preferences-acceptance/redis-test-runtime.txt').Trim()
$env:RAG_TEST_REDIS_CMD=ConvertTo-Json -Compress -InputObject @('wsl.exe','-d','Ubuntu','--exec',$bin)
$env:PYTHONPATH='backend;E:\RAG quention\.venv\Lib\site-packages'
$env:PYTHONIOENCODING='utf-8'
& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --junitxml=var/preferences-acceptance/m2-current-redis-browser-20261104.xml backend/tests/test_memory_live.py backend/tests/test_m2_frontend_faults.py
```

退出码 **0**，**3 passed、0 failed、0 skipped**，13 条依赖弃用提示；原始日志 `m2-current-redis-browser-20261104.log`。前两项使用隔离真实 Redis，覆盖 AOF 重启、独立 RDB 恢复再重启，以及浏览器经真实 HTTP 接口的偏好设置与刷新；后一项使用受控 HTTP 故障替身检查前端错误状态。测试使用临时端口、临时密码和数据目录，不操作生产 Redis，也没有模型调用。此结果不等于真实模型、生产部署或完整前端构建已通过。

## Agent 路由与偏好影响的进一步核对

逐项读取当前真实回执：Hotpot 和 Tribometer 两题各 8 个 Agent 身份的意图输入在组别和重复之间逐字一致，每题均只有一个不同的输入哈希；16 条实际意图结果均为 `chat`，随后走闲聊生成、没有引用事件。这两道题没有明确要求根据选定资料回答；当前 `analyze_intent` 不把偏好策略放入意图识别请求。检查 HEAD 中记忆接入前的代码，已有通用知识走 `chat` 和训练知识回答的路由；未进行旧版真实模型重复试验，不能据此断言旧模型输出必然完全相同。当前证据没有证明这一现象由记忆修改造成，也不应将闲聊回答当成已基于知识库的回答。

有证据后的一次工具可调用请求中仍可能包含偏好策略，工具选择与最终回答并未完全分成两次调用。已运行 `test_memory_chat.py` 中生成/辅助输入隔离、真实工作流两轮检索、第三轮重试三项替身测试：退出码 0、3 passed；它们不能证明真实模型在所有情况下的工具决策不受提示语影响。这个语义隔离验收保持 **PARTIAL**，不将未复现的因果关系当成已确认缺陷，也不擅自改变既有检索策略或引入严格答案校验。

## 当前界面与 RSI 文件契约补充复验

以下命令均于本次续跑实际执行，退出码均为 0：

```powershell
node frontend/node_modules/typescript/bin/tsc -p var/preferences-acceptance/ui-tsconfig.json --pretty false
node --test frontend/tests/chat-stream.test.mjs
& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra backend/tests/test_rsi_playbook_contract.py
& .\.venv\Scripts\python.exe scripts/rsi_playbook_verify.py docs/examples/rsi-playbook-v1.jsonl
```

窄范围 TypeScript 检查无输出；流事件测试 **8 passed、0 skipped**；RSI 契约测试 **7 passed、0 skipped**；示例文件校验输出 `PASS: Playbook format checks`。这些测试验证目标组件、流事件处理和文件格式，不代表全站前端构建通过，也不证明经验内容的事实正确性。既有全量前端构建此前的 45 项问题没有在本轮宣称修复。

## 分步偏好真实补充试验及输入检查

新增隔离实验入口 `scripts/m2_current_supplement.py`，沿用既有运行、预算守卫和只读冻结语料；历史源码、grant、租约和结果均未修改。定向测试 `& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q --tb=short --junitxml=var/preferences-acceptance/m2-current-supplement-offline.xml backend/tests/test_m2_current_supplement.py` 退出码 **0、23 passed**。实际预检 `& .\.venv\Scripts\python.exe scripts/m2_current_supplement.py preflight --manifest var/preferences-acceptance/m2-current-supplement-manifest-v1.json --output var/preferences-acceptance/m2-current-supplement-preflight-v1.json` 退出码 **0**，0 模型调用、计划 8 个独立身份。第一次替身测试由于测试夹具禁止 Windows asyncio 本地 socketpair 失败；修正夹具并保留失败记录后通过，没有发送模型请求。

实际执行 `& .\.venv\Scripts\python.exe scripts/m2_current_supplement.py execute --manifest var/preferences-acceptance/m2-current-supplement-manifest-v1.json --output var/preferences-acceptance/m2-current-supplement-real-v1` 退出码 **1**，终态 STOPPED，已有 **4 条普通回答、1 条 Agent 无最终回答、3 条未开始**。四条普通回答明确说明资料不足；从完整语料 Gold 看均未回答原问题，但对它们实际收到的只有标题的资料而言，不能把如实拒答判成无依据编造。独立核查 BM25 候选、最终模型请求和引用，4 条都仅含 `word-paragraph-5` 标题，未召回承载要求的 `word-paragraph-6`；损失发生在检索阶段，不能归因于分步偏好或上下文截取。两次 D 组的实际请求均收到分步偏好，A 组未收到；没有可靠证据支持评价完整的分步回答质量。

Agent 已开始的身份在已有一次成功模型请求后，下一次发送前被 `PRE_SEND_VALIDATION_FAILED` 拦截；原始诊断没有底层异常，因此原因 **UNKNOWN**。事后对同一冻结清单和运行进程的只读检查通过，但不能据此反推当时原因。没有重试任何已开始身份。该补充试验 5 次已发请求均 COMPLETED、0 新增 UNKNOWN，输入 550、输出 2230 Token，按公开价格计算 **0.0066840 元**；累计预算占用 **1.7227956 元**，余额 **0.2772044 元**，实际账单 UNKNOWN。独立逐题审查见 `var/preferences-acceptance/m2-current-supplement-real-v1/independent-supplement-review-v1.json` 和同名 Markdown。

事后在同一只读隔离索引做零模型调用检索诊断：原中文问题仅命中标题；另一条带英文材料关键词的新问题使 `word-paragraph-6` 排名第 1（top_k 10），且 `step_by_step` 偏好适用。该新问题是另一个开发样例，不能代替或改写已冻结的原题结果；BM25 命中也不等于实际模型最终收到证据。

对上述新样例单独建立题号与一次性运行，继续使用同一索引、模型和检索配置；原中文样例不被替换。`scripts/m2_current_supplement_english.py` 复用上一补充入口，定向测试退出码 0、17 passed；真实预检退出码 0，0 模型调用并确认目标段落排第 1。另用真实临时 Redis 与两个模型客户端的替身传输，验证原入口运行路径，退出码 0、1 passed；没有复现先前的拦截，这不能证明原先失败原因。

新样例的真实执行退出码 **1**，前 3 条普通模式有最终回答（A1、D1、A2），D2 在模型请求发送前因进程排除检查终止，4 个 Agent 身份未开始。三条回答的实际候选和模型请求均含完整 `word-paragraph-6`，核心答案均覆盖六项原文要求且有 `[1]`；D1 的分步偏好确实送达，回答采用了有用的五步组织。A 组也有条理地组织答案，因此不能据此断定偏好导致了格式改进。独立复核发现 D1 把 `na` 示例扩大为普遍规则，A2 的一项补充信息与所引标题之间缺少确定关联；这两条全文忠实性与引用支持性为 PARTIAL，不改变核心 PASS。逐题可读 JSON 与 Markdown 为 `m2-current-supplement-real-english-v1/independent-english-supplement-review-v1.json` 及同名 `.md`。

D2 的诊断为 `PRE_SEND_PROCESS_ValueError: Cannot exclude legacy supplement/HTTP writer`；它没有保存进程检查返回码，无法确认当时是匹配旧进程还是部分进程命令行不可读。该身份虽没有发出模型请求，但已经记录为 GUARD_STOP，不重发。4 个 Agent 身份尚未启动。此次 3 次模型请求均 COMPLETED、0 新增 UNKNOWN，输入 3115、输出 4133 Token，按公开单价计算 0.0140644 元。连同前面所有试验，累计预算占用 **1.7368600 元**，余额 **0.2631400 元**；此前 UNKNOWN 仍全额保留，供应商账单仍为 UNKNOWN。当前 D1 只是单题单次可用表现，不能代替 Agent 或全面四组验收。

随后只对新样例的 4 个从未启动的 Agent 身份续跑，保留已记录的普通 D2、不重发。`scripts/m2_current_supplement_english_agents.py` 移除每次发送前对所有进程名称的宽泛扫描，仍使用新的一次性原子 grant，并在发送前核对历史账本、租约、请求身份和预算。测试退出码 0、18 passed；预检退出码 0、0 模型调用；实际执行退出码 **1**，4 个身份均有记录，其中 3 个返回、1 个 UNKNOWN 后停止。

| Agent 身份 | 实际终态与独立核对 |
|---|---|
| A1 | 原文摘录回退，包含目标段落六项要求，但还包含其他摘录；不能算已生成分步答案 |
| D1 | 自然语言回答覆盖 6/6 项，实际最终模型请求包含分步偏好；没有编号行内引用 |
| A2 | 自然语言回答覆盖 6/6 项，没有编号行内引用；两项补充信息的原文支持不足，全文忠实性 PARTIAL |
| D2 | 最终回答未产生；第 15 次模型调用记录 ConnectError，结果 UNKNOWN，分步偏好尚未送入最终生成调用 |

四个身份都检索到了目标段落；前三个最终模型请求实际含完整正文。D1 提供了有条理的分项操作，A2 也有类似结构，因此不以单题断言偏好造成格式变化。A1 的两次并行工具结果顺序为 SUCCESS 后 NO_RESULTS，最终走摘录回退；D1 顺序为 NO_RESULTS 后 SUCCESS，保留自然语言回答。这是当前工作流的实际行为线索，不能据此证明由偏好造成。完整逐题审查及定位在 `m2-current-supplement-real-english-agents-v1/independent-english-agent-review-v1.json` 和同名 Markdown。

这次 15 次请求中 **14 COMPLETED、1 UNKNOWN**；已知输入 24854、输出 5088 Token，价格计算 0.0341296 元，UNKNOWN 另保留 0.023488 元。总体预算占用 **1.7944776 元**，剩余 **0.2055224 元**，包含两笔历史 UNKNOWN 的完整预留共 0.099456 元；实际供应商账单 UNKNOWN。`rejected-request-0016.json` 是 UNKNOWN 第 15 次请求的诊断副本，不代表发送过第 16 次。所有身份均已记录，没有重发。独立账本核对 `m2-current-supplement-english-agents-independent-reconciliation-v1.json` 退出码 0，验证全程最高保守占用 1.8902336 元、低于 2 元。

## 最新代码真实 HTTP/SSE 补充验证

基于现有真实 HTTP 测试入口新增两条独立身份：相同英文材料问题的普通 D 与 Agent D。在独立 Redis 的实际默认作用域写入 `step_by_step`，经回环 TCP 请求真实 FastAPI HTTP/SSE；模型固定 GLM-5.3-Flash，沿用同一 fulltext/BM25、top_k=10、context_window=0、不启用重排和改写。它是两条 D 组的接线验收，不是新 A/B 效果试验。运行入口 `scripts/m2_current_http_smoke.py`，本地真实 HTTP/Redis/两个 SDK 与模型替身的定向测试退出码 0、8 passed；预算和重放守卫测试退出码 0、7 passed；预检退出码 0、0 模型调用。此前一次测试在源码更正环境变量期间发生冻结哈希变化，发送前停止，修正顺序后复验通过，未产生真实模型调用。

两条真实 HTTP 均为 200、`text/event-stream`，原始分块响应与事件记录一致，有 `done`、无 `error`。普通模式使用一次模型调用，Agent 使用四次；两条实际最终模型请求都收到目标段落全文和分步偏好。独立逐题核对均覆盖六项原文要求且核心编号引用 `[1]` 受来源支持；普通输出采用七步，Agent 三步。普通回答把原表中的 `na` 示例扩展为普遍规则，该补充内容的全文忠实性与引用支持性为 PARTIAL；Agent 未发现明确无依据的实质补充。普通 `generation_status=COMPLETED`；Agent 的 `done` 未返回该字段，应用生成状态仍为 UNKNOWN，不能由 HTTP 200 或 `done` 推定。实际模型调用 5 次均有 COMPLETED 使用量回执。独立审查保存在 `m2-current-http-real-v1/independent-current-http-review-v1.json` 与同名 Markdown。

本次已知输入 7209、输出 3749 Token，按公开单价计算 **0.0162644 元**；此前两笔 UNKNOWN 继续全额预留。全程预算占用 **1.8107420 元**，剩余 **0.1892580 元**，最高保守占用 **1.8713104 元**；供应商实际账单及此前无效凭据 401 的实际计费仍为 UNKNOWN。`m2-current-http-independent-reconciliation-v1.json` 独立核对 500 次受控尝试（498 COMPLETED、2 UNKNOWN）、身份、响应、用量、租约与来源，退出码 0。测试目录中的隔离数据库保留供复核，生产服务未改。

该真实运行明确启用了 Langfuse 现有 `e12`、`LOCAL_ONLY`、BASIC 和关闭快照策略，并请求 flush；实际云端回读及字段隐私核对见后续增量记录，不能仅凭 flush 判断导出成功。完整 M2 仍不能宣布通过：实际 Hybrid/Rerank 偏好对照、Agent 稳定引用及工具选择与偏好的完全语义隔离尚未完成。

## 真实 HTTP Trace 云端回读及用量缺陷

已对两条实际请求的 Langfuse Cloud Trace 分页回读：普通 `20dec26b38cafeb408caf460a3b5011d`，Agent `ffaeeb05f55de0be5eccdcff367816f1`，所属项目和 `development` 环境一致，共找到 15 个 Observation。普通模式可见偏好读取及 `preference_policy_applied=true`；Agent 可见偏好读取，但两个承载 `memory.delivery` 的 Agent 包装 Observation 未在 Cloud 中找到。本地共 4 个 Agent 包装节点缺失，分别有紧邻的 `sdk.unsafe_span_rejected` 记录。因原始完整 SDK span 属性没有留档，无法排他性证明每条拒绝的具体属性。

对四个本地模型响应的已保存元数据进行纯规则检查，`usage.output_token_details.reasoning` 是数字 Token 计数，但当前 `_forbidden` 按 `reasoning` 键名拦截；在诊断副本中只移除这个数字键后，四份元数据不再命中该规则。这是解释云端包装节点缺失的强证据，仍不能当作找回的原始 exporter 拒绝细节。已对云端回读字段、展开的长 metadata 及输入/输出执行问题、来源正文、答案和凭据的精确或前缀扫描，未发现命中；该扫描不能证明绝对无泄漏。Cloud 回读总体 **PARTIAL**。完整字段、节点、隐私和用量检查见 `var/preferences-acceptance/m2-current-http-cloud-verification-v2.json` 与同名 Markdown。

5 个云端 Generation 中 4 个用量与供应商最终回执一致；`agent.quality` 的云端和本地用量错误地显示 90270 / 11630 / 101900 Token，供应商最终回执为 590 / 151 / 741。原始流式响应 153 个对象均携带累计 usage，逐对象相加恰好得到上述虚高数；当前 LangChain 把每段 usage 合并时相加。Cloud 的 `costDetails` 为空，用量不是费用。预算守卫使用最后一条供应商用量，独立费用账本没有因这项遥测错误重复计费。此缺陷 **FAIL**，历史 Cloud 记录不覆盖，不能用该错误数字计算总 Token；后续修复需保留供应商差异和真实 UNKNOWN，不能推断所有供应商都使用累计用量。

## 遥测用量和数字计数的最小修复

已在 `backend/app/services/observability.py` 和 `backend/app/services/llm.py` 做仅限 Trace 的修复：真实 SDK 的原始 `usage` 或非流式返回的 `response_metadata.token_usage` 继续记录；只有 LangChain 聚合 `usage_metadata`、无法确认累计或增量语义时，Trace 用量标为 UNKNOWN，不从最后一个分片猜测全部供应商语义。模型返回对象、请求、回答、既有业务指标和预算逻辑未改。数字 `output_token_details.reasoning` 在已确认供应商用量的**复制副本**中，仅在非负整数且无冲突时规范为 `reasoning_tokens`；敏感内容过滤器未放宽，字符串推理、布尔值、负数、对象和冲突字段仍不被此处理放行。

有意义的替身复现通过实际 ChatOpenAI 流式处理：供应商最终 7/4 Token 被 LangChain 累加成 28/13，而回答文本不变。修复后 `backend/tests/test_trace_usage_authority.py` **12 passed、退出码 0**，包括累计流、仅末尾用量流、非流式、内存 Langfuse 导出、数据不变和隐私边界；`test_provider_unknown.py` 与 `test_llm_answer_limit.py` **25 passed、退出码 0**。关联 E1.2 回归 **33 passed、2 failed、退出码 1**；使用精确的修复前源码通过只读导入覆盖、包括子进程，复现同样两条失败：重排测试预期 0.6、实际 `None`，以及 Agent 测试夹具硬编码根模式为 chat 导致验证失败。它们未被本次改动引入，也未为通过测试修改 RAG 行为。

修复前两份源码按字节保存在 `var/preferences-acceptance/telemetry-correction-20261104/`，SHA256 与原真实运行冻结清单完全一致；老验证器仍指向当前工作树，因源码变化正确报 `Source code snapshot changed`，没有修改旧清单或把旧运行冒充新版本。精确命令、前后哈希、失败对照和日志在该目录。此前真实 HTTP 与 Cloud 结果属于修复前版本；新版本真实模型 Cloud 回读暂为 **NOT RUN**，不能把旧 Trace 当作修复后实测。

修复后另发送一次明确标识为 SYNTHETIC_ONLY 的合成 Trace，不调用模型或真实检索。经 Langfuse Cloud 实际回读，3 个节点按 `qa.request → agent.agent_node → agent.quality` 正确关联；Agent 节点内的 `memory.delivery.preference_policy_applied=true` 可见，数字计数被记录为 `reasoning_tokens`，合成 Generation 的用量为 7/4/11。该次本地没有 unsafe-span 拒绝；凭据、合成标记原文未出现在扫描范围内。逐项验证见 `var/preferences-acceptance/m2-postfix-synthetic-cloud-v1/verification.json` 与同名 Markdown。合成结果只证明该测试路径，修复后的真实模型和真实 HTTP Cloud 回读仍是 **NOT RUN**，旧 Trace 不被覆盖。

为验证历史账本在源码变化后仍可独立核对，零模型调用对账在内存中仅将两处源码哈希替换为保存的修复前文件哈希，先逐字节确认其与旧清单一致，其他源码仍逐项核对当前哈希；结果退出码 0、PASS、原始 96 身份、历史占用 1.7161116 元。证据为 `telemetry-correction-20261104/saved-source-reconciliation-v1.json`。此检查验证旧回执和旧源码身份，没有执行旧模型请求，也没有声称当前工作树与旧清单相同；后续补充实验及本轮真实 HTTP 的账本应继续与它分别合计，不得只用该旧数字作为最新总预算。

遥测修复后的关联记忆与 Agent 回归命令：

```powershell
$env:PYTHONPATH='backend'
$env:PYTHONIOENCODING='utf-8'
$env:DATABASE_URL='sqlite://'
$env:ENV='test'
& .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --tb=short --junitxml=var/preferences-acceptance/telemetry-correction-20261104/memory-agent-final.xml backend/tests/test_memory_chat.py backend/tests/test_m2_explicit_acceptance.py backend/tests/test_agent_quality_outcomes.py backend/tests/test_agent_request_parameters.py
```

实际退出码 0、**142 passed、0 skipped**，日志和 JUnit 同目录 `memory-agent-final.log/xml`。这不包含新的真实模型调用，也不将修复前的真实 HTTP 回执重标为新版本。

## 修复后真实 GLM 与云端 Trace 复核

使用修复后的代码、独立题号和隔离的真实 HTTP/SSE 路径，针对已有 Word 材料执行一次 Agent 验证；模型为云端 `zai-org/GLM-5.3-Flash`。真实执行退出码 0，6 次供应商调用均 COMPLETED、0 新增 UNKNOWN。独立账本核对退出码 0：本次价格计算 0.0203956 元，累计受控预算占用 1.8311376 元，包含两笔历史 UNKNOWN 的 0.099456 元完整预留；运行期间最高保守占用 1.9611528 元。旧的无效凭据 401 的账单及供应商实际账单仍为 UNKNOWN。

独立复核实际 200/SSE 响应、17 个事件和 6 次供应商原始回执：最后回答覆盖全部 6 项 Gold，最终请求收到原文及分步偏好，但回答没有编号行内引用；两处补充将原表内容泛化，全文忠实性 PARTIAL。`done` 未提供 `generation_status`，应用生成状态仍记 UNKNOWN。问题本身也明确要求分步，不能将本次输出用于证明偏好导致格式变化。逐项依据见 `var/preferences-acceptance/m2-postfix-real-agent-v1/independent-review.json`。

Langfuse 实际云端回读 24 个 Observation，本地开始记录 26 个；意图分析包装层及其 Generation 未在 Cloud 找到。本地运行日志存在一次批量导出失败，但未记录受影响节点，不能断定缺失的唯一原因。3 个 Agent 包装节点的 `memory.delivery` 均可见；3 个非流式生成的用量与供应商回执一致，2 个质量评分流的用量标为 UNKNOWN，未再显示虚高累计值，未找到的意图生成无法比较。回读字段及展开 metadata 的限定扫描未发现问题、原文、答案和非公开凭据的匹配；Langfuse SDK 的公开项目标识存在于 scope metadata，其值未写入报告。云端结果为 **PARTIAL**，完整对照见 `var/preferences-acceptance/m2-postfix-real-cloud-audit-v1.json`。

另增加 `backend/tests/test_m2_agent_citation_regression.py`，用实际 Agent 图和模型/检索替身检查偏好开关下的两轮检索、重试、来源编号、重复与重排，不引入答案格式规则。与已有 Agent 来源映射测试一起执行退出码 0、**41 passed、0 skipped**，证据 `var/preferences-acceptance/m2-agent-citation-20261104.log/xml`；它不能证明真实模型的语义或引用一定正确。

Hybrid/Rerank 替身测试初次运行发生意外外发：模拟响应缺少请求关联，实际重排代码进入聊天打分回退，测试使用合成 `q`、`candidate` 和无效测试凭据 `mock`，日志显示 `AuthenticationError`；没有成功的模型结果。原始状态码、精确时间、请求次数和供应商回执没有记录，费用 UNKNOWN，不推断为一次或特定 HTTP 状态。事实和随后仅修复测试回退的记录见 `var/preferences-acceptance/m2-hybrid-free-accidental-auth-v1.md`。后续替身测试须先阻断网络及重排聊天回退；该历史请求不重试。


## 云端 Qwen Hybrid 回归与偏好请求兼容性（增量）

初次隔离运行使用云端 `Qwen/Qwen3.5-4B`、`BAAI/bge-m3` 和 `BAAI/bge-reranker-v2-m3`，不是 Ollama。80 个冻结片段建立隔离索引；11 次供应商尝试中 10 次完成，偏好组请求被 HTTP 400 拒绝。供应商错误为 system 消息位置非法；成功组角色为 system/user，失败组为 system/system/user，其余参数及原有消息一致。该对照强烈指向连续 system 消息的兼容性问题，但提供商内部规则未知。初次默认回答冻结 Gold 5/6、引用可支持，见 `var/preferences-acceptance/m2-hybrid-free-run-v1/independent-review-a-v1.json`；不得作为修复后版本结果。

对普通 RAG、Agent 最终回答和无知识库路径，在原系统消息内追加独立偏好段落；不改变检索配置、证据规则或回答格式校验。`backend/tests/test_memory_chat.py` 执行 47 passed；Agent 相关五组回归 106 passed，均退出码 0。Agent 回答节点仍绑定工具，偏好与所有可能工具决策的完全语义隔离为 PARTIAL。

修复后的新版本复用已建索引，普通默认组实际 Hybrid（vector/BM25 权重 0.7/0.3）及 10 条重排后答出 6/6 Gold，但有额外无依据内容，全文忠实度与引用支持性 PARTIAL；实际编号映射一致。偏好组云端返回 HTTP 503（提供商 code 50508），生成及用量 UNKNOWN，不重发；不能得出同题 A/D 比较。独立逐题审查见 `var/preferences-acceptance/m2-hybrid-postfix-run-v3/independent-review-answers-v1.json`，供应商回执见 `var/preferences-acceptance/m2-hybrid-postfix-v3-independent-audit-v1.json`。这两题调用 `ChatService.chat_stream`，不能称为网络 HTTP/SSE 验收。

启动隔离续测时先后发生 Redis 参数格式错误和检索前异常，两次均有零供应商调用的独立回执；离线复现支持 GBK 控制台下日志输出触发 `UnicodeEncodeError`，但后一运行未保存完整堆栈，具体原因仍为推断。两个失败记录保留在 `m2-hybrid-postfix-run-v1` 和 `m2-hybrid-postfix-run-v2`。实际账单和重排用量 UNKNOWN；真实计费需以供应商账单核对。


### Qwen 修复后分段回归及逐题复判

同一修复后应用源码、冻结题集和检索配置计划 12 个身份（3 题 × 普通/Agent × A/D），实际 10 个应用层 RETURNED、2 个生成 UNKNOWN：流程普通 D 返回 503，PDF 普通 A 为 ConnectError 且没有响应。不能将 10 个 RETURNED 全视作有效答案：流程 Agent D 返回检索状态不确定，未实际调用检索工具，也未形成实质答案。其余 9 个可评分核心答案与各自冻结 Gold 一致；这是开发样本中 9/9 个可判定答案，不是把 UNKNOWN 排除后得到的系统总体准确率。逐题和来源检查见 `m2-hybrid-postfix-run-v3/independent-review-answers-v1.json`、`m2-hybrid-postfix-run-v4/independent-review-answers-v1.json`、`m2-hybrid-postfix-run-v5/independent-review-answers-v1.json`。

流程 Agent A 的例子将合同截止从下一季度末误写为本季度末；Excel 普通 A 和两个 Agent 将来源不能独立确认的货币单位写为美元；PDF 两个 Agent 存在原页不能确认的补充或内部矛盾。5 个形成实质回答的 Agent 均没有行内数字引用，尽管部分返回了来源列表，不能将来源列表等同正确引用；普通 Excel D、PDF D 的可检查引用得到支持。流程题偏好组未形成实质答案，Excel 与 PDF 的 step-by-step 偏好不适用，因此此处没有可支持偏好因果判断的有效 A/D 对照。不可回答题依冻结 Gold 评判，原 PDF 物理第 10 页核对过，但不能据此证明整份文档均无答案。

修复后分段运行实际供应商尝试为 v3 6、v4 29、v5 19；其中 v3 503 和 v4 ConnectError 的生成与费用未知。已知聊天 Token：v3 721 输入 / 396 输出，v4 95011 / 1644，v5 65704 / 667；真实账单 UNKNOWN。v4/v5 rerank 返回未提供模型身份字段，只能确认请求标识；记录的统计包含 Agent 意图、评分和生成，不能全部算作最终回答。原始首次运行另有 11 次尝试，不能并入修复后对照。真实 Agent 启用重排时设置 top_k 10，实际预取 20，重排保留最多 10；详细回执见对应目录的 `independent-audit-20261104.json`。

启动命令退出码 v3 1、v4 1、v5 0；退出码 0 仅说明 v5 运行完成、结果仍需复判。各调用的失败、未知和未运行不改写为成功；原始结果及失败证据均保留。各段入口为隔离 `ChatService.chat_stream`，不是网络 HTTP/SSE；Cloud 未启用，该组不能证明 Langfuse 云端写入。此前 GLM 独立网络 HTTP 与云端回读应按其独立版本和结论评估。

修复后的相关离线回归：`$env:PYTHONPATH='backend'; $env:PYTHONIOENCODING='utf-8'; $env:DATABASE_URL='sqlite://'; $env:ENV='test'; .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --tb=short backend/tests/test_memory_chat.py backend/tests/test_m2_agent_citation_regression.py backend/tests/test_agent_engineering_repairs.py backend/tests/test_agent_tool_observation.py backend/tests/test_m2_explicit_acceptance.py backend/tests/test_agent_request_parameters.py`；退出码 0，153 passed、25 warnings，见 `var/preferences-acceptance/m2-qwen-system-message-regression-final.log`。不代表真实生产 Agent 工具隔离及引用已通过。

隔离索引与 SQLite：源索引约 1.99 MB；续跑复制小索引和 SQLite，而非重复计算全部 embedding。各次 Chroma 文件使用后哈希不同，尚未证明是可安全丢弃的完全重复副本；保留待核实，未删除原始资料和历史结果。磁盘清理前后的完整数值未记录，为 UNKNOWN。


## 更正：Excel T07 币种（追加复核）

原先对 `office50:031` 的“美元不受原工作表支持”判断有误。复核本地原件 `var/preferences-acceptance/m2-source/excel-2.xlsx` 的 `T07`：C5 为 `(In thousands)`，C9 的 Alabama 原始数值为 `108149.892`，同一列汇总行 C7:G7 的 Excel 数字格式包含 `$`（`"$"#,##0`）。美国 IMLS 官方 [2018 年补充表第 29 页 Table 7](https://www.imls.gov/sites/default/files/2021-02/fy2018_pls_tables.pdf#page=29) 的汇总值带 `$`，Alabama 显示为四舍五入的 `108,150`（单位：千）；其值与 Excel 原始小数一致。因而回答“108,149.892 千美元”，即 108,149,892 美元，在事实和原文件证据上成立。

此前模型实际看到的解析文本确实没有 `$`；这是解析/送达遗漏了单元格格式，不足以将符合原文件的美元表述判为模型捏造。本次按原文件证据更正 Excel 四条回答的币种结论，保留原始评测和请求快照。Excel 普通 B 的全国 Total3 脚注作用范围歧义仍需保留；Agent Excel B 只有文末来源而缺乏行内引用的事实不变。

对本轮六条 Agent 回答，按原文件支持性修正后：核心正确 6/6 不变；全文忠实度由 PASS 3、PARTIAL 3 修正为 **PASS 5、PARTIAL 1**（剩余季度 B 的 RFI 扩写）；引用支持由 PASS 3、PARTIAL 1、MISSING 1、SOURCE_LIST_ONLY 1 修正为 **PASS 4、MISSING 1、SOURCE_LIST_ONLY 1**。行内引用 4/6 不变。普通 Excel A 的忠实度从 PARTIAL 修正为 PASS；普通 Excel B 因脚注范围歧义仍为 PARTIAL。以上只修正美元问题，不改变其他题的判定，不修改冻结 Gold。逐项修正的机器可读记录为 `var/preferences-acceptance/m2-agent-preference-ab-20965c41264e4759b79309df82d74b60/currency-correction-v1.json`。

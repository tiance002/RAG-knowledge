# RSI R0 离线实验入口验收

**工程入口 PASS，E1c 正式效果 NOT_RUN。** 2026-11-03 在 Windows 工作树 `D:\RAG-R0` 验收。实际运行 3 道合成业务样例，2 道形成有效配对，1 道符合预期地阻断；本地替身收到 4 次生成请求，外部模型请求为 0。工程断言覆盖来源、送达、隔离和归档。正式候选、留出集、收益和晋级留给后续 RSI。

## 本次实测

题目、输入哈希及两臂预期先冻结于 `experiments/r0/{synthetic,cases,expected}.json`。A 用基线普通聊天扩展送达，B 只从其已排序、已校验的成员中省去同一来源位置的重复项。编号均为公开合成 ID。

|样例|运行前的预期|实际客户端证据|工程断言|
|---|---|---|---|
|delivery|A 送 header、row、copy；B 保留 header 和独立命中 row。来源、数值行、表头与单位齐全|两臂均送达 report.pdf 的“季度 \| 收入（单位：万元）”及“春季 \| 120”。A 送了第二份相同数值行。上下文 A 106 字符/166 字节，B 72 字符/118 字节|符合|
|collision|两份文件共享 chunk_1，仅允许 public 库；两臂只送 color|两臂实际证据段均为 29 字符/49 字节，包含 colors.pdf 的“颜色：青绿”。对完整请求检查，另一库的合成标记出现 0 次|符合|
|missing-owner|预期有 1 个种子，但 file_id=missing 无可核验归属；A/B 前阻断|准备阶段记录 `missing FileDocument ownership: missing`，输出目录中无 A/B 回执|符合|

**分母。** 总题数 3，实际配对 2，预期阻断 1，非预期失败 0。两个有效配对共 2 个独立种子，两臂各保留 2/2。评分器核对了数值、表头、单位以及颜色字段共 4 项来源关联断言，两臂各 4/4。实际证据段合计 A 135 字符/215 字节，B 101 字符/167 字节。计长包含编号、来源行和换行；第一题单计成员正文是 30→22 字符，其余差异来自被省去的来源行和分隔符。本组三题没有已知结构或来源退化，阻断题的覆盖、长度均无可比值。Token 未测，回答正确性、真实推理耗时、成本收益及正式选优均未运行。

**从共同输入到请求。** 第一题快照 SHA-256 为 `1e48f0dc63dbcffc90b365c36c12815b8a58f33e07989e66524acca5936f38c5`。三项都在同一文件，row 与 copy 的页、bbox、父组、标题、正文相同。B 保留独立种子 row。A/B 实际消息哈希分别以 `6ed43323e1bc` 与 `5b921f2fbb1f` 开头。碰撞题的两个请求哈希相同。证据来自 `ChatService._chat_stream_normal → build_chat_context → generate_grounded_candidate → chat.completions.create`。末端由本地替身捕获真实消息。检查每个编号来源行及正文在系统消息中的位置，分别存储偏移与 SHA-256；核验时从保存的请求按偏移切回原文逐项复算。预期答案只在父进程评估。模型返回固定占位文本，不参与打分。

## 如何运行、恢复及核查

本机复制到 PowerShell 执行。输出必须是新目录，每次新建一个小型运行记录；命令可重复使用。

```powershell
Set-Location 'D:\RAG-R0'
$env:RAG_TEST_EXTRA_SITE_PACKAGES = 'E:\RAG quention\.venv\Lib\site-packages'
& 'D:\RAG knowledge\.venv\Scripts\python.exe' scripts/run_rsi_offline.py --manifest experiments/r0/synthetic.json --out (Join-Path 'var' ('r0-public-' + [guid]::NewGuid().ToString('N')))
```

本次同一命令指定 `--out var/r0-public-01`，退出码 **0**。在该目录查看 `results.json`、`report.md`、`case-*/result.json`；完整请求、来源及偏移在对应的 `A/capture.json`、`B/capture.json`。阻断原因在准备快照和逐题结果内。合成回放从有序种子和冻结 SQL 材料开始，真实在线检索、重排及远程生成均未运行。

|职责|变更文件与行为|
|---|---|
|清单与恢复|公开夹具与配置位于 `experiments/r0/`。A 为 baseline-expanded，B 为其子代 verified-exact-duplicate-v1；固定配置见 synthetic.json。JSON 身份按 UTF-8 规范化内容计算，排序键、无多余空格。运行目录保存完整配置及实际使用的脚本源码，versions.json 记录源码的原始字节哈希。|
|隔离执行|共用 CLI 为 `scripts/run_rsi_offline.py`，子进程为 `scripts/rsi_delivery_worker.py`。每题 prepare 一次，A/B 分别在各自目录使用同一快照及新的进程。子进程屏蔽外部网络，Chroma 为替身，SQL 为内存 SQLite，普通聊天不连 Cloud。不在共用入口导入旧 E2.0 脚本。|
|配对与归档|匹配 QID、实际读取的上游快照、固定配置指纹及评测器版本；末端请求可以不同。单臂失败或配对不符保留证据并计入总数。已存在的输出目录拒绝复用；中断后保留现场，恢复时从同一清单在新目录重建，不宣称支持断点续跑。|
|旧回放兼容|修复 `scripts/run_e20_delivery_ab.py`，两种入口共享 `scripts/replay_source_preparation.py`。复现原来只插 DocumentChunk 时扩展实际为空。现在要求可信文件与 KB 归属、验证预期种子及每个返回成员，并向真实扩展器传入请求 scope。已知但不在请求范围的文件可以在目录中，用于检验隔离。显式声明的正常零命中可以通过准备。旧六题脚本需另提供经过核验、绑定快照哈希的 `--source-manifest`。|
|定向回归|`backend/tests/test_rsi_offline_runner.py` 与 `backend/tests/test_replay_source_preparation.py`。前者用 stub 检查故障、旧输出保护、恢复和错误身份，后者用真实内存 SQL 与当前扩展器检查资料准备和来源隔离。|

实际定向命令如下。使用相同的 D 盘 Python，设置 `$env:PYTHONPATH='D:\RAG-R0\backend;E:\RAG quention\.venv\Lib\site-packages'` 后分别调用 `-m pytest <文件> -q`。旧业务题集没有运行。

|文件或检查|退出码与结果|
|---|---|
|backend/tests/test_e21_context_selection.py（基线已有）|0，18 passed|
|backend/tests/test_replay_source_preparation.py|0，6 passed，32 条依赖弃用警告|
|backend/tests/test_rsi_offline_runner.py|0，20 passed，包括换小清单/输入的 stub 工程验证|
|公开三题 CLI、独立逐回执读取与偏移/内容哈希核对|均为 0，2 对配对、1 次预期阻断|
|选择器接口收尾后的独立复核|将同一条选择规则提取到 `select_member_indices`，明确输入文件归属的说明。对 4 份既有基线成员与实际回执复算所选顺序，4/4 一致，退出 0；py_compile 退出 0。没有再次执行三道业务样例。|

恢复原始实测版本时，在独立的 `90d86ce87e8c76c8c45f5f296160ddf53de71d54` 工作树中保留公开清单与输入，按 `versions.json` 比对 `implementation/` 内三个脚本的哈希，将它们放入该工作树的 `scripts/`；A/B 配置与清单按各自指纹核对。这样可还原产生上述回执的精确代码，包括接口收尾前的 worker。当前源码中的正式候选扩展点是 `select_member_indices(members, chunks, files, seed_ids, selection)`，返回有序选择的索引，不接收 Gold 或 QID。请选择受版本管理、经过审查的代码，不在清单里动态执行外来脚本。

## 资源、历史资料和后续 RSI 调用契约

- 远端基线已有 E2.0 回放、E2.1 选择工具、普通聊天、来源校验和两份工程台账。基线没有 `scripts/e1_historical_replay.py` 或 `scripts/run_e12_acceptance.py`；依赖这两个脚本的历史复跑入口不在本轮调用链。原仓库没有根 progress.md 或 docs/adr 索引。
- 仅本地可见的历史材料包括 `D:\RAG knowledge\var\e20-20261030\research-frozen-contexts.json` 和 `var\e2-1\` 下的成员账本/标注。只检查了目录和快照结构，六题快照内没有独立 files 归属表。本轮未核实可用于正式比较的外部归属清单、正式候选与独立留出集；未读取原库填补缺口。历史 E1/E2 的质量或性能数字未并入本次结果。
- Python 3.13.0，SQLModel 0.0.27、SQLAlchemy 2.0.55、Pydantic 2.14.1、FastAPI 0.115.5、LangGraph 1.0.5、langchain-core 1.7.0、openai 3.40.0、Langfuse 5.0.4、pytest 8.3.5。复用现有 D 盘解释器和 E 盘补充依赖，无安装。新建一个 detached 工作树，保留给负责人验收；无新容器、语料副本或持久索引。三题运行目录的文件共 77,508 字节。原 D 盘仓库保持在 57f9972，开工及收尾均有 52 个状态项，未 reset、clean、提交、推送或部署。
- 正式计划为 `experiments/r0/e1c-planned.json`，**NOT_RUN**。后续 RSI 应先冻结获准的输入、文件→KB 可信目录、来源哈希、独立留出、评测口径及候选源码/配置。正式输入应去除评审字段，只给工作进程问题、请求范围、可靠归属、位置、成员和种子。把正式 Gold 和字段/表头/单位放入仅评分器读取的 evaluation 文件，引用各自规范化 SHA-256。每臂每题一次，最多 20 题，外部调用预算 0。将经审查的 B 策略名注册到 worker，在一份**新** READY 清单中绑定配置、父代与版本。统一命令为 `python scripts/run_rsi_offline.py --manifest <已冻结的READY清单> --out <新运行目录>`。生成质量或真实费用需另外的实验设计和预算。
- 工程经验的依据是 case-001 的上游快照与 A/B 回执、case-002 实际请求以及 case-003 的阻断记录。关于是否扩大 E1c、是否节省真实推理成本或晋级候选，本轮不作判断。

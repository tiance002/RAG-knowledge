# 问答性能优化：隔离候选与验收进度（2026-11-11）

本报告接续 `qa-performance-p0-p1-20261111.md` 和 `provider-cold-start-diagnosis-20261111.md`。业务基线 HEAD 为 `2041bfb5355bab5c0a4e123d413dabcbd619125a`；本轮改动尚未提交。已有工作区改动保留，正式 ECS 服务未切换。

## 最新版本与验收范围

后续真实进展：已按负责人单独授权完成一次 Chroma 384→512→384 MiB 临时试验和 **1 道真实 Hybrid＋Rerank 问答**。数值答案正确，实际 Provider Prompt 与 Cloud 输入哈希一致，Cloud 问答树 10 节点与本地相符；引用精度有同页邻段限制，完整 Token/费用仍 UNKNOWN。恢复 384 MiB 后的只读元数据检查再次超时。详见 `docs/qa-performance-live-baseline-20261111.md`。本报告下方“真实基线尚未运行”等表述保留早期现场时间线，以该专项报告的后续回执为准；各候选真实 A/B 尚未通过，不能将这一题升级成整体质量或提速结论。

P6 与 Token 记录补齐后，当前组合源码在 ECS 的 30 个相关模块通过 **386 项测试，退出码 0，0 failures/errors/skipped，1,076 warnings**。随后仅修复应用退出时的额外孤立 Trace 风险，另外两模块的 **6 项 ECS 定向测试退出码 0**；两组范围重叠，不能相加，也未为该小修复重跑全部 386 项。

386 项回执位于 `ecs-targeted-r6-20261111-r1/ecs-evidence/`，最后退出修复回执位于 `ecs-targeted-r7-20261111-r1/ecs-evidence/`。均为本任务 planning 目录中的真实命令、OS 退出、原始日志和 XML，全部使用现有环境与私有回环网络，不调用外部模型或生产端点。当前 117 个源码/辅助文件与 R7 清单一致；实际启动器核对完整的 79 个 app Python 文件也通过。最终启动清单为 `final-current-source-20261111-r7.json`，SHA-256 `9648e8d3ecccbb014914a66dc6118e5e229ef469a8ac4cf293334512a95dba1d`。

R7 的一次性代码副本已全部删除，只保留三项差异、清单和回执；可以从 R6 的公共源码归档加 R7 差异恢复，未留下又一份运行代码或测试索引。后续单题真实基线已完成，但各候选的真实质量、时延和费用收益仍未验证；共享 Chroma 恢复原限额后仍会阻断进一步测量。

## P2–P5 冻结版本组合验收

最终 Chroma 首次初始化 v2、原始历史身份和流式状态接线均完成后，执行同一集成脚本，新增首次初始化及历史身份测试：**退出码 0，306 passed / 182 warnings，0 failures / errors / skipped**，耗时 62.12 秒。命令参数、原始日志、逐项 XML 位于 `integrated-final-20261111-r2/` 中的 `integrated-p2-p5-command.json`、`integrated-p2-p5.log`、`integrated-p2-p5.xml`。

先前 292 项组合验证及各次专项测试保留作版本对照，范围重叠不相加。全工作区 `git diff --check` 检出了已有修改的 `pytest_html_report.html` 中的空白；未改该报表。限定本轮源码/测试/脚本/报告范围的检查退出码 0，不把无关报表格式当作产品失败。

可复制命令：

```powershell
Set-Location 'D:\RAG knowledge'
& '.\.venv\Scripts\python.exe' '.planning\2026-11-11-qa-performance-p0-p1\run-integrated-p2-p5.py' --out ('.planning\2026-11-11-qa-performance-p0-p1\integration-repeat-' + [guid]::NewGuid().ToString('N'))
```

该命令供之后复跑，新增目录保护已完成结果；实际本次命令仍以 `integrated-p2-p5-command.json` 记录为准。复跑参数未改变测试范围，只新增输出位置参数，不能覆盖旧证据。

以上为合成 Provider、真实本地 SDK/SSE/受控 HTTP 传输及状态契约验证，不能替代真实模型回答的事实和引用审核。CRLF 提示未被算作产品缺陷。

### ECS r5 同版本验证

已将上述 v2 冻结源码更新到独立目录 `/opt/amrag/experiments/qa-performance-p2p5-r5-20261111-01`，复用既有 Python 和 pytest 工具；没有为兼容 ECS 修改任何产品或测试断言。**同一 306 项范围在 ECS 通过：退出码 0，185 warnings，0 failures/errors/skipped**。pytest 耗时 21.32 秒，仅说明套件执行时间，不用于推断真实问答速度。

归档含 84 个冻结文件和 28 个必要测试/辅助源码，共 1,382,576 字节，压缩包 415,320 字节；无环境密钥、数据库、原始语料或索引。原 r3 的 105 个已知源码在暂存、测试与清理后均匹配。测试在仅有回环接口的新网络命名空间执行，外部模型、Cloud 导出及生产端点调用均为 0。

已核对真实子进程退出、测试进程及命名空间结束，并删除自身 808,352 字节的临时数据和缓存；源码、日志、XML 与退出回执保留。一次 SCP 收集超时只影响回读，改用小归档成功，测试没有重启。原始证据及实际操作系统命令见 `ecs-targeted-r5-20261111-r1/ecs-evidence/os-exit.json`、`results/results.xml`、`cleanup.json`，位于本任务 planning 目录。该结论仍为合成工程验收。

## 范围与开关

用户已批准继续实施剩余必要改动，并要求候选相互隔离，以便后续单因素对照。以下开关均由服务端环境控制，不能根据合成测试自动成为正式默认。

主要修改位置：

- `backend/app/services/bounded_chroma_executor.py`、`bounded_chroma_bootstrap.py`、`vector_store.py`、`retrieval.py`：有界查询及首次初始化、资源归属、取消和原检索策略接线。
- `backend/app/services/observability.py`、`backend/app/main.py`：沿用 SDK 批处理的可选刷新策略、生命周期关闭及异步生成器取消时的资源释放。
- `backend/app/services/agent_evidence_projection.py`、`agent_workflow.py`：模型请求副本的正文去重、可选首次直接检索。
- `backend/app/services/chat.py`、`grounded_generation.py`、`llm.py`：实际正文流、明确终态和 usage 来源、裁剪前的原始历史身份。
- `backend/app/services/retrieval.py`、`task_effect_context.py`：成功查询向量的请求内复用及只读 effect scope 判断；`llm.py`、`scripts/run_performance_baseline.py` 同时修复数字 usage 留存与按请求去重统计。
- `scripts/celery_migration_acceptance.py`：更新现有隔离验收入口、当前角色/队列与副作用回执、进程归属和临时资源收尾。

各定向测试文件在最终命令 JSON 中逐一列出。P0/P1 的客户端、Rerank 和测量入口变更另见对应先前报告，不把已有未提交的前端和数据文件列成本轮修改。

| 候选 | 开启方式 | 默认/回退 | 实际变化 |
|---|---|---|---|
| P2 Chroma 查询卸载 | `RAG_CHROMA_EXECUTOR_ENABLED=true` | `false` | 数据库范围和 Embedding 已确定后，在有界线程中初始化检索专用客户端并执行原 Chroma 查询；原 SQL Session 不跨线程 |
| P2 SDK 批处理 | `RAG_OBSERVABILITY_FLUSH_POLICY=LIFESPAN` | `REQUEST`，无效值也使用 REQUEST | 保留 SDK 现有队列和节点，结束请求时不主动同步 flush；退出时最终 flush |
| P3 工具正文投影 | `RAG_AGENT_EVIDENCE_PROJECTION=CITATION_BODY_DEDUP_V1` | `OFF` | 只在模型请求副本中移除已在同一工具 context 中逐字出现的 citation.content |
| P4 首次直接检索 | `RAG_AGENT_FIRST_RETRIEVAL=DIRECT_KB_V1` | `MODEL`，未知值也走 MODEL | 单轮纯文本、明确知识库范围、工具仅为知识库检索时，省去首次模型选工具；评分、重试、生成保持原流程 |
| P5 Normal 真流式 | `RAG_GROUNDED_STREAM_ENABLED=true` | 默认 `false` | 仅在普通知识库回答、已有可用引用且路由实际关闭时逐段发送正文；其他路径保留原行为 |
| P6 请求内查询向量复用 | `RAG_QUERY_EMBEDDING_REUSE_ENABLED=true` | 默认 `false` | 仅在同次检索内复用相同有效绑定及完全相同标准文本请求的成功向量，仍逐库执行原范围查询 |

P4 V1 对所有多轮请求保留模型选工具。原因是 `retrieval_query` 在理解失败后仍可能等于原问题，不能仅凭非空字段推断追问已消歧。该问题由交叉审查发现，已在本轮候选中修正。

原始历史身份在裁剪前由角色序列确定并传入 Agent，避免只有上一条助手消息的追问被裁剪成“单轮”。历史身份未知也保留原模型入口。`p4/candidate-r2.json` 记录当前候选、公共共享源码归档、哈希和回退；早期 `candidate.json` 与测试日志保留，不能当作最终源码版本。

## 本次实测

### P3：实际 SDK 请求体差异

本地 HTTPX MockTransport 捕获实际 LangChain SDK 序列化请求，使用公开合成正文，外部业务模型调用为 0。

| 指标 | OFF | 候选 |
|---|---:|---:|
| 请求 JSON 字符数 | 4,217 | 4,171 |
| 请求 UTF-8 字节数 | 5,579 | 5,529 |
| 同一工具结果内正文副本 | 2 | 1 |

完整 context、来源编号、定位、工具调用 ID、其余消息和参数均保留；工作流原始消息、来源注册表和引用正文未改。不同来源的相同文字、不同轮工具结果不在本候选中合并。以上仅证明送达结构与字节变化；真实 Token、回答质量、费用及延迟收益未由此证明。

### P4：真实 StateGraph、合成 Provider 回执

命令：

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH = 'backend'
& '.\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_agent_direct_first_retrieval.py backend/tests/test_agent_engineering_repairs.py backend/tests/test_agent_evidence_projection.py --junitxml=.planning/2026-11-11-qa-performance-p0-p1/p4/integrated-final.xml
```

该命令在补充原始历史标记前执行，退出码 **0**，**64 passed / 15 warnings**；不是新源码的最终回归。原始日志 `p4/integrated-final.log`，逐项回执 `p4/integrated-final.xml`，位于同一任务 planning 目录。

验证了正常首轮高分和首轮低分后重检两条真实图路径：候选只省去第一次工具选择调用；检索实际参数、候选引用、最终合成答案、评分值、重试次数均一致。确定性分发在消息和事件中标记 `workflow_direct_retrieval`，不伪装成模型 Generation。多轮、图片、无知识库、其他工具、已检索请求保持原入口。

针对原始历史标记，`backend/tests/test_agent_history_signal.py` 的后续定向测试退出码 **0**、**4 passed / 7 warnings**；当前完整组合回归结果以之后的实际运行记录为准。

该结果不证明真实模型一定少调用一次：最终模型仍保留重新调用检索工具的能力，也不证明真实质量或耗时收益。

### P2：已完成的定向验证

- Chroma 定向范围：**51 passed / 4 deselected，退出码 0**；日志 `p2-chroma/final-scoped-tests.log`。初次扩大范围时另有 4 条查询改写夹具失败，原始失败记录保留；因未证明改前状态，不称其为“既有失败”。
- flush 最终生命周期测试：**4 passed / 6 warnings，退出码 0**；关联测试与原生 SDK 内存导出器验证见 `p2-flush/`。不同命令测试范围重叠，不相加作为独立测试数。
- 慢 Chroma 期间事件循环可继续调度；取消等待者不提前释放仍在执行的线程容量；来源过滤与排序不变。HTTP 适配只支持已核对的 Chroma `1.3.7` 请求形态；不支持时明确失败。
- 两种 flush 策略的回答和终态一致；内存导出器中根、检索和 Generation 的父子关系一致。后续 Cloud 只验证了两条合成 Trace 的回读，限制与退出时的诊断抑制见下一节，不能据此宣布真实问答 Trace 已验收。

### P2 v2：首次客户端初始化一并卸载

r1 仅处理查询，首次同步构造仍会触发身份/租户元数据等待；这是 ECS 启动调查证实的缺口。v2 沿用同一默认关闭开关，让检索专用 HTTP 客户端延迟创建，将首次构造、正常身份/租户/数据库验证、原查询放进同一个有界工作任务。SQL Session、范围计算和 Embedding 仍留在原请求任务，同步入库路径保留。

固定 Chroma 1.3.7 的短期 SDK 钩子只处理本次独有 Settings 所创建的主/Admin 系统，失败及退出时关闭其会话并按对象身份移除自身注册，保留无关客户端；已配置认证在会话分配后失败的路径也有实际测试。PersistentClient 按路径共享的系统不由此候选移除或停止。能力不支持时保留明确事件；若 BM25 仍有可靠证据，既有降级可继续提供业务结果，但不能把该结果计作 P2 能力验收通过。

定向命令 `python -m pytest -q backend/tests/test_bounded_chroma_bootstrap.py backend/tests/test_bounded_chroma_executor.py` 退出码 **0，24 passed / 43 warnings**，日志 `p2-chroma-bootstrap/pytest-final.log`；之后已纳入本报告顶部最终 306 项组合回归。源码与依赖清单为 `p2-chroma-bootstrap/candidate-v2-manifest.json`，公共源码归档为同目录 `candidate-v2-public-source.zip`。独立只读审查没有发现具体资源归属缺陷。

边界：HTTPX timeout 是 I/O 超时，不是整项操作的绝对总时限；PersistentClient 本地操作也没有 HTTP 时限。SDK 最终 flush、线程原生操作发生异常挂起时，没有可宣称的可靠总退出上限。进程被强制终止或 SDK 有界队列溢出仍可能丢失未导出节点。上述 v2 仍未在 ECS 真实查询及模型质量对照中验证。

### P2 Cloud 合成回读与退出修复

复用已授权项目和缓存的官方 CLI，实际发送了两条合成 Trace，每条均有 **3 个 Observation、0 个 Generation**。Cloud 回读的 Trace ID、Observation ID、父子关系和数量均与本地保存值一致；没有模型调用，没有真实问题、私有正文、Gold、Score 或 Dataset 写入。

- [REQUEST 合成 Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/c0b1d154960a8f8fecc555bfa3780c02)
- [LIFESPAN 合成 Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/41ab7c509da49a98e289f396aa4c51a6)

重要限制：这两次发送发生在退出修复前，测试脚本抑制了关闭后的额外计时事件。因此 Cloud 结果证明合成传输和回读，**不证明修复后原样 main 的 Cloud 端到端路径**。没有为此重发 Trace 或扩大两条上限；当前原样 main 的 Cloud 端到端仍为 NOT_RUN，后续真实问答时一起验证。

调查确认 REQUEST 的最终 flush 在根关闭后还会生成计时事件，可能另开孤立 Trace。本地产品只改一处：退出阶段调用 `flush_observations(emit_timing=False)`，请求内原有计时仍保留。真实 SDK 内存导出器和生命周期测试本地/ECS 均 **6 passed、退出码 0**。完整证据、源码前后哈希、Cloud 差异和回读为 `p2-cloud-flush/candidate-evidence.json`。Cloud 故障注入没有运行；原有本地 fail-open 测试单独保留。两条冷进程合成耗时不是实际问答延迟 A/B。

### P5：Normal 知识库回答真流式候选

源码开关 `RAG_GROUNDED_STREAM_ENABLED=true` 默认关闭，仅在检索成功且引用已固定、有效路由关闭时生效。来源先于正文发送；普通生成使用与缓冲路径相同的证据、提示词和输出上限。完成原因缺失时标为 UNKNOWN；截断或错误保留已发送正文，取消时关闭底层流并释放连接池名额。实际 Provider 未返回 usage 时记录 UNKNOWN，不推算 Token。现有前端已按 `answer_chunk` 增量追加、展示终态并保留错误前的内容，本轮没有改动前端。

执行命令：

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONPATH = 'backend;.'
& '.\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_grounded_streaming.py backend/tests/test_provider_unknown.py backend/tests/test_performance_timing.py backend/tests/test_lifespan_flush_policy.py backend/tests/test_normal_grounded_answer.py backend/tests/test_grounded_candidate_snapshot.py
```

该命令针对后续 P4 历史标记修改前的冻结源码，退出码 **0**，**64 passed / 9 warnings**；当前组合源码的完整回归另行记录。原始日志 `.planning/2026-11-11-qa-performance-p0-p1/p5-stream/pytest-complete-final.log`。本地回环 SSE 测试使用真实 OpenAI SDK 和 HTTPX MockTransport；两段正文的到达有受控时间间隔，不调用外部 Provider。测试覆盖原提示词与输出上限、引用顺序、截断、缺失完成原因、错误、取消、usage 来源和默认关闭行为。

实测曾发现真实 SDK 取消后连接池名额未立即释放，失败日志 `p5-stream/pytest-managed.log` 保留；修复观测装饰器对内层生成器的关闭后，上述最终测试覆盖普通与 E12 观测路径。更早的回环测试夹具曾读取一次本机 `system_settings` 并失败，原始日志 `p5-stream/pytest-r2.log` 保留；最终测试已将设置和 Redis 替换为夹具。没有真实模型请求或外部网络请求。

P5 的 `done` 与部分输出后的 `error` 现在都带 `response_delivery=streaming`，同一标记也写入对应 `qa.answer` / `qa.error` Trace；默认缓冲路径未添加该标记。现有基线脚本保存完整终态载荷。改动后的定向命令 `PYTHONPATH=backend;.; python -m pytest -q backend/tests/test_grounded_streaming.py` 退出码 **0**，**17 passed / 9 warnings**，日志 `p5-stream/pytest-delivery-final.log`；本地真实 SSE 分别验证正常终态和部分输出后的错误终态。

候选源码与测试快照、依赖版本及 SHA-256 清单见 `.planning/2026-11-11-qa-performance-p0-p1/p5-stream/candidate-manifest-r3.json` 和同目录 `candidate-p5-public-source-r3.zip`。前三个源文件的修改前快照及补丁单独保留；`observability.py` 的补丁包含从 HEAD 至当前的既有 P0/P2 和 P5 改动，不能误称为纯 P5 补丁。当前源码同时包含其他独立候选，是否启用分别由各自开关控制。

真实 Provider 的流式传输、Cloud Trace 回读、质量及引用判定、首正文和完整回答耗时均为 **NOT RUN**；不能以合成流测试宣称真实加速或验收通过。

## ECS 隔离验收进度

复用原虚拟环境、只读生产 SQLite、现有 Chroma 与 BM25 索引，未复制整库或语料。候选独立监听 `127.0.0.1:18203`，正式端口 `8200` 保留。

r3 冻结源码第一次测试 **223 passed / 1 failed，退出码 1**。失败原因是增量包漏带 `scripts/run_e12_acceptance.py`，不是父子关系断言不成立。补齐公开辅助脚本后，只复跑失败项：**1 passed，退出码 0**。应记录为原始 223 项通过加 1 项纠正复跑，不宣称另跑了一套 224 项。

本机后续 P2/P4/P5 仍是独立候选，不自动带入已冻结的 r3 性能基线。真实问答、完整阶段耗时、Cloud 回读及候选质量比较，以后续实际回执补充；不能用 GET 健康或测试进程退出代替答案判定。

### 真实 A/B 前的启动器接线

复用 `live-baseline/serve_candidate_r4.py`，保存原字节后修正单因素控制：启动器先显式固定所有候选为基线，再只开启一个实验臂；有效开关、P2 参数、PID、源码清单及实际源码哈希写入回执，导入应用前核验完整源码集合。旧 `--projection` 参数保留兼容，冲突组合拒绝，不继承环境中未声明的其他实验开关。

P2 臂取消无关的旧客户端预初始化，改为约束实际延迟创建的检索客户端：只读集合包装和向量写方法禁用，SDK 真实身份/租户验证及超时仍执行。带历史的追问题集必须能匹配真实前题回执和消息历史，Gold 不发送给 API。两组离线检查退出 0，包含实际 Chroma 1.3.7 回环验证，见 `live-baseline/single-arm-repair-evidence.json`。后续增加的 `P6cache` 臂只是显式单因素接线；产品候选和相应源码清单另行冻结，现有三个单知识库题无法用于其收益验收。

### 启动阻断：真实问答尚未发生

原 r3 隔离进程在 Chroma 初始化租户/数据库元数据阶段等待，未进入 Uvicorn 就绪，**真实 QA 与业务模型调用均为 0**。核对 PID 和启动身份、确认仅有本机 Chroma 连接后，已结束本任务的隔离进程；没有重启共享 Chroma。

Chroma `1.3.7` 客户端创建的同步 HTTP 会话默认 `timeout=None`。隔离 r4 harness 为自己构建的同一 Settings 对象所对应的主/Admin 会话加上有限超时，保留身份、租户及数据库验证，初始化后恢复 SDK 类方法；不放宽来源或索引检查，不修改生产代码。Windows 与 ECS 的受控真实 SDK 构造测试均通过。

最终真实只读预检与 harness 的 10 秒上限一致：租户 GET 返回 200、约 3.213 秒；数据库 GET 约 10.010 秒超时，状态为 `BLOCKED_CHROMA_METADATA_FINAL_BOUND`。较早心跳及身份接口可响应，不足以证明租户/数据库可用。ECS 为 2 CPU，观察时 loadavg 为 4.52 / 3.87 / 2.79，MemAvailable 约 1.4 GB；这只是同一时点的资源状态，不能据此认定超时原因。

因此停止真实基线，r4 实际问答 API 未启动、无模型请求、无本轮 Cloud QA Trace。原错误、进程身份和有限超时回执保留在 `live-baseline/`。真实性能、质量、并发 1/2/4 及候选 A/B 均不能宣布通过。

### 进一步只读定位与待批准的资源试验

实际 Chroma 为 `amrag-file-chroma.service`，PID 3561886，非 Docker。有效 `MemoryHigh=402653184`（384 MiB），`MemoryMax=infinity`。当前 cgroup 用量约 409 MB，略高于软上限。1.006 秒采样内 `memory.events.high` 增加 1,086，完整内存压力 avg10 为 81.68%，完整停顿计数增加 868,432 微秒；两个线程停在 `mem_cgroup_handle_over_high`。同次观测 CPU 约为一个核心的 18.8%，CPU 限流次数为 0，OOM/OOM-kill 为 0。

这些事实证明该服务当时正在遭遇内存高水位回收/限速，与元数据长等待吻合；尚无调整前后的反事实验证，不能声称排除了数据库锁等其他原因。45 分钟窗口内 journal 没有可见记录，不等于服务没有发生过错误。

最终于 **18:40:42 CST** 再次只读核对：服务 PID/启动身份和 MemoryHigh 都未变，当前用量 **418,570,240 B**，1.00075 秒内 high 事件增加 **162**，完整内存压力 avg10 为 **66.03%**、完整停顿增加约 **994,193 微秒**，未观察到自然恢复；无 OOM/OOM-kill。该次没有调用元数据端点、模型、资源写入或服务信号。最新原始证据为 `live-baseline/chroma-revalidation-closeout-r8.json`，不是沿用早期快照假定当前仍故障。

负责人已明确回复 **“批准临时调整，测后恢复”**。批准范围仅为一次运行时 MemoryHigh 384→512 MiB 试验、有限元数据就绪检查和一题冻结表格问答，结束、失败或到期后恢复 384 MiB；不重启服务、不改数据库或索引、不在这次试验中追加其他候选 A/B。操作与回退为：

```sh
systemctl set-property --runtime amrag-file-chroma.service MemoryHigh=512M
systemctl set-property --runtime amrag-file-chroma.service MemoryHigh=384M
```

执行前重读当前属性和可用内存，并准备独立于 SSH 连接的有界回滚进程，避免覆盖其他会话已改变的资源策略。该项为共享服务资源调整，不把它混成隔离候选代码变更。批准不代表已经执行或通过；实际限额变化、模型请求和恢复结果以此次试验回执为准。原始采样和完整数值见 ECS 阻断专项报告及其只读证据。

## 尚待完成

1. ECS 持续 API 首题及逐题审查、真实阶段耗时和 Trace 核对；追问绑定真实前题回答。无答案题缺少当前全部知识库范围的可靠 Gold，不擅自沿用窄范围旧 Gold。
2. P5 真流式的真实 Provider 传输兼容、真实问答 Cloud Trace 回读、逐题回答和引用质量以及首正文与完整回答耗时对照。
3. 依据真实 BM25/Chroma/Embedding 阶段耗时决定 P6 调度或请求内查询向量复用；未测量的收益不写成已实现收益。
4. 真实问答与入库并行时的完整性能仍需后续评测；以下 Celery 独立可靠性验收已完成，不能把健康探针或一次全文召回当作真实 QA 延迟结论。
5. 按获准范围把已冻结候选放入后续 ECS 单因素 A/B；本地代码、定向回归和恢复依据已归档。没有自动提交、推送或正式启用候选。

判定原则：本次三个已核验的基础开发题每题只查询一个知识库，不能展示查询向量复用收益。后续只为已由源码确认的多库重复计算加入默认关闭候选，实测目前限于合成请求计数。未对 Hybrid 两路直接套 gather；条件评分也没有校准后的可省调用条件，原有评分和阈值保留。不能把这些未运行项记成实际机制收益。

## P6：请求内查询向量复用候选

源码确认 `_vector_search` 和 `_hybrid_search` 会按库重复调用查询 Embedding；连接池复用不等于向量复用。按用户“可以先实施、保持隔离”的追加授权，V1 只在同次检索内保存成功向量，默认关闭。每个库仍在原任务解析模型绑定、检查有效性，并执行自己的索引范围、过滤、排序和查询；不存在跨请求、跨 Agent 工具轮次或持久化缓存。

键包含有效端点、模型、凭据、绑定/来源/上下文约定和精确查询，内部身份不输出到日志。旧 Ollama、Doubao、图像/文件相关请求、未知约定及任何非空 effect scope 均保留原调用。只保存有限数值的成功向量快照；每次命中返回独立列表，失败、UNKNOWN 和取消不缓存或重试。原 `use_cache` 检索结果缓存仍保持旁路，不能把它说成已命中。

本地定向命令见 `p6-query-embedding-reuse/candidate-source-freeze.json`，退出码 **0，94 passed / 898 warnings**，含 39 项新用例。实际 HTTPX MockTransport 表明 vector/hybrid 两种模式下，两库相同请求的 **POST 从 2 次变为 1 次**，两库得到的向量、作用范围、证据、分数和状态一致；不同身份、原始查询变化、跨请求、取消及未明结果等边界保留。独立只读审查未发现具体阻断问题。

该结果只证明受支持合成场景的调用减少；真实 Token、费用、时延和回答质量收益仍为 UNKNOWN / NOT_RUN。既有三道单库基线题对此候选为 **NOT_APPLICABLE**，不得用它们宣称优化有效。Hybrid 并行尚未实施。

后续 ECS R6 已把这些用例纳入上述 386 项组合验证，原始 XML 保留 vector/hybrid 两项 2→1 POST 的实际属性；没有新增真实模型或改变现有三题的 Gold/范围。

## P0：修复供应商已提供但本地漏记的 Token

审查确认：Embedding 的数字 usage 原先只进入 Prometheus 计数，关闭 Prometheus 时会丢失；部分旧 Rerank/LLM usage 没有实际请求 ID，无法可靠归因。现于既有 `provider.request_finished` 中按原 `local_request_id` 保存供应商实际返回的输入、输出和总 Token，覆盖 HTTP、可信 SDK、直接生成及已迭代的流式 usage；不新增调用、Generation 或观测节点。

显式返回的 0 保持已知；缺失、布尔、无效、别名或重复记录冲突的计数保持未知。基线汇总按实际请求 ID 去重，旧未关联事件单独保留，不根据顺序猜配或重复累加。Prometheus 关闭也可记录这些数值；缺少可靠全链路完成证据时，整体覆盖继续是 UNKNOWN。

本地定向命令见 `p0-provider-usage/candidate-source-freeze.json`，**退出码 0，80 passed / 9 warnings**。原先一次同范围日志显示 80 passed 但退出码凭证丢失，保留为 UNKNOWN；核对原进程已结束后才进行一次有明确退出回执的重跑，数字不相加。没有外部模型或云服务调用，也不据合成用量推导真实账单。

后续 ECS R6 的 386 项范围也已包含用量、重复记录和 UNKNOWN 等相关用例，退出码 0。历史未关联记录不会被修改，也不倒推为新的完整账单。

## Celery 独立实测

复用并更新原 `scripts/celery_migration_acceptance.py`，未另建任务执行器。使用 ECS 现有 Python、Chroma 可执行程序及现有 Redis ELF 启动独立进程，不安装依赖或拉镜像；生产 Redis 进程作为程序来源，未访问其数据或对其发信号。测试运行在仅有 lo 的新网络命名空间，私有 Redis/SQLite/Chroma/上传及 Artifact 目录，角色顺序运行、并发 1、继承一个 CPU。

最终运行 ID：`b4a00f329f1843788141030ef26340ce`，**真实进程退出码 0，8 项业务检查 PASS，自动清理成功**。覆盖真实 Worker 完成并发布、全文可读取、重复投递不改变块/不增加 Embedding、租约 fencing、对实际 PENDING 副作用所属 Worker 执行故障后 UNKNOWN、显式重复消息不重发该不明请求。租约过期通过私有记录的时间控制触发，不能声称验证了自然等待整个租约或 Redis visibility timeout 的重投。

前一次完整运行退出码 **1**，业务 8 项通过而原生 Redis 清理失败：Redis 改写进程信息导致环境标记不可读。已通过已记录 PID/启动时间、进程组、私有命名空间、目录及 ELF 身份修复清理逻辑；独立回归及之后完整复跑均通过。原失败不覆盖。最终原始 stderr 仍有 `AsyncResult.__del__` 在私有 Redis 已关闭后的忽略异常，作为非阻断清理顺序技术债记录，没有为了消除日志而继续改业务或重跑。

41 次资源采样中，测试自身进程 PSS 合计最大观测约 **570.19 MiB**，RSS 合计最大观测约 **833.73 MiB**；主机可用内存最低观测约 **834.25 MiB**。这些是采样观测，不是精确峰值。入库期间 60 次健康探针中位数约 **1.46 ms**、最大约 **4.52 ms**；入库后一次全文召回约 **157.06 ms**。没有真实生成模型，不能声称 QA P95 或质量改善。

本次只有 **2 次本地合成 Embedding HTTP**，外部业务模型调用 **0**。独立审查确认测试进程和私有命名空间全部结束、原 Redis 身份及原 105 个暂存源码未变。两个一次性合成 Chroma 索引已删除，逻辑字节合计 **367,272 B**；保留 SQLite UNKNOWN/attempt/effect 账本、检查点、小型公开输入和日志，约 1.25 MB。没有删除正式资料、索引或其他阶段数据。

命令、退出码、原始日志及资源清理证据见 `docs/qa-performance-celery-reliability-20261111.md` 与 `.planning/2026-11-11-qa-performance-p0-p1/celery-reliability/`。

## 复用和保留

任务归档位于 `.planning/2026-11-11-qa-performance-p0-p1/`。`source-before`、各次日志和逐题回执保留以便恢复；私有快照、请求正文和临时凭据不进入 Git。本次 ECS 未进入 Cloud QA 初始化，专用临时 Langfuse 凭据已删除；两条 Cloud 合成测试仅在本地进程读取原有授权配置，没有复制密钥。v2 的 84 文件核对保留作历史，当前完整 117 文件及 app 79 文件核对见 `final-current-source-20261111-r7.json`、`final-current-source-verification.json`；Celery CLI 有独立源码冻结和真实回执。

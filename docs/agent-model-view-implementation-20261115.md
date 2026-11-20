# Agent 模型证据视图精简：实现与 ECS 隔离回归

日期：2026-11-15（北京时间）。任务目录延续 `agent-model-view-20261114`。

## 1. 结论及边界

**本地实现与定向回归 PASS；ECS 隔离运行 3/3 正常完成；来源编号和后端证据映射检查 PASS。** 两道此前失败的开发题这次均正常生成并答出目标数值。打印机对照题核心正确，但缺少数字行内引用，引用表达记为 **PARTIAL**，不能用 `source_mapping_status=VERIFIED` 代替答案引用质量。

正式服务没有替换或重启，代码未提交、未推送。没有重解析、重建索引、修改 Gold、模型、Prompt、检索阈值或权重。没有加入严格答案校验；原有 32,000 **应用请求 JSON 字符**预算保持不变，它不是模型的 32k Token 上下文规格。

本轮只改 Agent 发给模型的工具消息副本。后端完整 `context`、`citations`、`source_registry`、父子关系、来源身份、哈希和原始工具消息继续保留。普通聊天链路没有进行本次视图替换。

## 2. 版本、实现和恢复

- 本地目录：`D:\RAG knowledge`，分支 `codex/bm25-stats-write-optimization-20261112`，HEAD `7c7415f85a063e3f7645f676bd22f7c8f766a732`。该工作区已有大量未提交成果，未 reset、clean 或覆盖。
- 本机验证环境：Python 3.13.0；langchain-core 1.7.0、langchain-openai 1.7.3、langgraph 1.0.5、openai 3.40.0、httpx 0.28.1、pytest 8.3.5。复用现有依赖，未安装升级。
- ECS 复用 `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python` 与既有隔离源码 `/opt/amrag/experiments/hybrid-concurrency-20261114-r1/followup/current-source/backend`，只更新两个产品文件；不是把整个本地工作区覆盖到正式目录。
- 新版本 `AGENT_EVIDENCE_VIEW_V2` 为候选源码默认值；隔离健康检查的 `projection_env=null`，实测使用的是代码默认行为。`RAG_AGENT_EVIDENCE_PROJECTION=OFF` 可回到未投影的原始消息用于诊断。旧 V1 不继续执行或冒充 V2；显式配置旧名字会报不支持，正式部署前须检查。此次正式与隔离环境均未发现该旧变量覆盖。
- 两产品文件的本轮修改前副本位于 `var/agent-model-view-20261114/before/`；本轮增量补丁、前后哈希在 `delta/changes.patch`、`delta/model-view-manifest.json`。恢复时按这些副本和补丁处理本轮变更，不能拿 HEAD 强行覆盖整个文件，因为文件原先已有未提交修改。

| 文件 | 本轮改动 |
|---|---|
| `backend/app/services/agent_evidence_projection.py` | 复用现有 `project_agent_evidence_messages`，升级为 V2。验证后端注册映射与完整正文；从调用副本剔除明确的实现元数据和 citation 重复正文；保留 canonical context、来源编号及语义位置/结构字段。 |
| `backend/app/services/agent_workflow.py` | Agent 默认调用 V2，传入实际 `source_registry`；路由与偏好组成后、原预算及 SDK 调用前投影。已有升级路径复用同一视图，并按实际工具 schema 检查原预算。 |
| `backend/tests/test_agent_evidence_projection.py` | 视图、副作用、未知语义字段、表格单位/位置、图片派生标记和真实 ChatOpenAI 网络层捕获验证。 |
| `backend/tests/test_dynamic_routing_agent.py` | 补测试替身缺失的 `model` 字段，生产路由逻辑未改。 |
| `scripts/replay_agent_model_view.py` | 恢复旧失败回执的真实消息与工具 schema，零外部调用回放和检查。 |
| `scripts/agent_model_view_probe.py` | 复用现有隔离 HTTP/SSE 探针，另存实际后端 registry、投影前后消息和逐来源正文检查；不参与答案判定。 |
| `scripts/monitor_hybrid_followup.py` | 在已有资源保护器的白名单中增加本任务隔离 API/任务；沿用原资源底线，不控制正式进程。 |

产品最终 SHA256：投影模块 `3ee53e4b13646c86cc31bc4a191b0d0224abb51dc99e5b73b7f41b5bb1b4f68b`；Agent 工作流 `9d5310f509ef265362404cb9785981dc9b3ac77e454886e0846efef60a2e1694`。本轮增量包 SHA256 `a1c0d08d1f66503d52d7040d3b43edbe6de180bdf67cacedfac343f6af1acb00`。

## 3. 模型视图与保留证据

处理流程为：完整检索工具结果/注册表 → 现有路由与偏好处理 → **克隆模型调用消息并投影** → 原预算检查 → 原观测/实际模型调用。不是在检索、Rerank 或父子扩展阶段删证据。

- `context` 原字节内容保持相同，已选正文只在这里保留一次；citation 中相同的 `content` 不再重复送模型。正文中的表头、单位、数值、内部章节及文内说明不改写。
- 模型保留 `source_id`、顺序、可读标题、页码/工作表/区域等实际存在的位置，以及必要语义元数据。未知位置保持未知，不生成假页码。图片描述的模型生成、派生/不确定性标记保留；真实图片内容本轮没有另做视觉正确性实验。
- citation 容器中的文件/KB/Chunk UUID、父子/seed/group 列表、评分、哈希、整理过程字段不再反复送模型，完整原始对象仍在后端。已明确识别的实现字段才删除。
- 不将 `source_metadata` 内所有字段泛化为技术信息。直接 `id`、`score`、`generation`、`members` 等可能承载业务含义的字段以及未知嵌套对象保守保留；不按 `*hash` 等宽泛规则递归删除。
- 正文、编号或注册映射无法确认时，**原工具消息原样传递并记录未投影原因**，不是拒绝答案，也不猜测映射来节省字符。
- 路由仍看原输入，避免本轮顺带改变动态路由选型；实际模型收到精简副本。将路由负载估算改为投影后输入属于后续独立议题。

## 4. 本地验证

### 4.1 同一已保存超限输入的回放

输入原始回执：`var/hybrid-followup-20261114/followup/current-receipts/68d4bd03546448a5b334a7dcb18d07a9.json`。

```powershell
Set-Location 'D:\RAG knowledge'
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe scripts/replay_agent_model_view.py --backend-root backend --receipt var/hybrid-followup-20261114/followup/current-receipts/68d4bd03546448a5b334a7dcb18d07a9.json --out var/agent-model-view-20261114/saved-input-replay.json
```

实际退出码 **0**。原请求 **42,359 字符 / 43,967 UTF-8 字节**，投影后 **17,969 字符 / 19,421 字节**，减少 **24,390 字符，约 57.6%**，原 32,000 字符预算检查 PASS。全部 9 项已选正文、context、来源顺序、消息协议与工具 schema 保持一致，外部调用 0。

该历史回放的 registry 是从归档工具引用构造的代理，不能独立证明当时运行态注册表；真实运行态另由下一节隔离探针核验。历史归档的 `qa500_import.source_label` 九项均为整理数据集标签 `EnterpriseRAG-Bench`，不作为事实正文送模型，后端继续保存。未知语义元数据仍需保守处理。

### 4.2 定向测试

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_agent_evidence_projection.py backend/tests/test_agent_tool_observation.py backend/tests/test_agent_request_parameters.py backend/tests/test_dynamic_routing_agent.py backend/tests/test_agent_evidence_guard.py backend/tests/test_agent_grounded_answer.py backend/tests/test_image_caption_parent_child.py backend/tests/test_generation_budget_error.py -q -o addopts='' --junitxml=var/agent-model-view-20261114/semantic-boundary-junit.xml
```

实际退出码 **0**：**90 passed，0 failed/errors/skipped，241 条弃用警告**。原始日志 `semantic-boundary-tests.log`，JUnit `semantic-boundary-junit.xml`。

测试含真实 `ChatOpenAI` 的网络层替身捕获，对比 OFF 与默认 V2 实际请求，并验证原状态、路由输入和正文不变。替身回复只证明接线/不可变性，不当作真实答案质量证据。

最初 89 项运行有 1 个路由替身缺 `service.model` 的失败，已保留 `focused-tests.log` 和退出码 1；仅修替身后复验。后续语义元数据风险修复并增加反例，最终是上述 90 项，不能把各轮通过数累加。没有重跑全量套件。

## 5. ECS 真实 HTTP/SSE 隔离回归

固定开发输入：`var/hybrid-concurrency-20261114/development20.json`，SHA256 `6496c8d219447a290afeff4aa612683da2bbb8a771d02c929cd7041dc84daa08`。只运行其中索引 **15、13、0** 三道开发题，没有运行冻结集。

使用现有十库非空 allowlist、当前在线父子索引、Hybrid、向量/BM25 0.7/0.3、请求 top_k=10、Rerank 输出 top_k=10、context_window=1、Qwen/Qwen3.5-4B 且实际请求 `enable_thinking=false`、BAAI/bge-reranker-v2-m3。Agent 工具内部仍按现有规则过召回 20，不修改以凑成普通模式配置。改写/联网关闭，路由 OFF 以固定模型比较该变更。实际检索、Embedding、Rerank、评分与回答均调用真实现有链路。

隔离 API 监听 `127.0.0.1:18335`；使用只读 SQLite 连接，不跑生产 lifespan/迁移/worker，不写正式数据库或索引；Langfuse 关闭，观测证据是本地捕获，**Cloud 回读 NOT_RUN**。

真实执行的任务命令（在上述既有隔离目录，环境配置由隔离服务装载，未在此展示密钥）：

```text
python -u check_hybrid_chat_delivery.py --cases development20.json --out model-view/live-agent --base http://127.0.0.1:18335 --model-id a8753099-fe56-403b-a7db-ff457a6403f5 --indices 15 13 0 --mode agent --rerank-model-id fe787636-d155-42aa-848a-24d177f417d9 --rerank-top-k 10
```

任务 `amrag-agent-model-view-test-20261115.service` **ExecMainStatus=0**。原始日志 `model-view/test.log`。

| 开发题 / 本轮运行 ID | 之前事实 | 本轮最终请求字符：同次投影前→后 | SSE 完成时间 | 回答和引用复核 |
|---|---|---:|---:|---|
| qst_0032 / `0aa17a4d766b4cbd8d1dc33965a5f839` | 此前在最终回答前 ValueError；当时未保存精确类型，不能追认一定超字符限额 | 31,375→16,660 | 9.44 秒 | COMPLETED；默认 30 分钟、有理由可至 1 小时，核心 PASS，`[1]` 支持；映射 PASS |
| qst_0017 / `52768093e4be40a68868fa0c5d8eb8ae` | 保存的旧实际请求 42,359 字符，明确 `FINAL_REQUEST_OVER_CHAR_BUDGET` | 43,105→17,983 | 14.90 秒 | COMPLETED；2.7→1.9、下降 0.8，核心 PASS；`[2]` 支持分数、`[1]` 支持版本；映射 PASS |
| terra-ecs-b1-024 / `5d710d28769d4c04bc5b6ea02968fa32` | 先前可完成的打印机对照题 | 23,851→12,368 | 12.05 秒 | COMPLETED；两个设置位置核心 PASS；**未输出 `[n]` 行内引用，引用表达 PARTIAL**，映射 PASS |

本轮投影前/后是**同一次调用消息副本的尺寸对照**；新旧真实回答不是固定候选重放的成对性能实验，不能用旧失败与新耗时声称整体延迟降低多少。qst_0032 本轮投影前已低于 32,000，也说明 Agent 工具查询/候选会变化，不能把它的历史 ValueError 全部归因于本轮尺寸变化。

三题各有一次工具检索、一次评分、一次最终回答，没有检索重试；每题 3 次真实 Chat 网络请求，共 **9 次**（工具选择、评分、最终回答），另有 **3 次 Embedding 调用记录、3 次真实 Rerank HTTP 记录**。探针同时捕获 SDK 外层与实际 HTTP 发送；只计 `network_attempt=true`，不把两层算作两次模型调用。

SSE 返回的 `usage_scope=AGENT_NODE_CALLS` 累计输入 19,138、输出 807 Token；该口径不是全部供应商调用账单，不能据此覆盖独立评分/Embedding/Rerank 的全链路用量或宣称费用节省。完整费用 **UNKNOWN**。三次最终 `finish_reason=stop`、`generation_status=COMPLETED`，没有空输出、截断或 UNKNOWN；`validation_status=NOT_RUN` 保持原样。

### 5.1 送达与映射核验

逐题实际 registry 共 **18 项（3+9+6）**：

1. 捕获的是实际 `state.source_registry`，不是仅靠最终引用重建。
2. 18 项工具 citation 均与 registry 完整对象相等；对应正文完整存在于模型 `context`。
3. 投影前后 registry 与原始 ToolMessage 保持不变；context、source_ids 顺序、tool_call_id 不变。
4. 最终**实际网络请求**中的工具消息 `content` 与投影视图完全相等；不是只检查扩展器“选中了块”。
5. SSE 的来源编号、Chunk ID 和正文能对应回 registry。

以上是当前所捕获链路的一致性检查，不把传输身份一致当成每句都被正确引用。打印机题原文的“文章2”是系统来源 `[1]` 内部文章编号，非系统来源 `[2]`。其“端口”细节见同一来源内的文章1；模型将两部分合并说明了，但没有规范行内编号。这一问题明确保留，不新增严格门禁或顺带改 Prompt。

此外 qst_0017 和打印机题原有 `expansion_delivery_status=PARTIAL` 仍保留，qst_0032 为 DELIVERED。本轮“投影前后 canonical context 不变”不代表上游全部父块/扩展内容都进入 context；没有将 PARTIAL 改成 DELIVERED。原评分仍只检查前 2,000 字符，三题 `grading_scope.context_clipped=true`，本轮没有扩大评分范围。

## 6. 资源、隔离与收尾

- 37 个一秒资源样本：最低 MemAvailable **1,906.6 MiB**、最低磁盘剩余 **11.67 GiB**；未触及 800 MiB/8 GiB 保护线。隔离 API cgroup 峰值约 **278.6 MiB**；Chroma RSS 峰值约 **486.5 MiB**。这是短时单请求串行回归，不是多会话容量/尾延迟结论。
- 最终核对正式 81 个源码/配置文件哈希，**0 漂移**；正式 API PID **778457**、Chroma PID **567489** 未变，Chroma MemoryHigh 保持 **640 MiB**，没有临时提高内存限额。
- 隔离 API、测试任务与资源保护器均 **inactive，MainPID=0，退出码 0**；Chroma `/api/v2/heartbeat` 返回 **200**。最初使用已退役 `/api/v1/heartbeat` 得 HTTPError 的诊断原件保留为 `final-state-v1-probe.json`，改查当前 v2，不认定为服务故障。
- 复用现有 venv、SQLite、Chroma、已完成索引和隔离源码；新建两个源码文件的增量包和本任务必要日志/原始回执，没有复制语料、数据库、向量库或启动新容器。
- 最终状态 `model-view/final-state.json` 记载任务原始证据约 **0.74 MiB**，任务末尾磁盘约 **11.67 GiB**；压缩证据包是归档快照，后生成的 final-state 另存，不把旧包假称包含后写文件。没有删除原始历史结果。

## 7. 证据入口、未知项及下一步

可读独立审查：`docs/agent-model-view-independent-review-20261115.md`。机器可读逐题复核：`var/agent-model-view-20261114/reviewed-results.json`（开发代理复核标签，非负责人手工签字）。原始 SSE 与模型/registry/实际网络请求位于同目录的 `model-view/live-agent/` 和 `model-view/receipts/`，按上表运行 ID 一一对应；其中包含获准测试正文，仅保存在本机和现有 ECS 实验目录，未上传 GitHub/Cloud。

原始回执汇总可重算：

```powershell
$env:PYTHONUTF8='1'
.\.venv\Scripts\python.exe var/agent-model-view-20261114/summarize_receipts.py
```

实际退出码 **0**，该命令只读取保存的文件、不调用外部模型，不是运行新评测或改评分器。

| 项目 | 状态及限制 |
|---|---|
| 本地代码、定向测试、旧超限请求回放 | PASS；同一输入瘦身和正文/编号守恒已验证 |
| ECS 隔离实际 Agent、Hybrid+Rerank、模型视图接线 | PASS，3/3 完成；非全量质量/性能证据 |
| 核心答案 | 本轮开发代理及独立代理复核 3/3 PASS，不宣传总体正确率 100% |
| 来源映射/实际送达 | PASS；18 项实际捕获映射一致；不替代原资料真实性核验 |
| 答案行内引用 | 两题 PASS，打印机题 PARTIAL；没有静默判通过 |
| 全部父子扩展的送达覆盖 | 部分为 PARTIAL，继续保留；本轮不改 Context Builder |
| 图片原图语义、全格式真实样本、大样本/多轮极长上下文 | NOT_RUN；合成结构/图像派生标记验证不能代替原图核查 |
| 动态路由真实模型选择、查询改写、Langfuse Cloud、正式发布 | NOT_RUN；本轮故意固定模型、关闭改写与隔离遥测，不应推断已测 |
| 全链路实际 Token、费用节省、稳定 P95 改善 | UNKNOWN；只有相同消息字符/字节减少和本轮三题实际耗时 |

后续若批准推广，先检查部署环境无旧 V1 覆盖，使用两个文件的兼容增量进行独立发布；本轮没有执行。多轮长工具消息或未知 metadata 保守保留仍可能超预算，不能保证所有问题不再超限。打印机的行内引用表达缺失适合单独记录为后续生成质量实验，不通过新增严格校验或扩大本轮修改范围解决。

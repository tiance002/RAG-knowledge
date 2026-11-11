# 问答性能 P0/P1 实施与验证（2026-11-11）

## 结论边界

后续冷启动诊断与启动预初始化已完成，最终源码定向测试为本机191/ECS149通过。下方186/144是预初始化前的原始范围，保留作版本对照。最新追加结论、实际修改及原始证据见 `docs/provider-cold-start-diagnosis-20261111.md`。

已完成本地代码实现、ECS 隔离源码同步、定向回归和真实供应商传输冒烟。**持续 API 的冻结业务题完整性能/质量基线尚未运行**，不能把下列合成结果当作真实问答提速或正确率提升。未改正式服务，未提交或推送。

本次范围按附件第一轮 P0/P1：持续 API 测量入口、普通/Agent 异步 Rerank、受管理 Provider HTTP 连接池。Chroma、BM25、来源指纹及观测 flush 仅增加计时。没有改检索权重、模型、Prompt、质量阈值、数据库、索引或 Gold。

## 版本与现有差异

- 基线 HEAD：`2041bfb5355bab5c0a4e123d413dabcbd619125a`；分支 `codex/project-upload-20261107`。实际候选是未提交工作区，必须以源码清单哈希识别，不能只用 HEAD。
- 预先存在的前端 KnowledgeBases、output、HTML 报告、observations 和其他规划目录修改保持原样。
- ECS 隔离目录：`/opt/amrag/experiments/qa-performance-p0p1-20261111-r1`。首份源码包 95 个文件、354255 字节，SHA256 `5118cf0a954ff8e42f962e171dabbb2209b4a44b74aca3723f27c45e11d27f10`。仅源码和测试，无凭据、语料、索引或私有快照。
- 正式 API 核验 PID `3597452`、端口 `127.0.0.1:8200`，本轮未重启或换代码。隔离脚本使用现有 Python 环境及已有 pytest 工具目录；未安装依赖。
- Normal 与 Agent 的候选数差异保持：Agent 工具原本扩大检索 top_k；不能仅用请求参数一样宣称两者同配置同候选。Rerank 失败时原检索分数与真实 Rerank 分数仍分开，UNKNOWN 不增加自动重发。

| 依赖 | 本机 | ECS 现有环境 |
|---|---|---|
| httpx | 0.28.1 | 0.28.1 |
| openai | 3.40.0 | 1.109.1 |
| langchain-openai | 1.7.3 | 1.1.5 |
| langfuse | 5.0.4 | 5.0.4 |
| chromadb | 1.3.7 | 1.3.7 |
| fastapi | 0.115.5 | 0.115.5 |
| sqlmodel | 0.0.27 | 0.0.27 |

## 改动与调用链

| 文件 | 实际改动 |
|---|---|
| `scripts/run_performance_baseline.py` | 持续 API 客户端，离线预检默认，逐增量解析 SSE，分别记录正文/终态/EOF/清理。冻结输入与配置、独立输出，失败不自动重跑。 |
| `backend/app/api/chat.py` | 可选性能运行身份头；BASIC 作用域不授权正文外发。作用域只围绕内部生成器推进/关闭，避免跨任务取消时 ContextVar 错误。 |
| `backend/app/services/rerank.py` | 新增异步服务调用及批处理入口，同步/异步共用批处理策略；部分失败、阈值、映射及 UNKNOWN 语义保持。 |
| `backend/app/services/chat.py`、`backend/app/tools/retrieval_tool.py` | 正式普通/Agent 的网络 Rerank 改用异步入口。 |
| `backend/app/services/provider_http.py`、`main.py` | API 生命周期连接池，按进程/事件循环/地址/凭据摘要/TLS/超时/环境策略隔离，默认在途16、连接16、空闲保留30秒。Worker 的独立事件循环不借用旧池。 |
| `backend/app/services/llm.py`、`embedding.py` | 直接 Provider、LangChain、Embedding 接入受管传输；完整流关闭后释放名额，保留错误/超时语义。 |
| `performance_timing.py`、`vector_store.py`、`retrieval.py`、`index_lineage.py`、`observability.py` | Chroma、BM25、扩展、来源指纹、flush、Provider 等待等计时。事件失败不阻断业务，不把嵌套耗时相加。 |
| `scripts/benchmark_rerank_transport.py` | 仅 loopback 的同步/异步传输对照，不连接生产或模型供应商。 |
| 新增/调整专项测试 | 异步重排、池隔离/关闭、实际 SDK 传输、UNKNOWN、SSE 身份和取消、计时/运行器。旧替身同步改异步，未降低 Gold 或业务评分。 |

`OPENAI_PROXY` 显式设置时，LangChain 保留自己原来的代理客户端，该分支不受新池并发计时控制。未强行改变代理行为。同步本地 Rerank 推理和 Doubao 同步路径没有迁移；全链路仍不能称为完全无同步阻塞。

## 本次实测

| 验证 | 数量/结果 | 退出码 | 证据 |
|---|---|---:|---|
| 本机集成定向测试（替身/loopback） | 186 passed，0 failed/error/skipped，138 warnings | 0 | `.planning/2026-11-11-qa-performance-p0-p1/integrated.log`、`integrated.xml` |
| ECS 隔离定向测试 | 144 passed，0 failed/error/skipped，14 warnings | 0 | `ecs-evidence/pytest-r2.log`、`pytest-r2.xml`、`test-receipt-r2.json` |
| 本机旧同步/新异步合成传输 | 6 次 localhost 请求，正文/结果相同，后两次异步复用连接 | 0 | `rerank/synthetic-transport-local-20261111/report.json` |
| ECS 同一合成传输 | 6 次 localhost 请求，同样通过 | 0 | `ecs-evidence/synthetic-transport/report.json` |
| ECS 真实 Rerank 传输 | BAAI/bge-reranker-v2-m3，返回2个分数，约217.38ms | 0 | `ecs-evidence/provider-smoke-receipt.json` |
| ECS 真实 Embedding 传输 | BAAI/bge-m3，1条向量、1024维，约153.27ms | 0 | 同上 |
| 冻结业务题正确率、整题性能、Cloud 回读 | NOT_RUN | — | 不能以传输冒烟代替 |

本机186和ECS144为不同环境的重叠测试范围，**不得相加当作330个独立用例**。首个ECS测试因漏传测试辅助文件而收集失败退出2；补传已有辅助文件并核对哈希后144通过。旧失败日志保留，没有改业务代码绕过。

本机实际命令（工作目录 `D:\RAG knowledge`）：

```powershell
$env:RAG_OBSERVABILITY_ENABLED='false'; $env:LANGFUSE_ENABLED='false'; $env:REDIS_URL='memory://'
& '.venv/Scripts/python.exe' -m pytest backend/tests/test_async_rerank.py backend/tests/test_rerank_unknown.py backend/tests/test_provider_http_pool.py backend/tests/test_provider_unknown.py backend/tests/test_performance_timing.py backend/tests/test_e11_candidate_transitions.py backend/tests/test_observation_stages.py backend/tests/test_normal_evidence_guard.py backend/tests/test_retrieval_tool_status.py backend/tests/test_agent_engineering_repairs.py backend/tests/test_evidence_repairs.py backend/tests/test_e12_native_observability.py tests/test_performance_baseline.py -q -o addopts='' --junitxml='.planning/2026-11-11-qa-performance-p0-p1/integrated.xml'
```

ECS 实际完整 argv/环境版本、退出码保存在 `ecs-evidence/test-receipt-r2.json` 和 `versions.json`。重跑应使用新的日志/输出目录。

### 合成时间含义

ECS 同步三次完成耗时145.79/117.05/123.76ms，10ms轻量回调延迟135.85/107.11/113.80ms；异步153.66/101.93/101.61ms，回调延迟37.99/1.81/0.76ms。供应商替身固定睡眠100ms。连接编号为1、2、3、4、4、4。

本机异步首次约390.79ms，随后约104.22/104.10ms；首次回调延迟274.04ms。这提示初始化仍阻塞，但**旧实验不是独立冷进程，不能直接归因SSL或TLS**。用户提出的SSLContext诊断单独开展；结论及独立冷进程证据写入后续冷启动报告，不用旧结果冒充新实验。

新增真实外部调用仅上述2次：公开合成短句，1次重排+1次向量化，0次Chat，0次重试/UNKNOWN。价格/账单和完整Token金额UNKNOWN；Cloud关闭，仅本地事件核对到2条开始/结束。此结果只证明当前SDK/传输兼容。

## 真实基线的剩余前置条件

现有旧题集和索引仍在，不需重建。历史 runtime200-r4/office50 具有旧冻结材料，但不能把旧 qid、空 KB scope、旧全文检索参数直接用于当前活动 Hybrid 索引。新qa500仍未完成可用题/来源/活动索引绑定。本轮没有从引用猜知识库归属。

最小后续步骤：只读选一个来源明确的既有直接问答，补齐非空KB范围、当前索引身份、模型与全部实际检索参数，冻结 cases/config/provenance，再用同一持续候选 API 的普通/Agent分别测。调用契约见 `docs/performance-p0-baseline-contract.md`。这是完整业务基线的NOT_RUN前置，不影响本次传输工程验证。

完整服务端ASGI清理、所有Provider usage完备性尚未证明，计数输出观察下界及UNKNOWN。没有供应商时间戳不能把客户端耗时叫纯推理耗时。小样本不宣称整体P95提升。

## 资源与回退

复用现有虚拟环境、pytest工具、数据库/索引只读入口；不复制整库，不新建容器。隔离暂存约4.3MiB，留存源码和回执用于复核，所有loopback服务已关闭。主机可用内存从约1416MiB到1349MiB、空闲磁盘约9.5GiB到9.1GiB为共用主机瞬时数，不归因于本任务，也不是峰值开销测量。

正式服务未变，无需生产回滚。候选与旧版可以按源码清单区分；失败日志/UNKNOWN/历史资料没有删除。后续如果部署，需单独完成应用备份和正式运行版本核验，不能直接覆盖已有未提交源码。

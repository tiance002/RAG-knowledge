# ECS 真实问答基线阻断记录（2026-11-11）

本轮完成候选暂存、定向测试纠错、只读启动定位和候选级超时护栏验证；**真实 QA、模型调用和 Langfuse 云端验收均为 0 / NOT RUN**。共享 Chroma 的数据库元数据接口在最终 10 秒边界内未返回，因此没有发出原计划的一道表格问答。没有重试 UNKNOWN，没有修改或重启生产 API、Worker、Chroma、数据库和索引。

## 冻结版本与暂存范围

- 候选根目录：`/opt/amrag/experiments/qa-performance-p0p1-20261111-r1`；源码在其 `code/`，生产仍是 `/opt/amrag/backend`，生产 API 为 8200。
- r3 归档：`p0-p3-r3-delta.zip`，103,093 B，SHA-256 `af70ecc35ea4d30974331a3417dd48c311e86080c68de0594e0892626405b4d8`。修改清单 13 个代码/测试文件、展开 215,206 B；11 个输入、展开 98,076 B。逐一核对 105 个已知源码文件及变更前 SHA；旧文件保留在 `before-p0-p3-r3/`。此后没有复制主工作区新增的 P2/P4 改动。
- 测试补充件仅为原先遗漏的公开、自含合成验收脚本 `scripts/run_e12_acceptance.py`，32,426 B，SHA `f2e5c38806243a80631864523441fbcf174cd90f182da1a0567febc473be7de5`；记录在 `supplemental-helper-r3.json`。没有携带 `.env`、真实资料、历史快照或新依赖。
- r4 只增加隔离 harness 与受控构造测试，不修改冻结 r3 应用源码：`serve_candidate_r4.py` 18,710 B，SHA `31ed5fd1a1f69227aadf80a1429c9763e09fb11cfc9634f1cb5d572e416e20a6`；`test_harness_r4_constructor.py` 3,690 B，SHA `83f47f5ed337aaf8e14b2811ab1608a277890db4b4fb5faaa14e3e820d714ffd`。
- 输入保留 `cases-r2.jsonl`、`request-config-r1.json`、`provenance-r2.json`。已有三题覆盖直接问答、多文档、表格，来源/SQL/BM25/选定 Chroma 记录已核对；追问仍待真实父回答，无答案题仍待精确服务端范围，未伪造历史或扩大 Gold 适用范围。

参数是**通用测试授权下由执行者选定的实验参数**：Hybrid、top K 10、Rerank 开启且 top K 10、阈值 0、权重 .7/.3、邻块窗口 1、改写/网页关闭。固定使用持久化默认 `11052e2b-570d-4dc0-9090-a7aa05f8e1cd` / `XingChenAGI/Xing4.0-29B`；路由和 Agent 投影均 OFF。没有把这些数值说成用户逐项指定或当前 UI 的实测配置。

## 实际执行与退出码

以下命令均已经执行；文件位于 `.planning/2026-11-11-qa-performance-p0-p1/live-baseline/`。SSH 脚本本身保留完整 argv、断言和输出位置，未用省略的命令冒充运行证据。

| 实际入口/命令 | 退出码与结果 |
| --- | --- |
| `Get-Content -Raw -LiteralPath '.planning\2026-11-11-qa-performance-p0-p1\live-baseline\stage_r3.py' \| ssh -i "$env:USERPROFILE/.ssh/study-plan-dev.pem" -o BatchMode=yes root@120.55.115.162 python3 -` | 0；归档 SHA、105 个已知文件、13 个增量和 11 个输入核对成功，生产未变更。 |
| 同上 SSH stdin 入口运行 `launch_ecs_tests_r3.py` | 发起命令 0；真实 pytest 子进程 **1**：224 项中 223 PASS、1 FAIL、0 ERROR、0 SKIP。不能用发起进程成功替代测试成功。 |
| 同上 SSH stdin 入口运行 `rerun_missing_helper_r3.py` | 发起命令 0；仅失败项重跑退出 **0**，1 PASS，5.03 s。原始失败日志保留。 |
| 同上 SSH stdin 入口运行 `launch_candidate_r3.py` | 首次前置检查 1：指定的 `/opt/amrag/config/runtime-secrets.env` 不存在，未启动进程；修正为经授权的候选临时凭据文件后发起 0，PID 3791029。未修改生产配置。 |
| 同上 SSH stdin 入口运行 `readiness_r3.py` | 探针命令 0；候选 `/health`、`/api/v1/settings` 连接拒绝，**语义结果为未就绪**，没有问答请求。 |
| 同上 SSH stdin 入口运行 `stop_blocked_candidate_r3.py` | 0；核验 PID/启动时钟、无 QA/Provider 记录、仅本机 8001 连接后发送 SIGTERM，确认退出。 |
| `.venv\Scripts\python.exe .planning/2026-11-11-qa-performance-p0-p1/live-baseline/test_harness_r4_constructor.py` | 修正测试计时口径后 0；本机真实 SDK + 回环替身验证通过，外网/模型请求 0。首次将总构造耗时误当网络超时而失败的日志另行保留。 |
| `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/live-baseline-r4/test_harness_r4_constructor.py --code-root /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/code --output-dir /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/harness-r4-constructor-ecs/evidence` | **0**；ECS 受控构造验证 PASS；真实网络等待 0.10037 s 对应 .1 s 测试边界，外网/模型请求 0。 |
| 最终 SSH stdin 只读 GET 探针（原样代码及结果见 `chroma-preflight-final10s-r4.json`） | 探针命令 **0**，但语义为 **BLOCKED_CHROMA_METADATA_FINAL_BOUND**；租户 GET 200 / 3.213 s，数据库 GET Timeout / 10.010 s。不是 readiness PASS。 |

首次失败的精确错误是：

```text
test_actual_stategraph_tool_retrieval_descriptor_parent_is_sdk_tool
assert completed.returncode == 0
python: can't open file '.../code/scripts/run_e12_acceptance.py':
[Errno 2] No such file or directory
```

补充件没有更改产品或测试断言。最终证据是 **223 个首次通过 + 1 个补齐公开依赖后的定向通过，覆盖 224 个唯一测试、共执行 225 次**；没有重新宣称完整 224 项套件一次性通过。原始 rc=1 和重跑 rc=0 都保留在 `validation-r3/`、`validation-r3-helper/`，合并口径在 `validation-r3-corrected.json`。

## 启动阻断与已验证的候选修复

现有候选 PID 3791029、`/proc` 启动时钟 167273555 一直停在启动阶段，RSS 约 156,828 kB、2 个线程，两个 ESTABLISHED socket 均连接 `127.0.0.1:8001`。没有候选监听端口，没有 `candidate-runtime.json`，没有 `observations.jsonl`，没有 QA/Provider 启动记录。没有向它发送未注册的 USR1 或盲目重启。

只读探针显示 heartbeat 和 `/api/v2/auth/identity` 能迅速返回，而租户/数据库元数据请求会超时。安装的 Chroma 1.3.7 实现中，`Client.__init__` 在返回前调用 `_validate_tenant_database`，先 `AdminClient.get_tenant`、再 `get_database`；实际 HTTPX 会话构造为 `httpx.Client(timeout=None)`，因此可无限等待。源码定位证据见 `chroma-client-source-r3.json`、`chroma-system-source-r3.json`、`chroma-from-system-source-r3.json`。

r4 harness 仅在其构造窗口临时包装 `SharedSystemClient._create_system_if_not_exists`：核验精确版本、**同一个候选 Settings 对象身份**、实际 FastAPI/HTTPX Client 类型后，只给这批实际会话设置 10 秒 timeout；正常 identity/tenant/database 验证保留，原 classmethod 在成功/失败的 finally 中恢复。没有全局替换 HTTPX，也没有绕过租户验证。单纯先创建一个 SharedSystemClient 并不足够，因为该 SDK 的 HTTP 客户端会为各 System 生成新 UUID；主 Client 和 AdminClient 均须覆盖。

本机及 ECS 的替身测试证明主/Admin 实际会话都受限、正常验证三条端点、阻塞时有限返回且恢复原描述符。父子 SSL/客户端构造时间没有被错误相加或当作网络 deadline。`startup-stages.jsonl` 保留阶段/状态；受控测试不是共享服务或真实模型验收。

最终按实际 10 秒边界只探测一次后，数据库元数据仍超时。按已确认的停止条件，**r4 实际 QA API 未启动，表格问答未发出**。共享服务阻塞的进一步根因没有确定；不能仅凭 CPU 负载快照断言是资源不足，也不能把心跳 200 视为完整检索就绪。

## 运行版本与资源快照

ECS 实际依赖：Python 3.12.3；HTTPX 0.28.1；HTTPcore 1.0.9；OpenAI 1.109.1；LangChain 1.2.0 / core 1.2.2 / langchain-openai 1.1.5；LangGraph 1.0.5；Langfuse 5.0.4；Chroma 1.3.7；FastAPI 0.115.5；SQLModel 0.0.27。pytest 实际为既有 `offline/test-tools/pytest` 的 8.3.5，不是虚拟环境已安装 distribution；没有补装依赖。

最后只读资源快照为 2 个 CPU、load average `4.52 3.87 2.79`、MemTotal 3,714,252 kB、MemAvailable 1,472,320 kB。这是瞬时值，**不是峰值**。其他会话的父子切块/索引工作仍在运行，本轮没有停止它们。

进一步只读定位到 8001 的实际服务是 **`amrag-file-chroma.service`**，不是 Docker 容器；MainPID 3561886、启动时钟 160541991，入口解释器为既有实验虚拟环境的 Python，实际 executable `/usr/bin/python3.12`，cwd `/opt/amrag/backend`。服务 active/running、NRestarts=0，RSS 416,480 kB、10 个线程；SQLite 文件描述符指向 `/opt/amrag/backend/storage/chroma_db/chroma.sqlite3`。只记录环境变量名称，没有输出环境值。45 分钟 journal 查询返回 0 条记录，**不能据此断言不存在错误**；数据库锁、损坏等原因尚未获得日志证明。

新增明确证据是该服务的 **cgroup memory.high 压力**，而非泛称“CPU 忙”：

| 实际读取项 | 结果 |
| --- | --- |
| `/sys/fs/cgroup/system.slice/amrag-file-chroma.service/memory.high` | 402,653,184 B，即 384 MiB |
| 同目录 `memory.max` / systemd MemoryMax | `max` / infinity；不是硬上限 OOM |
| `memory.current`，相隔约 1.006 s | 408,866,816 → 409,382,912 B，均超过 high |
| `memory.events` 和 `memory.events.local` 的 high | 1,718,366 → 1,719,452，增加 1,086；oom/oom_kill 均为 0 |
| `memory.pressure` | full avg10=81.68%、avg60=79.68%、avg300=84.69%；本采样 full total 增加 868,432 μs |
| 线程等待位置 | 两个线程为 `mem_cgroup_handle_over_high`，其余主要为 futex/ep_poll |
| CPU | 约 1.01 s 内消耗 0.19 CPU 秒，约单核 18.8%；CPUQuota=infinity，nr_throttled=0 |

这些是**实际观察到的内存 high 回收/节流，与请求停顿一致**。本轮没有通过调整限额做对照，因此没有声称它是唯一根因或宣称调整后必然恢复。原始数值、路径与只读命令退出 0 见 `chroma-unit-diagnostic-r4.json`、`chroma-cgroup-memory-r4.json`、`chroma-limit-policy-r4.json`。

### 仅供负责人批准的单因素、可回退验证建议（未执行）

实际永久 unit 路径是 `/etc/systemd/system/amrag-file-chroma.service`；目前 high 来源已有 runtime drop-in `/run/systemd/system.control/amrag-file-chroma.service.d/50-MemoryHigh.conf`。如负责人批准，建议先只把 MemoryHigh **384 MiB → 512 MiB**，增加 128 MiB，保留 MemoryMax=infinity、swap、CPU 和模型/索引配置不变；不重启服务。后一次主机快照 MemAvailable=1,487,936 kB，约 1.42 GiB。扣除额外 128 MiB 的静态估算仍约 1.29 GiB，但这不是资源预留，其他任务的内存增长必须另行观察。

下列是拟议命令，**NOT RUN**：

```bash
# 变更前先保留该 runtime drop-in 原字节/SHA、有效属性及 cgroup 计数。
systemctl set-property --runtime amrag-file-chroma.service MemoryHigh=536870912

# 需要恢复时回到已经核实的原有效值；同样不重启服务。
systemctl set-property --runtime amrag-file-chroma.service MemoryHigh=402653184
```

批准后的验证也应只改变这一项：重新读取 high/current/events/pressure，再做有限的元数据 readiness，比较同样边界；只有恢复就绪才回到尚未执行的单题。若压力仍持续或影响其他任务，应按负责人确定的停止/回退条件恢复，不自行逐级放宽。原 runtime drop-in 已存在，回退应核对其有效值和原始配置差异，不能使用会撤销其他属性的整体 `revert`。本报告没有发出上述写入、任何额外端点请求或生产进程信号。

复用现有虚拟环境、生产只读 SQLite、Chroma HTTP 和冻结数据；没有数据库/语料/索引副本、新容器或 Embedding 重建。候选证据归档 18,633 B；本地该调查目录在最后统计时约 10.44 MB（主要是之前只读元数据清单）。没有本轮完整磁盘 before/after 测量，故不宣称整盘空间变化。

## 进程、凭据与证据收尾

候选只通过独立 session 脱离 SSH 运行，保留 PID/启动时钟/完整无密钥 argv。确认零 QA、零 Provider、无外部活动连接后，仅对本任务 PID 3791029 发送 SIGTERM 并确认退出；测试子进程均已结束，当前没有本任务 API 或 QA runner。未对生产进程发信号。

经已有授权复用的 Langfuse 三个字段只通过 SSH stdin 传送到候选专用 0700 目录中的 0600 文件，没有进入 argv、日志、patch 或归档。确认启动停在观测初始化前、没有本任务导出后，**只删除** `/opt/amrag/experiments/qa-performance-p0p1-20261111-r1/candidate-secrets-r3/langfuse-basic.env`；核对删除成功，空目录保留。没有删除其他凭据或数据。此后不得把 Langfuse 云端 Trace 回读说成通过：本轮 **Cloud export/readback NOT RUN**。

原始日志和已脱敏的无凭据证据已回读到本地：

`live-baseline/live-baseline-evidence-r4.tar.gz`，18,633 B，SHA-256 `1af9297300fc9aaba98d0aa13a6cfa83835c61891fae18a8d42c0f6f0b5f2aad`。解包目录 `live-baseline/ecs-evidence-r4/` 包含原始失败/修复 XML、日志、暂存回执、PID/停止回执、r4 构造测试和最终元数据阻断、候选凭据清理回执；不含凭据文件。`evidence-readback-r4.json` 重新审查了统计分母、退出码与清理结果。

后续最小动作由负责人协调：先恢复或解释共享 Chroma 数据库元数据就绪，再复用已有冻结输入、r3 应用源码和 r4 harness，重新做零问题 readiness；只有就绪后才执行仍未发出的单题。不得未经批准变更生产服务或用合成通过替代真实 QA。无提交、推送、Tag 或里程碑验收声明。

有效真实基线形成之前，P6 等依赖实测收益的优化对比继续保持依赖阻断；本报告不批准延期，也不把合成测量推广为真实问答结论。

未实现的命令不得报告通过。

## 18:18 当前状态只读复核

2026-11-11 18:18:58（北京时间）重新读取了同一服务的 systemd 属性、PID/启动时钟与 cgroup 数值，没有沿用旧快照推断服务永久阻断。实际命令为：

```powershell
.\.venv\Scripts\python.exe .planning/2026-11-11-qa-performance-p0-p1/live-baseline/revalidate_chroma_state_r5.py
```

本地及 SSH 退出码均为 **0**，原始回执为 `.planning/2026-11-11-qa-performance-p0-p1/live-baseline/chroma-revalidation-r5.json`。服务仍为 PID `3561886`、启动时钟 `160541991`，`active/running`、`NRestarts=0`；MemoryHigh 仍为 `402653184` B（384 MiB），MemoryMax 仍无限制。1.0007 秒采样内：

- `memory.current` 从 `418480128` 变为 `418476032` B，仍高于 MemoryHigh。
- `memory.events.high` 从 `2544474` 增至 `2544652`，新增 **178** 次。
- `memory.pressure full avg10` 从 `82.75%` 变为 `80.99%`；full stall 累计量增加 `915746` 微秒。
- PID 的线程数为 8；CPU cgroup 未出现配额限流计数。

因此此次没有观察到明确恢复迹象，结论为 **当前仍 BLOCKED**。按照授权条件，没有发起元数据 GET；生产端点请求、模型调用、资源限制写入与服务信号均为 **0**。持续的 MemoryHigh 事件和高内存 PSI 与阻断相符，但未经过资源调整干预，仍不将其表述为已证明的唯一原因。负责人对资源写入的批准仍然待定；未来若外部状态确实恢复，可重新按有效授权进行只读就绪验证。

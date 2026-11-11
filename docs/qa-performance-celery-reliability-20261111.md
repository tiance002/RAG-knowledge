# Celery 隔离可靠性验证（2026-11-11）

## 1. 目标与改动

本次将已有 `scripts/celery_migration_acceptance.py` 更新为可复用、可导入、默认只检查能力的隔离验收脚本。最终 ECS 完整入口实际退出码为 **0**，状态为 `PASS_ISOLATED_RELIABILITY`，8 项业务检查全部通过；首次完整运行因自动清理缺陷退出 **1** 的原始记录完整保留。最终运行仍有一条非阻断的 Python destructor 异常，不能宣称 stderr 干净。

业务代码只修改上述既有脚本，没有新增第二套业务执行器，没有改变生产路由、并发、模型、Prompt、Gold、资源限制或数据。真实应用代码复用既有公共 r3 源快照，105 个已知源码文件在上传、运行和收尾后 SHA 均未改变；新增 runner 始终作为实验目录内的独立 sidecar。没有安装依赖、拉取镜像、复制生产语料/数据库/索引、提交、打 Tag 或部署生产服务。

最终 runner SHA-256：

```text
f9c8958dd8e41866a651111a69f559420356b9bf8fbb3d36a923e77aebfb6b12
```

源代码冻结、实际命令、退出码及原始证据哈希见 [source-freeze-r3.json](../.planning/2026-11-11-qa-performance-p0-p1/celery-reliability/source-freeze-r3.json)。原始旧脚本原字节为 `original-source.py`，SHA 为 `5eae040f0ea04565a587f64f558ad59989cd19b3306eb8ebdecec913f5e47097`；中间版本与失败回执均未覆盖。

## 2. 执行命令与真实结果

以下本地命令实际执行，退出码均为 **0**。第一项验证最终脚本语法；第二项是同轮显式路径/身份参数的离线验证；第三项验证最终清理代码处理一个真实私有 Redis，其结果包含环境标记确实消失、错误 cwd/namespace/ELF SHA/start_ticks 均被拒绝、自动清理成功。

```powershell
.venv/Scripts/python.exe -m py_compile scripts/celery_migration_acceptance.py
.venv/Scripts/python.exe .planning/2026-11-11-qa-performance-p0-p1/celery-reliability/check_explicit_binary_options.py
.venv/Scripts/python.exe .planning/2026-11-11-qa-performance-p0-p1/celery-reliability/regress_ecs_native_cleanup_r3.py
```

ECS 最终 preflight 命令如下，退出码 **0**，`AVAILABLE`，空 namespace 能力探针退出码 **0**。检查到 MemAvailable 为 **1340.13 MiB**；开始完整运行时为 **1324.65 MiB**。1024 MiB 是可配置的启动预检，不是已测得的栈容量保证。

```bash
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/celery-reliability-runner-r3.py --backend /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/code/backend --output-dir /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/celery-reliability-preflight-r3 --timeout-seconds 120 --min-free-mib 1024 --redis-server /proc/1774/exe --chroma-bin /opt/amrag/.venv/bin/chroma --redis-start-ticks 1265 --redis-exe-sha256 87dd1979b0c12e68d2ed0187bb8eff8b64a2bdc6ac72db8e5c561f1758745d28
```

经主代理授权，最终完整命令实际执行一次，退出码 **0**。首次完整运行的失败已经定位，修复后只增加一个极小 Redis 回归，再执行这一轮；没有第三次完整运行。

```bash
/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/celery-reliability-runner-r3.py --backend /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/code/backend --output-dir /opt/amrag/experiments/qa-performance-p0p1-20261111-r1/celery-reliability-execute-r3 --timeout-seconds 120 --min-free-mib 1024 --redis-server /proc/1774/exe --chroma-bin /opt/amrag/.venv/bin/chroma --redis-start-ticks 1265 --redis-exe-sha256 87dd1979b0c12e68d2ed0187bb8eff8b64a2bdc6ac72db8e5c561f1758745d28 --execute
```

最终 run_id 为 `b4a00f329f1843788141030ef26340ce`。原始输出约 765 KB，完整保存在 [ecs-execute-r3.log](../.planning/2026-11-11-qa-performance-p0-p1/celery-reliability/ecs-execute-r3.log)；工具展示可能截断，判断与统计读取的是本地完整原文件。退出码另存于 [ecs-execute-r3-dispatch.json](../.planning/2026-11-11-qa-performance-p0-p1/celery-reliability/ecs-execute-r3-dispatch.json)，紧凑统计见 [ecs-final-summary-r3.json](../.planning/2026-11-11-qa-performance-p0-p1/celery-reliability/ecs-final-summary-r3.json)。远端报告为 `/opt/amrag/experiments/qa-performance-p0p1-20261111-r1/celery-reliability-execute-r3/report.json`。

历史失败与能力阻断分别保留：Windows preflight 退出 **2 / BLOCKED**；ECS r1 因当前 PATH/解释器 bin 未发现 Redis/Chroma 可执行文件退出 **2 / BLOCKED**，不是缺少 Python 包；找到既有可执行文件并显式传参后，r2 preflight 退出 **0**。首个完整 r2 退出 **1 / FAIL**，见 [ecs-execute-r2.log](../.planning/2026-11-11-qa-performance-p0-p1/celery-reliability/ecs-execute-r2.log)。

## 3. 已验证内容及适用范围

| 检查 | 结果 | 真实触发与证据 |
|---|---|---|
| `real_worker_pipeline_completed` | PASS | 真实 parse/embed/index 流程完成，发布 1 个 chunk，初始 embedding HTTP 请求 1 次 |
| `published_evidence_readable` | PASS | API 可回读已发布的公开极小文本证据 |
| `real_duplicate_delivery_no_mutation_or_embedding` | PASS | 真实 Celery 重复投递，没有改变 chunk，也没有再次请求 embedding |
| `controlled_lease_claim_created` | PASS | 两角色均静止时建立私有 lease fixture，避免与 Worker 抢占竞态 |
| `controlled_lease_recovery_fences_old_owner` | PASS | 私有 lease 时间受控移入过去，恢复后旧 owner 被 fencing |
| `pending_effect_owner_matches_owned_process` | PASS | 按实际 stage、owner token 与 attempt worker_id 定位本轮真实所属 prefork Worker |
| `owned_worker_loss_pending_effect_becomes_unknown` | PASS | 仅 SIGKILL 本轮所属 Worker 进程组，受控 lease 过期后 PENDING effect 转为 UNKNOWN |
| `unknown_effect_not_replayed_by_explicit_duplicate` | PASS | 沿实际 stage/queue 显式重投递，UNKNOWN 保留，embedding 不重放 |

真实部分包括 Redis 7.4.2、Chroma 1.3.7 HTTP、SQLite、Celery 5.6.0 prefork 父子进程、任务队列、进程丢失及 FastAPI API 回读。Embedding 是 3 维本地 HTTP 替身，输入是一份极小公开文本；整轮共 2 次替身 embedding 请求，包含故障 fixture 的请求。没有真实模型、真实问答质量或临床正确性验证。tokens/cost 为 **UNKNOWN**，不把替身当真实模型能力证据。

自然等待完整 lease/Redis visibility-timeout、自然 broker 重投递均为 **NOT RUN**。受控修改私有 lease 时间不能证明自然时钟路径或完整 Redis 可见性窗口。顺序 parse/embed 后停 Worker，再启动 index 角色，并非全角色并行负载测试；没有真实 QA 内容竞争、吞吐提升、P95、生产性能或资源策略胜出结论。

## 4. 隔离、故障修复、资源及清理

执行前拒绝会按绝对路径加载 dotenv/runtime secrets 的源树；子进程移除继承的凭据、代理、模型 ID 与外部端点，显式固定私有 Redis DB0/1/2、SQLite、工件和空 runtime secrets 文件。执行使用新的 network namespace，实际从父 `net:[4026531840]` 进入子 `net:[4026532369]`，接口仅 `lo`、路由为空。该 OS 边界覆盖 Python 与 native Chroma；Python socket guard 没有拒绝记录，但不能据此宣称 native 从未尝试 telemetry。没有外部模型可达路径，实际 paid provider 调用为 0。

复用 `/proc/1774/exe` 只借用现有 Redis ELF，不连接、重启或发信号给生产 Redis。借用前及执行前校验 PID1774/start_ticks1265 和 ELF SHA，保留 `/proc/1774/exe` 字面路径，避免解析为宿主机不存在的容器内路径。主代理授权的故障信号仅发给本轮记录的 Worker。

首轮 r2 的真实清理缺陷是 Redis 改写 process title/environment，导致 `/proc/<pid>/environ` 不再包含 `RAG_TEST_RUN_ID`；原自动清理按保守规则拒绝终止，整轮因此报 FAIL。随后用 PID/start、SID/PGID、私有 namespace、私有 cwd 和 ELF SHA 独立核实自有 Redis3801898，再仅对该组发 SIGTERM，手动清理成功；原 FAIL 未改写。修复只改变 native Redis 的归属校验：从启动即固定私有 cwd，记录完整身份元组，每次清理均重新检查；其他进程仍检查环境标记。极小回归验证四种身份不匹配均被拒绝，真实标记消失的 Redis 也能安全自动清理。

最终完整运行报告无存活自有进程。独立复查确认两轮共 14 个登记 leader PID 均已消失、两个私有 namespace 内均无活进程、生产 donor PID/start/ELF SHA 未变、105 个源码 SHA 未变。原始确认与清理回执见 [ecs-artifact-cleanup-r3-v2.log](../.planning/2026-11-11-qa-performance-p0-p1/celery-reliability/ecs-artifact-cleanup-r3-v2.log)。第一次工件清理辅助脚本因占位符替换导致 Python SyntaxError、退出1，远端未执行任何删除；原日志 `ecs-artifact-cleanup-r3.log` 保留，修正辅助脚本后退出0。此事没有触发业务重跑。

41 次进程资源采样测得自有 RSS 求和最高 **833.73 MiB**、PSS 求和最高 **570.19 MiB**、主机 MemAvailable 最低 **834.25 MiB**。RSS 跨父子进程求和重复计算共享页，PSS 更接近归属占用；采样间隔和阶段覆盖有限，均不是连续峰值保证。首次服务启动至最后自动清理信号约 **30.80 秒**，不包含所有导入、前置检查、信号后等待和 destructor 收尾；不能当完整 wall time。

同时记录 60 次回环 health 探针，范围约 **0.88–4.52 ms**、中位数 **1.46 ms**；入库后 fulltext 一次约 **157.06 ms**。这些是极小合成环境的原始测量，没有对照组、真实并发问答或生产流量，不推导生产 contention 改善。

仅在确认无活进程或相关挂载后，删除两轮私有 `chroma/`，合计逻辑 **367272 字节**、实际分配 **385024 字节**。保留 SQLite UNKNOWN/attempt/effect 账本、checkpoint、被账本引用的极小公开输入、日志和回执；两轮分别保留约 596430 与 655444 字节。文件系统空闲从 7999660032 变为 8000061440 字节，该差值可能含其他主机活动，归因采用逐路径测得的删除字节。没有删除其他阶段或生产资源。

## 5. 风险、版本与下一步

最终 stderr 原文仍包含：

```text
Exception ignored in: <function AsyncResult.__del__ ...>
RuntimeError:
Retry limit exceeded while trying to reconnect to the Celery redis result store backend. The Celery application must be restarted.
```

它发生在自有 Redis 关闭后的 Celery result 对象 destructor，属于非阻断的清理顺序技术债。原始堆栈完整保留在 `ecs-execute-r3.log`；没有把它当生产 Redis 故障，没有为消除告警继续修改或第三次运行。当前证据证明列出的 8 项隔离合同及实际退出/清理，不证明 stderr 无异常或全部 Celery 故障场景。

当前 HEAD 为 `2041bfb5355bab5c0a4e123d413dabcbd619125a`，改动未提交，没有 Tag 或生产验收声明。下一最小任务是主代理将本报告与 P0/P2/P3/P4 证据合并评审；若以后处理 destructor 顺序，应作为独立小修复验证，复用现有证据，不重放真实模型或重置 UNKNOWN。

**未实现的命令不得报告通过。**

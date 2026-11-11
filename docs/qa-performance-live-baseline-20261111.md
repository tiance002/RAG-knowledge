# ECS 单题真实基线与临时资源试验（2026-11-11）

本报告区分一次真实问答、资源试验和后续候选比较。**该题核心数值正确，Cloud 与本地问答树一致；尚不能用它证明各性能候选提速或提高正确率。** 没有更新正式 API 代码、重启 Chroma、修改数据库或索引。后续候选 A/B 另记。

## 版本与范围

- 本地分支：`codex/project-upload-20261107`；HEAD：`2041bfb5355bab5c0a4e123d413dabcbd619125a`。实际测试的是保留未提交修改的工作区源码，以清单哈希识别，不能只用 HEAD。
- 完整清单：`D:\RAG knowledge\.planning\2026-11-11-qa-performance-p0-p1\final-current-source-20261111-r7.json`，SHA256 `9648e8d3ecccbb014914a66dc6118e5e229ef469a8ac4cf293334512a95dba1d`；117 个文件，应用代码 79 个文件。ECS 复用既有 r6 源码与 r7 差异，未复制数据库或 Chroma 索引。
- 隔离代码：`/opt/amrag/experiments/qa-memoryhigh-trial-r9-20261111-01/code`。正式 `/opt/amrag/backend` 与 8200 入口没有切换。
- 单题 QID：`ecs-table-probe/v2:118`；运行 ID：`167f508a1d3e431382f3e041c3627068`；attempt：`7570286243844dd99d33d70e0f5511c9`。
- 问题询问采样误差表中 Trump voters 的未加权样本量和正负抽样误差。冻结答案点为 `533`、`5.1 percentage points`，Gold 仅供评分，不发送到 API。
- 当前文件 SHA256：`8f49f93f1665fd5ff604e66ffed9649d8b18ce7f114ab43463aeac46cd3d00cb`。只读预检核对文件→知识库、活动索引范围指纹、原始块文本、BM25 和向量成员，不从引用反推归属。

固定配置为 Normal、Hybrid、top_k=10、阈值=0、向量/BM25 权重=0.7/0.3、Rerank 开启且 top_k=10/阈值=0、context_window=1、查询理解关闭、联网关闭、动态路由 OFF。请求模型绑定为 `XingChenAGI/Xing4.0-29B`，Embedding 为 `BAAI/bge-m3`，Rerank 为 `BAAI/bge-reranker-v2-m3`；未擅自切换模型。Generation 记录与请求绑定一致，供应商实际推理版本及思考细分仍未得到额外确认。

这是 **baseline 臂**：Chroma 执行器、生命周期导出、Agent 证据投影、直接首次检索、真流式、查询向量复用均未启用。不能把此次测量当作这些候选的 A/B。

## 资源试验和恢复

负责人明确批准一次运行时 `MemoryHigh` 384→512 MiB，完成有限就绪检查和一道问答后恢复。独立 guard 在调整前就绪，SSH 或客户端断开不承担回滚职责；比较前核对 systemd、内核限额、进程启动身份和策略文件，避免覆盖其他更改。

| 项目 | 实测结果 | 判定 |
|---|---|---|
| 调整/恢复命令 | 两次 `systemctl set-property --runtime` 均退出 0 | PASS |
| Chroma 身份 | PID 3561886、start_ticks 160541991、NRestarts=0 前后相同 | PASS |
| 最终限额 | systemd 与内核均恢复 402653184 B，原策略 SHA 完全恢复 | PASS |
| 独立 guard | `SUPERVISOR_RESTORED_VERIFIED` | PASS |
| 512 MiB 生效至恢复快照 | 约 23.46 秒 | 观察事实 |
| 数据库/索引 | 只读复用，未执行迁移或写入 | PASS（此次隔离路径） |

累计计数器显示：此前两个 384 MiB 快照间约 81.91 秒，high 事件增加 13012，full PSI 停顿增加 56.14 秒；512 MiB 生效后的约 23.45 秒，high 事件增加 0，full PSI 停顿仅增加 10.74 毫秒，无新增 OOM/max 事件。这与回收压力缓解强一致，但不是随机化、同时配对的性能实验，不能宣称排除了所有其他原因。

512 MiB 下 tenant/database metadata 分别约 4.16/1.92 毫秒返回，向量只读 get 约 52.09 毫秒，SDK metadata 启动验证约 149.02 毫秒。历史 10 秒超时来自不同时间，不能据此算生产提速倍数。

恢复后的 **23:18:29 CST** 又做了一次独立只读检查：限额仍为 384 MiB、进程未重启；唯一一次数据库 metadata GET 在 10.00057 秒超时，无重试。约 1.005 秒采样内 high 增加 88、full PSI 停顿增加约 897 毫秒。因此该时点剩余真实 A/B 为 `BLOCKED`，不能把成功的一次临时试验说成环境已长期修复。

后续外部状态出现变化：矩阵启动前只读发现另一项操作已将 unit/kernel 限额改为 **480 MiB**，本轮没有执行该调整。**23:36:29 CST** 的一次只读 database metadata GET 在 140.28 毫秒返回 200，进程启动身份未变。为避免覆盖其他操作，后续矩阵改用 `OBSERVE_ONLY` 资源模式，在 480 MiB 原样环境下进行同模式配对；不写限额，也不把这个外部设置恢复为 384。原定 384→512 计划保留为未执行记录，后续矩阵不得直接与 r9 的 512 MiB 时延混作受控 A/B。

## 答案、实际输入与引用

该题 HTTP 200、`SSE_DONE`、`generation_status=COMPLETED`、`finish_reason=stop`，SSE 解析错误 0。回答给出 **533、±5.1 个百分点**，实际 Provider 请求包含对应原文，核心正确性 `PASS`，不是仅凭正常结束判对。

实际请求快照 SHA256 为 `537c74f1c0650108568611da3e6ee2d76d2c4936de7c957d15165b1ac311ebd1`，11894 字符；Cloud Generation 的输入引用哈希、字符数与该快照相同。Prompt 中有 22 个来源编号，其中存在重复片段；它们不能等同 22 份独立证据。

引用 [2]、[4] 的片段直接包含目标行。回答又称 [5]、[7]、[8] 也给出同样信息，但这些片段主要是同一物理页中的其他组别或相邻说明，没有直接包含目标行。因此：

- 数值正确性：`PASS`。
- 22 项来源/Scope 校验：`PASS`，只证明引用来自有效范围/原文，不证明每条都蕴含答案。
- 直接片段支持精度：`PARTIAL`；同页定位可回读，但不把邻段当作直接数值证据。
- 页码：PDF 物理第 45 页，对应报告印刷第 43 页。回答称“报告第 45 页”不够精确，作为定位说明保留，不因此否定正确数值。

本次没有新增严格句式、数值字符串或引用门禁，也没有为让测试通过而改 Gold。

## 单题耗时与用量

| 指标 | 原始值 | 口径 |
|---|---:|---|
| 首正文 | 17500.165 ms | 收到首个实际 answer_chunk，不含进度 |
| SSE 终态 | 17500.254 ms | 收到 done，不等于 EOF |
| EOF | 17754.685 ms | 客户端读到结束 |
| 客户端释放 | 17754.748 ms | 客户端连接/对象退出 |
| ASGI 应用 | 17733.877 ms | 实际应用调用范围 |
| 生成阶段 | 14952.725 ms | 含调用边界，不称为纯 GPU 推理 |
| 检索整体 | 2006.398 ms | 嵌套包含部分下列阶段 |
| BM25 | 1301.372 ms | 首次 API 样本，稳态未验证 |
| Embedding | 133.797 ms | 调用 span |
| Chroma | 122.437 ms | 实际查询计时 |
| Rerank 调用 | 213.358 ms | 批次整体另为 233.078 ms |
| 上下文构建 | 24.185 ms | 独立计时 |
| 请求末导出 | 251.146 ms | REQUEST flush |

不能把嵌套阶段相加。该次生成阶段约占 server pipeline 的 84%；buffered 路径首次正文和完成几乎同时发生。下一项最直接的实际对照是 P5 真流式，而不是依据这一个样本立即修改评分阈值或断言 Hybrid 并行收益。BM25 首次耗时也需要与稳态区分。样本量仅 1，不报告总体 P95 或系统吞吐提升。

观测到 3 个不同 Provider 请求 ID，均有 RETURNED 结束：Embedding、Rerank、grounded_answer。Generation 实际报告 4119 输入 / 667 输出 / 4786 总 Token；Embedding 37 / 0 / 37；Rerank usage 缺失。因此可确认的 Token 下界为 **4156 / 667 / 4823**，完整总量与费用 `UNKNOWN`，不把缺失当成 0。调用覆盖的严格完备性尚未证明，3 是观测下界，不冒充供应商账单核对。

`actual_candidate_count`、`final_evidence_count`、`retry_count`、`upgrade_count` 的 runner 汇总字段仍因缺少服务端权威总计而为 null；阶段中可读到的数目、来源编号和扩展记录另行审查，不偷偷代填成相同含义。

## Cloud 回读

[真实问答 Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/b601655e246e7518e458e6f4be7fc78a)

使用现有 Langfuse CLI 只读查询该精确 Trace，未新增 Trace 或模型请求。HTTP 200、CLI 退出 0，完整分页返回 10 个 Observation：1 GENERATION、2 CHAIN、6 SPAN、1 RETRIEVER。与本地该问答 10 个 start 节点的 ID、父子关系和 run_id/attempt_id/qid 全部一致；唯一 Generation 的 usage 与本地回执一致，最终回答哈希也匹配。

本次实际运行包含已修复的 main 关闭路径，不再用合成 harness 的关闭抑制替代实际源码。此次问答树核对 `PASS`；启动阶段另外出现的本地诊断 trace_id 不属于该树，未查询其 Cloud 状态，不据此宣称已完成全项目孤立节点审计。

Cloud 的模型 I/O 为 `LOCAL_ONLY` 引用，长文快照保留本地。已检查已知两个原始块和最终回答没有以全文出现在这份 Cloud 回读中；这是特定样本/引用策略核对，不是全量 DLP 审计。Cloud 元数据体积仍较大，主要来自检索阶段结构化记录，作为后续技术债记录；本次不扩写观测架构，Units 与费用继续 `UNKNOWN`。

## 实际命令、退出码与归档

以下命令工作目录均为 `D:\RAG knowledge`。远端使用已有虚拟环境，没有安装依赖。

| 操作 | 命令/保存的真实 argv | 退出码 |
|---|---|---:|
| 远端控制器 | `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python /opt/amrag/experiments/qa-memoryhigh-trial-r9-20261111-01/trial_control.py --root /opt/amrag/experiments/qa-memoryhigh-trial-r9-20261111-01` | 0 |
| 真实问答 | `plan.json` 中完整 `runner_command`，`--case ecs-table-probe/v2:118 --concurrency 1 --repetitions 1 --max-requests 1 --timeout 180 --execute --ack-model-requests` | 0 |
| Cloud 只读回读 | `.venv\Scripts\python.exe -X utf8 .planning\2026-11-11-qa-performance-p0-p1\live-baseline\memoryhigh-trial-r9\read_cloud_existing.py` | 0 |
| Cloud/本地交叉检查 | 同目录 `review_cloud_existing.py` | 0 |
| 恢复后单次只读检查 | `post-restore-readiness-r10.json` 记录 SSH 包装退出 0，业务结果 `BLOCKED_TIMEOUT` | 0 / BLOCKED |

全部本轮公开摘要、计划和审查记录位于：`D:\RAG knowledge\.planning\2026-11-11-qa-performance-p0-p1\live-baseline\memoryhigh-trial-r9`。

原始请求、回答、SSE、快照、完整 Cloud 回读以及私有归档仅在其 `private-terminal` 子目录。该目录取消权限继承，仅当前用户与 SYSTEM 可访问，并加入本地 `.git/info/exclude`；含原始材料的传输 ZIP 也本地排除，不提交。归档 233188 字节、145 个文件，SHA256 `016d8baff3107ed782a386bdbd0c6972f52e0d7430e2bc2da53cd224c252129f`。

终态轮询与 wait/退出回执支持自有 supervisor、guard、steps、API、runner 已结束，root 另有历史只读 `/proc` 无进程回执；不把它冒充当前全机孤儿进程审计。临时 Langfuse 文件已删除，模式原为 0600，其他密钥未动。117 文件的小型公共源码目录为后续 A/B 复用而保留，没有语料、数据库或索引副本。

## 下一步及未运行项

负责人已另行批准：下一窗口临时 512 MiB、最长 30 分钟、最多 21 次串行冻结题问答；Normal 的 baseline/P2chroma/P2flush/P5 各 3 题，Agent 的 baseline/P3/P4 各 3 题。复用原模型、Gold、检索配置及源码，各臂只改一个开关。该批准不代表已经执行；逐题结果、停止和恢复另行归档。

由于启动前发现外部 480 MiB 配置，测试发送上限、时限、题目和实验臂保持批准范围，资源操作改为不写共享设置，仅设置本任务进程的独立到期清理。具体以修订后的矩阵清单与前后策略身份核对为准。

真实候选质量/耗时 A/B、P5 真实流式结果、Agent 多轮成本对照目前 `NOT_RUN`；P6 对这三道单知识库题的真实复用收益 `NOT_APPLICABLE`。独立无答案范围、实际追问、1/2/4 并发完整测量仍未覆盖。Hybrid 两路并行与条件评分未实施，不自动跳过原评分或改变阈值。原有 ECS 386 项和后续关闭修复 6 项仅为工程回归，范围重叠，不与此处单题正确性拼成一个总分。

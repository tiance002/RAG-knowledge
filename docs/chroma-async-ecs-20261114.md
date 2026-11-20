# ECS Chroma 查询异步卸载

Chroma 检索客户端首次初始化和同步查询统一放入受限线程执行器，释放 API 事件循环。复用本地已有 `bounded_chroma_executor.py`、`bounded_chroma_bootstrap.py`，按 ECS 原始文件生成适配包，保留原有 provider、查询范围、排序和入库算法。SQL Session 留在请求线程，工作线程仅接收查询向量与范围参数。

ECS 使用一个 Chroma 执行槽、每次 HTTP 请求 30 秒超时。取消请求后，执行槽仍等待实际线程结束才释放；关闭时排空真实线程并清理本执行器拥有的 HTTP 客户端。30 秒不是多页查询总时限，SQL 范围计算仍在请求线程中。

新增 `RAG_HYBRID_PARALLEL_ENABLED` 可单独控制双路重叠，代码缺省为 `true`。Chroma 卸载开关缺省为 `false`；ECS 在服务配置中启用，并设置 `RAG_CHROMA_QUERY_WORKERS=1`、`RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS=30`。本地 `.env.example` 明确列出了这些开关，模板不代表本机进程已启用。

## 验证

精确 ECS 适配包最终 61 项测试全部通过，退出码 0，无失败/错误/跳过。覆盖首次 HTTP bootstrap、取消后的容量保留、SQL Session 线程归属、两个执行器关闭和禁用并行时仍使用 Chroma 异步卸载。实际 ECS 隔离导入也通过。

同一冻结五题分别运行了 Chroma 卸载+双路并行、Chroma 卸载+双路串行。两轮各请求五次，无重试，HTTP 200 / SUCCESS 各 5/5，命令和 journal 采集退出码 0，stderr 为空，原始响应 SHA256 核对通过。两轮 Top-10 ID 集合和顺序均一致；其中一题第二个 Gold 来源覆盖仍为 UNKNOWN。这是召回端点测试，不包含最终答案生成。

| 题目 | 卸载+并行客户端耗时 | 卸载+串行客户端耗时 |
|---|---:|---:|
| b5-013 | 7,607.9 ms | 4,278.2 ms |
| b7-013 | 448.8 ms | 559.2 ms |
| b1-024 | 13,825.5 ms | 16,060.7 ms |
| b1-030 | 12,565.3 ms | 13,083.9 ms |
| b6-024 | 3,095.5 ms | 2,039.1 ms |

并行配置与召回严格重叠的 65 个健康探针全部成功，中位数 1.267 ms，最大 2,781.522 ms。串行配置 34 个重叠探针中 29 个成功、5 个在约 3 秒时超时；成功样本中位数 1.515 ms。超时与慢题 BM25 阶段重叠。两轮是不同新进程下的一次采样，分母不同，不视为稳定吞吐/因果结论。

异步 Chroma 查询期间大部分健康探针可快速响应；仍存在同步 SQL 范围解析的长尾，最大一次约 2.6 秒。Chroma 查询实际耗时最高约 10.5 秒，BM25 初始分支最高约 13.8 秒；执行器准入等待仅约 0.004–0.008 ms。同步 Chroma 已卸载后慢请求仍存在，因此不能将此前的全部延迟归因于这一阻塞点，也没有验证出平均检索时间稳定缩短。

最终选择保留 Chroma 卸载和双路并行，以避免串行 BM25 的长时间事件循环阻塞；并行开关可独立回退。后续优化对象是 SQL 范围查询、BM25 初始读取和 Chroma 查询本身。

最终 API PID 为 `630790`，新进程实际环境核实 `RAG_HYBRID_PARALLEL_ENABLED=true`、`RAG_CHROMA_EXECUTOR_ENABLED=true`、`RAG_CHROMA_QUERY_WORKERS=1`、`RAG_CHROMA_QUERY_HTTP_TIMEOUT_SECONDS=30`。健康检查 HTTP 200，配置切换退出码 0，六个源码哈希未改变。只有 API 被重启，其他服务状态保持本次部署前快照；SQL 文件/切片/已完成任务计数保持一致，队列和 lease 为空。

## 资源与证据

两轮资源保护均未触发。并行轮 45 个样本的 MemAvailable 最低 2,431,844,352 字节，磁盘最低 12,877,643,776 字节；串行轮最低内存 2,450,272,256 字节、磁盘 12,876,419,072 字节。复用现有 venv、SQLite、Redis 和 Chroma 索引，仅新增小型适配包、服务配置、回退文件及原始日志，没有复制语料、数据库或索引，没有删除原始数据或评测证据。

最终配置切换时磁盘可用由 12,877,201,408 变为 12,877,152,256 字节，净减少 49,152 字节（共享主机变化，不全归因于本任务）；最终 MemAvailable 为 2,479,255,552 字节。最终回退/审计目录为 ECS `/opt/amrag/evaluation/parent-child-parse-20261111/io-reconcile-20261112/chroma-final-parallel-582465ea77e5fc5e`。

- 六文件包及测试：`var/ecs-chroma-offload-20261114/`
- 并行原始结果与阶段日志：`var/ecs-recall5-20261114/after-chroma-offload/`
- 并行健康探针：`var/ecs-recall5-20261114/after-chroma-offload.health.jsonl`
- 串行原始结果与阶段日志：`var/ecs-recall5-20261114/after-chroma-offload-serial/`
- 串行健康探针：`var/ecs-recall5-20261114/after-chroma-offload-serial.health.jsonl`
- 开关适配和最终服务选择：`var/ecs-chroma-serial-hybrid-20261114/`

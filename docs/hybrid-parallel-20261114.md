# 混合检索首轮双路并行

本次调整用于本地项目的 Hybrid 检索。向量检索与 BM25 首轮并行，等待两路完成后，按切片 ID 合并去重；唯一候选数小于本次 `top_k` 时，才执行已有的本地关键词扩展。扩展最多五条，仅查询 BM25，复用首轮 BM25 结果，不增加向量模型调用。Weighted RRF、权重、分数归一化和后续来源校验沿用原流程。

FastAPI lifespan 管理一个最多两个线程的 BM25 读取执行器。每个工作项使用独立的只读 SQLite 连接及 Session，不占用调用方连接池。取消请求不会提前释放仍在运行的线程名额；应用退出时等待实际工作结束，再释放引擎。

以下场景保留串行：没有受管执行器的命令行调用、无真实 SQL Session、内存数据库、共享单连接池，以及非 SQLite 数据库。独立读取不是跨 SQLite/Chroma 的原子快照，返回结果仍经过现有 enrichment 来源检查。

检索 metrics 增加 `hybrid_initial_parallel` 和 `hybrid_execution_reason`；`retrieval.hybrid_execution` 事件记录相同的执行模式。命令行基准若需要验证并行，必须在 `managed_lexical_executor()` 上下文内调用，并检查该标记。通过 FastAPI 正常启动时由 lifespan 自动管理。

本次验证使用临时 SQLite 和可控的向量/模型替身，不修改真实语料、索引或冻结题集。替身延迟测量只能证明等待重叠，不能代表 ECS 上的检索或回答提速；真实效果需要同题集、同配置的后续对照测试。

2026-11-14 最终相关回归：94 项通过，退出码 0；原始日志及退出码分别为 `var/hybrid-parallel-20261114/tests-final.log` 和 `test-final-exitcode.txt`。受控实验中两路各设置约 150 ms 延迟，串行 309.70 ms、并行 154.18 ms，验证的是调度重叠。更广的首轮回归中，`test_evidence_repairs.py::test_seed_larger_than_limit_is_explicitly_omitted` 失败；隔离加载 Git HEAD 的原检索代码后仍可复现，保留于 `unrelated-baseline.log`，未在本任务修改上下文预算行为。

本次未发布 ECS，也未重启线上服务；服务器上是否已包含此改动必须通过后续发布与实际请求的 `hybrid_initial_parallel` 指标核对。

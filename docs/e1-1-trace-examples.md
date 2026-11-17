# E1.1 Trace 与证据链示例

最新源码的 r6 验收见 [最终实施报告](e1-1-trace-metric-implementation-report.md)。[r6 Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/49f0d5973d3ecff4dd02c3ce9c21e001) 根 ID `82850055a3ef7a49`、run `synthetic-a24f83d1eb84477d84d1afcffc0d6882`：Cloud 26 个节点唯一根且父子完整，实际检索 Query、Provider 消息和最终答案为 LOCAL_ONLY；6 个引用均已由 ECS CLI 读取并核对哈希。`provider.call` 是有真实起止时间的跨度，模型仍是替身。证据 `var/e11-20261029/synthetic-r6-cloud-readback-summary.json`、`snapshot-cli-readback-r6.json`、`runtime-manifest-r6.json`；r6 当前归档源码与工作区字节一致。以下 r4 保留为较早独立运行，不冒充最新代码。

以下分别标明真实 Cloud 回读与替身测试。替身结果展示代码路径和可核对字段，不是生产检索质量评测；历史 E1 答案不因这些测试被重判。

## 1. Cloud 回读：r4 合成 HTTP/SSE 请求

r4 在隔离环境使用合成资料和替身 Provider：HTTP 200，替身调用 1 次，外部模型调用 0 次。Cloud API 回读 25 条 observation，关联同一 Trace，父节点无缺失。运行与身份：

- `run_id=synthetic-3a24fe8ca0af43d08c7ef5879d53dffc`、`qid=synthetic-1`、`dataset_version=e11-synthetic-v1`。
- [Langfuse Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/695898cf39f395b28ad8728a272b5098)：`trace_id=695898cf39f395b28ad8728a272b5098`，根 `qa.request` observation ID `5939a0ccc93a3909`。
- Cloud 回读 `grounded.request.sent` observation ID `17654fd759d1c219`，`context.delivery`（Provider）ID `413e43ef246a96ad`，另有候选转换节点。真实 API 数据：`var/e11-20261029/synthetic-r4-cloud-observations-page-001.json`；关联检查：`synthetic-r4-cloud-readback-summary.json`。
- 根问题、最终答案和 Provider 消息在 Cloud 以 `LOCAL_ONLY` 引用保存，不是 Cloud 可直接读的全文。受控 CLI 对六个本地引用逐项读取，退出码均为 0、内容 SHA-256 均匹配：`var/e11-20261029/snapshot-cli-readback-r4.json`。消息引用 `3106de259fd71eec63964586532419423fb3f6e20a1de642ba43e86ca40ba837`；最终答案引用 `a2e017f3087c6b76fd127138a914344cd31fc702f90104895a2c4c77af8e9680`。
- 运行日志 `var/e11-20261029/cloud-execution-r4.log` 中的 `cloud_export=NOT_CONFIRMED` 是运行时状态；上面的 API 回读是事后证据。r4 代码包哈希见 `staging-manifest-r4.json`；之后的修改不属于这次运行。

## 2. 候选去重：20 → 19 → 7（本地替身）

`backend/tests/test_e11_candidate_transitions.py::test_twenty_to_seven_dedup_observations_do_not_change_results` 构造 20 个候选：7 个不同来源的 seed、12 个同源相似副本和 1 个完全相同 ID 的副本。`dedup_exact_id` 从 20 到 19，移除原第 20 项；`dedup_same_source_similarity` 从 19 到 7，记录 12 个删除项、对应保留代表、Jaccard 1.0 和阈值 0.85。不同文件的相同文本保留。埋点开关两次的业务结果相同。以上是候选数量，不能推论其他真实案例的 20 个 BM25 候选为何变成 7 条引用。

## 3. Hybrid 与 Rerank（本地替身）

`test_hybrid_to_rerank_transitions_and_observability_equivalence` 使用合成 Vector/BM25 和替身 Reranker。`weighted_fusion` 为 Min-Max 加权和，四个候选分数分别为 a=0.7、b=0.5、c=0.3、d=0；融合 Top-K 得 a、b、c，d 在 Top-K 外。模拟 Rerank 返回 b=0.2、c=0.9、a=0.7；排序为 c、a、b，阈值 0.3 删除 b，最终 Top-1 选择 c、删除 a。每一步均按测试记录的相应事件和规则判断，埋点开关不改变结果。不存在真实 Embedding 或 Rerank 模型请求。

## 4. 扩展与最终 Provider 消息（本地替身）

`backend/tests/test_e11_context_delivery.py::test_expanded_neighbor_does_not_reach_final_model_message` 调用真实 `expand_context`、`_chat_stream_normal` 和 `generate_grounded_candidate`；邻块数据库查询与 Provider 使用替身，扩展函数实际生成组成员及邻块记录。选中结果 `content="seed plus GOLD_IN_NEIGHBOUR"`，`original_content="seed without target"`。代码实际使用 original_content：SSE 引用及替身 Provider 收到的消息含 seed，但不含 `GOLD_IN_NEIGHBOUR`。`context.delivery` 在 `citation_selection` 记录 `replaced_by_original_content`、选中与送达内容各自的 SHA-256，且 `is_final_model_delivery=false`；另一个 `provider_request` 事件记录实际送达证据哈希且标记 `true`。这是当前路径的可观测事实，不表示已修改扩展算法或证明真实题目的 Gold 未送达。

## 5. Agent 两轮（本地替身）

`backend/tests/test_agent_tool_observation.py::test_two_attempts_quality_boundary_and_retry_are_linked` 的第一轮为 `NO_RESULTS`，第二轮为 `SUCCESS`；两次调用的 `retrieval_attempt_id` 不同，质量事件与各自 ID 关联。第二轮只对 2,100 字符上下文的前 2,000 字符进行评分，并记录长度、裁剪标志与哈希；重试决定依次为 `SCHEDULED`、`NOT_SCHEDULED`。这些是运行过程，不代表证据补救成功。

本地验证命令：

```powershell
$env:PYTHONPATH='D:/RAG knowledge/backend;D:/RAG knowledge/.venv/Lib/site-packages;D:/RAG knowledge/var/p1-metrics-python'
& 'E:\RAG quention\.venv\Scripts\python.exe' -m pytest backend/tests/test_e11_candidate_transitions.py backend/tests/test_agent_tool_observation.py backend/tests/test_e11_context_delivery.py -q
```

实际退出码 0：7 passed，6 条 Pydantic 弃用警告。测试使用替身与合成数据；该段命令为早期定向验证；最新完整套件见交付报告（91 passed/5 POSIX skipped，及内容父ID修复后16 passed/1 skipped）。不等于真实模型或历史题目的正确性验收。


## 6. 最终 r7b Cloud 契约验证

[真实 Trace](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/25d63f7688bf7dcd76229f6a825a0c79) 使用零外部模型请求的替身HTTP/SSE；根ID `5c9e13222a46b9ff`，26节点，独立CLI回读退出0。与r4/r6分开保留，旧Trace不补写新字段。

- 根实际配置：fulltext/top5、关闭改写与重排、synthetic-fake、temperature 0.7、max_tokens 4096、实际消息Prompt哈希；未执行的Embedding/Rerank参数为NOT_APPLICABLE。
- 根code_hash、capture_mode、已观测文件索引范围；子节点内容描述符自己的真实Observation ID和parent ID分别可核对。
- `rag.candidate_transition` 标准input/output与metadata中的实际候选ID一致；检索标准output保存真实输出ID；非Excel合成资料的来源核验明确reason_code=not_applicable。
- Cloud内容引用均能与ECS离线CLI校验交叉对应；Cloud中的5个不同快照引用全部包含在6个实际CLI成功回读的引用集合中，没有无法回读的引用。

原始字段及结论见 `var/e11-20261029/readback-r7b-final/summary.json`，源码归档和Cloud根代码哈希独立验证见 `current-source-archive-verification-r7b.json`。这些仅验证接线及采集，不证明真实模型质量、成本或生产性能。

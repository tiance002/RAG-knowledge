# P0 持续 API 性能基线合同

`scripts/run_performance_baseline.py` 仅连接已持续运行的候选 API，不启动 API、不复制语料/数据库/索引、不改写服务设置，也不重放失败问题。原有 `run_p1_http_regression.py` 保留隔离回归用途；本脚本仅导入其中无副作用的 `sha`、`source_hash`、`final_citations` 和 `citation_check`。

当前脚本合同与 loopback HTTP 测试可运行；真实模型质量和真实性能尚未验收。HTTP 200、SSE `done`、脚本退出 0 均不代表回答正确。

## 输入冻结

1. `--cases` 是冻结 JSONL，`--cases-sha256` 必须精确匹配。每行含唯一 `qid`、`question`、`expected_chunk_ids`、`answer_points`、非空 `kb_scope`（KB ID 数组）。可选 `history` 是已冻结的 `user`/`assistant` 消息；追问不得在没有真实祖先消息时作为独立题测量。Gold 字段仅用于本地审查，不发送到 API。
2. `--case` 精确选择题目，可重复参数，不允许重复 QID。不要把 `item_id` 静默改写成已经存在的历史 QID；需要显式转换文件并保留来源版本与哈希。
3. `--request-config` 必须显式含下列全部字段，没有默认检索策略覆盖：

```json
{
  "model_id": "VERIFIED_RUNTIME_MODEL_ID",
  "search_mode": "hybrid",
  "top_k": 3,
  "score_threshold": 0.0,
  "rerank_enabled": true,
  "rerank_model_id": "VERIFIED_RUNTIME_RERANK_MODEL_ID",
  "rerank_score_threshold": 0.0,
  "vector_weight": 0.7,
  "bm25_weight": 0.3,
  "rerank_top_k": 3,
  "context_window": 1,
  "use_rewrite": false,
  "web_search_enabled": false
}
```

数值仅展示文件格式，不是批准的真实参数。应从候选环境有效配置和本轮 Owner 范围取值。Normal 与 Agent 目前候选上限不同；同样 `top_k` 不是同样实际候选数，应保留现有差异。

4. `--provenance` JSON 由运行时清点生成，并通过 `--provenance-sha256` 冻结。必须有 `dataset_version`、`corpus_version`、`active_index_version`、`model_bindings`、`dynamic_route`、`thinking_parameters`、`context_policy`、`api_concurrency`、`worker_concurrency`、`observability`、`machine_resources`。无法核验的值用 `null`，不得填写猜测。`context_policy` 应覆盖历史轮数、父/邻块扩展、输入预算；模型绑定应区分配置模型和实际响应模型。建议同时记录远端 commit、应用源码哈希、解析/切块/Embedding/Prompt 版本、运行命令/进程/镜像、实际 Worker 并发、内存/CPU 和已有 UNKNOWN 账本核对证据。
5. `provenance.artifacts` 可列 `{"path":"...","sha256":"..."}`；相对路径从 provenance 文件目录解析，逐项重新校验。列入题集 freeze、source/chunk/index manifest、运行设置快照及相关账本。凭据不得写入以上文件；脚本拒绝包含常见密钥名称的配置字段。

执行时先只读 GET `/api/v1/settings`，把实际持久化设置和 revision 写入 manifest。读取失败则保存 `BLOCKED_RUNTIME_SETTINGS` manifest/summary，退出 2，不发任何问题。该接口不能证明 Worker/路由/索引全部配置，这些仍由 provenance 与每题 Trace 补齐。默认离线预检不访问网络，`runtime_settings_status=NOT_PROBED`。

## 精确调用合同

以下 PowerShell 变量都指向已经人工核实的文件或服务；示例不提供占位资源 ID 的伪运行结果。

```powershell
$baselineCases = 'D:\RAG knowledge\.planning\2026-11-11-qa-performance-p0-p1\baseline\cases.jsonl'
$baselineConfig = 'D:\RAG knowledge\.planning\2026-11-11-qa-performance-p0-p1\baseline\request-config.json'
$baselineProvenance = 'D:\RAG knowledge\.planning\2026-11-11-qa-performance-p0-p1\baseline\provenance.json'
$baselineCasesHash = (Get-FileHash -LiteralPath $baselineCases -Algorithm SHA256).Hash.ToLowerInvariant()
$baselineProvenanceHash = (Get-FileHash -LiteralPath $baselineProvenance -Algorithm SHA256).Hash.ToLowerInvariant()
.venv\Scripts\python.exe scripts/run_performance_baseline.py --base-url http://127.0.0.1:8000 --cases $baselineCases --cases-sha256 $baselineCasesHash --case VERIFIED_QID --request-config $baselineConfig --provenance $baselineProvenance --provenance-sha256 $baselineProvenanceHash --output .planning/2026-11-11-qa-performance-p0-p1/baseline/offline-preflight-001 --mode chat --phase warm --concurrency 1 --max-requests 1
```

该命令只做离线预检。真正执行使用另一个不存在的 `--output` 目录，并显式追加 `--execute --ack-model-requests`，可追加 `--telemetry-jsonl` 指向候选 API 本机追加式 observation log，`--citation-db` 指向同一运行版本的只读 SQLite。脚本使用每请求 HTTPX 客户端、禁用环境代理且不自动重试；客户端策略记入 manifest，A/B 必须一致。

服务端需显式设置 `RAG_PERFORMANCE_IDENTITY_HEADERS=true`，才把 `X-RAG-Run-ID`、`X-RAG-Attempt-ID`、`X-RAG-QID`、`X-RAG-Dataset-Version` 绑定到 BASIC Trace 身份。该开关不授权内容外发或 FULL 捕获；Trace 原有隐私规则继续生效。每次请求有独立 attempt UUID，重复 QID、不同模式、预热和并发档位不会混淆。

`--max-requests` 限制问答 HTTP 请求数，含预热；**它不是 Provider 付费调用次数上限**。一次 Agent 问答可能有多次选择工具、评分、改写、升级调用。真实执行前仍需明确 Owner 的模型、数据外发、预算、UNKNOWN 核对及实际 Provider 边界。脚本不修改、不复用也不清空旧 grant/UNKNOWN 账本。

## 冷、暖与并发

- 冷态只允许 `--phase cold --concurrency 1 --repetitions 1`、一个 QID、零预热，并要求 `--cold-attestation` 说明外部已核实的冷态及范围。脚本不重启服务、不删除缓存；不能把随后请求也标为冷态。
- 暖态使用 `--phase warm`，预热次数必须显式 `--warmup-count N`。预热是额外真实请求，完整保存结果并计入 cap，串行完成后才发测量请求。零预热时记录 `UNVERIFIED_EXTERNAL_WARM_STATE`，不假称已充分预热。
- 每次只接受一个 `--concurrency 1|2|4`。后续档位必须单独调用、新输出目录、单独的有效发送授权；脚本不自动对生产服务升压。
- 请求按有界 wave 执行。任一失败、空答案、截断或不完整终态阻止下一 wave；同一 wave 已在途请求无法撤回，逐项留存。没有自动重试。`request_started_monotonic_seconds` 可检查实际重叠，分母包含所有已发请求，并另报未发数。

## 时间口径与原始证据

每个 attempt 保存 `request.json`、精确接收的 `response.sse`、分析前的 `client-receipt.json`、关联 `telemetry.json`、最终 `receipt.json`。输出目录与文件均以排他创建，既有结果不覆盖。

| 字段 | 口径 |
| --- | --- |
| `first_body_ms` | 请求开始到增量解析第一段实际 `answer_chunk`；排除独立偏好确认前缀、空白、progress 事件和显式 progress/ack 内容角色。不是浏览器绘制时间。当前服务的偏好确认以完整独立 chunk 发出；未来拆分该协议需同步更新测试。 |
| `terminal_ms` | 到首个完整 SSE `done`/`error` 帧；不是 EOF。`SSE_DONE` 只表示终态交付，质量另判。 |
| `eof_ms` | 到客户端真正读到响应 EOF；终态后仍可有延迟。传输失败时为 null。 |
| `client_release_ms` | 到本地响应及 HTTPX 客户端退出/释放；传输失败也保留。不是服务器 teardown。 |
| `server_pipeline_ms` / `post_terminal_cleanup_ms` | 只使用服务端 `qa.performance` 的明确值，附 `server_timing_scope`。当前范围是 `chat_generator_including_flush_excluding_outer_span_close`。 |
| `server_request_cleanup_ms` | 完整服务端请求结束；当前没有覆盖外层 ASGI/span 关闭的权威事件，因此通常为 null，不用 EOF 或 pipeline 冒充。 |
| `stage_timings_ms` | 保留全部 span latency 与 `performance.stage` 时序数组；Embedding、Chroma、BM25、Rerank、评分、生成、flush、并发等待，未观测到的项不补 0。嵌套阶段不直接求和。 |

## Provider、质量与 UNKNOWN

按同一 attempt/Trace 汇总所有 `provider.request_started`（按 local request ID 去重），包含 Embedding、Rerank、评分、改写及生成，另保留 `provider.request_finished` 的失败/UNKNOWN 结果。Token 来自全部 `llm.response` 和直接 `/rerank` 返回的 `rerank.usage`，按 event ID 去除重复导出；同样模型和 usage 的不同调用不去重。保留每条原始 Provider 记录、实际模型和全部原始 usage。`done.usage` 从不充当整个 Trace 的调用/Token 总数。Embedding 当前只有指标层 usage 而无同等完整的逐请求 usage Trace 时，必须保留未知。

只有权威 `qa.performance.provider_telemetry_complete=true` 且遥测无解析错误时，`provider.call_count` 才是精确计数；所有调用都有 Provider 返回的非负完整计数时才输出总 Token。当前服务未证明这个完备性，默认 `UNKNOWN`，同时输出 `observed_request_count`、`observed_response_count`、`observed_call_count_lower_bound`、`observed_token_lower_bound`、缺失 usage 数；不得把未记录的调用当 0。响应记录也不能自动证明失败前的所有 Provider dispatch 都有记录。`missing_usage_records` 为保守数量：不完整 usage 记录加上 dispatch 数超出响应 usage 记录数的差额，不是已完成一一映射的账单核对。

`provider_local_wait` 是本地名额等待，`provider_slot_finished` 是取得名额到释放的占用时长（包含客户端构造和临时客户端关闭），`provider_client_initialize` 单独记录同步构造；后两者可能嵌套，不可相加。直接 Rerank/Embedding 的 `provider.call` 在取得客户端之后开始，SDK 外层调用可能包含等待；不能统一解释为纯供应商执行时间或统一包含本地等待。所有阶段保留原始 scope 和明细，不把相互包含的时间直接相加。

`correctness.status` 默认 `UNKNOWN`，必须按最终事实、答案点和真实证据审查；引用可选择复用旧 helper 用匹配的只读 DB 核验 scope/原文或 source hash，无 DB 则 `UNKNOWN`。`NO_CITATIONS` 不自动判断无答案题失败或成功。实际候选数、最终证据数、重试/升级数只认服务端明确计数，缺失为 null。完整原始 Trace 仍可继续人工核对，不能以引用总数替代入模证据数。

退出 0 是完成测量/离线预检；退出 1 表示终态失败并停止后续 wave；退出 2 表示参数或运行设置预检阻断。不要用十来题 P95 宣称性能改善；当前 summary 明确 `NOT_COMPUTED_SMALL_SAMPLE`，应报告样本数、分母、全部原始时序和错误。

## 已有资源调查与最小真实预检

只读检查旧 runner 源码可确认，其历史 ECS 输入引用 `/opt/amrag/experiments/pdf-table-hardcases-20261027/dataset200/{freeze.json,gold100.jsonl,registry200.jsonl}`、`runtime200-r4/{isolated.db,vectors.db}`，以及 `office50-v1/dataset250/{freeze250.json,office50.jsonl}` 和 `office50-v1/evaluation`。这是代码中的历史路径，**本子任务尚未核实 ECS 当前文件、进程、活动索引或服务就绪**。旧 runner 默认全文检索、关闭 Rerank，不能当成本轮 Hybrid 参数证明。

本地已有 `qa500_development_runtime_questions.jsonl` 和 `qa500_frozen_runtime_sessions.json`；其 2026-11-10 progress 明确没有冻结 parser/chunk manifest、runtime slice membership/eligibility 未定，所以不能直接作为本轮已就绪运行题集。无需重复制现有语料或索引。

最小真实预检应先只读核实一个已有冻结、已入库的直接问答 QID：freeze/source/chunk/index 哈希、KB scope、活动索引、模型绑定、费用/外发许可、旧 UNKNOWN 账本和 observation log。冻结上述三个输入文件并离线预检后，仅在已授权候选 API 执行一个 `chat`、并发 1、最多 1 次 HTTP 请求，不预热、不自动重试，检查真实 SSE、引用和所有 Provider Trace。该一次只验证兼容性，不得声称覆盖 Agent、表格、追问或质量。

该预检通过后，再冻结覆盖直接问答、多文档、表格、带真实历史追问和无答案的五类小组，分别测 chat/agent 和冷/暖。并发 2/4 与后续额外请求需要明确范围，保持 K、阈值、模型、Prompt、路由、观测配置一致。

## 本地验证

使用已有 `.venv`（HTTPX 0.28.1、pytest 8.3.5），没有新增依赖。`tests/test_performance_baseline.py` 使用真实 loopback HTTP 服务，固定延迟验证首正文、终态、EOF 的区别；覆盖偏好确认/进度排除、失败/空/截断/坏帧、请求身份、完整 Trace 计数与缺失 usage、离线零网络、运行设置失败零问题、失败 wave 停止、显式并发 2/4、哈希与输出不覆盖。此处测试均为替身，不能替代真实 Provider 兼容性/质量验收。

日志及 XML 存在 `.planning/2026-11-11-qa-performance-p0-p1/baseline/`。完整命令、退出码和逐项结果以该目录实际记录为准。未实现的命令不得报告通过。

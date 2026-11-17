# E1.1 观测差异审查（2026-10-29，前置审查进行中）

本文件保留开始实施前的审查事实和计划，当前实施、运行、验收及未覆盖项以 docs/e1-1-trace-metric-implementation-report.md 为准。后续已找到规范并完成最小闭环；Prometheus/Grafana 的继续建设要求已由 Langfuse-only 补充指令覆盖。

状态：零模型调用审查已产生实证；实施 A–E 开始，尚未验收。唯一字段/公式/隐私实施契约《RAG-knowledge｜Langfuse Trace 与评测指标规范 V1.0》随后在用户 Downloads 找到并全文读完，原文副本为 docs/e1-1-trace-metric-spec-v1.md，副本与原件 SHA256 一致。早先附件目录未找到的结论限该目录，不再构成阻断。不得由本报告替代规范。

## 当前版本及范围

- 本地 HEAD：`a834f8f7006bc1e9b1fb998fc9f8cb220f79d80a`，分支 `codex/pdf-parser-migration`。审查起点 tracked diff 为空，SHA256 `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`；存在既有未跟踪 archive、output、HTML 报告及 storage，未修改。
- 本地 app_hash：`e04ee698913df92fc40347d42529715decf7d78117082fdecf58cb550a163fd3`。算法为排序 backend/app/**/*.py，逐个附 backend 相对 POSIX 路径、NUL、原字节、NUL。此值不冒充 ECS 快照哈希。
- 已读 AGENTS.md、E1 两份报告、p0-evaluation-schema.md、Langfuse skill 和 instrumentation 参考。progress.md、docs/adr/ 未发现。P0/P1 其他报告与剩余前置审查仍待完成。
- `var/e11-20261029/local-runtime-manifest.json`：本次 PATH Python 3.13.0，无 Langfuse/OTel/Prometheus distribution；不代表项目各 venv。US 配置确认 `https://us.cloud.langfuse.com`，未输出密钥。
- ECS 只读探测 `runtime-readonly-v2.json`：`/opt/amrag/.venv` Python 3.12.3，Langfuse 缺失、OTel 1.39.1、Prometheus client 缺失；既有 P0 venv Python 3.12.3，Langfuse 5.0.4、OTel 1.45.1、Prometheus client 缺失。实际进程 PID 1760402、cwd `/opt/amrag/backend`、入口 app.main:app；其精确 venv、PYTHONPATH 注入依赖与指标开关仍待只读核实，不能仅从探测环境推断进程依赖。
- 首次 runtime-readonly.json 的包名判断实现不够稳健，已用直接 importlib.metadata.version 的 v2 复核，旧证据保留。第一次本机 manifest 因 utf8-sig 拼写错误失败，修正为 utf-8-sig 后退出 0。
- 已读取实际 Langfuse 5.0.4 `start_as_current_observation`、`create_score`、`flush`、`run_experiment` 签名，保存于 v2 结果。官方 SDK 页确认当前 Python v5；本轮不升级依赖。
- dataset/source/index fingerprint、ECS app_hash、运行时不可变 manifest 尚未全部核实，标 NOT_YET_VERIFIED。

## 源码差异表

| 文件与函数 | 已有观测 | 仍缺/脱敏/路径差异 |
|---|---|---|
| observability.py sanitize | key allowlist，任意正文转 hash/chars，secret 等 key 脱敏 | action 等控制字段也可能被哈希；无 BASIC/EVAL 与 manifest 授权。不能全局关闭 mask |
| observability.py snapshot | 显式开关后内容寻址本地文本 | 无 content_role/捕获时间/实际 observation 绑定；已存在文件未复核字节；无容量、期限、受控 CLI |
| observability.py span/event/observe | ContextVar 本地父子，SDK current observation，fail-open | SDK 不可用时为本地随机 ID；不提供标准 input/output；无 SDK 成功/失败回读验收状态。flush 错误吞掉 |
| observability.py candidates | rank、ID、来源、分数、parent、正文 hash | 不保存该时刻正文快照，不统一 score_kind，不记录过滤决定 |
| retrieval.py search | before_dedup/after_dedup/outcome/metrics | 无逐轮稳定 attempt_id、无完整实际 query 绑定 |
| retrieval.py _vector_search/_bm25_search | 实际两路候选事件 | fulltext 无 Vector 是不适用，不能补零；各 KB 候选与最终数量不同 |
| retrieval.py _hybrid_search | raw、normalized 两路、截断后 fusion | min-max 加权和，非 RRF；缺 Top-K 前完整融合列表及每项选择/丢失事件 |
| retrieval.py deduplicate_results | 仅前后列表 | 相同 ID 或同 file_id/kb_id 字符集合 Jaccard >0.85；缺代表 ID、真实相似度与原因。跨来源保留规则不得改变 |
| rerank.py rerank / batch_rerank | batch 输入/输出/filtered/selected；失败事件 | Agent 直接 rerank，非 batch；聊天回退正文 [:500]、batch 过长 [:max_context_tokens*2] 无送达 hash；缺 fallback_score_origin |
| retrieval_tool.py search_knowledge_base | 输出上下文和引用 | 直调 rerank，阈值/sort/topk 操作未逐项观测；[] 保留原检索分数，不得当作 rerank 分数 |
| rerank.py expand_context / retrieval_tool.py | parent、considered/included siblings、邻块 ID | 扩展后工具使用 original_content 生成引用/上下文；缺实际 delivery 对比与 group 成员映射 |
| agent_workflow.py run_tools/grade_documents | actual_query、配置、retry_count、quality、retry_decision | retry_count 不唯一对应工具调用；quality 仅 context[:2000] 未记录裁剪；action 会被 sanitize 哈希 |
| chat.py chat_stream / api/chat.py | qa.request 根装饰、qa.config、qa.answer、SSE 事件 | 根 input/output 未赋值；缺最终 SSE 与原始候选的完整变换映射；HTTP 成功不等于答对 |
| grounded_generation.py generate_grounded_candidate | grounded.request 实际 request_messages 快照、candidate 快照 | Cloud 仅 hash/ref；需明确局部快照与 Cloud 回读不同；原文件补证须区分来源 |
| llm.py generate_stream/generate/model_response | 请求快照、真实模型、usage、finish_reason、状态 | 流式、非流式和 Agent 实际送达消息须逐路核实；不能推算隐藏推理正文 |
| evaluation.py evidence_state/score_record/compare_records | raw/merged/reranked/context/cited、coverage、相邻完整覆盖与增减 | 缺同粒度完整 qrel 合约与新 catalog；已有 p0 字段不改义。缺 Gold/阶段=UNKNOWN |
| publish_p0_scores.py score_payloads/main | 稳定 trace/version/name/p0-v1 hash ID；dry-run；分页 ID 回读 | 回读主要检查 ID 子集，未核对 value、reviewer、answer hash、目标 observation；无效/未知不发布伪 0 |
| metrics.py | HTTP/SSE/provider 计数与 histogram，实际 usage | 聚合运行监控，不承载逐题质量；TTFT 为首非空 answer_chunk，不能混同模型首 Token |

## 可复盘的现有 E1 证据

只读抽取 ledger-v4 并拼接原 SSE answer_chunk，与私有 snapshot 字节 hash 比对，子代理命令退出 0。

| 题目 | 版本关联与实际证据 |
|---|---|
| 错题 row-0958 | qid `mmlongbench-doc:2ff6aa9237fc777b6627dc57a486e9225ac5fb86:row-0958`；run `p1-http-20261029-145902-5b9d1ff3` case-02；trace `883e966cd104857ce8fafcc003d422a1`；SSE SHA `70414ef18c2ea21cc5cbf924eb172eb2160117195670a4fb0c90990e63ff7345`；answer UTF-8 SHA `1e1547427fb8eece169c083f8d3b6027654babb36d9291c4a6fd37300753ef2c`；user core FAIL。原页9有 mirror，检索前20缺目标页；SSE 存在替换字符，禁止凭推测修复旧正文 |
| 正题 office50:001 | run `p1-http-20261029-145237-092fbc9b` case-00；trace `2883f3a0b30402a54e8e8b3a8a36e213`；SSE SHA `7d08892c1c6076681ff42d22b7c08117f129658a88f4b12422bbf575a08f89de`；answer SHA `abd31d8d370e4be4101c2337ce0f9ca29a6261d5f32786d8c447ccfb09fa483d`；user core PASS，忠实性/引用支持不能由 core PASS 推定 |

上述历史 cloud_readback RETURNED 与 result_case cloud_delivery NOT_VERIFIED 的范围不同；本阶段未重新 Cloud 回读，不认定已上传质量分数。历史记录无完整 reviewer/rubric/answer_hash 标准关联时应新建派生导入，不改原账本。

不可补齐例：office50:001 prior_ledger_record 的 review250-20261028 before/after 只有 answers.jsonl 回答和历史判定；sources_at_10 不能赋给未知 raw/fusion/dedup/rerank 阶段。新代码不能回推旧运行实际过滤原因，旧事件无 SDK observation_id 不补造。

## 后续最小改动与验收范围（待规范）

先由规范逐项确定字段/枚举/公式，然后最小扩展 observability.py（授权、input/output、快照），retrieval.py/rerank.py/retrieval_tool.py（实际操作与送达），chat.py/agent_workflow.py/grounded_generation.py/llm.py（真实消息与输出变换），evaluation.py/publish_p0_scores.py（catalog、reviewer/answer 关联和回读）。只读快照 CLI 与小型 schema 工具按必要性添加。

定向验证必须覆盖 E0.1–E0.7 及开关业务结果等价；之后才执行零模型 HTTP/SSE 的 Cloud 写入/回读与 E2 指标兼容检查。当前：代码实施 NOT_RUN、自动化测试 NOT_RUN、Cloud 新写入/回读 NOT_RUN、付费模型调用 0。未重跑 E1 或 Judge，未改正式服务、权重、Prompt、Gold，未提交/推送。规范缺失阻止相关字段/公式/权限实现，不阻止本报告中的只读审查。

官方资料核对：[Python SDK v5](https://langfuse.com/docs/observability/sdk/overview)、[Trace best practices](https://langfuse.com/docs/observability/best-practices)。官方建议不覆盖用户禁止上传隐藏推理的隐私要求。

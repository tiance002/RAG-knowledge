# RAG-knowledge｜Langfuse Trace 与评测指标规范 V1.0

> 文档类型：实施约束规范（供用户评审与 Codex 执行）。状态：待批准实施。制定日期：2026-10-29。
>
> 目标：在不重构问答算法的前提下，使**同一道题**能从问题、检索、候选变化、实际模型消息追溯至回答、引用与评审，同时使指标有统一定义、可信分母和准确的计算归属。

## 0. 适用范围、既有事实与禁止推断

**目标仓库：** `https://github.com/tiance002/RAG-knowledge`；审查基线：`codex/pdf-parser-migration` 分支的 `a834f8f7006bc1e9b1fb998fc9f8cb220f79d80a`。**Codex 实施前必须读取实际工作区 HEAD、脏文件、ECS 运行快照和当前 SDK 版本；若已更新，以新代码核查并报告差异，本规范的数据语义不自动变化。**

既有实现：`backend/app/services/observability.py`、`evaluation.py`、`metrics.py`、`scripts/publish_p0_scores.py`；Langfuse Cloud US、E1 30 题及 P0/P1 证据账本已经存在，应扩展、复用而非重建。E1 报告中的 27/30 是混合评判者在固定全文检索配置下的核心正确数，不是独立人工盲评，更不是 Hybrid 总体正确率。既有 Judge 校准存在大量 INVALID/UNKNOWN，不可直接作为自动验收或 RSI 的决策器。

当前源码默认 `sanitize()` 将多数非白名单字符串转成 SHA/字符数，自定义 `observe()` 只自动建立阶段 Span，`snapshot()` 仅在显式开启时将原文保存本地。因此**节点存在不代表输入输出正文已记录，且 snapshot 哈希不代表能从 Cloud 回读全文**。必须明确修复这一接口/内容策略，而不是仅重命名节点。

本规范不要求：改 Prompt、改 LLM、重建索引、重新部署监控、提高 Agent 阈值、恢复严格句式校验、修改 PDF 解析路线、让 Judge 自动修改代码。**Trace 中“执行完成/模型 stop/来源可回读”均不等于“回答正确/证据充分/引用支持”。**

## 1. 唯一数据责任：四种对象、两个平台

| 对象 | 权威记录所在 | 最低关联键 | 存放内容 |
|---|---|---|---|
| DatasetItem（题目） | 现有冻结 Gold 文件；获准时可镜像为 Langfuse Dataset | `dataset_id, dataset_version, qid` | 问题、标准答案、必答点、qrel 与证据粒度、可回答性、数据审查口径 |
| ExperimentRun / Attempt（运行） | 本地/ECS 不可变账本 + Langfuse Experiment | `run_id, attempt_id, qid, actual_code_hash` | 模型、Prompt、数据/索引、真实配置、请求状态、账单与退出码 |
| Trace / Observation（执行事实） | Langfuse + 有版本的受控快照 | `trace_id, observation_id, run_id, attempt_id` | 实际查询、候选、转换、消息输入、模型结果、最终 SSE、引用 |
| Evaluation / Review（判定） | 人工/本地评分账本；经审查同步 Langfuse Score | `trace_id, answer_sha256, rubric_version, reviewer_id_or_type` | 核心正确、忠实、引用支持、意见、来源证据、未知情况 |

`trace_id` 必须是实际 Langfuse/OTel SDK ID，不能把离线临时 ID 假称 Cloud 已收到。一个 `qid` 可以对应多个模型和版本的 Trace；同题不同答案不得共用上一轮的判定。

**工具分工固定：** Langfuse 保存执行链、Dataset、Experiment、Scores 和逐题成本/耗时；项目现有 Python 评测脚本计算 qrel/阶段证据指标后通过 SDK 写 Score；人工提供主要质量判定；可校准的 Judge 仅作辅助；Prometheus/Grafana 监控 QPS/P95/SSE 错误、服务资源和真实存在的后台任务队列，不持有 per-qid 高基数指标。不要重复建设一套 Grafana 质量评测数据库。

## 2. 采集档位与隐私（必须实现为同一套 Schema 的策略）

| 档位 | 使用条件 | Cloud 允许 | 本地受控保存 | 生产默认 |
|---|---|---|---|---|
| `BASIC` 基础运行 | 所有正常请求（观测开启） | ID/版本/配置/阶段数量/分数/状态/耗时/Token；敏感正文脱敏 | 依现有日志最小化；必要时受控摘要 | 是；不外发任意用户正文 |
| `EVAL` 完整评测 | 显式启用，数据集/文件获准上传，固定 manifest | 原问题、实际 Query、获准的候选文本或安全片段、实际消息、原始回答、最终回答、评分说明 | 不可变完整快照和散列，便于复现 | 否，仅允许清单范围 |
| `DEBUG` 深入诊断 | 某些失败题显式开启 | 候选差异、相似去重决策、排序/过滤理由等安全结构化事实 | 原始完整候选、裁剪边界、合并成员、解析来源，按需存储 | 否 |

**注意：** `DEBUG` 是 EVAL 上的附加诊断开关，不要求建立第三套系统；Langfuse SDK 采样是可选的数据量控制，不能丢掉同一已采样 Trace 的关键子节点。若采用 EVAL/DEBUG，必须由授权的冻结 `dataset_version/qid/source_sha256` allowlist 确认；不能仅凭 `.pdf` 或“测试环境”就认定可上传。若存在敏感内容/授权不明，Cloud 保留哈希和 `snapshot_ref`，正文仅可通过已授权的本地离线审计工具读取；**禁止新增无鉴权公网快照接口**。本地快照应限制权限、路径遍历、保留期限、加密/删除策略，禁止把密钥、Authorization、连接字符串、私密思考内容和模型 API Key 写入任何遥测。

推荐仍使用 Langfuse observation 的标准 `input` 和 `output`（便于 UI Preview），`metadata` 保存结构化诊断字段；需要层级时复用当前父子 Span，而不是为一个字段再创建新顶级 Trace。若 SDK Mask 在导出前仍把获准的 `input/output` 哈希掉，必须在**明确 allowlist 下**对内容字段执行精确脱敏策略；不可把全局 `mask=None` 作为修复。

每个原文快照应包含 `content_sha256`、`snapshot_ref`、`captured_at`、`source_sha256/index_fingerprint` 与 `content_role`（`original_chunk`, `rerank_sent`, `context_sent`, `provider_request`, `provider_response`, `final_answer`）；**不能只用当前 DB 的 Chunk ID 替代历史运行时实际内容**。未保存原文时标记 `NOT_CAPTURED`，不得从哈希猜测或“回填”。

## 3. 通用字段字典（所有节点复用，不得改义）

| 字段 / 类型 | 必需性 | 语义与来源 |
|---|---|---|
| `trace_id`, `observation_id` / string | M | 实际 SDK ID；本地未上报时 `cloud_export=NOT_CONFIRMED` |
| `run_id`, `attempt_id`, `qid`, `dataset_version` / string|null | M | 运行/尝试/题目/冻结集身份；非评测题 qid 可为 null |
| `session_id`, `turn_id` / string|null | O | 仅在确有多轮会话身份时填写，不推断 |
| `stage`, `stage_attempt_id`, `parent_observation_id` / string | M | 子阶段及所属检索轮，支持多次检索不串行混淆 |
| `source_type` / enum | M（证据记录） | `indexed_chunk` / `context_expansion` / `native_source_expansion` / `image_ocr` / `image_caption` / `other` |
| `kb_id`, `file_id`, `chunk_id`, `parent_chunk_id` / string|null | 条件必需 | 有身份才填；原文件补证不能伪造 indexed chunk ID |
| `source_sha256`, `chunk_content_sha256`, `index_fingerprint` / string|null | M（可获取时） | 明确区分上传源字节 SHA、索引观测指纹、运行时正文 SHA；缺失保留 null |
| `position` / object|null | 条件必需 | PDF page/bbox/table；DOCX heading/table row；XLSX sheet/cell；CSV row/column；TXT/MD 行/标题；image bbox |
| `rank`, `score_kind`, `score_value` / int,string,number|null | 条件必需 | 每一种分数明确所属算法；不可把不同分数混为 `score` |
| `operation`, `reason_code`, `rule_version`, `input_ids`, `output_ids` | 发生操作时 M | 可重现的实际动作、决策原因及候选变化 |
| `execution_status` / enum | M | `RETURNED`, `FAILED`, `UNKNOWN`, `TRUNCATED`, `CANCELLED`, `NOT_RUN` 等，按子系统实际语义记录 |
| `duration_ms`、`usage`、`cost` | 条件必需 | 真实计时与供应商报告；费用无可信价格记 null/UNKNOWN |
| `capture_mode`, `content_visibility`, `snapshot_ref` | M | BASIC/EVAL/DEBUG；Cloud 正文/本地回读/未采集 |

**禁止高基数 Prometheus Labels：** `qid`, `trace_id`, `file_id`, `chunk_id`, `query`, `prompt` 等仅放 Langfuse metadata/受控日志。`reason_code` 使用固定枚举并带 schema_version，不把自由错误信息当枚举值。

## 4. 节点契约：沿用现有节点名（M=必需，O=推荐，D=深入诊断）

### 4.1 请求入口与历史上下文

| 节点 / 执行条件 | 标准 input | 标准 output | 必需 metadata（M） | 推荐/诊断 |
|---|---|---|---|---|
| `qa.request` 根；普通和 Agent | 实际收到的用户问题；EVAL 获准则可读，其他为 Hash/快照 | 实际送达用户的最终答案、SSE 终态 | `run_id, attempt_id, qid, dataset_version, mode, kb_scope, effective_config, code_hash, index_fingerprint, capture_mode, final_answer_sha256, answer_mode, execution_status` | session/turn、用户选定知识库状态、SSE 首个 token 时间 |
| `qa.history`（仅真正传入历史时） | **实际送达下游模型**的消息序列/快照 | retained IDs、删去的消息 IDs | `history_source, history_message_count_in/out, truncation_rule, effective_messages_sha256` | 按消息 role/顺序、字数/Token、历史窗口 |
| `agent.analyze_intent`（Agent） | 真实分类消息内容/快照 | 原始响应与最终 `chat/qa` | `decision_method=rule|llm|fallback, raw_intent, final_intent, fallback_reason` | 意图评测另由评审完成，不把分类规则等于真值 |
| `llm.rewrite_query` 或 `qa.query_rewrite`（仅开启） | 原问题和实际传入改写模型的消息，历史有无要可知 | 原始改写、是否采用、最终检索 Query | `rewrite_enabled, rewrite_status, original_query_sha256, effective_query_sha256, constraints_provenance` | Agent 自主查询与普通改写分开 |

根节点保存 `effective_config`：**实际生效**的 `search_mode/top_k/score_threshold/vector_weight/bm25_weight/rerank_enabled/rerank_model_id/rerank_top_k/rerank_score_threshold/context_window/use_rewrite/chat_model/embedding_model/prompt_hash/temperature/max_tokens`；没有执行的模型参数为 `NOT_APPLICABLE`。不要只复制前端默认值。

### 4.2 检索与融合

| 节点 / 执行条件 | 记录（M） | 建议补充（O/D） |
|---|---|---|
| `retrieval.search`（每轮） | `retrieval_attempt_id, actual_query, kb_scope, effective_search_mode, candidate_count_in/out, retrieval_status, duration_ms` | 查询词分词器版本、按知识库拆分的耗时 |
| `retrieval.vector`（向量/Hybrid） | 实际 Query、Embedding 模型、索引身份、Top-K、阈值、有序候选 ID + 原始向量分数 + score_kind | vector distance/similarity 定义，index build 版本，Embedding 耗时和用量 |
| `retrieval.bm25`（全文/Hybrid） | 实际 Query、BM25 Top-K、候选 ID + `bm25_raw_score` + rank | 分词后的 query_tokens、tokenizer_version、每个来源 KB |
| `retrieval.normalized_vector`、`retrieval.normalized_bm25`（Hybrid） | `method=minmax`, 输入原始分数、归一化分数、输入/输出候选 ID | min/max、常数分数组处理 |
| `retrieval.fusion`（Hybrid） | `method=weighted_normalized_sum`, 实际权重、每项向量/BM25 贡献、`fusion_score`、排序和 Top-K | Top-K 之外候选去向、排序决策事件 |
| `retrieval.before_dedup` / `after_dedup`（所有模式） | 前后**有序**候选、实际 rank、score_kind、input/output count、scope | 相似去重决策明细，见 4.3 |

**对照当前代码：** Hybrid 是 `Min-Max + weighted sum`，不是 RRF；Hybrid 内部先 `top_k` 再做相似去重，必须分开记 `fusion.topk` 和 `retrieval.dedup`；候选数不足是否影响 Gold 需后续实证，不得单凭代码直接计为失败。

### 4.3 候选操作事件 `rag.candidate_transition`（推荐统一结构）

每次候选集合变化必须记录 `operation`（仅实际发生时）：`union_by_chunk_id`, `score_normalization`, `weighted_fusion`, `sort`, `topk`, `dedup_exact_id`, `dedup_same_source_similarity`, `threshold_filter`, `batch_rerank`, `parent_or_neighbor_grouping`, `truncate`, `exclude_image`, `restore_native_source`, `omit_from_prompt`。

事件结构：`stage_attempt_id`, `operation`, `input_kind`, `output_kind`, `input_candidate_ids`, `output_candidate_ids`, `sort_key`, `score_kind`, `top_k`, `threshold`, `rule_version`, `removed[]`。对每条实际移除记录 `candidate_id`, `reason_code`, `retained_representative_id`（去重才有）, `actual_similarity`, `threshold`, `rank_before`。非去重的 `topk` 原因必须是 `outside_topk`；缺失不是自动删除，可能是 Context Group 合并；不要将 Chunk 数和 Group 数直接相减。

可存明细于本地受控 JSONL、Cloud 仅存总量/关键差异与 snapshot_ref；EVAL 模式应可在允许范围内回读完整动作记录。日志只记录**真实执行**的理由，旧历史没有这些字段时必须 `NOT_CAPTURED`。

### 4.4 Rerank 与上下文

| 节点 / 执行条件 | M 字段 | O/D 字段 |
|---|---|---|
| `rerank.batch_input`（启用时） | 实际 Query、模型、batch_index、候选 ID→实际发送正文版本 Hash、顺序、裁剪标志 | 每个输入裁剪区间及估算 Token |
| `rerank.batch_output`（启用时） | 批次响应下标→Chunk ID 映射、rerank_score、状态 | fallback 的原分数来源、失败归属 |
| `rerank.filtered` / `rerank.selected` | 过滤阈值、前后 ID/rank、因阈值或 Top-K 丢失的 ID | 分数贡献、排序规则版本 |
| `retrieval.expand_context` | seed chunk IDs、parent IDs、considered sibling IDs、**included sibling IDs**、邻块 ID、Group IDs、group->members | 发生裁剪时保留/丢弃的实际内容和原因 |
| `context.delivery`（**新增关键节点**） | 选中证据 ID + 实际送达 LLM 的证据 ID、`content_sha256`、顺序、是否使用扩展正文/`original_content`、排除原因 | 发送前 Prompt 摘要、Token 数及出处 |
| `qa.source_verification` / `qa.source_expansion`（实际执行时） | 所用来源、状态与不适用/不可用/失败 `reason_code`、原文件 Hash、原表/单元格坐标、补证来源类型 | geometry/bbox、原文快照；扩展来源不得冒充初始召回 |

`retrieval.expand_context` 与最终 `context.delivery` **必须分开**：本仓库普通/Agent 路径存在扩展后改取 `original_content` 的代码。若证据被扩展找到但未交给模型，必须能看出真实丢失位置，不在本轮擅自修业务算法。

### 4.5 Agent 循环与答案生成

| 节点 / 条件 | M 字段 | 禁止错误理解 |
|---|---|---|
| `agent.actual_query`（每次工具调用） | `retrieval_attempt_id, retry_count, effective_query, origin, actual_parameters, tool_call_id, kb_scope` | 有一次重试 ≠ 补救成功 |
| `agent.quality` | `score_raw`, `score_parsed`, `score_valid`, `quality_fallback`, `threshold`, `context_sent_sha256`, `context_chars_examined`, `reason` | 质量分数 ≠ Gold 命中概率 |
| `agent.retry_decision` | `previous_attempt_id`, `decision`, `reason_code`, `retry_count`, `next_action`, 累计实际检索次数 | 重试提示不等于实际后续调用 |
| `llm.request` / `grounded.request` | 提供商实际收到的消息数组/可回读快照、模型 ID、有效参数、input hash、调用阶段 | 快照必须对应**发送前最终消息**，不是早期草稿 |
| `llm.response` / `grounded.candidate` | provider 原始正文/快照、finish_reason、usage、可用 reasoning_tokens、provider 状态 | `stop` ≠ 回答正确；隐藏推理不要求保存 |
| `qa.answer`（最终） | 最终 SSE 文本/快照、`answer_mode`, `generation_status`, `validation_status`, `original_answer_sha256`, `final_answer_sha256`, `transform_reason` | `validation_status=NOT_RUN` 不等于没有离线质量审查 |
| `qa.citations` / `rag_result` | `citation_id`, `claim_id`（审查时可补）、证据身份/来源/定位、引用文本快照、版本 Hash | 引用可回读 ≠ 引用支持断言 ≠ UI 可点击 |
| `qa.sse_terminal`（可与 qa.answer 合并） | `done/error`, chunk_count, empty/truncated 状态, final_bytes, delivery_type | HTTP 200 也可能 SSE error/业务失败 |

**Agent 相关限制：** 最近轮评分只看部分 Context 文本，不保证覆盖全部候选；此前 `NO_RESULTS→SUCCESS`、质量不充分、故障状态应分别展示。多轮工具调用次数与低分重试计数是两个不同指标。

## 5. 统一 EvidenceCandidate / ContextGroup / Citation（逻辑 Schema）

**EvidenceCandidate：** `evidence_id`, `chunk_id?`, `kb_id?`, `file_id?`, `source_sha256?`, `format`, `location?`, `content_sha256`, `snapshot_ref?`, `content_visibility`, `rank`, `scores{vector_raw,bm25_raw,vector_normalized,bm25_normalized,fusion,rerank}`, `producer_stage`, `retrieval_attempt_id`, `source_type`。缺失分数为 `null`，未执行的 Rerank 不能填 0。`score_value` 与 `score_kind` 可用于兼容当前旧字段。

**ContextGroup：** 独立 `context_group_id`, `seed_evidence_ids[]`, `included_evidence_ids[]`, `considered_but_excluded_ids[]`, `parent_ids[]`, `group_content_sha256`, `group_snapshot_ref`, `truncation`, `membership_reason`。不能用一个 `chunk_id` 代表整个父块合并组。

**Citation：** `citation_id`, `evidence_id/source_type`, `source_sha256`, `file_id`, `chunk_id?`, `location`, `text_snapshot_ref`, `claim_id?`, `locator_status`, `support_verdict?`, `reviewer?`, `verification_reason_code`。来源验证是运行事实；断言支持是评测结论；前端跳转是独立 UI 事实。

**位置标准（只填可靠来源）：** PDF `page/table_id/row/column/bbox`；DOCX `heading_path/table_index/row/column/paragraph`；XLSX `sheet/cell/cell_range/header/footnote`；CSV `row/column`；TXT `line_start/line_end`；MD `heading_path/block_id`；图片 `image_id/bbox/ocr_or_caption_provenance`。无法证明就 `null`，不要拿 PDF 页码填 Word/Excel。

## 6. 固定 Score 字典（严禁 Codex 自己发明同义指标）

**总体原则：** 每个指标指定 `owner`（谁计算）、`scope`（题目/阶段/实验）、`grain`（文档/页/Chunk/表格字段/单元格）、`denominator`、`rubric_version`, `label_source`、`observation_id` 与 `evidence_refs`。所有缺失、未运行、无 Gold、执行 UNKNOWN 的数值评分均为**未判定**，不得写 0。下面的 `eval.*` 名称是本项目 V1 建议的标准名称；已有 `p0.*` 保留历史，不改名覆盖，建立映射。

### 6.1 本期必做（P1）：答案与证据

| Score 标准名 / 类型 | 算法、分母或判定规则 | 计算者 / 来源 | 不能怎样理解 |
|---|---|---|---|
| `eval.core_correctness` / CATEGORICAL | `PASS/PARTIAL/FAIL/UNKNOWN`；核心事实与必答信息口径，允许同义表达 | 人审为主，AI 辅助分开 | 不等于忠实度或来源支持 |
| `eval.core_pass` / BOOLEAN | 仅明确 `PASS=1`, `FAIL=0`；PARTIAL/UNKNOWN 不发布数值；实验汇总另报确认正确/全部槽位 | 本地派生 | 不把未知强行算错误或正确 |
| `eval.answer_completeness` / CATEGORICAL | `COMPLETE/PARTIAL/MISSING/UNKNOWN`；须对冻结必答点逐项核对 | 人审/可靠结构化判断 | 不以回答字数当完整性 |
| `eval.faithfulness` / CATEGORICAL | `PASS/FAIL/UNKNOWN`；模型每条实质断言是否有**当次可用证据**支持 | 人审；校准后 Judge 可辅助 | 不证明来源真实世界正确 |
| `eval.citation_support` / CATEGORICAL | `PASS/PARTIAL/FAIL/UNKNOWN`；逐事实断言对照对应引用后汇总 | 人审/引用审查 | 不能把引用可回读当支持 |
| `eval.citation_coverage` / NUMERIC | `有支持性引用的应引事实断言数 / 全部应引事实断言数`，分母>0且事实段落可标注 | 本地/人工审核 | 不把无引用但有证据的断言算正确引用 |
| `eval.abstention` / CATEGORICAL | `CORRECT/INCORRECT/UNKNOWN/NA`；有可回答性 Gold 时判断该答/拒答是否合适 | 冻结 Gold+人审 | 空输出不等于正确拒答 |
| `eval.execution_status` / CATEGORICAL | 返回/失败/未知/截断/取消/未运行等原始状态 | 确定性 | 不等于质量评分 |
| `eval.answer_empty` / BOOLEAN | 确认最终正文为空时为 true | 确定性 | 没有保存正文≠空正文 |
| `eval.answer_truncated` / BOOLEAN | `finish_reason=length` 或已确认流式截断时 true | 确定性 | 不能推断 token 消耗组成 |
| `eval.evidence_state.<stage>` / CATEGORICAL | 同粒度 Gold 的 `PRESENT/PARTIAL/ABSENT/UNKNOWN`；stage 仅实际发生 | `evaluation.py` 扩展 | 没有 stage 观测≠ABSENT |
| `eval.evidence_coverage.<stage>` / NUMERIC | `|Gold_units ∩ observed_units(stage)| / |Gold_units|`，有同粒度完整 Gold 才可计算 | 本地确定性 | 文档级/页级不可混为 Chunk Recall |
| `eval.evidence_lost_or_recovered` / TEXT/元数据 | 相邻已知阶段 Gold coverage_delta、retained/recovered/lost，以及 transition identity | 本地确定性 | 阶段证据找回≠最终答案正确 |

### 6.2 本期可算即算（P1）：检索排名

| 指标 | 数学定义与前提 | 计算者 |
|---|---|---|
| `retrieval.hit_at_k` | `1[TopK 中至少一条已确认相关证据]`；无完整评估则未命中可为 UNKNOWN；明确 K 与阶段 | Python + qrel |
| `retrieval.recall_at_k` | `|TopK ∩ 该题全部 Gold 相关证据| / |Gold 相关证据|`；必须确认 Gold 集合完整，标记 grain/K | Python + qrel |
| `retrieval.mrr_at_k` | 首个相关证据排名 r 的 `1/r`；没有相关命中且完整标注才为0；跨题算均值 | Python + qrel |
| `retrieval.ndcg_at_k` | `DCG@K / IDCG@K`；仅具备可信相关性等级时计算，否则 NOT_AVAILABLE | Python + graded qrel |
| `retrieval.context_precision` | 按固定相关性标注的上下文命中精确率/排名精确率，必须先写明公式/粒度 | Python/人工；暂可 NOT_RUN |
| `retrieval.query_preservation` | 原问题实体、时间、字段、单位、否定、数量限制是否被改写保持 | 人审/对照；仅启用改写时 |

**强制规范：** 不可用相同的 `retrieval.recall_at_k` 混合页、Chunk、文档、表格行；元数据必须含 `grain`, `k`, `stage`, `qrel_version`, `gold_completeness`。对一题多个相关来源，`Hit@K` 和“完整证据覆盖”必须分开。

### 6.3 下一期按真实错误启用（P2）

| 指标 | 为什么先不强制 |
|---|---|
| `agent.evidence_rescue_rate` / `agent.final_rescue_success` | 必须有保持原意的改写、Gold 首轮缺失、后续恢复及最终正确的有效案例，现有非空 Hybrid 成功补救仍未通过 |
| `retrieval.rewrite_help_harm` | 需要改写开关同题配对，不能从一条 Trace 猜收益 |
| `parse.table_field_accuracy`, `parse.ocr_accuracy`, `parse.source_location_accuracy` | 需要对应格式的结构化 Gold/原件级比对，不能由答案 Gold 推算 |
| `eval.judge_error_precision/recall` | 先修 Judge 格式稳定性，准备足够的人审错误样本；当前 E1 Judge 不能自动决定修复 |
| `eval.answer_relevancy`、模型式 Context Precision | 若使用 LLM Judge，先完成校准和用量约束 |

### 6.4 质量评测的分母与缺失值

- 必须分别报告：`n_planned`, `n_sent`, `n_returned`, `n_nonempty`, `n_reviewed`, `n_core_pass`, `n_partial`, `n_fail`, `n_unknown`, `n_not_run`；空/截断可以与其他状态重叠，不能直接相加。
- **全计划槽位确认正确率** `n_core_pass / n_planned` 只是一种保守完成指标，须与执行完成率、已审核核心正确率并列；不要把 UNKNOWN 默认为事实错误。
- **已审核可判定正确率**：固定分母 `n_pass+n_fail+n_partial`，并明确 PARTIAL 单独呈现；如使用严格 pass rate 只把 PASS 入分子，不用不同分母混报。
- 全文忠实、引用、核心正确**独立分开**；同一题允许多重错误标签，错误类别数量之和可超过题数。
- 以前历史 `p0.evidence_recall` 为最终 context Gold 来源单位覆盖，不等于新 `retrieval.recall_at_k`；必须保留旧语义和来源版本。

## 7. Langfuse / Prometheus + Grafana 计算责任

| 分类 | 推荐责任 | 上传 / 展示要求 |
|---|---|---|
| 根 Trace、耗时、Model/usage、上下文 | Langfuse SDK 记录；Langfuse 显示/聚合 | 用真实 SDK observation ID，分层稳定 |
| qrel/Gold 指标：Hit、Recall、MRR、nDCG、阶段覆盖、转移 | **ECS/本地 Python**，复用 `evaluation.py` 和 `publish_p0_scores.py` | SDK Score 附 Trace/Observation、rubric、grain、K；UNKNOWN 不写数值 0 |
| 空/截断、规范格式检查 | 优先现有 Python 确定性逻辑，必要时 Langfuse Code Evaluator | 同一种规则只设一个权威实现，避免重复评分 |
| 核心正确、完整性、忠实与引用 | 用户/人工审查；Judge 经校准可辅助 | Reviewer 来源和 rubric 独立，AI 判断不伪装人审 |
| 实验比较 | Langfuse Dataset + Experiment（固定版本）+ A/B 配对派生 | 保存每题 PASS→FAIL、FAIL→PASS、成本与时延；不只报均值 |
| QPS、SSE 业务错误、HTTP P95、模型延迟、后台任务、CPU/RAM | 现有 Prometheus + Grafana | 低基数 Label；暂缺的 queue 指标 NOT_AVAILABLE |

Langfuse SDK v5 已可能与旧 SDK 例子不同。**实施时检查当前已安装版本及实际 `create_score`、`start_as_current_observation`、Dataset/Experiment API 签名，运行最小 Dry-Run + 云端回读，不要假定 v2/v3/v4/v5 一致**。当前官方文档显示 Python SDK v5；Experiment SDK 中的 evaluator 运行在项目 Python 进程，不能说 Langfuse 自动知道 Gold qrel。Code Evaluator 适合简单确定性检查，但要先验证当前权限、版本和执行限制，禁止重复造评分口径。

## 8. 最小可复现实例与验收标准（实现阶段硬约束）

**验收必须覆盖以下真实行为，但优先用既有输入、日志、确定性替身，不先发模型请求：**

1. 全文检索+未启用 Rerank：根问题、实际查询、20 个候选的排名/得分、去重操作、实际 Prompt、原始与最终回答、人工判定可通过 Trace/安全回读连接；Rerank `SKIPPED`，不得虚构其分数。
2. Hybrid + Rerank：存在 vector/bm25、Min-Max、fusion、去重、rerank 分数和过滤/Top-K 决策；同一候选能够追踪 rank 与 score_kind；失配候选可解释去向。
3. 父子/邻块：已扩展证据在 `context.build` 与 `context.delivery` 不一致时，明确标识 `omitted_from_llm`，不能谎称送达。
4. Agent 至少两轮：每轮独立 `retrieval_attempt_id` 和实际 Query，质量评分与重试决策可追踪；空库重试不能计为有效救援。
5. Gold 粒度：一页中有多个不同表头字段，页命中不能自动算目标字段命中；同题多证据 `PARTIAL` 正确计算；无 qrel 的 Recall 不产生假 0。
6. 原始与最终回答：模拟模型输出与 SSE 最终文本不同，可追溯中间变换/回退原因；截断不会被记为成功完整回答。
7. Cloud 与本地：可核对实际 SDK Trace/Observation IDs、Preview 的标准 input/output、关键 metadata 与 Scores；必须在云端 API 回读验证，只有本地 Trace ID 不算通过。
8. 隐私与降级：非 allowlist 正文绝不外发；密钥与私有路径不在 Cloud；SDK/Cloud 不可用不阻断用户问答；快照回读需要权限；没有新增公网数据接口。
9. 评测一致性：现有 E1 30 题结果仅作已有审查材料，能关联则连接；无法复原的历史字段标记 `NOT_CAPTURED`；不得重新调用历史 UNKNOWN，不重算已冻结 Gold。

**一轮达标即可停止：** 一道已确认错误题完整复盘、一个 Hybrid+Rerank 正常/筛除案例、一个 Agent 迭代案例，以及 E1 判定成功关联；没有必要为了“所有节点都齐”重跑 250 题。P2 扩展指标仅保留接口与定义，不强制全部实现。

## 附录 A：固定枚举、事件样例和数据契约（避免实现时自由发挥）

**规范版本字段：** 所有新事件写 `trace_schema_version="rag-trace-v1"`；所有评分写 `metric_catalog_version="rag-metrics-v1"`、`rubric_version`、`evaluation_source`。后续如更名或改公式，必须新建版本，不能覆写旧语义。

| 字段 | 本期允许值 | 使用规则 |
|---|---|---|
| `capture_mode` | `BASIC`, `EVAL` | `debug_details=true/false` 单独开关，不扩充第三套系统 |
| `content_visibility` | `CLOUD_INLINE`, `LOCAL_ONLY`, `REDACTED`, `NOT_CAPTURED` | `LOCAL_ONLY` 必须具有有效 `snapshot_ref` 且可通过授权工具回读；否则 NOT_CAPTURED |
| `stage_execution` | `EXECUTED`, `SKIPPED`, `NOT_CAPTURED` | 不执行的 Rerank、Vector、Rewrite 是 SKIPPED，而不是空候选或 0 分 |
| `retrieval_status` | `SUCCESS`, `NO_RESULTS`, `LOW_RELEVANCE`, `RETRIEVAL_ERROR`, `UNKNOWN` | 沿用当前业务状态；新增失效必须明确区分实际故障和无结果 |
| `generation_status` | `COMPLETED`, `EMPTY`, `TRUNCATED`, `FAILED`, `UNKNOWN`, `NOT_RUN` | 与最终 `answer_mode` 分开，状态不可覆盖 |
| `answer_mode` | `GROUNDED_GENERATION`, `EXTRACTIVE_FALLBACK`, `INSUFFICIENT_EVIDENCE`, `RETRIEVAL_ERROR`, `UNKNOWN` | 沿用当前代码实际模式，不引入未执行状态 |
| `candidate_reason_code` | `exact_id_duplicate`, `same_source_similarity`, `outside_topk`, `below_threshold`, `rerank_fallback`, `grouped_into_context`, `truncated_content`, `replaced_by_original_content`, `image_not_used`, `source_expansion`, `missing_source`, `other` | `other` 必须有实际 `reason_detail`；不可为已知行为使用 unknown |
| `verification_reason_code` | `not_applicable`, `source_manifest_missing`, `source_hash_mismatch`, `unsupported_layout`, `ambiguous_location`, `unmatched_field`, `source_unavailable`, `provider_or_parser_error`, `not_run`, `unknown` | 无法核验不等于引用错误；已观察的实际原因才填 |
| `review_status` | `PENDING_REVIEW`, `REVIEWED`, `UNRESOLVED` | PENDING 与审核后仍无法判定是不同状态 |

**候选事件结构约定（仅示意，不是实测）：** 一个 `rag.candidate_transition` 的标准 `input` 为 `{"operation":"dedup_same_source_similarity","input_kind":"chunk","candidate_ids":["A","B"]}`；标准 `output` 为 `{"output_kind":"chunk","candidate_ids":["A"]}`；metadata 为 `{"removed":[{"candidate_id":"B","retained_representative_id":"A","actual_similarity":0.91,"threshold":0.85,"reason_code":"same_source_similarity"}],"stage_attempt_id":"attempt-1","rule_version":"source-dedup-v1"}`。实际运行中只有对应真实执行的数据才能填写这些值；不能把例子作为默认值。

**生成消息契约：** `context.delivery` 的 `output` 必须是送给生成模型的**有序证据清单**及每项实际正文身份；`llm.request` 的 `input` 才是供应商收到的完整消息数组。两者之间的额外 System Prompt、历史消息、原文件补证均应可辨认。若某项证据仅在前者存在、在后者不存在，记录 `omitted_from_llm` 与实际原因，禁止标记 `context_delivered=true`。

**数据授权契约：** EVAL Cloud 正文采集先验证 allowlist，后抽取数据，最后脱敏导出；不同来源混在一个 Prompt 中时，只要任一片段未经授权，不允许把整个 Prompt 原文上传，改为 LOCAL_ONLY。Gold 与 Judge 输入不能成为被测模型的答案提示。

## 9. 外部参考与项目源码（供实现时核实）

- Langfuse SDK & Instrumentation：https://langfuse.com/docs/observability/sdk/overview ；https://langfuse.com/docs/observability/sdk/instrumentation
- Langfuse Scores：https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk
- Langfuse Datasets & Experiments：https://langfuse.com/docs/evaluation/experiments/datasets ；https://langfuse.com/docs/evaluation/experiments/experiments-via-sdk
- OpenInference：https://arize-ai.github.io/openinference/spec/semantic_conventions.html
- Ragas 指标：https://docs.ragas.io/en/stable/concepts/metrics/available_metrics/
- Promptfoo RAG 指南：https://www.promptfoo.dev/docs/guides/evaluate-rag/
- 项目源码：`backend/app/services/observability.py`, `retrieval.py`, `rerank.py`, `chat.py`, `agent_workflow.py`, `grounded_generation.py`, `evaluation.py`, `metrics.py`；`scripts/publish_p0_scores.py`；`docs/p0-evaluation-schema.md`, `docs/E1-30题复核与Judge诊断-20261029.md`。

**规范优先级：** 本文规定字段语义和边界；具体 SDK 参数必须与安装的真实版本核对。若源码行为与本文描述冲突，先列冲突与风险，不擅自修改业务流程迎合规范。

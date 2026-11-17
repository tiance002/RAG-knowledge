# P0 评测字段定义

Each record is keyed by `qid` and `experiment_version`. Gold identifiers and observed identifiers must use the same evidence granularity. A missing or empty gold set, or a missing stage observation, yields `UNKNOWN`; a known empty observation yields `ABSENT`. Partial overlap yields `PARTIAL`; complete overlap yields `PRESENT`. Transitions report retention, recovery, and loss only when both stages are known. They describe complete-gold coverage, not partial coverage changes.

Independent scores are `core_correctness`, `faithfulness`, `citation_correctness`, `citation_recall`, `evidence_recall`, `empty`, `skipped`, `truncated`, `latency_ms`, `tokens`, `cost`, and `remediation_success`; absent values are null, never zero. Location normalization supports PDF page, DOCX heading/table row, XLSX sheet/cell, CSV line, TXT/MD heading, and image bounding box; unavailable fields are null.

The replay CLI reads frozen JSONL and emits one record per input with unknown stage states and scores. Historical answer text and source audits do not establish stage candidate identity. This replay makes no live calls and does not reconstruct missing traces. The caller supplies an experiment version; duplicate comparison keys are rejected.

Coverage is the fraction of gold identifiers observed at each known stage. Transition `coverage_delta` captures partial gains and losses; complete-coverage retention/recovery/loss remain separate. Location objects retain supplied fields and expose absent standard fields as null, including source hash and index version.

Replay preserves explicit `sources_at_10` chunk identifiers as `observed_top10_chunk_ids`, without assigning them to a pipeline stage. It derives correctness only from included `correct`/`incorrect` verdicts; excluded or other verdicts remain unknown. Empty and truncation are derived only from explicit answer/finish fields or an explicit empty verdict. The arm is appended to the experiment version. Optional `--summary` reports stage state counts and top-10 observation count.

## 关联、粒度与运行约束

新增派生记录不修改历史冻结 Schema。`question`、`expected_chunk_ids`、`answer_points`、`kb_scope` 等继续留在原数据；云端只传 ID/指纹。`trace_id/span_id/parent_id` 是实际 SDK/OTel ID；未连接 Cloud 时为本地 ID。`source_sha256` 为可读源文件字节 Hash，`index_version` 为 DB 有序 chunk ID+内容 Hash 集合的观测指纹，不能当作 Chroma 物理版本。

候选保留实际顺序、分数、ID、页码和脱敏来源元数据。`raw/merged/reranked/context/cited` 为统一阶段，另有 fusion 状态。live10 的 reranked 对应 `rerank.filtered` 全候选而不是 Top-K；新代码另记 `rerank.selected`。`sibling_ids` 是考虑过的集合，`included_sibling_ids` 才是实际保留集合；旧事件有父块却没有保留列表时不能推断 context 覆盖。

## 独立评分语义

| 字段 | 定义 |
|---|---|
| core_correctness | 核心答案正确性，有明确核验才取 1/0 |
| faithfulness | 全文是否有来源不支持的断言，与核心正确分开 |
| citation_correctness / citation_recall | 引用支持关系与覆盖；本轮 smoke 未执行正式引用，未知 |
| evidence_recall | live10 派生记录为最终 context 的 Gold 来源单位覆盖率，不是 chunk recall@10 |
| empty / skipped / truncated | 分别为空、明确跳过、输出长度耗尽；未知为 null |
| latency_ms | 明确指定根跨度的耗时 |
| tokens | 实际提供商 usage；派生记录 token_scope=generation_only，不冒充全链成本 |
| cost | 用量与可信价格均充分才计算，缺失为 null |
| remediation_success | Gold 与判定充分时才判断，重试本身不等于补救成功 |

`COMPLETED` 仅表示请求返回，不等于非空或正确。`summarize_p0_trace_run.py` 连接不可变 events/results 和证据评分，写新派生文件，不覆盖原始事件。`publish_p0_scores.py` 默认离线 dry-run，显式 --publish 才提交，--verify-only 只回读；score_id 由 trace/version/name/schema 确定。Cloud 已确认 10 条 Trace 的 124 个确定性分数；未知判分不提交为 0，证据 UNKNOWN 仅按分类状态记录。

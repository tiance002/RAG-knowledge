# RSI Metrics Schema v1

31项指标均由确定性代码计算。代码规范：`rsik/analytics/schemas.py`。UNKNOWN表示缺失或不可信；N/A表示资格明确但分母为空。0仅表示在完整观测分母中实际测得0。

分区维度：experiment_id、epoch、role_profile、role_profile_sha256、evidence_mode、compatibility_fingerprint、evaluator_version、profile。agent_role/parent_id/candidate_id支持筛选，不能据其缺失猜测身份。

实际供应商账单、usage估算和账本上界分开；训练ADOPT不等于独立VERIFIED。Sandbox严格ENGINEERING_DEMO。

## A1：Agent Invocation Success Rate

| 字段 | 规范 |
|---|---|
| metric_id | A1 |
| description | Agent Invocation Success Rate |
| numerator | successful contract-valid attempts |
| denominator | actual invocation attempts |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## A2：Agent Contract Valid Rate

| 字段 | 规范 |
|---|---|
| metric_id | A2 |
| description | Agent Contract Valid Rate |
| numerator | contract-valid received responses |
| denominator | received responses |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## A3：Agent Abstention Rate

| 字段 | 规范 |
|---|---|
| metric_id | A3 |
| description | Agent Abstention Rate |
| numerator | explicit abstentions |
| denominator | actual invocation attempts |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## A4：Agent Failure Rate

| 字段 | 规范 |
|---|---|
| metric_id | A4 |
| description | Agent Failure Rate |
| numerator | failed attempts excluding explicit abstention |
| denominator | actual invocation attempts |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## P1：Proposal Valid Rate

| 字段 | 规范 |
|---|---|
| metric_id | P1 |
| description | Proposal Valid Rate |
| numerator | schema-valid proposal instances |
| denominator | proposal attempts |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## P2：Static Pass Rate

| 字段 | 规范 |
|---|---|
| metric_id | P2 |
| description | Static Pass Rate |
| numerator | static-passing proposal instances |
| denominator | schema-valid proposal instances |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## P3：Proposal Diversity

| 字段 | 规范 |
|---|---|
| metric_id | P3 |
| description | Proposal Diversity |
| numerator | distinct normalized AST strategies |
| denominator | static-passing proposal instances |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## P4：Duplicate Rate

| 字段 | 规范 |
|---|---|
| metric_id | P4 |
| description | Duplicate Rate |
| numerator | static-passing instances minus distinct normalized AST strategies |
| denominator | static-passing proposal instances |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## P5：Replay Completion Rate

| 字段 | 规范 |
|---|---|
| metric_id | P5 |
| description | Replay Completion Rate |
| numerator | candidates with completed paired replay in complete fixed-three families |
| denominator | candidates entering replay in complete fixed-three families |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## S1：Training Adoption Rate

| 字段 | 规范 |
|---|---|
| metric_id | S1 |
| description | Training Adoption Rate |
| numerator | unique training-adopted candidates |
| denominator | fully adjudicated candidates in complete fixed-three families |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## S2：Holdout Verification Rate

| 字段 | 规范 |
|---|---|
| metric_id | S2 |
| description | Holdout Verification Rate |
| numerator | unique independently verified candidates |
| denominator | unique candidates completing independent verification |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## S3：Parent Diversity

| 字段 | 规范 |
|---|---|
| metric_id | S3 |
| description | Parent Diversity |
| numerator | distinct explicitly selected parents |
| denominator | explicit parent selection events |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## S4：Branch Survival

| 字段 | 规范 |
|---|---|
| metric_id | S4 |
| description | Branch Survival |
| numerator | branches eligible at predeclared window end |
| denominator | branches eligible at predeclared window start |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## S5：Search Depth

| 字段 | 规范 |
|---|---|
| metric_id | S5 |
| description | Search Depth |
| numerator | maximum validated lineage generation |
| denominator | not applicable |
| unit | generation |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## S6：Search Concentration

| 字段 | 规范 |
|---|---|
| metric_id | S6 |
| description | Search Concentration |
| numerator | sum of squared explicit parent selection shares |
| denominator | not applicable |
| unit | HHI |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## M1：Memory Retrieval Count

| 字段 | 规范 |
|---|---|
| metric_id | M1 |
| description | Memory Retrieval Count |
| numerator | explicit raw retrieval events |
| denominator | not applicable |
| unit | count |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## M2：Memory Injection Rate

| 字段 | 规范 |
|---|---|
| metric_id | M2 |
| description | Memory Injection Rate |
| numerator | explicit injected experience uses |
| denominator | explicit retrieved experience uses |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## M3：Memory Reference Rate

| 字段 | 规范 |
|---|---|
| metric_id | M3 |
| description | Memory Reference Rate |
| numerator | distinct injected experiences referenced per round |
| denominator | distinct injected experiences per round |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## M4：Verified Association Rate

| 字段 | 规范 |
|---|---|
| metric_id | M4 |
| description | Verified Association Rate |
| numerator | experience use episodes associated with independently verified candidates |
| denominator | experience use episodes with completed independent verification |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## M5：Memory Conflict Rate

| 字段 | 规范 |
|---|---|
| metric_id | M5 |
| description | Memory Conflict Rate |
| numerator | conflicting co-retrieved pairs |
| denominator | explicit co-retrieved pairs |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## M6：Memory Staleness

| 字段 | 规范 |
|---|---|
| metric_id | M6 |
| description | Memory Staleness |
| numerator | observed entries mismatching explicit current fingerprint |
| denominator | entries with both entry and current fingerprints |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## C1：Total Model Cost

| 字段 | 规范 |
|---|---|
| metric_id | C1 |
| description | Total Model Cost |
| numerator | sum of actual provider invoiced nanodollars |
| denominator | not applicable |
| unit | USD |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## C2：Cost per Valid Proposal

| 字段 | 规范 |
|---|---|
| metric_id | C2 |
| description | Cost per Valid Proposal |
| numerator | actual provider invoiced nanodollars |
| denominator | valid proposal instances |
| unit | USD/proposal |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## C3：Cost per Completed Replay

| 字段 | 规范 |
|---|---|
| metric_id | C3 |
| description | Cost per Completed Replay |
| numerator | actual provider invoiced nanodollars |
| denominator | unique completed candidate replays in complete families |
| unit | USD/replay |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## C4：Cost per Verified Improvement

| 字段 | 规范 |
|---|---|
| metric_id | C4 |
| description | Cost per Verified Improvement |
| numerator | actual provider invoiced nanodollars |
| denominator | unique independently verified improvements |
| unit | USD/improvement |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## R1：Stage Failure Rate

| 字段 | 规范 |
|---|---|
| metric_id | R1 |
| description | Stage Failure Rate |
| numerator | explicit failed stage attempts |
| denominator | explicit stage attempts |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## R2：Recovery Success Rate

| 字段 | 规范 |
|---|---|
| metric_id | R2 |
| description | Recovery Success Rate |
| numerator | successful explicit recovery attempts |
| denominator | explicit recovery attempts |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## R3：Duplicate Execution Rate

| 字段 | 规范 |
|---|---|
| metric_id | R3 |
| description | Duplicate Execution Rate |
| numerator | extra actual executions for one action identity |
| denominator | actual executions |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## R4：Infrastructure Failure Rate

| 字段 | 规范 |
|---|---|
| metric_id | R4 |
| description | Infrastructure Failure Rate |
| numerator | attempts explicitly classified infrastructure failures |
| denominator | actual invocation attempts |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## R5：Rollback Rate

| 字段 | 规范 |
|---|---|
| metric_id | R5 |
| description | Rollback Rate |
| numerator | completed rollback operations |
| denominator | completed promotion operations |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## R6：Incomplete Evidence Rate

| 字段 | 规范 |
|---|---|
| metric_id | R6 |
| description | Incomplete Evidence Rate |
| numerator | entities failing declared evidence completeness contract |
| denominator | entities assessed against declared evidence completeness contract |
| unit | ratio |
| aggregation_level | compatibility partition |
| evidence_source | validated immutable event or explicitly bound metadata projection |
| eligibility_rule | explicit observed fields; fixed-three complete families for replay/adoption; independent verification for verified credit |
| unknown_policy | UNKNOWN for absent or ambiguous evidence; N/A for an observed empty denominator or no independent verified improvement; no implicit zeros |
| compatibility_scope | ["experiment_id", "epoch", "role_profile", "role_profile_sha256", "evidence_mode", "compatibility_fingerprint", "evaluator_version", "profile"] |

## 覆盖解释

A1/A2需要语义契约证据，账本收到响应不足以判成功。M1/M2/M5缺原始检索事件时UNKNOWN。S3/S6缺实际选择事件时UNKNOWN；父代准入/round父代只作为部分观察，不替代选择分母。S4需预登记窗口。S5需完整有效血统。

P3/P4只在完整策略AST身份可验证时计算；仅替换片段、相同新代码或缺失锚点不证明独立策略等价。Patch字节、AST、精确假设和预期影响身份分开。自然语言语义等价UNKNOWN。

C1必须供应商实际账单，C2/C3/C4沿用同一实际成本资格；账本上界只作独立成本分布。已验证候选缺improvement标志时C4为UNKNOWN，明确没有独立验证改进时为N/A。

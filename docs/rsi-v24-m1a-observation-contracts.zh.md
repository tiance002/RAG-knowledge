# RSI v2.4 M1-A 运行观测契约

既有EventLog信封rsi-event/v2.2保持，新增payload.schema_version=rsi-observation/v1。新运行目录observations.sqlite是既有EventLog的版本化观测事件，不复制费用事实，不修改历史事件。公共日志仅标识、枚举、数字与哈希；请求/响应/问题/答案正文不进入事件。

## 生命周期

| 类型 | 事件及身份 | 资格解释 |
|---|---|---|
| Agent | INVOCATION_STARTED → RESPONSE_RECEIVED → CONTRACT_VALIDATED → INVOCATION_FINISHED；invocation_id、attempt_id、request_key、round_id、agent_role、attempt | SUCCESS/ABSTAINED/CONTRACT_ERROR/API_ERROR/TIMEOUT/INFRA_FAILURE/CANCELLED分别记录；每次格式修复新attempt；收到响应不等于契约通过 |
| Proposal | PROPOSAL_GENERATED/PROPOSAL_CONTRACT_VALIDATED/PROPOSAL_STATIC_PASSED/PROPOSAL_STATIC_REJECTED/PROPOSAL_REPLAY_ENTERED | proposal_instance_id独立，重复Patch不合并槽位；候选和family只在实际产生后追加绑定事件 |
| Memory | MEMORY_RETRIEVAL_STARTED/COMPLETED、MEMORY_INJECTED/REFERENCED/USED、COMPATIBILITY_REJECTED/CONFLICT_DETECTED/CO_RETRIEVED | 原始扫描/兼容性/返回/全局top3注入/明确引用/进入回放分开；USED不是验证有益 |
| Parent | PARENT_SELECTION_STARTED/PARENT_REJECTED/PARENT_SELECTED及完成记录 | 只在select实际执行记录，准入不冒充选择；沿用排序和seed；无round上下文保持UNKNOWN |
| Stage | STAGE_STARTED/COMPLETED/FAILED/CANCELLED | stage_attempt_id、run_id、operation_id、attempt、input/output_hash、checkpoint_id；缓存验证是新的阶段尝试，不能当新副作用执行 |
| Recovery | RECOVERY_STARTED/COMPLETED/FAILED/BLOCKED | recovery_attempt_id、recovery_operation_id、original_operation_id关联；初始Learning提交标为Stage，显式恢复才是Recovery；重复恢复不造候选 |

失败类别区分RECOVERABLE/UNRECOVERABLE/INFRASTRUCTURE/CANDIDATE_QUALITY/EVIDENCE_INTEGRITY/CANCELLED。实际action进入处记录EXECUTION_STARTED，重用receipt/checkpoint不新增实际执行。观察存储失败归STORAGE_ERROR并按基础设施全局失败处理，不记候选质量拒绝。

默认观察发生在新实际调用；历史缓存缺少已观察START时，不补造生命周期。已有完整终态的缓存读取不重复计数。新事件与旧费用由cost_source+cost_request_key关联，旧账本只读分析时不额外增加模型分母。角色完整profile哈希、Evaluator和evidence mode继续分区。

所有Stage/Recovery错误原样重新抛出或沿原blocked路径处理；枚举分类不写原始异常消息。Duration为本地观察耗时；未捕获的CPU/RSS等资源保持UNKNOWN，不虚构0。

## 公共metadata字段集合

`action, action_id, actual_execution_id, admitted, agent_role, allow_bootstrap, attempt, attempt_id, candidate_id, chars, checkpoint_id, compatibility_fingerprint, conflicting, contract_result, cost_request_key, cost_source, count, current_fingerprint, decision, duration_ms, eligible, entry_fingerprint, entry_id, entry_ids, entry_state, epoch, error_hash, error_type, evaluator_version, event_key, evidence_id, evidence_mode, evidence_refs, execution_mode, expected_impact_hash, experiment_id, exploration, failure_category, failure_origin, family_id, filtered_count, format, from_state, http_status, hypothesis_hash, input_hash, invocation_id, learning_frontier, lineage_id, lineage_state, model, new_state, observation_id, operation_id, original_operation_id, outcome, output_hash, owner_id, pair_entry_ids, parent_alias, parent_id, patch_hash, policy_version, population_count, previous_state, profile, prompt_hash, proposal_id, proposal_instance_id, rank, reason, reason_codes, recoverable, recovery_attempt_id, recovery_operation_id, relation, repair_of, replay_seconds, request_attempt, request_hash, request_key, response_hash, retrieval_id, retrieved_entry_ids, returned_count, role, role_profile, role_profile_hash, role_profile_sha256, round_id, run_id, schema_version, scope, scope_hash, seam_profile, section, seed, selected, selected_entry_ids, selection_id, slot, source_event_id, source_event_type, source_hash, source_id, stage, stage_attempt_id, stage_id, stage_key, stage_status, static_pass, status, support_refs, to_state, total, transport_status, used, worker_hash`

## 合成示例

以下仅SYNTHETIC_CONTROL，不是历史事件或真实运行收益：

```json
[
  {
    "actor": "observer",
    "event_hash": "65627c1be8f4aeff11da9e61873fb3d67b320371201aedeaac831f94e0185e4a",
    "event_id": "e665b8082401fe7b61687d2cd22eaa3b0424991f64afd140f042e5d6e7f1ba0a",
    "event_type": "STAGE_STARTED",
    "evidence_hashes": [],
    "idempotency_key": "observation:8d888cd8c119f47b8a69cbd330858b14a69008bddad37ccbff1148b95485364b",
    "occurred_at": "2026-11-17T15:00:11.161763+00:00",
    "payload": {
      "attempt": 1,
      "evidence_mode": "SYNTHETIC_CONTROL",
      "input_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
      "run_id": "example-run",
      "schema_version": "rsi-observation/v1",
      "stage_attempt_id": "example-attempt",
      "stage_id": "example"
    },
    "previous_event_hash": "0000000000000000000000000000000000000000000000000000000000000000",
    "schema_version": "rsi-event/v2.2",
    "sequence": 1
  },
  {
    "actor": "observer",
    "event_hash": "50433b8604887c2af7f7eb413cad6515ea214cef4676a887c6f6c92bf091317e",
    "event_id": "8e25edf904a314b1fb42eef8c33f126e658c677a7851949bf5e2e78a4573ba1e",
    "event_type": "STAGE_COMPLETED",
    "evidence_hashes": [],
    "idempotency_key": "observation:3e56a27b7a447cb3ac50788996c5810e90c04ecad6f848e09119a54de060b214",
    "occurred_at": "2026-11-17T15:00:11.165401+00:00",
    "payload": {
      "attempt": 1,
      "evidence_mode": "SYNTHETIC_CONTROL",
      "outcome": "COMPLETED",
      "output_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
      "run_id": "example-run",
      "schema_version": "rsi-observation/v1",
      "stage_attempt_id": "example-attempt",
      "stage_id": "example"
    },
    "previous_event_hash": "65627c1be8f4aeff11da9e61873fb3d67b320371201aedeaac831f94e0185e4a",
    "schema_version": "rsi-event/v2.2",
    "sequence": 2
  }
]
```

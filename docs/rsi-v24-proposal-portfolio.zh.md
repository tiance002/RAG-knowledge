# RSI v2.4 M0 Proposal Portfolio

实现：`rsik/analytics/portfolio.py`；同一候选三槽始终保留，重复Patch仍是独立提案实例。未修改三个Proposer的行为。

本次选定8份历史事件日志及1份公开旧round归档；未扫描封闭题目。下列数量由同一只读分析代码生成，不代表全部历史提案完整清点。

分区：`{"compatibility_fingerprint": "UNKNOWN", "epoch": "UNKNOWN", "evaluator_version": "UNKNOWN", "evidence_mode": "ENGINEERING_DEMO", "experiment_id": "UNKNOWN", "profile": "UNKNOWN", "role_profile": "UNKNOWN", "role_profile_sha256": "UNKNOWN"}`

```json
{
  "ast_structure": {
    "duplicate_instances": null,
    "known_instances": 0,
    "unique_count": null,
    "unknown_instances": 0
  },
  "expected_impact": {
    "duplicate_instances": null,
    "known_instances": 0,
    "unique_count": null,
    "unknown_instances": 0
  },
  "hypothesis": {
    "duplicate_instances": null,
    "known_instances": 0,
    "unique_count": null,
    "unknown_instances": 0
  },
  "instances": [],
  "interpretation": "Exact identities only; natural-language equivalence is UNKNOWN. Replacement AST is not full candidate AST.",
  "observed_proposal_instances_lower_bound": 0,
  "patch_bytes": {
    "duplicate_instances": null,
    "known_instances": 0,
    "unique_count": null,
    "unknown_instances": 0
  },
  "proposal_instances": null
}
```

分区：`{"compatibility_fingerprint": "UNKNOWN", "epoch": "UNKNOWN", "evaluator_version": "UNKNOWN", "evidence_mode": "UNKNOWN", "experiment_id": "UNKNOWN", "profile": "UNKNOWN", "role_profile": "UNKNOWN", "role_profile_sha256": "UNKNOWN"}`

```json
{
  "ast_structure": {
    "duplicate_instances": null,
    "known_instances": 0,
    "unique_count": null,
    "unknown_instances": 1
  },
  "expected_impact": {
    "duplicate_instances": 0,
    "known_instances": 1,
    "unique_count": 1,
    "unknown_instances": 0
  },
  "hypothesis": {
    "duplicate_instances": 0,
    "known_instances": 1,
    "unique_count": 1,
    "unknown_instances": 0
  },
  "instances": [
    {
      "agent_role": "P2_BUDGET",
      "ast_hash": null,
      "ast_scope": "UNKNOWN",
      "expected_impact_identity": "874b12c7b4355123d0aa483c6349cc769aa4a80c37765e4975fdad9850f47801",
      "hypothesis_identity": "4a139a8053003af926a3deb78c8b1df997732062f57ca1d63002ec69ff71a335",
      "modified_paths": [],
      "patch_byte_hash": "a68521d86ee953b787c70c8656063947cc58d0195abb6a01ff70f062e72467ae",
      "proposal_id": "proposal-seed-only-ablation",
      "round_id": "round-caf2ebc4d081483384d92f43cd0e5f77",
      "schema_valid": "UNKNOWN",
      "slot": "P2_BUDGET",
      "static_pass": true
    }
  ],
  "interpretation": "Exact identities only; natural-language equivalence is UNKNOWN. Replacement AST is not full candidate AST.",
  "observed_proposal_instances_lower_bound": 1,
  "patch_bytes": {
    "duplicate_instances": 0,
    "known_instances": 1,
    "unique_count": 1,
    "unknown_instances": 0
  },
  "proposal_instances": 1
}
```

## 准入和缺口

角色数量、Schema、静态通过、完整回放、训练采纳和独立核查都保留资格要求；历史缺响应契约、完整FamilyDecision或验证关联时，不能根据已保存proposal数量推断合格率。完整指标值见能力报告附带的计算结果。

旧round归档仅观察到一个合法结构的提案实例，来源身份不足以证明Schema检查已经执行，因此其valid状态保持UNKNOWN。未观察到其他槽位输出不等于它们实际生成0个提案。没有完整worker AST时多样性为UNKNOWN，字节/假设/影响指纹也不替代AST。

M1设计比较固定分工、独立假设、自适应分工、验证经验引导，必须使用新版本profile，保留LEGACY。详见`rsi-v24-capability-report.zh.md`。

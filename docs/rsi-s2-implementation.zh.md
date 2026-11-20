# RSI-S2 用户目标切换与历史候选比较

S2 从 `codex/rsi-s1` 的 `86f201cc` 建立。现有实验可以继续按原方式运行。设置目标时，RSI 从已校验哈希的 launch plan 读取用户配置，冻结本轮目标，将结构化目标发送到 Diagnosis 和 Proposer，并按目标检索经验。历史候选比较可以离线单独执行。

## 用户配置

在原有 launch plan 顶层写入 `objective_transition`。首次设置用 `previous: null`，续用时传入同一份 Profile，改变配置时使用递增版本或新 Profile ID。后续实验可以通过原有文件绑定格式 `previous_search_memory: {path, sha256}` 只读引用上一轮的 Search Memory。下面的 `current` 对象也可以作为比较 CLI 的 Profile 文件。

```json
{
  "previous": null,
  "current": {
    "objective_profile_id": "context-efficiency",
    "version": 1,
    "description": "缩短送给回答模型的上下文，同时保护必要事实和来源",
    "primary_metric": "context_chars",
    "direction": "min",
    "hard_constraints": [
      {"metric_id": "necessary_fact_status", "operator": "==", "value": "PASS"},
      {"metric_id": "provenance_status", "operator": "==", "value": "PASS"}
    ],
    "secondary_metrics": ["shorter_cases"],
    "tie_breakers": [],
    "required_metric_contracts": {
      "context_chars": "rsi-context-metrics/v1",
      "necessary_fact_status": "rsi-context-metrics/v1",
      "provenance_status": "rsi-context-metrics/v1",
      "shorter_cases": "rsi-context-metrics/v1"
    },
    "components": ["context_selection"]
  },
  "reason": "用户确认本轮先优化上下文字符数",
  "source_reference": "DEV-review-20261120"
}
```

Metric Contract 固定单位、方向、计算方法、评测器身份和证据要求。Profile 冻结用户的主指标、硬约束、次指标、平局时的指标和组件范围。现有 31 项分析指标继续由 S1 分析模块读取。上述 `context_chars` 始终指完整 A/B 配对中候选的上下文字符总数。运行时只有 `context_selection` 工作流接入目标。缺少真实最终回答、检索、首 Token 延迟或 Provider 回执时，对应质量、Recall、TTFT 和问答 Token 保留 UNKNOWN。

目标变更记录在 `objective-profile.json` 中，包含前后 Profile、哈希、原因、字段差异、配置来源和时间。Round 及入选候选的训练回执绑定目标哈希。新硬约束和次指标改变版本，主目标的判定继续使用主指标、方向和指标契约。模型看到验证后的结构化目标、当前父代、父代测量摘要和适用经验。原始逐题证据仍通过归档读取。

## 经验检索

| 场景 | Handbook 成功经验 | Search Memory 和失败经验 |
| --- | --- | --- |
| 续用同一目标 | 同一主目标且组件、适用条件和评测指纹匹配的已激活经验 | 同口径正面观察和 NO_GAIN，符合条件的确认失败 |
| 加硬约束或次指标 | 仍可检索主目标一致的经验，新的约束随请求发送 | 同主目标和组件范围检查继续生效 |
| 切换主目标 | 默认排除旧目标或无目标归属的成功经验 | 确认失败仍须满足组件、评测和适用条件，旧主目标的 NO_GAIN 留在归档 |
| 旧任务未指定目标 | 原有无目标条目延续原激活和冲突规则 | 按 S1 方式读取无目标归属的记录 |

Curator 原有输出契约保持严格。完成独立验证后，由宿主从验证回执保存目标适用范围和有证据的指标变化。接口硬约束需要确定性宿主规则或用户明确授权。基础设施、父代故障及未知状态保留历史记录。

## 只读历史比较

```powershell
python -m rsik.objective_comparison --profile .\objective.json --manifest .\historical-round-manifest.json --output .\comparison.json
```

比较制品以独占写入创建，记录候选、父代、指标原值和状态、规则判定、评测分组、Pareto 状态、排名及源哈希。存在硬约束失败的候选跳过排序。主指标缺失时标为 `NOT_COMPARABLE`。次指标缺失时，主指标仍可比较，涉及缺失维度的 Pareto 结论保留 UNKNOWN。并列测量共享排名，各评测分组独立。比较结果的 `admission_effect` 为 `NONE`，原有裁决、采纳及 ParentPool 算法由现有系统维护。

只读回放使用 S1 清单列出的 5 份真实 Round。原始 SHA-256 在比较前后均匹配。各次比较同时要求必要事实和来源证据通过。

| 主目标 | 可比较 | 无法比较 | 实际测量 |
| --- | ---: | ---: | --- |
| 上下文字符数 | 3 | 12 | `f089785bb873` 和 `7befbaf349b4` 同组各 14,618 字符，并列第 1。另一组 `50527c3b3d1b` 为 17,569 字符。 |
| 变短题数 | 3 | 12 | 三个候选均为 0 道变短，排名维持组内并列或单候选。 |
| 最终问答 Token | 0 | 15 | 缺少最终问答的真实用量回执。 |

15 是历史裁决槽数，实际保留候选身份且有完整配对的 3 个候选均维持历史 `REJECTED`，采纳状态均为 `false`。相同目标值的真实记录未观察到名次变化。跨目标名次变化和多指标权衡由合成对照验证。存量 Round 的回放时长仍为 UNKNOWN。比较器当前读取显式哈希绑定的 Round，尚未接入单独的完整执行耗时制品。

## Token 回执

```powershell
python -m rsik.analytics.objective_usage --ledger .\budget.sqlite --input-receipt .\roles\request.input-receipt.json --output .\objective-token-report.json
```

报告校验发送前账本事件与请求体哈希，按实际请求键去重，复用 S1 Provider 用量，分别统计每个目标的提案输入、总输入和输出。Provider 返回整次请求用量。缺少分段 Token 证据时，Search Memory 和 Handbook 有内容的分段记为 UNKNOWN。只在精确请求确认分段为空时记为零。历史无目标归属的请求保留在总量和未归属计数。

历史报告中 271 笔请求有 266 笔返回用量。已知输入 1,335,660 Token，输出 196,848 Token，另有 5 笔未知。历史提案角色的 63 笔请求有 153,109 个输入 Token。这些请求无 S2 目标归属，无法与目标切换后做实际用量对照。本轮仅以模拟 Provider 回执验证报告计算、UNKNOWN 和去重。

| 模拟回执 | 提案输入 | 总输入 | 输出 | Search Memory | Handbook |
| --- | ---: | ---: | ---: | --- | ---: |
| 目标版本 1 | 100 | 100 | 20 | 0，未注入 | 0，未注入 |
| 目标版本 2 | 100 | 100 | 20 | UNKNOWN，已注入但无分段用量 | 0，未注入 |

这里的 100 和 20 来自测试替身，验证的是统计链路。真实目标切换后的 Token 变化尚未测量。

## 变更清单

| 路径 | 用途 |
| --- | --- |
| `rsik/objectives.py`、`rsik/launch.py` | 指标定义、用户 Profile、目标变更回执及哈希绑定。 |
| `rsik/query_workflow.py`、`rsik/openai_roles.py` | 冻结目标、工作流及请求注入、训练回执与用量关联。 |
| `rsik/objective_comparison.py`、`rsik/analytics/objective_usage.py` | 只读历史比较和按目标统计 Token。 |
| `rsik/autonomous.py`、`rsik/autonomy_contract.py` | 复用历史 Search Memory 及按组件、评测和目标过滤。 |
| `rsik/handbook/objectives.py`、`rsik/handbook/store.py`、`rsik/query_learning.py`、`rsik/observability.py` | 绑定独立验证的经验范围和效果，沿用既有 Handbook 和事件存储。 |
| `rsik/tests/test_objectives.py`、`rsik/tests/test_objective_comparison.py`、`rsik/tests/test_objective_handbook.py`、`rsik/tests/test_objective_memory.py` | S2 新增离线用例。 |

实现代码增加 795 行、删除 30 行，测试新增 664 行。新 Agent 和数据库表均为 0。

## 验证与兼容性

| 运行 | 通过 | 失败 | 跳过 | 退出码 |
| --- | ---: | ---: | ---: | ---: |
| 完整离线回归，862.08 秒 | 847 | 1 | 0 | 1 |
| 修正测试子进程解码后，重跑全部 S2 用例，85.56 秒 | 33 | 0 | 0 | 0 |

完整回归包含 S1 的 815 个测试身份，均通过。唯一失败来自 Windows 测试进程以 GBK 解码 UTF-8 CLI 错误输出。CLI 禁止覆盖的退出码为 2，测试改用字节读取并仍检查该退出码和错误标识。运行代码未随测试修正而改变。通过两份 JUnit 核对共 848 个身份，本轮没有单次全绿的完整回执。先前因冻结审查或开发期间代码变动而中断、失败的记录保存在本地。

早期 Handbook 无目标记录继续通过原哈希读取。既有保护边界检查通过。比较和用量回执为独立只读投影，旧决策、历史血统和原入选门禁保持原有语义。源码和测试复用了现有 Python 环境、已冻结的 Round 和已保存账本。实际模型调用为 0，没有部署、修改生产服务或合并 main。

本地回执位于 `var/rsi-s2/`。其中 `verification-summary.json` 核对退出码、逐项身份和日志哈希，`final/` 及 `portable-recheck/` 保存完整命令、日志和 JUnit，`historical-verification.json` 保存五份源制品和三份比较结果的哈希，`synthetic-objective-token-report.json` 保存模拟 Token 对照。

## S3 接口

S3 可以读取比较制品中的目标和契约哈希、`cohort_id`、`candidate_id`、`rank`、`pareto_status` 与 `evidence_refs`，再独立检查候选资格、独立验证和候选血统。目标变化不回写旧实验或原始基线。

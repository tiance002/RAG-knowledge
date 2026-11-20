# RSI-S2.1 实施报告

基线为 `codex/rsi-s2` 的 `9ce2cf7d`。本轮修正目标身份、经验适用性、当前工作流的指标能力与历史候选排名。

| 原有问题 | 本轮修正 |
| --- | --- |
| 不同用户目标可能因主指标相同而共享成功经验。 | Handbook 和 Search Memory 同时匹配 Profile ID、主指标、方向和指标契约。同一 ID 的描述、约束或次指标修订继续检索原经验。已确认失败沿用原组件和兼容性规则；`NO_GAIN` 仍限定在原目标。 |
| 目标变更主要用 `primary_changed` 表达。 | 分别记录 `goal_family_changed`、`primary_metric_changed`、`constraints_changed`、`secondary_metrics_changed`、`component_scope_changed`，保留原字段供旧读取方使用。 |
| 已保存的指标影响没有进入方案输入。 | 检索公开摘要附带当前目标相关的指标影响及适用性。硬约束关联指标已知退化时标为冲突，缺少测量时标为未知。明确展示次指标退化。相对方向无法证明绝对阈值已满足，硬约束摘要不会由方向推断 `PASS`。 |
| 当前工作流无法观测的指标也能成为主目标。 | Query Round 和自主搜索在开始角色调用前验证能力。无法测量的主指标和硬约束拒绝执行；不可测的次指标保留，并在提案输入及历史比较中标为 `UNKNOWN`。 |
| 次指标默认打破主指标并列。 | 组内名次由主指标和显式 `tie_breakers` 决定；次指标进入 Pareto 轴。请求的 Pareto 轴缺少可比证据时状态为 `UNKNOWN`。字符数别名不能重复作为优化轴，`parent_chars` 只作为参照量，不进入主指标、次指标或平局规则。 |

已有 Profile、Scope 和 Handbook 文件保持原格式和哈希。旧 Profile 仍可加载；只有在执行或重新建立排名时应用当前轴规则。公开摘要仅投影已保存的状态枚举，不发送内部证据路径，也不赋予 Curator 写入效果的权限。现有验证链路的已绑定效果来自 Context Selection 字符数，其他指标没有证据时保留 `UNKNOWN`。最终问答 Token 没有从字符数、合成值或缺失的用量回执推算。父代选择、候选采纳和独立验证继续沿用已有代码路径。

## 历史制品复核

复用 S2 的五份冻结 Round。回放前后核对源文件 SHA-256，15 个历史候选槽的采纳标记、裁决、门禁与测量哈希均与 S2 制品相同。

| 主指标 | 可排名 | 无法比较 | 可比组 |
| --- | ---: | ---: | ---: |
| `context_chars` | 3 | 12 | 2 |
| `shorter_cases` | 3 | 12 | 2 |
| `answer_tokens` | 0 | 15 | 0 |

历史比较仍可用于查明旧指标缺少哪些证据，只有可执行工作流才要求主指标与硬约束具备实际测量能力。复核输出保存在 `var/rsi-s2.1/historical-verification.json` 和同目录的比较制品。

## 执行回执

最终版本单次完整运行通过 856 项，失败、错误、跳过均为 0，退出码为 0，耗时 694.85 秒。按 JUnit 的 class 和 test name 逐项匹配原制品，S1 的 815 项、S2 新增的 33 项及本轮新增的 8 项全部通过。原候选采纳门禁 `ANGLE_VIOLATION`、独立回放、候选资格和恢复用例均在这次运行中通过。首次运行在修正主指标缺证据时的经验分类后中止，保留在 `var/rsi-s2.1/full/`。

最终命令、完整日志、JUnit 和退出码保存在 `var/rsi-s2.1/final/`。逐项核对、历史 JUnit 哈希和本轮制品哈希保存在 `var/rsi-s2.1/verification-summary.json`。源码、测试和本报告的完整补丁保存在 `var/rsi-s2.1/rsi-s2.1.diff`。

复用 `D:\RAG knowledge\.venv` 和现有离线 fixture。真实模型调用、部署、新数据库表和新 Agent 均为 0。

按用户约定保留临时目录，由用户自行清理。两次运行各生成一个 pytest 临时目录，分别为 `C:\Users\22088\AppData\Local\Temp\rsi-s21-5216c89c`（32.97 MiB）和 `C:\Users\22088\AppData\Local\Temp\rsi-s21-479fdd70`（140.88 MiB）。保留工作区内的完整测试与历史复核回执。执行前和收尾时 C 盘可用空间分别约为 19.36 和 19.02 GiB，D 盘均约为 98.64 GiB；磁盘快照包含机器同期的其他活动。

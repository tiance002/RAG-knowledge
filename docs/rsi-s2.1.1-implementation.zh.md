# RSI-S2.1.1 实施报告

基线为 `codex/rsi-s2` 的 `7ab43f12`。本轮修正 Handbook 硬约束语义、模型请求前校验和 Tie Breaker 证据覆盖。

| 检查项 | 实现 |
| --- | --- |
| 相对效果与绝对阈值 | 使用 Metric Contract 的方向和约束运算符判断相对变化是否不利。不利时标为 `CONSTRAINT_RISK`。缺少证据、朝有利方向移动或使用等于约束时保留 `UNKNOWN`。可信绝对值经比较后才可返回 `PASS` 或 `CONFIRMED_FAIL`。 |
| 当前制品能力 | Handbook 只保存相对效果和证据引用，公开摘要因此只产生风险或未知。历史比较仍使用核验后的绝对值判断约束，沿用原有输出状态。模型指令解释两种证据的区别。 |
| 请求前校验 | 成功经验的指标集合须精确匹配当前 Profile 的主指标、次指标、Tie Breaker 和硬约束指标。硬约束状态按 Profile 顺序生成，允许同一指标同时设上下界。请求边界逐项重算风险及总体状态，拒绝缺项、伪造的通过状态和不可测指标的非未知值。 |
| 经验类型 | 带目标的摘要由宿主标明成功、失败或通用约束。失败和通用约束沿用原检索规则，不需要成功经验的测量字段。无目标工作流继续使用原摘要。Curator 输出仍由原 Handbook 写入契约校验。已有 Handbook 文件与 SHA-256 不变。 |
| Tie Breaker | 与其他指标共享效果投影。退化会显示为权衡，缺少证据会显示为未知。历史排名仍由主指标及显式 Tie Breaker 决定，次指标只进入 Pareto 比较。 |

代码路径覆盖 `rsik/objectives.py`、`rsik/handbook/objectives.py`、`rsik/redact.py`、`rsik/openai_roles.py`、`rsik/query_workflow.py` 与 `rsik/objective_comparison.py`。测试沿用原身份、目标切换、父代选择、采纳门禁与历史读取用例。

## 验证和回执

最终版本单次完整离线回归通过 862 项，失败、错误、跳过均为 0，退出码为 0，耗时 521.74 秒。按 JUnit 类名和用例名核对，S1 815 项、S2 33 项、S2.1 8 项、本轮新增 6 项全部通过。S2.1 中原硬约束冲突用例改名为相对方向风险用例，映射记录在核对回执中。真实模型调用为 0。

只读复核五份冻结 Round，源文件 SHA-256 与 S2.1 一致，三个历史比较输出逐字段一致。历史复核见 `var/rsi-s2.1.1/historical-verification.json`。

完整 JUnit、命令、日志及退出码保存在 `var/rsi-s2.1.1/final/`；逐项计数及四份测试制品的 SHA-256 位于 `var/rsi-s2.1.1/verification-summary.json`。其 JUnit SHA-256 为 `e8f33c910d80e246be79f72a28d05d085de951a1484eecf967bc4bdf0e5ea89f`。补丁位于 `var/rsi-s2.1.1/rsi-s2.1.1.diff`。

复用了已有 Python 环境和离线测试资源。本轮产生的 pytest 临时目录为 `C:\Users\22088\AppData\Local\Temp\s211-1ea75b`，共 141.13 MiB，按用户约定由用户自行清理。测试前后 C 盘可用空间约为 20.77 和 20.21 GiB，D 盘均约为 98.64 GiB。以上测量为整盘快照。

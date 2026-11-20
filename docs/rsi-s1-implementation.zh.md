# RSI-S1 实施与验收

## 基线与边界

`tiance002/RAG-knowledge` 的原工作区在 `codex/bm25-stats-write-optimization-20261112`，HEAD 与 `rag-knowledge/main` 同为 `e242e91a`，没有 `rsik`，存在本地未提交内容。S1 在独立工作树的 `codex/rsi-s1` 上修改，基于远端最新相关提交 `911ec3a9`。该提交继承 `7b7ae225`、M1-B `af1a0dbf`、M1-C `2816e91d`、S3.3 `f43388ba` 和 S3.4 `2b5b1173`，且含后续用量及传输回执修正。

## A. 实际变更

| 文件 | 原有机制与已确认问题 | 修复 |
| --- | --- | --- |
| `rsik/experiment.py` | `read_decision` 从回放重建逐题证据；P3 读到首个 `ANGLE_VIOLATION` 就返回，后续已经实际完成的题被截断。 | 继续核验和附加已保存的题。保留原角度违规裁决及其门禁判断，任何回执完整性检查仍有效。 |
| `rsik/autonomy_contract.py` | Search Memory 把所有非 `VALIDATED` 记录作为高优先级失败。 | 用已有 verdict、执行状态、来源与原因码识别确认回归、明确静态约束或候选单测失败。无收益及正面观察按普通事实呈现。基础设施、父代故障、未知和过程状态保留在本地历史。现有上限 8 条。 |
| `rsik/autonomous.py` | 同一条反馈可能同时进入近期历史与搜索记忆；实验终态缺少已知 launch ID 关联。 | 当前输入去掉两者的确切重复，保存精简计数及序列化字节数。沿用原终态事件并补足可用的实验身份。 |
| `rsik/handbook/projection.py` | `CANDIDATE_NOT_SELECTED` 被计为 `miss`。 | 新旧未采纳事件均投影为无关联结论。证据排除优先于 `miss`。独立验证、激活、冲突、隔离及已归因的回滚判定沿用原机制。 |
| `rsik/query_verification.py` | 已有独立 sealed 终态未记录明确改善标志。 | 同一终态事件携带真实 `VERIFIED` 对应的结构改善标志。替身控制和不可判终态保留空值。 |
| `rsik/analytics/projections.py` | 候选报告缺采纳字段和可读的逐题长度派生结果；账本请求关联后部分维度丢失，独立改善标志未投影。 | 从现有回放和 Round 生成只读候选汇总，保留候选身份与原制品引用。补齐已明确的用量和验证关联元数据。 |
| `rsik/analytics/metrics.py` | 已存 Provider 用量以费用为报告主线，缺按轮次、候选、实验的 Token 汇总。 | 复用原账本请求去重和独立核查分母。汇总模型、阶段、轮次、实验与可绑定候选，未知单列。 |
| `rsik/analytics/report.py` | 默认 Markdown 展示现有货币成本指标。 | 默认展示 Token。旧指标定义、机器可读货币字段、读入兼容和预算保护仍沿用。 |
| `rsik/tests/test_analytics_report.py` | 报告测试未串起落选候选、重复账本、未知和独立验证分母。 | 新增归档与账本实际读取测试，覆盖缓存不重复计量、跨维度汇总、完成实验与可靠分母。 |
| `rsik/tests/test_autonomy_boundary.py` | 长记忆请求用旧反馈模拟确认回归。 | 为同一测试样本写明已经执行、候选原因与真实 verdict，继续检验请求体上限与原始历史保留。 |
| `rsik/tests/test_autonomy_memory_regression.py` | 缺不同性质反馈的排序与去重验证。 | 验证频繁基础设施故障保留在本地、明确回归优先、正面观察与无收益进入普通记忆，以及同一次输入去重。 |
| `rsik/tests/test_experiment_adapter.py` | 缺角度拒绝后保留剩余测量的验证。 | 从合成配对制品走正式决策读取器，确认 20 题都保留且裁决仍拒绝。 |
| `rsik/tests/test_handbook_and_promotion.py` | 缺真实归档桥入账后区分未采纳与经验关联的验证。 | 用现有 Handbook/EventLog 记录引用、尝试和未采纳，重建视图并核对无负面信用。 |
| `rsik/tests/test_v23_feedback_promotion.py` | 原请求断言要求近期反馈在 Search Memory 再出现一次。 | 从实际发给替身的 Diagnosis 请求验证分类、近期反馈的完整性、去重及本地存档。 |

## B. 数据保存核查

- **现有真实记录。** Registry 和 Round 给出候选、父代、候选内容哈希、各套题与评测身份。各候选的 `decision.json`、`decision-N.json`、`manifest.json`、`results.json`、逐题 capture 和运行收据仍是原始账。旧版普通回放和来源关系回放都保存两臂、必要事实状态和来源支持、评测身份和执行状态。容器回放用时从现有 `execution-receipt.json` 读取，单位是每次容器调用时间。不同候选的多个 suite 各自留存，Round 主摘要无需复制全部辅助题。
- **可派生。** 已绑定配对的上下文双臂长度、变短／变长／持平计数、事实与来源状态的差分均可跨目标读取。辅套用于保护检查，独立样本分母沿用既有评测。候选采纳与否不参与原始测量筛选。按候选身份定位 Round 的 slot，再沿该 slot 的 suite 制品和 `completion.json` 查验原始身份与哈希。
- **Token。** 已知输入和输出来自 Provider usage，缓存用量是输入的组成部分。成本上界和模型输出上限不能替代真实用量。按同一账本及请求键去重；共享诊断、无法唯一绑定的请求和历史缺身份的请求保留在对应轮次或未知分组。候选用量表示可归属请求，不能解释为该候选完整的边际成本。独立改善分母绑定已验证候选，由无歧义的实验／候选关系归组。
- **未测量或仅有定义。** 结构及来源回放的字符、必要事实与来源完整性属于上下文指标。最终问答正确性、真实在线问答耗时、检索质量须有自己的真实调用回执。旧数据缺实际用量、完成事件或独立改善证据时保留 UNKNOWN。31 项原有指标定义保留，缺分母的派生指标维持其 UNKNOWN／N/A 区分。

## C. 到下一轮的链路

1. 回放保存逐题结果，`read_decision` 重建决策，`adjudicate_round` 给出候选质量及本轮采纳，Round／Family 保留各候选。质量裁决、采纳和使用经验分别读。
2. `autonomous.remember_round` 将确定性 verdict、执行状态、错误来源和原因码保存原样。下一轮 `project_search_memory` 给确认回归或明确契约失败优先位置。NO_GAIN 只说明这一轮无收益，OBSERVED 仍待验证。历史中的基础设施、父代异常、未知及停止留待诊断复盘，不作确认失败注入。
3. `query_workflow.run_query_round` 检索适用的 Handbook 条目并向 Diagnosis／Proposer 注入受限摘要。 `query_learning.record_query_round_learning` 分别记录命中、引用、实际回放使用和采纳；Handbook 投影依据独立结果记录关联，经验引用本身不证明因果贡献。只有明确回滚归因才满足既有 harmful 口径。
4. Diagnosis 和 Proposer 均读取受限公开上下文。当前父代汇总描述当前结构，近期反馈描述本轮事实，Search Memory 提供有范围的历史记录。实证重复的是近期反馈与 Search Memory 的同一条事实，已在原投影入口过滤。Handbook 的适用条件与建议动作保持独立；原始逐题评测不复制到模型请求。原有 16 KiB 请求体保护继续生效。

## D. 验证

运行记录和历史回放见工作树内 `var/rsi-s1/`。沿用原 Python 环境、历史账本和归档文件。报告命令只读校验事件链与制品哈希。历史实际账本有 271 笔请求，其中 266 笔报告用量，已知小计输入 1,335,660，输出 196,848，总计 1,532,508，另有 5 笔未知。样本包含多模型和其他工作，不能归为单个候选的消耗。

- `pytest-final.xml` 实际记录 760 项：757 通过、3 失败、0 跳过，退出码 1。失败身份为 `test_feedback_reaches_actual_next_diagnosis_request` 的 infrastructure、not_replayed、global_stop 三个参数用例。其 XML 显示 `IndexError: list index out of range`，但所附源码行是 `actual['search_memory']['entries']==[]`，该表达式本身不会产生此 IndexError；失败位置与当前源码无法互相印证，根因未确认。
- `pytest-last.xml` 记录 33 项通过、0 失败、0 跳过，退出码 0。文件时间为 15:30:08，早于 `pytest-final.xml` 的 15:32:16；因此这次定向通过不能证明之后的全量失败已修复。当前测试源码把旧的重复注入断言改为断言请求中的 Search Memory 为空，并另行检查本地记忆仍保留该历史；这符合“当前反馈只在近期历史传一次”的设计，但后续全量回执仍出现上述三个错误，不能据此宣称修复完成。
- `collected-final.txt` 有 761 个唯一测试身份；相对全量 JUnit 多出的唯一身份是 `test_s1_efficiency_requires_explicit_independent_improvement`，它出现在 33 项定向回执中。两份 XML 合并后确实覆盖当前 761 个身份且每个身份至少有一条通过记录、无跳过；但这是跨运行合并结果，不是最终代码的一次全量通过。`verification-summary.json` 的 `latest_results.passed=761` 因此不能作为全量通过结论。
- 原始日志未保存实际完整测试命令。仓库 README 给出的可复现命令是 `python -B -m pytest rsik/tests tests/test_query_workflow.py backend/tests/test_rsi_candidates.py backend/tests/test_rsi_feedback.py backend/tests/test_rsi_decision.py backend/tests/test_rsi_business_source.py -vv --tb=short`，但现有回执不足以确认生成这两份 XML 时使用的正是该命令。各次运行退出码和 XML 哈希见 `verification-summary.json`。
- 只读报告同时载入历史账本和 5 份旧 Round 制品，投影 15 条候选记录。报告的模型调用与来源写入均为 0。独立验证分母和未知用量用离线测试验证。
- Python 代码（含测试）增加 303 行、删除 24 行，其中运行代码增加 181 行、删除 22 行。新表和新 Agent 均为 0。代码、报告及两次逐项结果保留在隔离工作树供审查。
- 复用原依赖和历史制品，建了一个工作树及专属测试目录。清理命令在执行前被自动策略拒绝，窗口只报告 `blocked by policy`，所以保留本轮约 145.4 MB 测试临时目录与缓存。C 盘检视时可用 22,595,874,816 字节；无清理后的测量。

## E. S2 待审设计

在现有实验冻结输入里增加可选主目标、单位、方向与测量身份。复用 S1 已保存的原始测量，只有同一证据口径且已观测的指标可以重新排序；目标变化时沿用候选身份，写新的比较配置与结果，在离线对照证明有效前保留原采纳约束。

## 修复复核（2026-11-20）

- 原报告的 3 个 `IndexError` 无法在当前提交复现：六种参数化反馈场景独立运行全部通过；随后同一条仓库完整命令也全数通过。历史失败回执没有保存完整命令，无法倒推出当时的确切原因，因此把它保留为历史异常记录，不声称已经定位到原始异常来源。
- Search Memory 不再因反馈出现在近期历史中而丢弃整条记忆。只把重复的 `history` 改为 `recent_history_index` 引用，继续保留 `selector_sha256`、尝试次数及首末轮次；最终请求边界校验引用有效，16 KiB 裁剪也会维护引用或一起裁去对应记忆项。来源事实只在 `search_history` 出现一次。
- 新请求按 request key 保存 `*.input-receipt.json`，记录近期历史和记忆条目数、独立序列化的搜索上下文字节数、实际用户消息字节数及 UTF-8 HTTP 请求体字节数。HTTP worker 和回执共用同一序列化函数，回执通过 SHA-256 绑定 `*.request.json`。新轮次的 `search-context-plan.json` 记录发送前计划，供应商接收状态由独立传输回执表示。
- 旧 canonical 请求文件仍可读取。恢复继续校验原请求及账本哈希，升级后的提示词或裁剪若使请求变化，恢复会停止。需要继续旧的冻结运行时应使用它绑定的原版本。恢复历史请求时不会补写新的请求统计。
- P3 `ANGLE_VIOLATION` 后仍保留全部逐题结果；部分题未配对时，报告将上下文长度统计标为 `PARTIAL`，不提供完整的汇总差值或题数统计。
- 旧 `rsi-metrics/v1` 报告继续保留 C1–C4 定义和未知用量语义，并可与 Token 投影共存；本轮没有增加表、Agent 或真实模型调用。
- 第一次请求回执试跑有 17 项失败。其中 16 项因提前写入 `.request.json` 触发未知状态保护，另 1 项报 Windows 文件替换权限错误。恢复账本启动后写入请求正文的顺序，并用同一组用例复跑后，67 项通过。随后全量试跑另发现嵌套临时路径超过 260 字符，以及响应写盘故障注入匹配了新回执文件。最终验收使用短临时路径，故障注入绑定响应文件的确切路径。各次结果均保留在本地。
- 本轮可复核回执位于工作树 `var/rsi-s1/repair-verification-20261120/`，包括基线重跑、定向试跑、首次失败及修复后测试。测试仅使用本地合成 `transport`；没有联网、调用真实模型、部署或修改生产服务。
- 最终验收一次运行通过 **815 / 815** 项，0 失败、0 错误、0 跳过，退出码 0，用时 689.42 秒。JUnit 逐项核对覆盖原 761 个测试身份、52 个工作流及 backend 测试和本次新增的 2 个分析测试。完整命令、原始日志、JUnit 和哈希核验在上述目录的 `acceptance/` 下。最终 JUnit SHA-256 为 `5710d8aadc786495963bc4254c545ae3470f33adffa214703555d24d4a01e47d`。
- 独立复核确认 M1 使用包含记忆引用说明的冻结提示词。受保护源码边界检查通过。S1 可以提交合并审查，S2 可以进入设计审查，S2 实现及合并由后续授权决定。

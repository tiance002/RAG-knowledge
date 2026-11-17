# RSI v2.4 M0 自主进化能力基线

本阶段交付RSI自身只读观测和比较能力，未优化RAG/GLM40，未运行真实自主搜索。以下能力状态来自实际源码审计；分析层专项测试61项，完整回归和远端证据见验收文档。

| 能力 | 状态 | 实际入口 | 局限 |
|---|---|---|
| EventLog | 已实现 | events.py:EventLog; analytics.reader.read_event_log | 验证链与文件哈希；无可信锚点不能证明历史尾部完整 |
| 六个 Agent | 部分实现 | openai_roles.py:OpenAIRoles.call | 传输记录不能证明输出契约有效，缺统一语义 invocation 事件 |
| Proposal Portfolio | 已实现 | analytics.portfolio.analyze_portfolio | 仅比较已保存且可验证的策略身份，不猜测自然语言等价 |
| Query Workflow | 已实现 | query_workflow.py:run_query_round | 主要身份来自归档 round，事件源覆盖不完整 |
| Registry | 已实现 | query_aware.py:QueryRegistry | 只读已保存不可变身份；不以当前快照重建历史 |
| FamilyDecision | 已实现 | family_decision.py:make_family | 完整三槽与终态资格必须显式存在 |
| Memory | 部分实现 | query_learning.py; handbook/store.py | 原始检索次数、共同检索集合和完整冲突分母缺失 |
| Parent Selection | 数据不足 | lineage.py:ParentPool.select | 选择返回未写事件，不能把父代准入当选择 |
| Replay | 已实现 | holdout.py:replay_artifacts | 真实资格、配对、完成、版本缺一不可，演练单列 |
| Holdout | 已实现 | query_verification.py:verify_sealed | 仅投影授权终态汇总，不读取封闭题目/答案/证据 |
| Promotion | 仅有接口 | promotion.py:PromotionSandbox | 现有实现为隔离演练，不证明生产批准或发布 |
| Recovery | 部分实现 | query_learning.py:ExperienceSession | 最终状态文件不构成所有恢复尝试分母 |
| Cost | 部分实现 | budgets.py; analytics.metrics | usage、上界、UNKNOWN 与实际供应商账单分开 |
| 任意系统源码自改 | 尚未实现 | M0 未新增权限 | M0 只新增分析代码 |

## 已运行的历史分析

输入是8个显式SQLite和1份公开归档round，哈希与完整事件链验证后投影。没有通过当前Registry反推过去。相同版本重复生成的JSON、中文报告和Schema逐字节一致，输入SHA256保持不变。

输入指纹：`3b32961deba380b0c20ed7cede308918b681fde597b73fb558734a9bf19bb56f`。投影数量与类型由代码计算：

```json
{
  "AgentInvocation": 264,
  "Candidate": 8,
  "CostEvent": 264,
  "Experiment": 141,
  "Holdout": 5,
  "MemoryEvent": 7,
  "Parent": 6,
  "Promotion": 4,
  "Proposal": 1,
  "Replay": 1,
  "Round": 56
}
```

原预算账本包含264次请求，含不同阶段和非RSI角色的历史请求，不能全部归入六Agent自主搜索。实际usage完整总量有缺口；提供已知usage小计、5条UNKNOWN、费用上界及供应商账单UNKNOWN。费用上界1,370,204,365 nanodollars是USD1.370204365，不能当作最终实扣。

ENGINEERING_DEMO与UNKNOWN分区单列；未知模式不会升级为REAL_REPLAY；历史没有足够完整字段支撑真实自主改进率。发布/回滚记录仅证明隔离演练，回滚后没有当前有效演练晋级候选。

计算结果：[历史汇总JSON](evidence/rsi-v24-m0-history-summary.json)。私有完整逐记录报告、命令日志和输入manifest在`var/m0-20261117/`，该目录不上传原数据。

## M1 设计（未实施）

| Profile | 新版本标识 | 角色契约变化 | 对照重点 |
|---|---|---|
| A Specialized | M1_SPECIALIZED_V1 | 三Proposer专业任务固定，保留既有协议 | 专业分工基线 |
| B Independent | M1_INDEPENDENT_V1 | 三槽分别提出独立假设，保留全部实例 | AST/假设/影响多样性 |
| C Adaptive | M1_ADAPTIVE_V1 | Diagnosis从Observation选择版本化任务分配并归档 | 自适应分配是否提高核查收益 |
| D Experience-Guided | M1_EXPERIENCE_GUIDED_V1 | 仅注入已验证、兼容且可追溯经验；记录检索/注入/引用 | 经验关联，不能宣称因果 |

修改模块限于role_profiles、受控角色调度和新增观测契约。建议先增加不会改变决策的invocation终态、明确proposal实例/parent/round/family关联、原始Memory检索与注入、父代选择及恢复尝试事件。保留EventLog唯一事实链，不补造旧事件，不修改Evaluator/Gold/统计硬门。

四实验组用相同内核、匹配起点、独立学习区和同样模型/费用；每组3次独立搜索，名义USD0.20/搜索，总搜索计划USD2.40。独立问答/核查及格式修复费用另需执行前按真实输入上限核算；原累计USD5不重置，账本UNKNOWN仍保留。预算/经验/事实资格不足则相应阻塞，不能删掉不利组或给D塞入合成经验。当前M0没有授权实施M1。

建议在原允许模型集合内固定六Agent模型：三个Proposer与Curator使用gpt-6.1-sol medium，Diagnosis/Meta先评估gpt-6-luna是否满足契约；四组保持同角色同模型与相同上限，若需要改变模型必须新epoch并审查。费用计划为名义值，需用实际请求尺寸与允许价格核对可完成性，不保证USD2.40已足够。

数据契约包括 invocation_id/request_key、attempt、round_id、parent_id、proposal_instance_id/slot、family_id、candidate_id、角色/内核/实验epoch/profile/兼容指纹、来源模式、费用归属、契约判定与证据引用；Memory另有retrieved/injected/referenced三类实例及条件/冲突对。

搜索单位为预先冻结的独立search；统计保留完整三槽Holm与所有静态合格提案回放，重复提案不缩家族；训练采纳、独立验证改进分别计数。主要终点为去重后独立验证改进数与其实际成本，未核查或零收益时按N/A/UNKNOWN，不造赢家。

风险包括预算不足、角色相似、过度集中父代、经验污染、Memory冲突、版本错配和反馈泄漏。负控制包括重复Patch/同新代码不同锚点、错兼容经验、未知账单、缺回执、基础设施失败、封闭数据泄漏与回滚旧批准重放。晋级仍需现有事实/完整回放/统计硬门与独立核查，生产发布保持另行人工批准。

M0完成后停止等待审查，不自动进入M1。

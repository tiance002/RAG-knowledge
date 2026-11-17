# RSI v2.4 M1-A 验收记录

基线45e77f2cb83a543dd8049387dca0a1867ee0b398；分支rsi/evolution-v24-m1-observability。PR3保持Draft，未合并。

交付观测协议见rsi-v24-m1a-observation-contracts.zh.md，M1-B仅设计见rsi-v24-m1b-profile-plan.zh.md。修改范围为OpenAIRoles、workflow/query_workflow、Handbook.retrieve、ParentPool.select、StageJournal、ExperienceSession和analytics；不改Agent策略、模型请求体、Evaluator、Gold、六保护文件、统计硬门或生产权限。

已完成合成Invocation/Proposal/Memory/Parent/Stage/Recovery及18类分析负控制，原有Stage/Recovery29项通过，query-learning集成7项通过。真实M0冻结9输入的旧版三个输出文件逐字节一致；旧事件不回填，新事件缺终态保留UNKNOWN。所有测试仅合成或mock，不用真实Gold。

完整回归、原始日志、命令退出码、逐项结果、事件样例、指标对比和源哈希绑定见本地var/m1a-20261117/交付回执及PR的CI证据。首次本地完整回归617项中616通过、1项失败（旧Handbook替身不接受observer参数），修复并复测该项通过；新增存储故障负控制后，最终源码四环境CI每Job collected618/passed618/failed0/skipped0，pytest step success，原始日志与逐项状态已核对。新增专项39项通过，包含18类分析负控制。

新观察使用现有EventLog协议及新运行目录侧车，旧EventLog/费用账本保持原字节。新旧费用关联不复制计费事实。返回缓存没有旧START不造历史事件。初始学习提交和实际恢复分别记录，恢复重试不成为新候选。

限制：无传入round上下文的Parent选择保持UNKNOWN；观测盘写失败会阻断并归基础设施错误；未采集的资源项UNKNOWN。

本阶段模型API调用0，不运行四组Profile、真实搜索、Promotion、ECS或生产，不修改GLM40，不合并/部署。完成M1-A后停止等待人工审查。

实现提交854eaf0的[四环境CI](https://github.com/tiance002/RAG-knowledge/actions/runs/44110587071)回执见[evidence/rsi-v24-m1a-ci-audit.json](evidence/rsi-v24-m1a-ci-audit.json)；同618项四次运行，不是2472项独立测试。指标新旧对比见[evidence/rsi-v24-m1a-metric-comparison.json](evidence/rsi-v24-m1a-metric-comparison.json)，仅合成新事件示范；旧M0三个输出文件仍逐字节一致。后续仅验收文档提交的最新CI列在PR描述。

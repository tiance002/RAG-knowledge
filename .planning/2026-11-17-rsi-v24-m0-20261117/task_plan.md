# RSI v2.4 M0 任务计划

## Goal
交付独立只读分析层、统一指标、组合分析、中文审计/能力/验收报告；完成M0后停止，不进入M1。

## Phases
### Phase 1: 基线和事件审计 M0-A
**Status:** complete
- PR1远端head=87fd831；新E:/rsi-v24-m0分支隔离，PR2未带入。保护文件/原账本/用户脏文件哈希已保存。
### Phase 2: 指标契约 M0-B
**Status:** complete
- 确定性统计；分模式/版本/profile；缺失N/A或UNKNOWN。
### Phase 3: 只读投影与组合分析 M0-C/D/E
**Status:** complete
- 复用事件链，禁止初始化原EventLog/Budget，禁止模型API/ECS/生产操作。
### Phase 4: 负控制及历史分析 M0-F
**Status:** complete
- 合成测试不含Gold；真实历史只读分析，保留未知与证据缺口。
### Phase 5: 完整回归与交付
**Status:** in_progress
- 核对退出码/日志/分母/输入哈希，提交推送DraftPR，审查CI原始日志，完成M0停止。

## Next Step
等待完整回归后核对原始文件哈希，提交M0并收集四环境CI。

# RSI M1-A 运行观测契约

## Goal
基于M0补齐新运行观测，不改变Agent决策、不运行付费模型/真实搜索/生产；完成M1-A后停止，不进入M1-B。

## Phases
### Phase 1: 基线和契约
**Status:** complete
- PR3head45e77f2；隔离新分支E:/rsi-v24-m1a，保护/原始日志/账本/M0报告哈希快照。
### Phase 2: 实际运行接缝观测
**Status:** complete
- Invocation、Proposal、Memory、Parent、Stage/Recovery；既有EventLog信封承载版本化metadata侧车，不修改旧事件和前沿。
### Phase 3: 分析适配与负控制
**Status:** complete
- 新旧兼容，缺终态UNKNOWN，新分母仅来自新事件，18类合成负控制。
### Phase 4: 历史回归与完整测试
**Status:** complete
- M0历史只读重放，报告字节/指标比对、输入SHA和完整回归。
### Phase 5: 交付与停止
**Status:** complete
- 契约/样例/验收/M1-B方案，提交DraftPR，核对CI原始日志，停止等待人工审查。

## Next Step
M1-A完成；停止等待人工审查，不进入M1-B。

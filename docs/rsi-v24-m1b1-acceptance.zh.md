# RSI v2.4 M1-B1 验收

基线为 PR #5 的 `7b7ae2251b2443d4c2571ef866ef2fa2b97d9e72`。交付分支为 `rsi/evolution-v24-m1b-collaboration`。本阶段只验证协作框架，完成后等待人工审查。

| 项目 | 实现与证据 |
| --- | --- |
| Specialized | P1 重复证据与等价性，P2 预算及成员选择，P3 跨成员依赖与保守回退 |
| Independent Generalist | P1 / P2 / P3 独立角色标识，系统和任务文本相同，同轮没有互读。返回 schema 只区分角色身份，统一映射到三个既有统计槽位 |
| 冻结版本 | `runtime_profile_identity` 绑定六角色有效 Prompt、任务及哈希、模型、解码、响应契约、接缝与实现。协调器和调用前检查冻结，真实启动校验额外要求冻结的完整绑定。旧 Profile 继续使用其原 Prompt |
| 共同权限 | 同一六参数 Worker 接缝、AST 校验、回放入口、事实门与三槽 Holm。每个静态合格提案独立注册和回放 |
| 配对运行 | `rsik.collaboration.freeze_pair` 冻结公共上下文、起始 Worker、配置、四套 Fixture、初始经验、模型映射、预算、裁决版本，两组各自创建 Registry、经验区和替身账本。 `run_offline_pair` 通过实际协调器运行，模型响应和回放输入用合成替身 |

## 合成对照与分母

正常两臂各六角色调用、三个独立 Proposal 和十二次套件回放。两个相同 Patch 在各臂分别派生两个不同候选实例。第三个 Worker 与它们 AST 不同，三个提案使用同一假设文本。两臂各自有完整三槽 FamilyDecision。训练裁决仅用于确认数据流，独立核查保留 UNKNOWN。

| 指标 | Specialized | Independent Generalist |
| --- | ---: | ---: |
| Schema 合格提案 / Proposer 调用尝试 | 3/3 | 3/3 |
| 静态通过 / 契约合格提案 | 3/3 | 3/3 |
| 不同完整 AST / 静态通过 | 2/3 | 2/3 |
| 重复 AST 实例 / 静态通过 | 1/3 | 1/3 |
| 套件全部结束的候选 / 进入回放 | 3/3 | 3/3 |
| 训练采纳 / 已完成回放候选 | 1/3 | 1/3 |
| 独立核查 | UNKNOWN | UNKNOWN |

运行报表保留独立 Proposal、六角色调用、失败类型、Family ID 和 mock 账本用量。通用 analytics 对运行观察和历史兼容分区单独投影。配对的回放和采纳分母直接来自该臂归档与 FamilyDecision，通用报表里无对应观测合同的 P5/S1 继续保留 UNKNOWN。替身用量不计入原账本。合成记录不产生真实收益或实验胜方。

新的父代选择诊断为显式 opt-in 的 `rsi-selection-metrics/m1b1`。一次空选择加一次实际返回时，选择成功率 1/2、已选父代多样性 1/1、条件 HHI 1/1。保留旧 S3=1/2、S6=1/4。见[指标定义和口径](rsi-v24-m1b-selection-metrics.md)。

## 测试与证据

专项覆盖重复 Patch、同假设不同 AST、跨 Profile 分区、Prompt/模型/解码漂移、弃权、契约失败、AST 越界、基础设施失败、三槽不足及私有评测字段被阻断。全量包含 M1-A.1 的观察存储失败、Checkpoint 中断恢复、未知费用、请求去重、历史口径和 Legacy / Dependency 兼容性负控制。原始 CI 日志、各 Job 退出状态、逐项清点与本地回执见草稿 PR 和本地 `E:/rsi-v24-m1b/var/m1b/`。早期运行期间实现字节先后发生变化，冻结检查按预期阻止那一轮继续，此失败日志留存。

M0 按冻结 manifest 只读复算。原历史输入、EventLog、费用账本与保护文件的核对见最终回执。合成夹具和测试临时资源集中在本地 `for-user-delete`，复用现有解释器和已冻结输入。

[M1-B2 准备度](rsi-v24-m1b2-readiness.zh.md)结论为 BLOCKED_DATA，实际合格起始父代与独立核查就绪度尚未建立。本轮付费调用 0，真实搜索和 M1-B2 均未启动。

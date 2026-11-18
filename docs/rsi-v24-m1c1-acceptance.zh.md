# RSI v2.4 M1-C1 验收

基于 PR #6 的 `af1a0dbf5831d895df2fdcb16dbd59baac79eb5e`。本阶段交付纯 Dispatcher、Adaptive Profile、离线证据和 S3.2 数据审计。真实候选、真实搜索和 M1-B2/M1-C2 均未启动。

## 调度和边界

`M1_ADAPTIVE_V1` 保持 Diagnosis、三个独立 Proposer、Curator 和 Meta。Adaptive Diagnosis 通过严格版本化契约提供任务类别、支持/冲突标记和公开 evidence_refs。Dispatcher 复核所引 Observation 的真实公开计数与同类条件，输出三个槽位和独立任务 ID。较高 confidence 不会补足缺失证据。

| 合成输入 | 期望与受测行为 |
| --- | --- |
| 重复证据明确 | DEDUP 可分配，重复任务记录独立探索理由 |
| 非种子预算线索明确 | BUDGET 可分配 |
| 跨成员依赖计数支持 | DEPENDENCY 可分配 |
| 三类混合 | 依冻结顺序覆盖三类，三个槽位分别接收任务 |
| 不能归因或引用与任务不匹配 | 缺乏支持的专业任务不分配，可使用 GENERALIST |
| 需要额外证据或接缝不可表达 | 保留三个 ABSTAIN 槽位，跳过对应模型调用 |
| 同类支持和冲突并存 | 排除该专业任务。有其他支持则仍可分配 |
| 显式 Dispatcher 不可用 | 写入降级事件和冻结身份，符合条件时使用冻结通用任务 |
| 契约、存储、安全或恢复异常 | 停止依赖动作，保留失败，不降级绕过校验 |

Dispatcher 不调用模型，只读取白名单 Observation 和经核验的结构化诊断。它没有读取 Memory 或历史聚合来间接新增证据权重。本阶段保留这些可公开输入的原运行边界，历史计数缺少依赖字段时不推定支持。新 Profile 绑定 Dispatcher 和 Catalog 版本、基础权限、有效 Prompt、响应契约及降级策略。降级只改变当前 Proposer 任务，诊断原件保留。

Assignment 先落盘并发出元数据事件。每个 Proposer 只收到自己的 Assignment 和公共视图。可重复展开冻结计划，重复调用使用原有效回执或失败闭合。实际候选权限仍由原 AST、签名、事实门、回放和固定三槽 Holm 裁决。合成全轮用 Mock Transport、确定性夹具和实际 QueryWorkflow，测试三个提案、十二次套件回放与六角色；完成后的只读恢复重新计算裁决并抵御任务计划篡改。

## 验收与遗留

旧响应契约与新 Adaptive Diagnosis 单独版本化。三槽版本及六保护文件不改动。M0 只读复算 757 条投影，三份报告逐字节相同。九份冻结输入、原 EventLog 与原费用账本以及保护文件哈希见 [历史回执](evidence/rsi-v24-m1c1-history.json)。四环境 CI 和逐项原始日志以 Draft PR 的最终回执为准。本地原始日志和 Profile/合成证据保存在 `E:/rsi-v24-m1c/var/m1c/`。

实际数据仍为 BLOCKED_DATA。12 道基于两组已曝光 DEV 文档的问题仅供标注、检索和控制设计。当前没有经批准的 BASELINE_QUALIFIED 或可宣告的真实收益。见 [S3.2 审计](rsi-v24-s32-data-audit.zh.md) 和 [后续真实实验计划](rsi-v24-m1b2-m1c2-experiment-plan.zh.md)。

[Profile Manifest](evidence/rsi-v24-m1c1-profile.json)

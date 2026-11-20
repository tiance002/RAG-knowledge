# RSI 第一阶段 F4 反思与经验对照

阶段基于 F3 `e60eab4f`。Diagnosis、P1/P2/P3、Curator/Meta、Search Memory 和 Handbook 继续使用已有角色与存储。Search Memory 记录尝试和失败归因，Handbook 保留通过可信证据获得的策略及适用条件。S1、S2 已有的目标隔离、失败分组、硬约束风险及独立验证继续适用。

## 重复提议的记录

自主控制器在原 Round 和 Search Memory 保存后写出 `proposal-repeats.json`。每条记录区分 exact Patch、AST 等价但字节不同、同轮重复和已经出现的跨轮尝试。重复统计范围绑定父代与 Worker/配置、冻结 fixture、目标和兼容指纹。只有既有分类确认的候选失败才计入重复已确认失败；NO_GAIN、过程故障、未采纳或 UNKNOWN 各自保留。指标只是诊断，不触发拒绝、规则升级或权限变更。记录原 Round 的同时将摘要哈希绑定到状态与事件。

## 受控请求消融

固定同一个父代、目标、诊断、模型配置与基本输入。通过实际 OpenAIRoles 请求组装和 P2_BUDGET 角色输出验证路径运行四个有无经验输入的对照，传输由确定性替身接管。核对同一接收范围内的回执、实际 UTF-8 请求字节和哈希，以及应用提案所得 Worker artifact 摘要。除 Search Memory、Handbook 及其已有适用性说明外，请求内容一致。

| 离线控制 | 实际请求字节 | 比无经验增加 | 替身是否重复已知 Patch |
| --- | ---: | ---: | --- |
| 无经验 | 8758 | 0 | 是 |
| 仅 Search Memory | 9366 | 608 | 否 |
| 仅 Handbook | 9751 | 993 | 否 |
| 两者并用 | 10359 | 1601 | 否 |

替身接收到经验字段后改用另一条固定 Patch。经验经请求边界到达，实际 Diff 应用产生了不同的 Worker。真实模型的重复率、Token 用量和答案质量尚未测量，真实模型调用为 0。替身的 1/1 用量仅用于触发既有账本收据流程。初次对照缺少替身用量，预算账本以 UNKNOWN 拦截。第二次碰到临时 Handbook 路径重名，在独立目录执行的第三次完成四组对照。回执保存在 `var/rsi-first-stage/f4/`，四组对照结果见 `verified-experience-ablation.json`。

## 验收与资源

单次完整离线回归 1,034 项通过，失败、错误、跳过均为 0，退出码 0，耗时 495.36 秒。按 JUnit 测试身份逐项核对 F3 的 1,007 项和 F4 新增 27 项。定向回归 49 项通过。24 条既有 Pydantic、datetime 和 LangGraph 弃用警告均保留在原日志。完整 JUnit、命令、日志、退出码、阶段 Diff 和 SHA-256 在 `var/rsi-first-stage/f4/`。

复用既有 Python 环境、控制用 fixture、账本和请求回执结构。离线消融只写本地回执。完整回归的临时目录为 `C:\Users\22088\AppData\Local\Temp\rsif4-549ea`，约 189.75 MiB，保留供用户手动清理。C 盘可用空间约 18.77 / 18.46 GiB，D 盘均约 98.64 GiB。无新增容器、语料或索引副本，本阶段无删除操作。

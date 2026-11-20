# RSI v2.2 进度入口

实际 RSI 代码位于 `E:\codex_workspace\2026-11-03\task\rsi-work`。沿用已有执行器，本次未在产品仓库复制一套 RSI。

本次完成 P-1 数据准备的只读核查器、待准备清单和 25 项合成契约测试；真实 20 题人工审核、R0 登记和配对回放仍未完成。方案要求逐阶段人工确认，当前未进入 P0–P7。

- [实施与验收记录](E:/codex_workspace/2026-11-03/task/rsi-work/docs/rsi-v22-p1-foundation.md)
- [核查器](E:/codex_workspace/2026-11-03/task/rsi-work/scripts/rsi_foundation.py)
- [待准备清单](E:/codex_workspace/2026-11-03/task/rsi-work/experiments/v22/foundation.pending.json)
- [本任务计划](../.planning/rsi-v22-20261115/task_plan.md)

新测试 25 passed、退出 0；原候选/裁决/回放回归 30 passed、2 failed、退出 1。两项失败在已有业务工作区与冻结提交不一致的检查处停止，未放宽保护。pending 清单实际预检为 BLOCKED、退出 2。详见 RSI 工作树下 `var/rsi-v22-20261115/` 原始日志。

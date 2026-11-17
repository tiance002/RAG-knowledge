# S2.1 启动、恢复与基线资格收尾

设计基准为 PR #1 的 `dabaaa7f5219ec844b725bd05991d28783a691f4`。入场远端和本地一致，未发现新增提交。本阶段是工程实现和合成控制验收，不声称已有真实基线资格。

`LaunchContext` / `validate_launch` 替代准备清单布尔值作为执行前检查。实际单轮、自主循环、独立核查、基线资格重建，以及真实 OpenAI 传输和 Capsule setup 边界均要求受信上下文。检查实现清单、六文件、业务归档、策略/模型、fixture、覆盖重算、来源与实时暴露快照、原预算事件前缀及未决请求、价格审定、经验快照、运行目录和分析计划。准备工具的 `approved=true` / `ready_for_real_run=true` 不能替代这些证据。

许可绑定同一 run/phase/plan/工件；TRAIN_ONLY 父代需复算完整训练家族并证明同一运行血统，不能换 run ID 绕过停止。最终核查还要求冻结的 nominee 及训练证据绑定。旧五参数/旧 evaluator 继续保留；新来源结构按显式 profile 派发。

`BASELINE_QUALIFIED` 为独立起始资格。显式工程/迁移/用户指定的根通过 AST、不变式、四套件真实 A/A 和 G1–G4，且两臂一致、delta=0，才产生真实资格；不要求 G5/G6，不伪造 ADOPT、完整提案家族或 VERIFIED。模型普通子代不能自行切换为根。合成模式只产生 `CONTROL_BASELINE_QUALIFIED`，被隔离的版本不能自动恢复。

ExperienceSession 的新恢复入口打开 pending 状态后，核对已登记操作、初始 frontier、完整输入/请求/响应/证据绑定与已完成事件前缀。只补缺失后缀；未知请求、费用、事件归属或未声明任意回调保持 RECOVERY_BLOCKED。七个断点覆盖 intent、部分事件、条目创建、激活、完成回执、frontier 更新和 intent 清理前；各点测试重建 session、恢复及下一次实际请求构造（传输替身）。不重新发送模型请求，不重建空手册，不无条件删除 intent。

新增入口包括 `qualify_rsi_baseline.py`、`recover_rsi_learning.py`；原真实入口增加 launch 参数。默认真实模式无许可即拒绝，合成测试必须显式选择控制模式。

验收边界：正控制使用明确标注的合成数据、捕获和模型传输替身；完整回执形状控制不是实际容器执行证明。本次真实重建依赖 S3 数据、来源审定和模型身份，尚未运行；不存在可部署的新基线。实际测试和原始日志索引见最终阶段报告。

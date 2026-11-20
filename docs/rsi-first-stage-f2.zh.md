# RSI 第一阶段 F2 三类候选接入

正式产品 Worker 源码基线为 `e242e91a`，F2 工程工作树基于 F1 `af31f843`。组件授权遵循 [L1–L3 与 Git 交付门](rsi-first-stage-governance.zh.md)。

## Context Selection 受限代码

复用 R1 内容寻址资料、已审查的 `select_member_indices` AST 接缝、真实本地子进程运行器和隔离业务树 `D:\RAG-R0@90d86ce8`。从已保存的合法 A/B bundle 读取固化候选，写入本阶段新结果目录；不更改原制品、不使用远端 ECS Capsule。运行时按现有规则验证业务代码、参考 Worker、候选补丁、配对输入与交付内容回执。正式问答路径仍未执行。

| 控制 | A/B 上下文字符 | 结果 |
| --- | ---: | --- |
| 已审查等价选择器子代 `43309ef3fcea` | 72 / 72 | 配对 1/1，退出码 0，保持必要事实及来源。 |
| 已审查的合成故障注入子代 `0f178f4d77c2` | 72 / 32 | 配对 1/1，退出码 1 为预期。观察到 `required_fact_loss_vs_parent`，不能将缩短获益当作合格。 |

两次分别绑定父代 `3a82adba110b`、旧业务基线、变量与 Diff、合成用例及评测，并留下独立的命令、日志、结果、版本和反馈。SHA-256 核查见本地 `var/rsi-first-stage/f2/code-controls-verification.json`。两次实际外部模型调用均为 0，答案质量为 NOT_RUN，用量为 UNKNOWN。此控制是受限代码执行证据，不是实际业务错误修复或候选晋级证据。

## Prompt 和参数

`rsi_quality_loop.execute_controlled_loop` → 原有 Registry `register_baseline/derive/materialize/verify_manifest` → `rsi_quality_execute.execute_controlled_artifact` → 原正式 Worker 的产品 API、BM25、Prompt 编译与 SSE。使用显式 `rsi-f2-offline/v1`，旧 v1 Registry 可继续读取。新 profile 只接受 Prompt addendum 或 `top_k` 单字段变化；`top_k` 是 1–100 的整数。Memory、Workflow、Rewrite、Rerank、Hybrid 和其他参数在这次控制中固定。

宿主生成完全合成的本地样本和未评分的评测契约。受控子进程校验正式 Git blob 清单和实际导入源码，隔离运行目录，并在原 HTTP 边界用确定性响应替代出站生成。宿主核对实际发送给替身的编译消息、UTF-8 请求、原始请求回执和实际检索配置。通过前置检查不会产生这些证据。

| 同一基线的离线对照 | 实际消费者 top_k | BM25 返回 | 最终消息 |
| --- | ---: | ---: | --- |
| 基线 | 10 | 3 | 原始编译并冻结哈希 |
| Prompt 候选 | 10 | 3 | 现有 Prompt hook 添加约定指令，最终消息哈希改变 |
| 参数候选 | 1 | 1 | 实际消费参数不同，配置及消息均冻结哈希 |

三个臂各有 1 次替身请求，0 次实际外部模型调用。输入/输出 Token 保留 UNKNOWN，回答质量 NOT_RUN。首轮正式编排发现 API 将阈值转换为浮点数，导致期望配置哈希与实际不一致。保留首轮失败回执，按正式 Schema 规范化数值后，在独立目录完整重跑通过。回执见 `var/rsi-first-stage/f2/quality-control-v2/` 和 `quality-controls-verification.json`。

资源保留：子代理在单独合成测试结束后清理了该测试自生的 5 份可再生成 jieba 缓存（46,274,675 字节）。未清理用户或正式资源，124 个原始合成请求、回执、日志及结果保留在 `rsik/artifacts/f2-harness-synthetic-20261120/`。后续按用户约定只列清理位置。

## 验收

单次完整离线回归 969 项通过，失败、错误、跳过均为 0，退出码 0，耗时 457.11 秒。逐项核对 F1 的 926 项以及 F2 新增 43 项，包含两项实际正式源码离线集成。24 条既有依赖/时间 API 弃用警告保留在日志中。完整 JUnit、命令、日志、退出码、阶段 Diff，以及逐项核对和 SHA-256 均保存在 `var/rsi-first-stage/f2/`。

复用 Python 环境与既有业务源码，合成检索用内存 SQLite；未复制 Chroma、业务数据库、真实语料或部署。完整回归临时目录 `C:\Users\22088\AppData\Local\Temp\rsif2-3550a` 大小 186.49 MiB。C 盘整盘可用空间快照约 19.37 / 19.10 GiB，D 盘约 98.64 / 98.64 GiB。

F3 将合格父代选择接入现有自主循环。F2 的工程对照确保三类候选有受控执行入口。正式答案质量、独立 holdout、多指标收益及新的组件准入仍要在对应阶段验证；本阶段结果不触发合并或发布候选。

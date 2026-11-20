# P0/P1 后续独立复核（2026-11-12）

## 目标与范围

按负责人指定分工，使用 6luna high 独立审查、6.1sol medium 执行证据复算、6.1sol xhigh 审查复杂事务行为。主代理整合结果。仅继续当前 P0/P1 收尾，不自动扩张到 P2 或 Hybrid 并行。

实际仓库为 `D:\RAG knowledge`。当前分支 `codex/bm25-stats-write-optimization-20261112`，HEAD 为 `7c7415f85a063e3f7645f676bd22f7c8f766a732`。既有未提交文件保留，暂存文件数为 0。`origin` 是上游；用户仓库的别名是 `rag-knowledge`，本次未发起远端操作。

## 本次实测与复核

| 项目 | 结果 | 依据 |
|---|---|---|
| 交付文件身份 | PASS | 对旧 delivery-manifest.json 中的 11 个文件重新计算 SHA256，全部一致 |
| 完整补丁身份与适用性 | PASS | 补丁 SHA 与旧清单一致；`git apply --reverse --check` 退出 0，未应用补丁 |
| 测试账本与原始回执一致 | PASS | 重读 final-directed.xml 与原始日志：116 项、0 FAIL、0 ERROR、0 SKIP；这是旧测试回执复核，不是本轮重跑 |
| BM25 消费者与返回值兼容 | 未发现确定缺陷 | 检索仍从 Posting 取得查询词 DF；统计读取 N/平均长度；文件替换调用端未发现继续解包旧三元组的路径 |
| 提交失败与 ORM 状态 | 未发现确定缺陷 | 两个原提交边界不变；rollback 保留原异常，不推断外部结果；窄列加载未发现延迟 JSON 依赖；已加载删除实体按 fetch 同步 |
| 旧 JSON 恢复工具 | 未发现确定缺陷 | BEGIN IMMEDIATE、独立已提交读连接备份、更新前后验证、commit 异常 UNKNOWN 均与已保存的合成验证一致 |
| 新增功能测试/性能基准 | NOT RUN | 无产品变更和确定复现缺陷，无必要重复相同测试 |
| ECS、真实文档、真实模型 | NOT RUN | 沿用本轮禁止远端与外部模型操作的边界 |

本轮未修改产品代码，未升级依赖、改表、改 Gold、提交、推送或部署。新增的是本报告及 `.planning` 下的审查/身份核验记录，没有复制数据库、索引或新建容器。

## 原始性能复算

以下复用已保存的本地合成数据，不是新测量，不将不同实验合并：

| 实验 | 7c 完整替换中位数 | 候选完整替换中位数 | 结论 |
|---|---:|---:|---|
| 首次三臂，各 5 次正式样本 | 2019.362 ms | 2134.601 ms | 候选慢约 5.7% |
| 确认实验，各 5 次正式样本 | 2068.805 ms | 2042.641 ms | 候选快约 1.26%，不能据此替代首次结果 |

两次实验共同确认：SQL 执行次数 13→9，全库 DF JSON 热路径处理取消，WAL 增量 14,873,232→13,513,632 字节（约减少 9.14%）。候选仍要对新子块进行必要的词频 JSON 编码，不能说所有 JSON 编码归零。

完整耗时包含分词准备、SQL 替换和提交等阶段，不包含完整 Chroma 索引发布。JSON 计时嵌套在阶段计时内，不重复相加。7c 与候选样本中，总耗时和阶段总和之差约为 2–4 ms；历史 270.215 ms 未归属间隔位于一个 old2041 样本，不能把它当成候选实现的缺陷。

变动原因仍为 UNKNOWN。稳定的总延迟改善、绝对峰值内存改善以及 ECS 实际 IOPS/吞吐改善均未证明，不能宣布性能整体验收通过。

新增证据限制：P0 原始报告没有内嵌 runner 哈希，因此无法完整证明该次执行脚本的精确字节身份。后续 P1 与确认实验的候选、脚本及原始 JSON 身份已可核对。此缺口不通过补写历史字段伪造修复。

## 命令与退出码

执行位置：`D:\RAG knowledge`。以下为本次执行的关键命令/检查，均未修改产品内容：

```powershell
git -C 'D:\RAG knowledge' rev-parse HEAD
git -C 'D:\RAG knowledge' branch --show-current
git -C 'D:\RAG knowledge' diff --cached --name-only
git -C 'D:\RAG knowledge' apply --reverse --check '.planning/2026-11-12-bm25-stats-p0p1/changes.patch'
```

以上退出码均为 0，暂存清单为空。PowerShell `Get-FileHash` 与 XML 读取检查退出 0，逐项身份结果在 `followup-source-integrity.json`，Git/补丁核验在 `followup-git-integrity.json`。6.1sol medium 复算退出 0，具体公式/原始样本与哈希核验见 `followup-execution.md`。审查过程部分探查不存在文件/引用的命令失败，未用其输出宣称验证通过，相关限制保留在子代理记录中。

## 证据位置

以下均位于 `D:\RAG knowledge\.planning\2026-11-12-bm25-stats-p0p1\`：

- `followup-luna-review.md`：独立正确性/兼容审查。
- `followup-execution.md`：原始性能复算、版本与计时范围。
- `followup-complex-review.md`：事务、ORM、备份恢复审查。
- `followup-source-integrity.json`、`followup-git-integrity.json`：本次身份检查。
- `final-directed.xml`、`final-directed.log`：沿用的原统一 116 项测试回执。
- `benchmark/p1-three-arm.json`、`benchmark/p1-current-candidate-confirmation.json`：沿用的原始性能样本。

原交付报告 `docs/bm25-statistics-write-optimization-20261112.md`、旧清单和补丁保持原样，没有覆盖其身份记录。

## 决策与下一步

本轮没有发现可定位、可复现且必须追加补丁的 P1 缺陷，保留现有候选与负面性能样本。P1 功能证据与性能证据分别审查；本报告不替负责人批准部署、延期或晋级。

原任务明确不自动进入 P2。Hybrid 向量/BM25 两路并行也未在本轮实现，不能宣称 ECS 已有该能力。若后续选择实施该项，应另行固定当前检索行为、来源约束、扩展依赖和故障降级口径，再做单因素本地对照。

`progress.md`、`docs/adr/` 及 backend 级 `AGENTS.md` 在当前目标仓库未找到；没有用另一个项目的文件代替。恢复工具的真实生产数据库演练仍 NOT RUN，不以合成验证替代。

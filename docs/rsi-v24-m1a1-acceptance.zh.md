# RSI v2.4 M1-A.1 验收报告

基线是 PR #4 的 `b87f01e90e37d26b0e4ce649a1560b9cab70a2fc`。交付在堆叠分支 `rsi/evolution-v24-m1a1-metric-fixes`。PR #3 与 PR #4 保持草稿，GLM40 的 PR #2 在本阶段范围之外。

| 变更 | 实现文件 | 验收证据 |
| --- | --- | --- |
| 选择生命周期、S3/S6 | `lineage.py`、`analytics/projections.py`、`analytics/metrics.py` | 正常、空池、全排除、排序中断、选中后中断、重试、重复及冲突 |
| 经验回执、M1–M3 | `handbook/store.py`、`query_workflow.py`、分析层 | E1/E2 与 E1/E3 两次检索、实际注入/引用、空检索、重复、跨轮和中断 |
| 同一运行观察库 | `query_workflow.py`、`query_learning.py`、`state.py`、`openai_roles.py` | 真实协调器入口、合成传输。六个角色回执、三个候选的十二次套件回放及 Stage/Execution 同库。检查点恢复验证失败闭合与已匹配结果复用 |
| 写入失败闭合 | `observability.py`、`openai_roles.py`、`state.py`、`workflow.py` | START、RESPONSE、FINISHED 落盘故障注入。记录请求次数、终态、账本用量及 UNKNOWN。缓存重试核对原回执 |
| 完整 Worker 身份 | `workflow.py`、`experiment.py`、`analytics/portfolio.py` 等 | 三份 Worker 字节哈希不同，两种规范化 AST，同一 Patch 独立保留三个实例，P3=2/3、P4=1/3。缺完整源时 AST 为 UNKNOWN |

[指标规范与前后对照](rsi-v24-m1a1-metric-spec.zh.md)记录公式和样例。合成控制确认一次空选择和一次选中使 S3=1/2、S6=1/4。两次检索只注入第一份时 M1=2、M2=2/4、M3=1/2。未完成选择的投影保留 UNKNOWN，完成分母只使用有效操作。

历史回归只读打开冻结输入，生成 757 条投影。九份输入、六个保护文件和费用账本 SHA256 一致。三个 M0 报告逐字节一致。见[历史校验回执](evidence/rsi-v24-m1a1-history.json)。旧事件保留原口径，新观测使用 `rsi-metrics/m1a1` 分区。未为历史补写观测或费用。

本地原始日志与命令退出码保存在 `E:/rsi-v24-m1a1/var/m1a1/`。四环境 CI 使用现有 Ubuntu/Windows × Python 3.11/3.13 工作流，最终提交的逐项结果以草稿 PR 的 CI 回执为准。本地第一次全量命令因指定的 pytest 临时目录缺父目录产生 setup 错误，原件保留。创建父目录后重跑。

本阶段复用既有虚拟环境和冻结文件。合成 SQLite、回放工作文件和 pytest 临时目录只用于本任务。历史文件及原始失败日志保留，可删除产物集中在 `var/m1a1/for-user-delete/`。付费调用为 0。真实自主搜索、Profile、Promotion、ECS、生产与部署没有发生。工作停止在 M1-A.1 等待人工审查。

[后续准备度与遗留问题](rsi-v24-m1a1-m1b-readiness.zh.md)

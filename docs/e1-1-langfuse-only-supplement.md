# RAG-knowledge E1.1 补充指令：统一使用 Langfuse，暂停 Prometheus + Grafana

## 一、指令优先级

本指令是以下两份文档的补充与局部覆盖：

1. 《RAG Langfuse Trace 与评测指标规范 V1》
2. 《Codex E1.1 Trace 与评测实施指令》

如原指令涉及继续开发、部署、验证 Prometheus + Grafana，统一以本补充指令为准。

其他关于 Trace 字段、证据追踪、评测指标、实验版本、Gold、人工审核、调用预算和安全边界的要求保持不变。

## 二、总体架构调整

当前项目的核心目标是：

- RAG 机制评测。
- 问答正确性与证据质量分析。
- 检索、重排、上下文及生成阶段的问题定位。
- A/B 消融实验。
- 为未来 RSI（递归自我改进）建立可复现的反馈闭环。

因此，现阶段统一采用：

**Langfuse Cloud：唯一的主要 RAG 评测、Trace、Score、Experiment 和可视化平台。**

**现有 Python 评测程序：用于计算需要内部检索结果、Gold、数据库或原始文件才能计算的指标。**

不再要求 Prometheus + Grafana 参与上述评测工作。

不得为了取代 Grafana 而自行开发另一套独立 Dashboard 平台。

## 三、暂停 Prometheus + Grafana

首先只读检查 ECS 上已经部署的监控服务。

已知项目存在隔离监控配置，可能包括：

- `rag-p1-monitor-prometheus`
- `rag-p1-monitor-grafana`
- `rag-p1-monitor-node`
- 相应的 systemd 单元、存储目录、配置和 Grafana Dashboard。

必须先确认服务身份、所属目录、实际用途以及是否被其他业务依赖。

对于明确属于本项目、没有生产依赖的隔离监控服务：

1. 保存当前配置、运行状态和必要的监控结果。
2. 安全停止并禁用这些隔离服务的自动启动。
3. 记录停止前后的内存与 CPU 使用情况。
4. 核实正式 RAG API、Langfuse 和其他 ECS 服务没有受到影响。
5. 不删除历史监控数据。
6. 不删除 Grafana Dashboard JSON、Prometheus 配置或部署说明。
7. 不卸载全局依赖，也不影响其他项目使用的监控实例。
8. 如果无法确认某个服务是否专属于本项目，保留运行状态并标记 BLOCKED，不盲目停止。

已有 `backend/app/services/metrics.py` 等可选监控代码暂时保留，但正常业务默认不启用 Prometheus 指标导出。

不允许为了移除监控而大规模修改业务代码。

## 四、Langfuse 评测与可视化

使用现有 Langfuse Cloud 项目，不重新部署 Langfuse。

复用现有 Trace、Dataset、Scores、Experiments 和 E1 评测材料。

### 4.1 Trace

严格按照第一份规范补齐：

- 用户实际问题。
- 实际使用的检索查询。
- 检索模式和有效参数。
- 向量与 BM25 候选、排名及分数。
- 融合、去重、重排和 Top-K 的候选变化。
- 删除、过滤、合并、排序的实际原因。
- 父子块与邻块扩展前后的证据映射。
- 实际进入 LLM 的完整消息。
- 模型原始输出。
- 最终用户可见回答。
- 引用及原始来源。
- 执行状态、Token、延迟和可确认费用。

日常运行保留必要元数据；获准上传的冻结评测资料允许在 Langfuse 记录可读正文。

未授权内容必须保持脱敏，并提供受控、版本一致的本地回读方式。

不得直接把所有文档内容和凭据上传 Cloud。

### 4.2 RAG 质量指标

需要内部数据或 Gold 的指标，由项目现有 Python 评测脚本计算并通过 Langfuse SDK 上传：

- Core Correctness。
- Answer Completeness。
- Faithfulness。
- Citation Support。
- Citation Coverage。
- Hit@K。
- Recall@K。
- MRR。
- nDCG@K。
- Context Evidence Recall。
- Evidence Preservation / Recovery。
- Agent Evidence Rescue / Final Rescue Success。

不要强行全部实现为 Langfuse Code Evaluator。

某指标缺少可靠 Gold 或实际阶段数据时，标记 UNKNOWN 或 NOT RUN，不上传虚构的 0 分。

严格区分 PDF 页级、Chunk 级、Word 表格字段级和 Excel 单元格级指标。

复用已有 `evaluation.py`、`publish_p0_scores.py` 和相关测试，不新建平行评分系统。

### 4.3 Langfuse 原生计算

对于平台已有能力，优先使用平台：

- Trace/Observation 耗时。
- 已采集请求和模型调用统计。
- 模型 Token 用量。
- 有可信价格时的模型费用。
- 简单的 Code Evaluator。
- Scores 的分布和趋势。
- Dataset Experiments 的版本对比。

若当前 SDK 或 Cloud 版本不支持某项功能，先核对官方文档和实际接口，再采取兼容方式，不自行猜测 API 参数。

当前 Judge 校准仍存在 INVALID、UNKNOWN 和有效错误样本不足的问题，不允许直接将未校准的 Judge 结果用于自动优化或上线决策。

## 五、Langfuse Dashboard 建设

只使用 Langfuse 自带的 Dashboard 与分析能力。

优先配置或验证以下三个视图。

### A. RAG 问答质量

展示：

- 核心正确性及可判定分母。
- 部分正确、错误、未知数量。
- Faithfulness。
- Citation Support。
- 不可回答题表现。
- 主要失败类型。
- 文档格式与问题类型分组。

### B. 检索与上下文效果

展示：

- Hit@K、Recall@K。
- 各阶段 Gold Evidence Coverage。
- 证据丢失与恢复数量。
- Rerank 前后证据覆盖变化。
- 上下文送达完整性。
- Agent 补救成功与失败。

指标不适用、未计算或缺少 Gold 时，不能填零。

如果某项指标无法直接通过当前 Langfuse Dashboard 绘制，先确认 Metrics API 或已有 Scores 能否支持；不能支持的项目保留在已关联的实验记录和本地报告中，不为了单个图表另建系统。

### C. 模型调用与实验成本

展示：

- 请求次数。
- 模型调用次数。
- Token 用量。
- 可确认费用。
- 生成耗时与分位数。
- 空正文、截断及错误。
- 不同模型和 Prompt 版本的对比。

这里的请求量、P95 和失败比例必须注明只覆盖已采集的 Trace，不能宣称代表整个 FastAPI 服务。

不再要求搭建 Prometheus/Grafana 的对应看板。

## 六、现有 E1 数据的复用

当前已有 E1 30 题结果和人工/AI 辅助审核记录。

优先复用：

- E1 冻结问题与 Gold。
- 原始 HTTP/SSE。
- 已保存的模型输入输出快照。
- 原有 Langfuse Trace。
- `ledger-v4.jsonl` 及对应审核材料。
- 已完成的错误分类。
- Judge 原始响应和校准记录。

不得为了完善 Dashboard 重新发送全部 30 道题，也不得重复历史 UNKNOWN 请求。

已有核心正确性统计必须保留实际评判者身份，不能把混合评判结果标成全部人工审核结果。

逐题上传 Score 时，必须核对 qid、run_id、Trace ID、answer_hash 和评价标准版本。

对历史缺失字段不允许伪造回填；无法恢复的继续标记 UNKNOWN。

## 七、验收标准

首先用零模型调用方式复用现有数据。

然后选择少量已批准的真实测试题，验证完整链路。

必须证明：

1. Langfuse 能看到根 Trace 的实际问题和最终答案，或者从安全快照可靠回读。
2. BM25、Vector、Fusion、Dedup、Rerank 等实际执行的阶段，其输入、输出和候选变化能被解释。
3. 未执行的阶段明确标记 SKIPPED 或不出现，不生成虚假分数。
4. 模型实际消息和原始输出可以复核。
5. 扩展后证据是否真正进入 LLM 可以独立确认。
6. E1 的 Gold 与审核记录可以关联至正确的实验运行。
7. 至少一组既有评测分数成功发布到 Langfuse 并完成 Cloud 回读。
8. Langfuse Dashboard 或现有原生分析视图可以展示实际已支持的评分、延迟和 Token。
9. Langfuse 不可用时，RAG 问答仍能正常运行；评测环境需明确报告观测导出失败。
10. Prometheus + Grafana 停用后，正式 RAG 和历史评测数据不受影响。

不得只以“Trace 节点存在”“SDK 无异常”或“测试退出码为零”作为验收通过的唯一依据。

应实际从 Cloud 回读关键 Input、Output、Scores，并与本地原始数据核对内容或哈希。

## 八、明确禁止

- 不重跑全部 250 题。
- 不重新部署 Langfuse。
- 不新建 Grafana Dashboard。
- 不开发新的独立评测平台。
- 不新增 Tempo、Loki 或其他 Trace 平台。
- 不更换现有模型。
- 不调整 Hybrid 权重、Rerank 阈值或 Agent 评分阈值。
- 不重新启用已经停用的严格答案内容校验规则。
- 不修改 Gold 以适配模型输出。
- 不删除历史评测数据。
- 不自动重试 UNKNOWN。
- 不在未经授权的情况下切换或修改正式服务。
- 不自动提交、推送或合并代码。

## 九、最终交付

完成后输出：

1. 原监控服务检查和安全停用结果。
2. 更新后的 Langfuse Trace 节点及字段覆盖表。
3. 指标名称、计算位置、评分方法和 Gold 依赖。
4. 已有 E1 评分与 Trace 关联情况。
5. Langfuse 可视化功能的实际验收结果。
6. Cloud 与本地 Score/Trace 回读对照。
7. 剩余 UNKNOWN、BLOCKED 和 NOT RUN。
8. 下一步最值得开展的一项 RAG 机制消融实验建议。

报告必须包含真实运行命令、退出码、来源版本、样本分母及证据路径。

不以模拟测试代替真实 Cloud 回读，不以 Dashboard 有图表代替评测结论正确。

主过程和最终报告使用中文；子代理内部工作使用英文。

优先完成最小闭环，不因为个别可视化功能不支持而扩大工程范围。
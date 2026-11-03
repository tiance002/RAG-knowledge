# 动态路由任务特征优化：实施与历史回放报告

日期：2026-11-02。仓库 `D:/RAG knowledge`，分支 `codex/pdf-parser-migration`，HEAD `8fca08ad669b3ba8b2fb6fe0da50cb9c2c54b48d`。本轮修改未提交、未推送、未部署。

## 1. 结论与边界

按负责人最新口径，以 GLM 作为高风险问题的优先模型，不建立“GLM 错、Qwen 对”的反向分流规则。Qwen 仍承担未触发高风险特征的问题。这里是路由偏好，不是宣称 GLM 在所有问题上实测更好。

已实现可选 `AFFINITY_V2` 策略。默认仍为 `OFF`，选择策略默认仍是 `WEIGHTED_V1`。本轮 **0 次新模型调用**；没有切换 ECS 服务，没有修改 Gold、检索、重排、上下文构建或 Prompt，没有新增答案校验规则。

历史回答回放结果：

| 指标 | 旧规则 | 新候选 |
|---|---:|---:|
| 历史 Qwen 未完整答对、GLM 正确的 11 题，被分给 GLM | 1/11 | 11/11 |
| 最近 Qwen 再次未答对、历史 GLM 正确的 4 题，被分给 GLM | 0/4 | 4/4 |
| 178 个冻结输入中的 GLM 选择数 | 7 | 52 |
| Cheap 选择数 | 153 | 122 |
| BASELINE 选择数（本批实际配置为 Qwen） | 18 | 4 |

**这是用已保存答案和历史标签做的反事实回放，不是新运行的正确率，不是盲测，不保证新题全部补救成功。** 52 个候选 GLM 输入中，31 个没有可复用的 GLM 评分；这部分效果为 UNKNOWN。没有把它们以 Cheap 的评分替代。

## 2. 证据与样本身份

复用 `var/dynamic-routing-20261101/` 中的：

- `36题逐题复核账本.json`：历史两模型配对结果。
- `动态164题统一复核账本.json`：最近 164 个完整回答的复核。
- `dynamic166-review-input-private.json`：178 个冻结问题的实际路由输入特征、选择、输入 Hash 和结果身份。
- `full180-v2-review-readonly.sqlite3`：以 SQLite `mode=ro` 读取历史原始调用和回答。
- `gold150-contexts-review-private.json`：已经保存的原始输入与实际送达资料。
- `split.json`：已有分组；不重新划一个有利的分母。

PASS：36 对实际模型标识、消息 Hash、人工/AI 账本中的答案与 SQLite 原文逐字符比对；178 题旧规则选择重算均与已保存选择一致；178 题引用块顺序、来源标签、内容 Hash 与原始送达上下文匹配。运行前后源文件 SHA256 一致。

身份限制：模型名称来自供应商返回，不是对后端权重的独立鉴证。历史消息相同已确认；不是所有历史 Provider 参数都有独立完整快照，不能声称所有设置完全独立核验。核心评分沿用已有审核账本；AI 复核没有升级为负责人确认。

178 题包括 164 个完整回答、11 个超时 UNKNOWN、2 个供应商失败、1 个历史 UNKNOWN 阻断槽位。**候选路由可以离线计算这些输入，但不表示允许重新请求。** 新候选 52 个 GLM 选择中有 1 个属于历史超时 UNKNOWN；没有重新调用，也不得自动加入重试队列。

## 3. 为什么旧规则漏掉 GLM 有优势的问题

旧版 `T/C/J/D/L/M` 归一化加权，阈值 0.5。在只有一项明显操作需求时，信号被其他低项稀释：

- 湖泊高度关系比较：0.225，仍选 Cheap。
- 抽样表样本量与抽样误差双字段：约 0.358，仍选 Cheap。
- 图表横纵轴对应：下界约 0.458；D 缺失导致区间跨阈值，回到本批 Qwen baseline。
- 指定项目累计成本：0.2。问题短，但上下文包含多个项目同名字段，实际有较强对象混淆风险。

不能简单将 D 填为高风险或低风险：冻结输入缺少可靠源 ID/块 ID，这一 UNKNOWN 真实存在。也不能把文本长度、表格数量或原始检索分数当成可校准的错误概率。

最近 164 个完整回答中，有 157 个仍由 Qwen 回答。11 次覆盖改善与 2 次退步都发生在 Qwen 重答路径，不能计为 GLM 路由收益。历史上 11 个 GLM 完整补救案例中的若干题，Qwen 后来重答也正确；因此保留“历史差异”和“持续遗漏”两个指标。

## 4. 新增的生成前特征

实现见 `backend/app/services/model_router.py` 的 `task_interactions`、`measure_target_field_collision`、`apply_selection_policy`、`decide_route`。

| 特征 | 判断依据 | 当前例型与限制 |
|---|---|---|
| RELATIONAL_COMPARISON | 比较、区别、高低关系等操作词 | 高度关系方向、版本区别；不保证所有比较都需要 GLM |
| ORDERED_FIELD_ALIGNMENT | 分别、对应、口径、respectively 等 | 财政口径、表格双字段、横纵轴；词法信号可能误触发 |
| CONDITIONED_TABLE_LOOKUP | 查询明确提到表格/行列且含数字单位条件 | 功率与频率定位表格值；不是所有表格都升级 |
| CONSTRAINED_ORDERED_LIST | 排序和输出约束同时出现 | 课程清单按编号排序；不新增输出格式校验 |
| ENTITY_RELATION_JOIN | 角色参与关系加创作者关系查询 | 从演员饰演作品连接到导演；不按人名或题号路由 |
| VISUAL_REFERENCE_WITH_STRUCTURE | 问题指定图/课件，提出身份或数量问题，M 有已捕获的结构信号 | 层数、训练数据集、坐标轴；M 也可能来自无关表格，不证明图像信息完整 |
| TARGET_FIELD_DISTRACTORS | 问题显式来源名/通用标识前缀可定位；目标和其他来源的表格出现同一查询字段短语 | 项目累计成本等同名字段混淆；缺字段、未定位来源或同一来源重复不作为该命中 |

最后一项使用**实际问题中的任意文件名或标识前缀**与传入引用匹配，不内置某个项目 Hash、qid、金额或 Gold。正常 Chat 的 `fileName`、检索的 `file_name`、离线输入的 `source_file` 均支持。只记录是否命中、来源标签数量和方法，不把字段值、来源名或正文复制到路由元数据。

初版六类规则覆盖 10/11；调查剩余累计成本题发现，正确值实际在目标来源的第 5 个引用块中，其他项目也有同名累计成本表头。补入通用字段干扰规则后覆盖 11/11。**这第二次调整是在看过错误后完成的，属于开发集拟合，不能隐去这段调参过程。** 初版回放保留在 `affinity-v2/replay.json`，最终版在 `affinity-v2-final/replay.json`。

候选风险下界 = `max(原加权下界, 命中特征时的 task_risk_floor)`；上界同理。多个相近特征不重复相加。初始 floor=0.55，原阈值=0.5，原六项权重不变。这是未经校准的风险偏好，不是 55% 失败概率或最佳参数。D/M 等 UNKNOWN 值仍保留；若没有新的已观察任务信号，继续原区间决策。

配置新增 `RAG_MODEL_ROUTER_SELECTION_POLICY`（默认 `WEIGHTED_V1`）和 `RAG_MODEL_ROUTER_TASK_RISK_FLOOR`（默认 0.55）。仅显式选择 `AFFINITY_V2` 时启用新策略。保留 OFF、CHEAP_ONLY、EXPENSIVE_ONLY 的原选模行为；不新增费用升级、故障 fallback 或 UNKNOWN 重试。

## 5. 质量回放与代价

36 题是过去初筛选出的偏难样本，**不能代表总体正确率**。两题审核 UNKNOWN 不进入已知质量分母；PARTIAL 仍留在分母且不当 PASS。另保留全部 36 槽位统计，不删除失败。

| 使用保存答案的方案 | PASS | PARTIAL | FAIL | UNKNOWN | PASS/已知34 |
|---|---:|---:|---:|---:|---:|
| 历史 Qwen | 4 | 3 | 27 | 2 | 11.76% |
| 历史 GLM | 14 | 5 | 15 | 2 | 41.18% |
| 旧规则选取相应历史答案 | 5 | 3 | 26 | 2 | 14.71% |
| 新候选选取相应历史答案 | 15 | 2 | 17 | 2 | 44.12% |

旧规则这一行不是最近动态运行的实际正确率：特意统一复用同一组历史回答，隔离“重答波动”对回放的影响。新候选 PASS 超过历史全部 GLM 不是反向偏好规则的成果，只是没有为某个普通问题触发升级，而其保存的 Qwen 答案恰好通过。没有依据该反例写定向规则。

36 题中新策略选择 GLM 18 题：11 PASS、1 PARTIAL、4 FAIL、2 UNKNOWN。它解决了已知漏路由，但仍把证据不足/不稳定题分给 GLM；不把它们伪报为补救成功。已知来源缺失与对象型号不符案例继续留在失败分析中，本轮没有替代检索/证据修复。

178 输入中新选 GLM 45 题，其中 31 题没有 GLM 评分。31 题的已保存 Qwen 核心标签为 25 PASS、1 PARTIAL、3 FAIL、2 UNKNOWN。因此 **提升漏题覆盖率要付出潜在的过度升级代价**；不能从选择比例宣布降本或整体提准。

同样选取历史请求，可见下面的资源记录；不是新策略实测性能，不预测未来排队与网络延迟：

| 36 槽位历史请求选择 | 旧规则 | 新候选 |
|---|---:|---:|
| 完整 / 截断 | 35 / 1 | 36 / 0 |
| 保存请求 P50 | 18.85 秒 | 22.77 秒 |
| 保存请求 P95（n=36，nearest-rank） | 142.30 秒 | 111.56 秒 |
| 保存请求总 Token | 505,407 | 497,941 |
| 金额 | UNKNOWN | UNKNOWN |

截断题保留原始 TRUNCATED 状态和审核 FAIL；选取已有 GLM 回答不等于自动触发截断重试。Token 较少不能推出费用更低；两种模型单价、缓存与推理开销未统一核算。

旧分组 DEVELOPMENT 的已知回放 PASS 为 3/27 → 10/27；历史 HOLDOUT 为 2/7 → 5/7。这些标签早已审阅，**HOLDOUT 名称不再代表未见验证集**，并且原分组不保证所有内容完全不重叠。这里只作分组诊断。真正独立泛化验证为 NOT RUN。

## 6. 测试与命令

在项目根目录执行：

```powershell
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe scripts/analyze_router_affinity.py --out var/dynamic-routing-20261101/affinity-v2-final
```

退出码 **0**。178 个旧选择复现，36 对原始记录核对，输入源 Hash 未变。证据：`var/dynamic-routing-20261101/affinity-v2-final/replay.json` 和 `replay.log`。JSON 保留每题的匹配规则、旧/新选择、已知模型评分、缺失对照、来源分组和身份核对项，按字段换行。

```powershell
$env:PYTHONPATH='backend;E:\RAG quention\.venv\Lib\site-packages'
$env:DATABASE_URL='sqlite://'
$env:RAG_OBSERVABILITY_ENABLED='false'
$env:PYTHONIOENCODING='utf-8'
.venv/Scripts/python.exe -m pytest backend/tests/test_model_router.py backend/tests/test_router_task_interactions.py backend/tests/test_router_affinity_replay.py backend/tests/test_dynamic_routing_generation.py backend/tests/test_dynamic_routing_agent.py backend/tests/test_dynamic_routing_chat.py backend/tests/test_dynamic_routing_observability.py -q --tb=short -o addopts=
```

退出码 **0**：**132 passed，54 subtests passed，6 warnings，10.03s**。日志 `affinity-v2-final/tests.log`。测试为确定性替身/内存导出，不是外部 Provider 或 Cloud 验收。覆盖同名字段干扰、实际 Chat 字段别名、未知竞争来源、通用名字替换、不传入 Gold、默认 OFF/V1、原调用链、故障/UNKNOWN/截断不重发及缺臂统计。

首次仅 `PYTHONPATH=backend` 执行 Agent/Chat 组合测试时退出 **1**，收集失败：`ModuleNotFoundError: No module named 'langgraph'`。随后复用先前报告已使用的本机依赖路径完成上述测试，没有安装/升级依赖。该测试环境借用现有 E 项目包路径，不是独立可部署环境证明。132 项通过不抹去第一次失败。

独立新特征测试 `python -m unittest discover -s backend/tests -p test_router_task_interactions.py -v`：退出 **0**，23 tests。`git diff --check`：退出 **0**。未实现的命令不得报告通过。

## 7. 修改文件与未验证事项

- `backend/app/services/model_router.py`：配置、可选组合特征、字段干扰测量、风险下限与可解释元数据。
- `config/router-v1.env.example`：默认关闭的新策略模板；未改真实 `.env`。
- `scripts/analyze_router_affinity.py`：无调用回放、原始身份检查、分母及缺臂统计、源与代码 Hash。
- `backend/tests/test_router_task_interactions.py`：23 个针对新特征/边界的合成测试。
- `backend/tests/test_router_affinity_replay.py`：4 个统计边界测试。
- 本报告与原任务规划记录。

PASS：规则实现、定向回归、已知历史漏题覆盖、原始输入身份复核。

UNKNOWN：31 个所选 GLM 缺失臂的正确率、整体成本收益、未见题泛化、路由对引用与扩展事实的净收益。

NOT RUN：候选真实模型试验、真实 HTTP/SSE、Langfuse Cloud 回读、ECS 部署、正式启用。没有将离线回放伪装成 Cloud Trace。

## 8. 下一步最小实验

冻结当前源码 Hash 后，从新增 GLM 选择中，按七类规则挑少量**有可靠 Gold、真实证据充分且无历史 UNKNOWN 阻断**的代表题；保留已正确的 Qwen 题作为过度升级对照。只补必要 GLM 臂，继续复用原始消息与 Qwen 结果；不重跑全部 178 题。先列出逐题请求上限和预算，再进行真实运行。

验收同时看：遗漏补救覆盖、核心正确/部分正确、引用、补充事实、额外 Token、每题总延迟。证据不全的问题进入检索/上下文改进清单，不能仅通过放大路由范围声称解决。规则中的图像/字段词法启发式优先在未见题检验；若大量把本来 Qwen 正确的简单题升级，收窄对应特征，不能继续无限加关键词。

资源：复用既有本机虚拟环境、历史 SQLite、送达文本和审核账本；没有复制 Chroma/语料/数据库，没有新容器，没有连接或写入 ECS。仅增加源码、测试及小型 JSON/日志，两个回放版本保留作调参证据。ECS 磁盘前后测量 NOT RUN；没有可报告的 ECS 空间变化或清理收益。

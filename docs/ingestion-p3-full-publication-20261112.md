# P3：大文件父子块完整索引发布集成验收

日期：2026-11-12。目标项目：`D:\RAG knowledge`，仓库 `tiance002/RAG-knowledge`。

## 结论

本轮补齐了可运行的完整发布对照入口，完成本地真实 SQLite + 真实内存 Chroma 的 P1/P2 比较。**12/12 发布成功且完整逻辑状态一致；相关回归50项全部通过，两个主命令退出码均为0。** 没有发现需修补的产品缺陷，因此没有修改产品服务逻辑。

性能结果：冻结 P1 的完整发布中位数 **2908.146 ms**，已验 P2 为 **2954.175 ms**，约高1.58%，范围重叠。**没有稳定整体提速证据，也不能据此确认稳定退化。** P2 的有界对象/回读设计保留，但本轮不批准上线，不把上一轮合成 Python 分配降幅当成本轮真实 Chroma/进程内存降幅。

## 本轮实际实现

- 复用 `scripts/benchmark_bm25_write_payload.py` 单一 CLI，新增 `--publication` 模式与显式父代/候选源码目录参数。原 BM25 和删除策略模式保留。
- 新增支持模块 `scripts/benchmark_publication_helpers.py`，不是新业务执行器、队列服务、数据库或 Agent。它加载完整源码模块，执行实际 `publish_index_build`。
- 新增 `tests/test_publication_benchmark.py` 的7个可携带契约：成功双臂、后续批次真实读取损坏两臂、UNKNOWN保持原异常且不重试两臂、快照去重、源码目录选择。
- 新增本报告；其余变更为任务内回执和归档。

本轮没有改 BM25、Chroma、切块、提示词、模型、评分器、任务队列、Schema 或持久性默认值。没有 ECS/SSH/上传下载、外部模型、生产服务操作、提交或推送。

## 源码版本与并行工作隔离

分支仍为 `codex/bm25-stats-write-optimization-20261112`，HEAD为 `7c7415f85a063e3f7645f676bd22f7c8f766a732`。P1/P2 尚未提交，故对照身份由准确源码字节及上一轮清单确认，不能仅以 Git7c 表示父代。

开始时三个 P2 核心文件均匹配已验清单。过程中其他工作接入图片描述依赖，改变了工作区的 `index_builds.py`；config/VLM/staged_pipeline 等也有非本轮改动。没有回滚或覆盖它们，也没有把它们混进 P1/P2 正式对照。

从已保留的旧测试导入副本中恢复了3份已验P2源码，各自与 P2 `delivery-manifest.json` 的候选哈希严格一致，保存到 `p3/approved-p2-services/`；仅约61 KB源码，没有复制数据库/索引。来源与哈希见 `approved-p2-recovery.json`。

| 服务 | 冻结 P1 | 已验 P2 |
|---|---|---|
| bm25 | `8d73f3dcfa2d147f13ce85944efe3fb354f8adfbc4603b9d64207ba7fb86584e` | `de6ead4acf3f059119751dd89ab8507eded5777d670f0a56184a5d5cf3ae965a` |
| index_builds | `20acfce930400fb94fb337ec9aaead039166562f85fec5ba385f898df6ad9b5a` | `aadde1c156d117e5c2cf22d45552c6d3ee816dc74e61e374ef4a632dbe2e3df3` |
| vector_store | `31ab9c8fcd6fcb5c573d14d8f3586fe547529a1ac5efea020bf03ba99071a378` | `73e101e57e0008a5ab3aa67cb44478b41990f1a393a82977e39a6602da179180` |

正式运行使用明确的 `--candidate-services` 冻结目录；两臂共享运行时依赖，已加载的相关依赖前后哈希一致。**本结论不代表并行新增的图片描述功能已获验收。** 核心工具测试使用每个子进程实际加载的当前公开源码，验证入口契约；历史 P1/P2 等价由正式冻结运行单独证明，二者不混为同版本质量基线。

## 输入、路径与测量边界

输入复用先前的确定性合成父子块设计：120父块、1200子块、子块正文 **5,424,400 UTF-8字节**、50,000项背景词汇、256维确定性向量。source fixture 使用稳定UUID，产品真实 `remap_and_validate_graph` 在每次发布创建新的持久化UUID。

这是**预先构造的合法父子块 → 完整索引发布**，不包含实际PDF解析、OCR、产品切块器或真实Embedding；不能将此输入块长当作4096/384生产配置的实测。未读取真实用户资料。

每个样本：

1. 独立临时 SQLite WAL、唯一 EphemeralClient Collection；真实发布旧目标文件及背景文件为ACTIVE，均在计时外。
2. SQLite中创建排队回执，实际 `dispatch.claim_task` 领取目标第二代任务；不是用 `return True` 替身放行。
3. 计时从替换发布调用前到实际 `publish_index_build` 返回，包含函数局部对象自然释放。
4. 实际 STAGING提交 → 维度检查/写入 → 全量回读比较 → 真实所有权与generation检查 → SQL/BM25原子替换/ACTIVE/回执完成。
5. 计时外读取完整 SQL/向量/任务状态并验证；随后删除本样本Collection、释放engine并移除本样本临时数据库。

外发Python socket和默认应用数据库连接有明确拒绝护栏，样本只使用显式的本地临时engine及内存Chroma。护栏安装/恢复不计入发布时间。没有调用真实模型或创建真实持久化Chroma客户端。

每臂预热1次、正式5次，交替顺序，串行运行；总12次。未运行并行压力测试，没有删慢样本、换阈值或重复跑到结果变好。

## 等价性证据

每次依据真实内容/来源位置把随机持久UUID映射到稳定身份，比较完整状态，不只比较数量。12个样本得到同一个完整逻辑哈希：

`61c8d4d366487cf8c1e2a34e52244882c819ab21752b5fc1f9d728119be5e75f`

该快照包含：

- 1321个当前SQL块：120父块、1200子块及1个背景块，含正文哈希、页码、父子关系、位置与来源字段。
- 1201条ChunkIndex、60,800条Posting、每块词频/长度、Posting派生DF、N/平均长度及历史兼容JSON。
- 2401个实际存储向量：1200个退役旧目标、1200个当前目标和1个背景，含完整正文哈希、metadata和全部256维坐标；父块不进入BM25或向量。
- BM25实际返回的全部ID/分数/排名与真实Top-20；`fresh`命中1200个当前子块，`prior`不再命中旧目标。
- 构建、文件、知识库、任务回执及attempt状态、完成结果身份与指针。

中间审查发现早期工具草稿对检索结果重排序，会掩盖同分排名变化；正式运行前已取消。BM25真实顺序和Top-20保持严格比较。HNSW原始顺序仅作为诊断，完整成员/每成员距离与向量内容严格比较；**未声称近似向量检索排序或Hybrid问答质量获得验证**。

所有样本均证明：真实guard/complete各成功一次；持久提交观察到STAGING后ACTIVE；旧目标RETIRED、背景仍ACTIVE；回执和attempt均COMPLETED；任务、owner、构建和结果身份吻合。背景内容及全部退役旧向量在替换前后保持原样，仍满足延迟清理语义。

所有输入哈希相同：`7cf92ab8201dcac7e7a5675f1d6de2f49b2b3759558ac9c6debecbfa6ffe7c63`。源码、输入身份在首样本前已落盘，每个原始记录在接受判定前归档；未为历史日志补造执行阶段。

## 正式性能结果

以下均为各臂5个正式样本的 **中位数 / 最小 / 最大**，预热不计分母。

| 指标 | 冻结 P1 | 已验 P2 |
|---|---:|---:|
| 完整发布 ms | 2908.146 / 2663.412 / 3087.986 | 2954.175 / 2728.938 / 3311.857 |
| BM25准备 ms | 1642.388 / 1591.975 / 1712.107 | 1652.316 / 1604.915 / 1853.491 |
| 向量写入阶段 ms | 596.267 / 451.958 / 855.906 | 467.402 / 452.552 / 882.954 |
| 完整回读校验阶段 ms | 94.752 / 89.030 / 96.744 | 98.043 / 94.842 / 104.518 |
| 原子发布阶段 ms | 248.374 / 238.237 / 255.532 | 260.638 / 239.585 / 279.097 |
| 最终commit ms | 42.093 / 39.904 / 46.270 | 44.841 / 38.489 / 49.435 |
| WAL增量 B | 15,351,152 / 15,326,432 / 15,355,272 | 15,342,912 / 15,334,672 / 15,384,112 |

这是索引发布耗时，不是用户问答延迟。阶段存在嵌套，不能把所有阶段中位数相加当作整体时间；完整逐次数据及64行指标统计见 `performance-summary.csv`、`performance-analysis.md`。未采集单独的阶段SQL次数，不编造。

完整发布SQL调用在每臂所有样本均相同：**30→39**；其中SELECT **12→11**，INSERT **6→16**；单次Posting参数最大行数 **10800→1000**。一次未使用查询被移除，分批增加了INSERT调用。WAL相近，随机UUID及布局引起少量差异，不等于物理写字节或云盘IOPS。

最耗时的已记录阶段是BM25准备，在样本内占总时间的中位数约 **57.96% / 58.47%**。实际日志确认本地未安装jieba、使用降级分词；没有安装/升级依赖。这个结果不能证明ECS的真实分词器或瓶颈相同。

向量写入波动较大，P2也有较慢的STAGING样本；原因没有实测到，不归因于磁盘、杀毒、CPU或网络。没有P95/统计显著性/整体RSS/服务端内存结论。上一轮P2合成追踪分配下降83.27%仍是独立证据，未混入本轮计时。

## 本次测试与命令

统一相关回归真实结果：**50 passed、0 FAIL、0 ERROR、0 SKIP，退出0，37.74秒**。8834个警告为现有弃用提示。不是全仓套件。

| 测试文件 | 收集/PASS | FAIL/ERROR/SKIP |
|---|---:|---:|
| tests/test_publication_benchmark.py | 7 | 0/0/0 |
| tests/test_bm25_write_benchmark.py | 3 | 0/0/0 |
| backend/tests/test_index_builds.py | 17 | 0/0/0 |
| backend/tests/test_stale_publication_real.py | 2 | 0/0/0 |
| backend/tests/test_file_task_ledger.py | 13 | 0/0/0 |
| backend/tests/test_p2_index_publication.py | 8 | 0/0/0 |

命令工作目录 `D:\RAG knowledge`；实际输出都在 `.planning/2026-11-12-bm25-stats-p0p1/p3/`。

```powershell
$env:DATABASE_URL='sqlite://'
$env:REDIS_URL='memory://'
$env:CHROMA_HOST=''
$env:RAG_OBSERVABILITY_ENABLED='false'
$env:LANGFUSE_ENABLED='false'
$env:ANONYMIZED_TELEMETRY='False'
$env:PYTHONDONTWRITEBYTECODE='1'
& '.\.venv\Scripts\python.exe' -X utf8 -m pytest tests/test_publication_benchmark.py tests/test_bm25_write_benchmark.py backend/tests/test_index_builds.py backend/tests/test_stale_publication_real.py backend/tests/test_file_task_ledger.py backend/tests/test_p2_index_publication.py -q -ra -o addopts='' --basetemp=.planning/2026-11-12-bm25-stats-p0p1/p3/pytest-final --junitxml=.planning/2026-11-12-bm25-stats-p0p1/p3/final-directed.xml
```

上面为实际已执行命令，退出0；再运行时请用新输出目录，避免覆盖原回执。早期5项契约、归档单测及源码选择单测另有回执，**不与这50项相加**。早期固定历史哈希的测试已改为可携带的接口检查，没有因源码变化或字符串形式误判产品失败。

正式性能命令实际退出0：

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 scripts/benchmark_bm25_write_payload.py --publication --reference-services .planning/2026-11-12-bm25-stats-p0p1/p2/baseline/backend/app/services --candidate-services .planning/2026-11-12-bm25-stats-p0p1/p3/approved-p2-services --warmups 1 --repetitions 5 --output .planning/2026-11-12-bm25-stats-p0p1/p3/full-publication.json
```

需要复制复跑时，仅把最后输出改为未存在的路径，例如 `p3/review-publication.json`。入口拒绝覆盖已有报告、journal、identity或快照目录；并非失败后偷偷重发任务。

环境：Windows11、Python3.13.0、SQLite3.45.3、Chroma1.3.7、SQLAlchemy2.0.55、SQLModel0.0.27，见 `runtime.json`。独立合成SQLite使用WAL/autocheckpoint0记录本地测量策略，不修改产品持久性。

## 归档、资源与审查

复用现有工作区、虚拟环境、前一轮父代源码和单一CLI。P3只新增必要的实验支持代码，没有重建整库或依赖环境。

12条记录共享一个 **19,082,432 B** 的完整规范状态快照，文件名与内容SHA一致；每次记录保留快照引用、源码/输入身份、计时、SQL/WAL、任务及清理证据。完整状态没有在每次JSON和JSONL内重复保存。不同/不合格状态也会先写独立快照和原始记录，再执行判定。

正式样本Collection/数据库/engine全部按样本在finally中清理，源码导入临时目录自动退出清理。测试进程结束后，经路径、文件头/哈希核对，删除本轮84个可确认的一次性数据库或源码导入副本，共 **21,555,954 B**。清理前后D:可用空间为137,671,794,688→137,693,466,624 B；不把该变化当作精确删除量。并行修改导致6个导入副本出现其他源码哈希，约127 KB，保守保留并列入清单，没有追着当前源码覆盖或删除。

保留：所有原始JSON/JSONL/日志/XML、唯一规范快照、P1/P2及P3版本身份、已验P2恢复源码、失败/UNKNOWN合成测试回执及少量其他夹具。没有删除真实资料、其他任务文件、共享数据库、索引或容器。早期清理因源码身份变化停止的记录已保留，不属于产品失败。

6.1sol medium负责入口/支持模块和独立统计复算，6.1sol xhigh负责真实存储/任务契约与隔离审查，6luna high先审方法再复核全部原始回执；主代理整合并核对退出码、分母、排名、快照、版本及清理。

本轮4文件增量补丁与清单：`p3/p3-only.patch`、`p3/delivery-manifest.json`。它们不含密钥、私有快照、原始评测题集或并行图片描述改动。基准输入和结果均是合成数据。

## 下一步与停止

- 完整真实本地存储发布、任务/状态及P1/P2逻辑等价：PASS。
- 工具隔离与可恢复归档：PASS。
- 稳定整体提速：未证明；不晋级为性能成功。
- 当前并行图片功能、真实PDF/切块/Embedding、Hybrid问答质量、ECS资源/吞吐：NOT RUN。
- 不提交、推送、部署或自动切换候选，等待负责人审查。

下一项最值得单独验证的机制是 **BM25分词/词频准备热路径**：先在目标分词器与相同输入下分解CPU/日志开销，只有逐词输出、Posting、分数、顺序完全等价才比较候选。不要继续减少回读校验，也不以增加worker并发掩盖瓶颈。本轮到此结束，不顺手实现该新候选。

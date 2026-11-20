# P2：大文件父子切块索引写入与完整回读优化

日期：2026-11-12。项目：`D:\RAG knowledge`，仓库 `tiance002/RAG-knowledge`。

## 结论

P2 本地工程验证 PASS：统一 16 个测试模块共 **194 PASS，0 FAIL，0 ERROR，0 SKIP**，实际退出 0。实现了 Posting 有界构造、Chroma 有界完整回读/写入载荷、ACTIVE 退役路径省去无用途 SQL ID 查询。没有修改检索算法、模型、切块参数、持久性配置或任务发布语义。

内存证据支持减少额外临时对象；不能声称 ECS 整体提速。BM25 大文件完整替换中位数约慢 0.89%；Chroma 替身实验中，对称完整函数计时约低 10.10%，只是带计数器的合成实验。真实 ECS、云盘 IOPS、实际 Chroma 服务端资源及完整解析吞吐均 NOT RUN。

## 版本、边界与隔离

- 分支：`codex/bm25-stats-write-optimization-20261112`；HEAD 仍为 `7c7415f85a063e3f7645f676bd22f7c8f766a732`。
- 本轮实际父代是工作区已审查、尚未提交的 P1 源码，不能把 Git7c 单独当作 P2 对照。仅保存三份产品源码及既有基准脚本/测试，共五个小文件在 `p2/baseline/`；不是复制语料、数据库或索引。
- 基线三产品 SHA256：BM25 `8d73f3dcfa2d147f13ce85944efe3fb354f8adfbc4603b9d64207ba7fb86584e`；索引发布 `20acfce930400fb94fb337ec9aaead039166562f85fec5ba385f898df6ad9b5a`；向量服务 `31ab9c8fcd6fcb5c573d14d8f3586fe547529a1ac5efea020bf03ba99071a378`。
- 原 P1 报告、补丁、清单、负面样本和回执保留。P2 有独立增量补丁和身份清单。
- 所有 ECS 操作、真实外部模型、依赖升级、Schema/Gold 修改、Git 提交/推送、部署均未执行。正式服务不变。
- root/backend 的缺失文件已记录：目标仓库没有 `progress.md`、`docs/adr/`、backend 级 `AGENTS.md`；任务内新建进度文件，不伪造历史内容。

## 五项机会的处理

| 项目 | 处理 | 具体依据与边界 |
|---|---|---|
| 退役旧构建的 SQL ID 查询 | 已实施 | `index_builds.retire_file_builds` 原 ACTIVE 分支从未使用查询结果；仅将查询移入原无 active_build_id 分支。既有清理 manifest、退役状态、调用方事务均不变 |
| kb.chunk_count 增量更新 | 保留精确 recount，未实施 delta | 当前口径是未删除文件的全部 SQL 块，包含父块，不按文件状态过滤；其他入口存在向量计数及不同 recount 口径，旧值也可能漂移。直接 delta 会把偏差持久化，不能当作可靠优化 |
| Posting 构造和插入 | 已实施 | `BM25Index.finish_file_replacement` 每批最多 1000 个临时 Posting 字典，执行后清空；同一调用方事务、不加 commit、不跨文件，不改 N/平均长度或历史 JSON 行为 |
| Chroma 维度检查复用 | 保留每文件现有检查，未加跨文件缓存 | 当前 collection_operation 已是每文件一次 count/query；add 没有重复调用。没有可靠的跨文件 Collection 生命周期身份，不以 kb/model 名称缓存成功结论 |
| Chroma 写入载荷与完整回读 | 已实施 | `_add_documents_to_collection` 分批构造平行列表；新增 `verify_documents` 分批读取并逐项比较 ID、正文、metadata、维度及所有向量坐标，释放该批返回值后才读下一批 |

两项保留现状是明确的设计取舍，未宣称它们已优化。`chunk_count` 跨入口口径统一如另行实施，应独立评审；本轮没有用额外 Schema 或启动全库修复替代局部优化。

## 代码与调用链

产品改动三个文件：

1. `backend/app/services/bm25.py`：新增内部 `_POSTING_INSERT_BATCH_SIZE=1000`，只替换 `finish_file_replacement` 的 Posting 列表生成/发送方式。输入 prepared 的词频字典和 ChunkIndex 插入列表仍然物化，不能声称整个索引阶段恒定内存。
2. `backend/app/services/index_builds.py`：移动退役 ID 查询；在原校验阶段消费 callable `verify_documents`；旧服务/替身不提供该方法时保留原完整 `get_documents` 比较。生产 VectorStoreService 使用新有界路径。精确 recount 留存并注释口径。
3. `backend/app/services/vector_store.py`：新公开及 operation-scope `verify_documents`、有界 `_verify_documents_in_collection` 和逐批 `_verify_document_batch`；公开 `get_documents` 聚合 API 保持兼容。add 在发送前遍历校验必需输入字段，以保留原 API 遇到坏文档不先写部分数据的行为。

流程保持：

```text
STAGING 独立持久化
  → 同一 Collection 句柄，维度检查
  → 所有子块向量写入
  → 所有批次完整回读比较通过
  → guard_claim / generation 检查
  → 旧 SQL/BM25 替换及统计，单个文件同一事务
  → ACTIVE / 任务完成 / 原提交边界
  → 原延迟清理机制
```

没有边写边发布，没有把 SQL 与 Chroma 伪装为同一事务。后续批次失败、返回缺项、正文/来源元数据/向量损坏都会抛错，不能提前 ACTIVE。Provider UNKNOWN、租约与旧 Worker fencing 没有改写或自动重试。

批内数值使用原容差 `math.isclose(rel_tol=1e-6, abs_tol=1e-7)`；允许 Chroma 调整返回顺序，按 ID 对照。完整性检查只针对索引回读事实，不是对模型答案添加句式或数值校验。

## 本次统一测试

本次真实执行，不是沿用 P1 的 116 项总数。`final-directed.xml` 与日志已经逐项复核：**194 passed，35036 个既有弃用警告，53.82 秒，退出 0**。无空输出、截断、ERROR、SKIP 或 deselected。下面来自同一份 XML，不相加独立批次结果。

| 模块 | 收集/PASS | FAIL/ERROR/SKIP |
|---|---:|---:|
| test_bm25_file_replacement | 3 | 0/0/0 |
| test_bm25_inverted_index | 5 | 0/0/0 |
| test_bm25_version_compatibility | 3 | 0/0/0 |
| test_index_builds | 17 | 0/0/0 |
| test_parent_child_ingest | 15 | 0/0/0 |
| test_staged_pipeline | 19 | 0/0/0 |
| test_file_task_ledger | 13 | 0/0/0 |
| test_index_safety_regressions | 5 | 0/0/0 |
| test_stale_publication_real | 2 | 0/0/0 |
| test_bm25_stats_snapshot_compat | 13 | 0/0/0 |
| test_restore_bm25_legacy_df | 21 | 0/0/0 |
| test_bm25_bounded_postings（新增） | 3 | 0/0/0 |
| test_p2_index_publication（新增） | 8 | 0/0/0 |
| test_p2_vector_verification（新增） | 48 | 0/0/0 |
| test_vector_collection_operation | 16 | 0/0/0 |
| test_bm25_write_benchmark | 3 | 0/0/0 |

新增测试覆盖真实 SQLite 第二批 Posting 失败后全事务回滚、三次大文件替换/重复投递、父块不入 BM25、其他文件/KB 不变、ID/分数/排名一致；ACTIVE 不查多余 SQL IDs、无 active 的 legacy 精确 ID、空投影、回滚；第二批回读缺失/错 ID/重复/正文/元数据/维度/坐标损坏、NaN、存储异常、跨线程/KB、Collection 删除重建，以及 weakref 证明上一批返回向量不滞留。

真实安装的 **本地内存 Chroma** 用例已运行，包括原 1100 向量跨批及新完整校验/重建；没有外部 Chroma 服务或自动 Embedding 模型调用。中间 SQL 夹具无效 UUID、Counter 测试断言错误的失败记录保留；只修测试夹具/断言，不改 Gold、生产行为或验收标准。

复跑命令（工作目录 `D:\RAG knowledge`，`pytest-final-REVIEW` 必须使用新目录）：

```powershell
$env:DATABASE_URL='sqlite://'
$env:REDIS_URL='memory://'
$env:RAG_OBSERVABILITY_ENABLED='false'
$env:LANGFUSE_ENABLED='false'
$env:ANONYMIZED_TELEMETRY='False'
$env:PYTHONDONTWRITEBYTECODE='1'
& '.\.venv\Scripts\python.exe' -X utf8 -m pytest backend/tests/test_bm25_file_replacement.py backend/tests/test_bm25_inverted_index.py backend/tests/test_bm25_version_compatibility.py backend/tests/test_index_builds.py backend/tests/test_parent_child_ingest.py backend/tests/test_staged_pipeline.py backend/tests/test_file_task_ledger.py backend/tests/test_index_safety_regressions.py backend/tests/test_stale_publication_real.py backend/tests/test_bm25_stats_snapshot_compat.py backend/tests/test_restore_bm25_legacy_df.py backend/tests/test_bm25_bounded_postings.py backend/tests/test_p2_index_publication.py backend/tests/test_p2_vector_verification.py backend/tests/test_vector_collection_operation.py tests/test_bm25_write_benchmark.py -q -ra -o addopts='' --basetemp=.planning/2026-11-12-bm25-stats-p0p1/p2/pytest-final-REVIEW --junitxml=.planning/2026-11-12-bm25-stats-p0p1/p2/final-directed-REVIEW.xml
```

实际原命令使用 `pytest-final` 与 `final-directed.xml`，全量 stdout/stderr 在 `p2/final-directed.log`；`final-command.json` 记录实际退出码，`test-summary.json` 为逐模块解析结果。复跑命令不代表已再次执行。

## BM25 大文件 P1/P2 配对

复用并扩展现有 `scripts/benchmark_bm25_write_payload.py`，新增 `--reference` 指定冻结的 P1 工作区源码；共同 seed/tokenizer 仍取固定7c提交。未复制真实数据，使用独立临时 WAL SQLite，原持久性设置不变。每臂预热 1 次、正式 5 次，交替顺序，每次独立数据库；两臂完整 CI/Posting/词频/N/平均长度、父子关系、其他文件哈希及查询 ID/分数/排序完全一致。

输入：120 父块、1200 子块、5,424,400 字节新原文、50,000 项背景词汇。目标文件 10,800 条 Posting，总计含背景 60,800 条。不是以小文件代替大文件。

| 指标 | 冻结 P1（中位数/最小/最大） | P2（中位数/最小/最大） |
|---|---:|---:|
| prepare ms | 1537.869 / 1434.514 / 1820.695 | 1566.353 / 1515.635 / 1577.242 |
| begin ms | 33.310 / 30.724 / 45.759 | 31.776 / 29.904 / 33.804 |
| SQL 块替换 ms | 79.067 / 75.769 / 116.265 | 74.881 / 73.080 / 96.732 |
| finish ms | 61.694 / 54.040 / 87.245 | 54.885 / 52.740 / 78.738 |
| flush ms | 3.300 / 3.162 / 5.084 | 3.099 / 2.945 / 3.363 |
| commit ms | 42.755 / 39.196 / 60.487 | 38.194 / 35.106 / 40.082 |
| 完整替换 ms | 1762.198 / 1659.785 / 2123.859 | 1777.905 / 1739.753 / 1809.474 |
| WAL 增量 B（各样本相同） | 13,513,632 | 13,513,632 |
| 最大单次 Posting 参数行 | 10,800 | 1,000 |
| finish 阶段 SQL 调用 | 3 | 13 |

SQL 次数增多是分批的明确代价；不以减少瞬时对象伪装成减少 SQL。整体中位数约 +0.89%，没有稳定提速结论；分词准备占主导，分阶段中位数不能直接相加当作总时间。本地 WAL 不等于云盘写入量/IOPS；采样 RSS 不是隔离分配峰值，不用它宣称内存改善。

实际命令退出 0，12/12 样本语义 MATCH：

```powershell
& '.\.venv\Scripts\python.exe' -X utf8 scripts/benchmark_bm25_write_payload.py --reference .planning/2026-11-12-bm25-stats-p0p1/p2/baseline/backend/app/services/bm25.py --candidate backend/app/services/bm25.py --arms reference candidate --warmups 1 --repetitions 5 --output .planning/2026-11-12-bm25-stats-p0p1/p2/bm25-p1-p2.json
```

输出存在时脚本拒绝覆盖；重跑必须改新输出名。原始每次值、SQL、WAL、语义及源码哈希在 JSON 与逐次 JSONL，未删除慢样本。

## Chroma 回读生命周期与内存实验

实际完整导入冻结 P1 和当前 P2 模块，调用原 `_write_verified_vectors` 和真实 service 工厂；仅存储客户端为内存替身，返回独立解码风格的字符串、字典、列表/浮点对象。没有替换比较算法或提前构造“正确返回”。SQL connect 装有拒绝护栏。

样本含 120 父块、6000 子块，正文 **6,806,240 UTF-8 字节**，向量 256 维。父块仅作结构背景，不写向量。每个样本均核对 **6000 正文、6000 元数据、6000 维度、1,536,000 坐标**；两臂 lookup/count/dimension_query 各 1、add/get 各 6，所有 get 批量均不超过 1000。weakref finalizer 测对象生命周期，不仅看参数长度。

| 内存实验指标 | P1 | P2 |
|---|---:|---:|
| 同时存活的返回向量最大数 | 6000 | 1000 |
| 验证阶段 Python traced peak，正式样本均相同 | 70,425,282 B | 11,781,832 B |
| 实际函数返回后存活返回向量 | 0 | 0 |
| 完整字段/坐标核验 | 全部 | 全部 |

本合成范围的 Python 分配峰值约减少 **83.27%**；只含验证阶段新增 Python 对象，不包含输入向量、导入、写入、Chroma/native/server 内存，也不是整个进程 RSS。

### 计时缺口及一次纠正

原 traced 实验的 verify-phase 中位数为 1421.449→1591.008 ms。但旧版在退出 phase 时仍保留全部6000个返回向量，到函数返回才释放；新版在 phase 内逐批释放。因此该阶段时间不对称，**不得据此宣布慢11.93%或忽略它挑选有利结果**。原12条数据、日志及原 runner `measure-vector-readback.v1.py` 都保留，独立 xhigh 已复核控制流。

仅纠正计时器、没有调产品或换样例：新增 `--timing-only` 关闭 tracemalloc，从调用实际 `_write_verified_vectors` 前计时直到函数返回，包含两臂自然清理。单独新结果每臂预热1+正式5，顺序交替，全部12/12 PASS、退出0：

| 对称完整 helper 时间 | P1 | P2 |
|---|---:|---:|
| 中位数 | 394.8595 ms | 354.9889 ms |
| 最小–最大 | 386.4254–402.8260 ms | 343.3286–371.0945 ms |

观察到约低10.10%，仍包含替身物化和计数器开销，只能说明本合成条件，不能外推真实 Chroma 延迟。traced 与 timing-only 两实验不混算。

实际命令（各退出0；输出存在时须更名）：

```powershell
& '.\.venv\Scripts\python.exe' .planning/2026-11-12-bm25-stats-p0p1/p2/measure-vector-readback.py --output .planning/2026-11-12-bm25-stats-p0p1/p2/vector-readback-timing.json --children 6000 --timing-only
```

原 traced 数据由归档的 v1 runner 以相同参数、不含 `--timing-only`，写到 `vector-readback.json`。当前 runner 兼容 traced 模式但有新字段；不得将其当前哈希冒充原v1身份。两次源码前后哈希一致，原始 JSON/JSONL 不覆盖。

## 文件、恢复与资源

产品三文件已列明；新增三测试文件。既有基准 runner/测试加 reference 对照及最大 SQL 批参数统计，复用原工具，没有第二套业务执行器。新增本报告。`p2/p2-only.patch` 为冻结 P1→P2 的9文件完整增量，`p2/delivery-manifest.json` 保存父代/候选/回执哈希及实际反向检查退出码。

若只撤销 P2，应先检查当前文件仍匹配该清单、没有后续改动，再使用该增量补丁的反向操作；不能 reset 工作区，也不能拿 Git7c 覆盖工作区而丢掉 P1。当前没有实际撤销、暂存、提交或推送。离线恢复旧 BM25 JSON 的 P1 工具未修改。

资源复用：现有工作区、虚拟环境、已安装依赖、同一基准工具和只读源码。没有新容器、下载、整库/索引复制。临时基准数据库自动清理；最终测试进程退出后，仅删除 P2 `pytest-final` 内经 SQLite 文件头与独占打开核对的71个合成数据库文件，**34,004,992 B**，未递归删除目录。清理前后 D: 可用 137,749,057,536→137,783,037,952 B；同时有正常文件变化，磁盘可用增量不等同逐文件大小总和。清单/哈希与结果保存在 `cleanup-before.json`、`cleanup-result.json`。早期 worker 清理了自己已关闭的4个 C: 合成夹具，7,741,440 B，单独记录，不冒充 D: 回收量。

保留：基线五份小源码、所有成功/失败日志、原始JSON/JSONL、XML、UNKNOWN故障测试的回执、版本/补丁、少量非数据库测试文件。没有清理原P1、其他任务、真实资料、生产库或索引。

## 状态与下一步

- P2 已实施三类工程优化、功能与故障回归：PASS。
- 大文件父子结构、完整 BM25 等价：PASS（合成 SQLite）。
- 分批完整回读与生命周期：PASS（替身 + 本地真实 Chroma 功能测试）。
- Python 验证对象分配减少：PASS，仅上述合成范围。
- 整个解析/索引阶段绝对内存、真实吞吐/IOPS、线上延迟：UNKNOWN / NOT RUN。
- 来源缺失/损坏的历史 active manifest 清理边界：沿用旧行为，不在本轮假称已修复。
- `chunk_count` 全入口统一与跨文件维度缓存：未实施，原因已明确；不把不安全方案接入来凑完成数。
- ECS/外部模型/生产流量/提交推送：NOT RUN。

6.1sol medium 分别负责 SQL/BM25，6.1sol xhigh 负责完整回读及计时审查，6luna high 独立审查差异，未发现确定缺陷。主代理复核真实日志、退出码、分母、哈希、清理和时序后交付。未运行全仓套件，不宣称全仓验收。

建议先审核当前 P2 的内存收益与 SQL 批次数代价。后续如获准在 ECS 验证，应单独冻结相同大文件/父子配置及资源条件，测各阶段与主机资源；本报告不授权远端操作，不自动部署或改变并发。

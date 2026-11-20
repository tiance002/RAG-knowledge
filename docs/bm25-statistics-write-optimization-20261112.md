# P0 性能基线与 P1 BM25 统计写入优化

## 版本和执行边界

基准提交 `7c7415f85a063e3f7645f676bd22f7c8f766a732`，旧对照 `2041bfb5355bab5c0a4e123d413dabcbd619125a`。本地 HEAD、实际远端 `rag-knowledge/codex/project-upload-20261107` 和 GitHub 可见 SHA 均与基准一致。`origin` 指向原开源仓库，不存在附件中写的同名 ref；该读取失败已记录，随后按实际用户仓库核验，没有更改远端配置。

当前独立分支 `codex/bm25-stats-write-optimization-20261112`。原有 README、输出、观察日志、历史规划和归档的未提交改动保留；BM25 目标源码在开始时无未提交修改。根 AGENTS 已读取，backend/AGENTS.md 不存在。本轮不提交、不推送、不部署，不访问 ECS，不调用外部模型，不升级依赖或修改数据库 Schema。

## P0：消费者、存储和调用链

完整引用清单和逐项审计保留在 `.planning/2026-11-12-bm25-stats-p0p1/reference-audit-initial.txt`、`audit/P0-audit.md`，以下为实现决策所依据的结论。

| 分类 | 位置 | 已确认行为 |
|---|---|---|
| 运行时检索 | `bm25.py: BM25Index.search/_calculate_bm25` | 仅从 IndexStats 读取 total_docs/avg_doc_length；DF 来自匹配 Posting 的局部 Counter。评分函数同名参数不是持久化 JSON。 |
| 正式检索消费 | `retrieval.py: _bm25_query_results` | 调用 search(live_only=True)，不依赖全库 DF JSON。普通、Agent、Recall 共用此入口。 |
| 文件写入 | `bm25.py: begin/finish_file_replacement` | 基准逐条读取旧 terms 生成 removed_df，finish 全行加载统计并解码/编码全库 JSON；CI/Posting 原已批量插入。 |
| 单块写入 | `index_chunk/_increment_stats/_decrement_stats/delete_chunk_index` | 维护 CI、Posting 和统计；基准统计增减会读取/重写全库 JSON。 |
| 文件/库清理和重建 | `delete_file_index/clear_kb_index/rebuild_kb_index` | 文件清理原为逐块；清库按 Posting→CI→Stats 删除；重建只处理已发布 SQL 投影，保留不确定历史数据。 |
| 两阶段发布 | `index_builds.py: publish_index_build` | STAGING 持久化→外部向量写入和完整验证→fencing/generation 检查→BM25/SQL 替换→ACTIVE/任务完成原子 commit。 |
| 其他调用 | `parse_tasks.py/staged_pipeline.py/api/files.py/api/knowledge_base.py/chat.py` | 通过上述写入/存在性接口消费，没有直接读取 DF JSON 的产品逻辑。 |
| Schema/迁移 | `models.py/bm25_migration.py/schema_migrations.py` | JSON 列/default 保留；Posting 创建钩子从每块 terms 回填，不使用全库 JSON。 |
| 离线工具 | `ecs_benchmark200_helpers.py`、现有 BM25 benchmark | 初始化或比较 JSON，不是线上检索依赖。其等价性比较必须改用 Posting 事实；不执行 ECS 脚本。 |
| 测试/文档 | BM25、版本兼容、索引安全测试及历史报告 | 全仓搜索结果按上述类别区分；历史源码快照不是额外的线上消费者。 |

基准发布链：prepare → STAGING commit → Chroma 写入 → 完整回读验证 → guard_claim_in_session/generation → retire → begin 删除旧 Posting/CI → SQL 切块替换 → finish 新 CI/Posting/统计 → complete_in_session → commit。原子性、旧向量延迟清理和 UNKNOWN 不属于本次优化对象。

### Posting 不变量与既有局限

合成 SQLite 证明：联合主键 `(kb_id,term,index_id)` 防止同一物理索引的重复词条，重复词的 tf=2 而非两条 Posting；正常写入下物理 Posting 数准确表示 DF。正式 live_only 路径排除正常的其他 KB、父块、已删文件、孤儿和 inactive 版本；默认 live_only=False 则有意保留历史兼容行为，不能擅自更改。

同时，Schema 没有 UNIQUE(kb_id,chunk_id)，也没有约束 Posting.kb_id 与 CI.kb_id 匹配的复合外键。人工构造的逻辑重复 CI 可使物理 DF 增大；人工构造的 KB 关联不一致也可进入既有匹配 SQL。它们是已证明的非法数据反例，不代表生产已发生损坏。本轮不改变查询 DF 口径、过滤规则或自动修复数据，后续风险单列。

审计探针最终退出 0，前四次脚本初始化/反例夹具问题的失败日志均保留。该证明只使用内存 SQLite，无网络或真实文档。

## P0：修改前回归

九个指定模块在基准源码运行：80 passed、2 failed，退出 1。两项 stale-publication 测试仍 monkeypatch 旧的 `VectorStoreService.add_documents`，而基准已改用操作内写入，因此屏障没有触发。只将测试屏障移到实际 `_ingest_add` 写入边界，原任务抢占/取消删除/旧写入 fencing 断言全部保留；两项复跑通过，退出 0。没有修改生产向量实现或超时值。

日志：`baseline-nine.log/xml`、`baseline-stale-repaired.log/xml`。后续 P1 仍需重跑完整九模块，不把这两批结果相加或宣称已有统一全绿回执。

## P0 性能基线 / P1 状态

P0 完整替换基准已执行，进程退出 0。旧版/当前版各 1 次预热、5 次正式交替测量，120 父块、1200 子块、50000 项独立背景词汇；全部业务表与查询分数/排名一致，背景文件保持。旧版/当前版总时间中位数 2074.054/2019.978 ms；begin 24.283/22.193 ms，finish 99.932/85.458 ms；WAL 15000952/14873232 B。存在样本重叠，不以约 2.6% 的中位数差宣称显著提速。原始结果及命令位于 `benchmark/p0-old-current.json`、`benchmark/p0-old-current-command.json`，随后才开始 P1 实现。

## P1：实际实现

| 文件/函数 | 修改与原因 |
|---|---|
| `bm25.py: _accounting_stats` | 窄列加载 kb_id/total_docs/avg_doc_length/updated_at，不把大 JSON 通过 ORM 隐式传回 Python；不在日志中触发延迟加载。 |
| `begin_file_replacement` | COUNT/SUM 聚合旧数量/长度；Posting 和 CI 使用受文件、KB 约束的子查询删除，先 Posting 后 CI，返回二元组。没有读取旧 terms 或构造 removed_df，没有提交事务。 |
| `finish_file_replacement` | 只更新 N、AvgDL、updated_at；保留计数/长度异常边界和已有批量 CI/Posting 插入；不读写全库 JSON。调用者仍拥有原子事务。 |
| `_increment_stats/_decrement_stats` | 包括新统计行、单块替换和最后一块删除，只维护数量/平均长度/时间；旧历史 JSON 即使过期或格式异常也保留原字节。保留既有参数签名。 |
| `index_chunk/delete_chunk_index` | 只读取旧 CI 必要的 id/长度，不再为了统计增减解析无用的旧词频；新的每块 terms 和 Posting 仍正常写入。 |
| `_delete_sync` | 常规发布 Session 没有加载 CI/Posting 实体，使用 synchronize_session=False；若调用者已持有这些实体，使用 fetch 同步，避免删除后旧对象仍被当作有效。 |
| `models.py: IndexStats` | 仅注释说明 term_doc_freq 是历史兼容快照，列、类型和默认值均未改变。检索 DF 的权威源是 Posting。 |
| `ecs_benchmark200_helpers.py: verify_bulk_bm25` | 离线等价性比较改为 Posting 聚合，不能要求新写入器的历史 JSON 与初始化工具的快照实时相等。该脚本未在 ECS 执行。 |

旧版/当前版 begin 原来物化 1200 个旧 CI 的 id/长度/terms，并解码每块 JSON，随后 6 次分批 DELETE；两版 finish 都整列读取一次约 1.35 MB 的全库 JSON。当前紧凑版只是缩小序列化体积，并没有消除这项工作。

### 删除策略的实际选择

没有凭 SQL 语句少就宣称更快。候选子查询的 Posting 删除计划会使用以 kb_id 开头的联合主键范围扫描。又用同一合法数据、同样两个外层 KB 约束，独立比较了子查询与 500-ID 窄列分批方案：各 1 预热、5 正式、交替执行并回滚校验。子查询 begin 中位数 32.773 ms（30.844–35.822），分批 53.628 ms（50.889–55.644）。因此保留较快的安全子查询，但不把它与旧实现未额外约束 Posting.kb_id 的结果混作同口径优势。

该局部候选 begin 可能比旧版更慢；最终收益必须看下面完整三臂测量。没有使用强制索引、修改 Schema、ANALYZE、调整同步或 checkpoint 来制造收益。原始计划和结果：`benchmark/candidate-begin-paired.json`。

### 提交失败的必要修复

真实文件 SQLite、实际发布函数、真实任务领取和独立本地 Chroma 的合成故障测试发现：延迟外键约束在 DBAPI COMMIT 报错后，独立新连接看见旧投影，但复用池连接可看见未提交替换。基线写入器和候选都复现，证明是既有调用者问题。

`index_builds.py` 仅增加 `_commit_index_transaction` 包装两个原有 commit：异常后显式 rollback，保留并重新抛出原始异常，既不重试也不把回滚当成“肯定未提交”的证据。STAGING 和最终发布仍为原来两个持久化边界，没有提前 ACTIVE 或合并文件事务。原对照 2 failed；同一故障与断言在修复后 2 passed，新连接和复用连接均恢复旧状态。永久测试为候选回归，不依赖本地历史快照。

## 最终定向测试

一次完整命令覆盖附件指定的九模块，另加新兼容性与回退工具模块：**116 passed、0 failed/error/skipped，退出 0，32.57 秒，11562 条弃用等警告**。它是当前源码的一次统一回执，不与前面的 80、2、13、19 等不同范围相加。

```powershell
$env:RAG_OBSERVABILITY_ENABLED='false'
$env:LANGFUSE_ENABLED='false'
$env:REDIS_URL='memory://'
& '.\.venv\Scripts\python.exe' -X utf8 -m pytest backend/tests/test_bm25_file_replacement.py backend/tests/test_bm25_inverted_index.py backend/tests/test_bm25_version_compatibility.py backend/tests/test_index_builds.py backend/tests/test_parent_child_ingest.py backend/tests/test_staged_pipeline.py backend/tests/test_file_task_ledger.py backend/tests/test_index_safety_regressions.py backend/tests/test_stale_publication_real.py backend/tests/test_bm25_stats_snapshot_compat.py backend/tests/test_restore_bm25_legacy_df.py -q -o addopts='' --basetemp=.planning/2026-11-12-bm25-stats-p0p1/pytest-final --junitxml=.planning/2026-11-12-bm25-stats-p0p1/final-directed.xml
```

原始 `final-directed.log/xml` 保留。涵盖合法/过期/格式异常的历史 JSON、SQL 监听确认不 SELECT/UPDATE 该列、Parent/Child/空文件、统计量、重复替换、KB 隔离、分数/排序/tie、删除/插入阶段失败、真实 SQLite COMMIT 失败、任务所有权/generation/UNKNOWN。其他全量测试、真实 ECS、外部模型、生产数据校验均 NOT_RUN。

## 旧版本回退：先恢复快照再换旧写入器

不能直接让旧写入器消费已过期的 DF JSON，也不能让新旧统计维护版本混跑。必须停止相关写入，准备独立备份，再从 Posting 恢复每 KB 的 DF，校验后才回退代码。

新增 `scripts/restore_bm25_legacy_df.py`，默认只读 dry-run：

```powershell
& '.\.venv\Scripts\python.exe' scripts/restore_bm25_legacy_df.py 'D:\isolated-data\test.db'
# 仅在实际停止写入、明确选择数据库后执行；下面不是本轮生产命令：
& '.\.venv\Scripts\python.exe' scripts/restore_bm25_legacy_df.py 'D:\isolated-data\test.db' --apply --writers-stopped --backup 'D:\isolated-data\before-df-restore.db'
```

工具取得 BEGIN IMMEDIATE，通过独立读连接备份已提交内容（包含 WAL），独占创建新备份而不覆盖旧文件。它校验 Posting TF/DF 与每块 terms、N/平均长度一致，只在同一事务更新历史 JSON，并再次校验。坏关联、缺失或重复索引不会被偷偷合法化；工具报告拒绝，不修其他数据。备份和原始异常保留，commit 报错一律 UNKNOWN、退出 3，不自动重发；一般拒绝退出 2、成功退出 0。不会建表、改列、重建索引或改持久性参数。

21 项当前工具测试已包含于上述统一 116 项，含已提交 WAL 备份、备份保护、中途失败，以及 commit 报错前/后实际已提交两类 UNKNOWN。只是本地合成验证，不是生产回退演练。工具按全库物理索引定义重建兼容快照，不替代检索的 live_only 过滤。

## P2 只分析，不实施

| 项目 | 当前成本/潜在收益 | 一致性风险与必要实验 | 建议 |
|---|---|---|---|
| retire_file_builds 的 ID 查询 | 即使有 active_build_id 也先读整文件 ID；分支延后可少查询/分配 | 必须证明 ACTIVE manifest 完整、旧向量延迟清理 ID 不丢；分别测 active/legacy/未知状态 | 中 |
| kb.chunk_count | 全部未删文件的 SQL 块数，包含父块，不等于 BM25 N 或唯一送达证据数；当前仍全量重计 | 先明确 inactive/历史版本/父块口径，再做并发更新和已有计数漂移验证，不能直接 delta | 中 |
| Posting 构造/插入 | finish 仍一次构造整份文件 Posting 列表；分批可减少峰值内存 | 在同一原子事务验证中途失败回滚、顺序及重投；大词汇/大文件 RSS 对照 | 高，需独立实验 |
| Chroma count/维度检查复用 | 基准已单文件复用句柄，但每文件仍 count/query | 模型绑定、Collection UUID、维度和生命周期必须可信；删除重建/并发写入不能复用过期结论 | 中 |
| Chroma 分批写/验/释放 | 目前完整回读还保留所有向量/正文/元数据；分批有内存机会 | 必须保留 STAGING、精确 manifest 和全量校验、失败 UNKNOWN、最终统一发布；不能以少验换提速 | 高，需独立实验 |

以上业务逻辑本轮均未修改。检索中已有坏关联/逻辑重复的防护不足也只记录，不能以本地合法样本 PASS 宣称线上索引已审计或已修复。

## 最终三臂性能结果

本地环境 Python 3.13.0、SQLite 3.45.3、SQLAlchemy 2.0.55、SQLModel 0.0.27，Windows 11；未安装 jieba，三臂统一使用同一简单分词实现。合成新原文 5,424,400 B、120 父块、1200 子块、其他文件 50000 项背景词汇。以下每格为各自五次正式测量的 **中位数 / 最小 / 最大**，不跨实验合并。

| 指标 | 旧 2041 | 当前 7c | P1 候选 |
|---|---:|---:|---:|
| prepare ms | 1665.294 / 1611.461 / 1690.984 | 1679.870 / 1646.864 / 1788.070 | 1674.332 / 1559.204 / 1734.802 |
| begin ms | 21.796 / 21.296 / 22.764 | 21.965 / 21.739 / 23.423 | 31.874 / 30.341 / 34.377 |
| finish ms | 81.514 / 79.488 / 81.828 | 83.772 / 79.451 / 86.088 | 55.922 / 54.819 / 62.534 |
| flush ms | 8.248 / 7.811 / 181.013 | 7.079 / 6.795 / 7.961 | 3.275 / 2.927 / 3.687 |
| commit ms | 336.217 / 48.170 / 688.831 | 55.324 / 50.312 / 359.622 | 254.446 / 40.617 / 746.651 |
| 完整替换 ms | 2193.929 / 1841.404 / 2991.761 | 2019.362 / 1883.892 / 2217.974 | 2134.601 / 1756.037 / 2649.301 |
| WAL 增量 B | 15000952 / 15000952 / 15000952 | 14873232 / 14873232 / 14873232 | 13513632 / 13513632 / 13513632 |
| 累计 JSON decode ms | 12.743 / 12.042 / 13.553 | 13.134 / 12.652 / 14.592 | 0 / 0 / 0 |
| 累计 JSON encode ms | 19.316 / 18.540 / 19.825 | 19.843 / 19.593 / 21.039 | 11.507 / 10.266 / 13.069 |
| SQL 执行次数 | 13.000 / 13.000 / 13.000 | 13.000 / 13.000 / 13.000 | 9.000 / 9.000 / 9.000 |
| SELECT 次数 | 2.000 / 2.000 / 2.000 | 2.000 / 2.000 / 2.000 | 2.000 / 2.000 / 2.000 |
| DELETE 次数 | 7.000 / 7.000 / 7.000 | 7.000 / 7.000 / 7.000 | 3.000 / 3.000 / 3.000 |
| INSERT 次数 | 3.000 / 3.000 / 3.000 | 3.000 / 3.000 / 3.000 | 3.000 / 3.000 / 3.000 |
| 主库文件测量前 B | 24276992 / 24276992 / 24276992 | 24276992 / 24276992 / 24276992 | 24276992 / 24276992 / 24276992 |
| 主库文件测量后 B，未 checkpoint | 24276992 / 24276992 / 24276992 | 24276992 / 24276992 / 24276992 | 24276992 / 24276992 / 24276992 |
| 10ms 采样最大 RSS B，非绝对峰值 | 260251648 / 210726912 / 287834112 | 264241152 / 218177536 / 292593664 | 245821440 / 221982720 / 284016640 |

旧/当前各解码 1200 份旧块 terms 和一份全库 JSON，候选不解码它们；候选仍有 1200 次必要的每块词频编码，不能说所有 JSON 编码归零。全库 JSON 解码/编码次数由各 1 次降为 0，旧列的历史字节保留，SQL 监听确认热路径不 SELECT/UPDATE 它。

WAL/autocheckpoint=0/FULL/page_size=4096 的设置仅用于独立合成基准，测量前控制 checkpoint 并记录 WAL 基线，不能用于修改产品持久性配置。主库大小不包括尚未 checkpoint 的 WAL；WAL 增量不等于云盘 IOPS 或物理写字节。每次独立数据库，预热各一次、正式交替/轮换各五次，全部原始样本保留，没有删除慢样本。

首次三臂候选完整耗时中位数比当前版 **慢 5.7%**；commit 中位数约 254 ms 对 55 ms。为定位这一退化，仅追加同配置的当前/候选一组确认，各预热一次、正式五次：完整中位数 2068.805→2042.641 ms（约低 1.26%），范围分别 2062.459–2104.839 和 2039.205–2428.428 ms；commit 54.224→42.642 ms。首次 commit 退化没有复现，原因 UNKNOWN；不能据此挑选第二次结果宣称提速。

两次均确认：SQL 13→9 次、全库 JSON 热路径处理取消、WAL 14,873,232→13,513,632 B（约 -9.14%）。finish/flush 减少，但带完整 KB 约束的 begin 变慢；分词准备仍占较大比重。没有可靠的进程绝对峰值或独立分配峰值，所以不能声称已证明峰值内存下降。

完整逐次数据：benchmark/p0-old-current.json、p1-three-arm.json、p1-current-candidate-confirmation.json（均为退出 0 的实际命令）；all-formal-samples.csv 保存三个实验的 35 条正式记录。逐阶段中位数/范围及每次耗时表见 COMPACT-TABLES-AND-SAMPLES.md，JSON 包含全部语义哈希、查询结果、执行计划、运行时和 SQL/JSON 计数，增量 journal 也保留。它们不合并为一个分母，不报告 P95。

最终 runner 增加 --arms 后，受影响的两个基准工具契约测试另行运行：2 passed，退出 0。它不与统一 116 项相加。复跑使用新输出路径：

```powershell
& '.\.venv\Scripts\python.exe' scripts/benchmark_bm25_write_payload.py --candidate backend/app/services/bm25.py --output .planning/2026-11-12-bm25-stats-p0p1/benchmark/NEW-three-arm.json --warmups 1 --repetitions 5
```

## 决策与交付状态

- P0 审计与本地替换基准：PASS；全仓引用、旧/当前/候选版本和每次原始记录可追溯。
- P1 功能/兼容/原子性：PASS（116 项统一回执），有经本地验证的离线回退工具。
- 全局 JSON/SQL/WAL 的局部减少：已验证；稳定的完整替换提速和峰值内存改善：未证明，性能总体验收保留。
- 建议保留本地候选供负责人审查，不据此升级 ECS 或自动进入 P2。本轮没有回退必要的正确性修复，也没有降低持久性或范围保护以追求漂亮数据。
- ECS、真实用户数据和模型：NOT_RUN；提交/推送/部署：未执行。

本轮改动 11 个文件，完整含新文件的 Diff 为 .planning/2026-11-12-bm25-stats-p0p1/changes.patch；delivery-manifest.json 记录各文件 SHA，test-summary.json 记录逐模块收集/通过/失败/错误/跳过。README 等原有无关改动不在本轮 Diff。

基准进程的临时数据库已自动删除。自动审批审查拒绝了回退测试临时目录的原生 PowerShell 删除操作，返回 blocked by policy，未提供更具体理由；没有换工具绕过，约 0.91 MiB 的合成数据库/备份夹具保留。其他测试夹具依附件“不清理工作区”要求保留，历史资料、原始日志和所有失败证据均未删除。

## 逐模块统一回执

同一 final-directed 命令退出 0；下表不是多次测试相加。

| 文件 | 收集 | PASS | FAIL | ERROR | SKIP |
|---|---:|---:|---:|---:|---:|
| test_bm25_file_replacement.py | 3 | 3 | 0 | 0 | 0 |
| test_bm25_inverted_index.py | 5 | 5 | 0 | 0 | 0 |
| test_bm25_version_compatibility.py | 3 | 3 | 0 | 0 | 0 |
| test_index_builds.py | 17 | 17 | 0 | 0 | 0 |
| test_parent_child_ingest.py | 15 | 15 | 0 | 0 | 0 |
| test_staged_pipeline.py | 19 | 19 | 0 | 0 | 0 |
| test_file_task_ledger.py | 13 | 13 | 0 | 0 | 0 |
| test_index_safety_regressions.py | 5 | 5 | 0 | 0 | 0 |
| test_stale_publication_real.py | 2 | 2 | 0 | 0 | 0 |
| test_bm25_stats_snapshot_compat.py | 13 | 13 | 0 | 0 | 0 |
| test_restore_bm25_legacy_df.py | 21 | 21 | 0 | 0 | 0 |

## Git Diff 范围

下面是实际 git diff --stat 的已跟踪六文件结果；另有五个新文件也全部包含在 changes.patch 中，但未暂存，所以 Git 普通 diff --stat 不显示它们。

`	ext
 backend/app/models/models.py                 |   5 +-
 backend/app/services/bm25.py                 |  96 ++----
 backend/app/services/index_builds.py         |  21 +-
 backend/tests/test_stale_publication_real.py |   5 +-
 scripts/benchmark_bm25_write_payload.py      | 449 ++++++++++++++++++++++-----
 scripts/ecs_benchmark200_helpers.py          |   9 +-
 6 files changed, 436 insertions(+), 149 deletions(-)
`

回退工具当前全量校验物理索引/词频，内存随全库词条增长；大型数据库在实际回退前需要独立资源评估。本轮没有在 ECS 上试运行该工具。

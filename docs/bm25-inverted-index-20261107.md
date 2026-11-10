# BM25 倒排索引迁移与回归

工作树为 `codex/project-upload-20261107`，对照基线为 `ad397ad949caf16f656fd39a3385fcd591ed8736`。本轮修改在本地工作区。ECS 只读取已有库的词频，在独立进程的内存数据库里迁移和查询，业务模型调用次数为 0。

## 存储与检索

| 入口 | 实际行为 |
|---|---|
| `models.BM25Posting` | 每个知识库、词项、索引行对应一条词频记录。联合主键支持按知识库及查询词定位，`index_id` 索引支持撤回一个切片的全部词项。 |
| `BM25Index.search` | 先取知识库的切片数和平均词数，再只读取查询词的 postings，按命中行主键取得切片 ID 和长度。词项的文档频率来自这些 posting。分词、重复查询词计分、BM25 公式和 SQLite 原有同分顺序均保留。查询不到词直接返回空列表。 |
| `RetrievalService._bm25_query_results` | Fulltext 和 Hybrid 都沿用此入口。原问题、改写和补充关键词查询均使用新索引。向量查询、Weighted RRF、Rerank 继续使用原配置。 |
| `database.init_db`、`bm25_migration` | 首次建表从历史 `chunk_index.terms` 分批填充，不读取原件、不重新分词、不重新 Embedding。表创建与回填在同一事务中。SQLite 启动时串行执行建表检查。损坏的词频会报告索引行 ID 并回滚新表，修复数据后可重试。 |
| `BM25Index.index_chunk/delete_chunk_index/rebuild_kb_index` | 新增、替换、变空、删除及全库重建同步更新倒排记录。重建失败时保留原索引。父块只用于扩展。旧正排数据仍用于更新统计及兼容，不进入查询的整库扫描。 |
| `files.update_chunk/delete_file`、`knowledge_base.permanent_delete_knowledge_base` | 修改已发布切片会同步替换词项，编辑未确认预览不会提前发布。删除文件按切片清理，永久删除知识库按库清理。既有向量重新生成仍由索引提交流程负责。 |
| `ecs_benchmark200_helpers.bulk_bm25` | 评测批量建库也写入 postings，匹配已保存的文件归属、排除父块，在空索引上构建。 |

## ECS 实测

从实际 SQLite 库通过 `mode=ro` 读取切片数最多的一个知识库，共 19,242 个索引切片。隔离库生成 694,037 条 postings，首次内存回填用时 2.78 秒。旧实现取自上述 Git 基线。两版在同一进程中使用相同历史词频、统计、分词器和 Top‑10，每种查询预热后交错计时三次。

| 查询 | Top‑10 实际返回数 | 旧实现中位数 ms | 倒排中位数 ms | 切片顺序和原始分数 |
|---|---:|---:|---:|---|
| 低频词 | 2 | 289.10 | 0.66 | 相同 |
| 中频词 | 10 | 281.59 | 0.68 | 相同 |
| 高频词 | 10 | 282.08 | 16.41 | 相同 |
| 多词且含重复词 | 10 | 287.01 | 17.90 | 相同 |
| 无命中 | 0 | 281.82 | 0.45 | 相同 |

实际查询计划有两步，分别是按 `(kb_id,term)` 查 `bm25_postings` 和按 `id` 查 `chunk_index`。这是 BM25 单路、单知识库的热查询结果。高频词仍需读取其全部命中项。未测端到端回答延迟、回答正确率或大规模召回率。

## 命令与证据

在项目根目录通过本地虚拟环境运行下列命令。测试使用隔离的测试库。

```powershell
& .\.venv\Scripts\python.exe -m pytest -q --disable-warnings --tb=short backend/tests/test_bm25_inverted_index.py backend/tests/test_query_expansion_rrf.py backend/tests/test_parent_child_ingest.py backend/tests/test_index_safety_regressions.py backend/tests/test_index_api_safety.py backend/tests/test_parent_child_migration.py
```

- 本地 42 passed，退出码 0。ECS 在同样的六个模块中选前五个，38 passed，退出码 0。Hybrid 用真实倒排和可控的向量服务响应验证两路合并。ECS 真实索引对照程序退出码 0。回执分别在 `var/bm25-inverted-20261107/local-targeted.txt` 和 `ecs-final.txt`；只读实测脚本为同目录下的 `ecs_compare.py`。
- 扩展执行 `backend/tests/test_empty_kb_preflight.py` 时，在初始化的隔离设置库下有四个 SSE 用例失败。使用 `git archive HEAD` 提取修改前源码，在相同条件重放该模块，同样是 17 passed、同四项 failed、退出码 1。涉及空库 chat/agent 各一项及检索/模型故障各一项。最初使用未初始化的设置库时有六项失败。见 `baseline-regression.txt` 与 `local-regression.txt`。未将这些用例计入倒排专项通过数。

## 正式更新和回滚

正式 ECS 数据库本轮未应用迁移。发布时先备份 SQLite（使用在线备份 API 或停写后的完整快照），确认目标盘有容纳倒排表、索引及 WAL 的余量。停止旧 API 和任务 Worker 写入，再由新版单进程初始化数据库，核对已知查询与 posting 数，最后启动同版本的 API 和 Worker。隔离内存库上的迁移耗时不可作为生产停写时长。

若需回滚，应停写后使用备份或旧程序配套的恢复步骤。旧程序可以继续写正排词频，但不会同步倒排表。旧版若已产生新写入，下次升级前须在维护窗口备份现状，重新生成派生表或逐库重建索引，避免沿用过期 postings。

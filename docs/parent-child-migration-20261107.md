# 父子分块与文件身份：历史数据迁移和验证记录

日期：2026-11-07（Asia/Shanghai）。本记录对应当前工作区；远程部署、其他数据库和未连接服务的状态不在证据范围内。

## 只读审计

使用现有 `.venv` 运行 `scripts/audit_parent_child.py`，直接用 SQLite `mode=ro` 读取两个本地数据库与 Chroma SQL 元数据，没有加载服务配置后启动服务，也没有复制语料或索引。原始输出和机器可读记录位于 `.planning/parent-child-upload-20261107/audit-before.{json,log}` 与 `audit-after.{json,log}`。两次命令退出码均为 0；日志内容完整，Docker 子命令失败单独保留。

| 资源 | 实际发现 | 处置 |
| --- | --- | --- |
| `D:\RAG knowledge\local.db` | 1 个知识库、1 个文件、46 个普通 `content` 块、46 条 BM25 记录；0 个 `parent_id` | 保留，未应用迁移 |
| `D:\RAG knowledge\backend\local.db` | 0 个知识库、0 个文件、0 个块、0 条 BM25 记录 | 保留，未应用迁移 |
| `storage/chroma_db/chroma.sqlite3` | 0 个 collection、0 条 embedding SQL 记录 | 保留；SQL 文件状态为 READY 不代表向量索引实际就绪 |
| `backend/storage/chroma_db/chroma.sqlite3` | 文件不存在 | 没有创建或推断其索引状态 |
| 源文件 `var/preferences-acceptance/m2-source/excel-2.xlsx` | 可读取；属于 `file-excel` / `kb-g2` | 只读复用 |
| 冻结与历史评测产物 | 按文件名清点 212 个 Gold/qrel/dataset/manifest 类产物 | 保留；未据此自动认定它们是此次改造的有效 Gold |
| Docker | `docker ps -a` 退出码 1，Docker Desktop daemon 管道不可连接 | 容器状态 UNKNOWN；未停止、创建或清理容器 |

XLSX 源文件 SHA-256 为 `365eca3bcde0c1b8cc3d0c01a9e08cc85e931b39e655873c7c8b97be1fc5a232`。上传回填预览与离线检查的计算结果一致。

这份本地数据没有历史临时父 ID 的实例；不能从“当前数据没有脏父引用”推导旧代码正确。旧结构化解析生成临时 `chunk_1` 等值、入库重新生成 UUID 的实现问题由代码和定向测试核实。迁移工具不会凭内容相似度猜测这些关系。

前后审计的文件 ID、chunk ID、块类型、父引用和 BM25 数量一致。首轮审计未保存整行内容指纹，因此不能将数量一致表述为完整正文的前后哈希一致证明。末次审计增加了无正文的逻辑指纹，以便后续运维比对：

| 根目录数据库表 | 末次逻辑 SHA-256 |
| --- | --- |
| `file_documents` | `e6d4219d70ecd9739012161f0cace9136936f97e24746a754c16e76512d6f72c` |
| `document_chunks` | `61bc168f5e21889d0dea061ddb259d0cab9eafbfcc0a0a76d7c3124164d22655` |
| `chunk_index` | `4b14133d3c30ddff883e8da361683021c83899ec676357f546643d8a6a2062af` |

主代理的第一轮回归中，旧测试 fixture 导入运行设置，原始日志出现对 `backend/local.db` 的 `system_settings` 默认行写入。末次只读审计确认该表有 1 行；知识数据表仍为空。首轮未保存该表行数，写入证据来自回归原始日志。这是配置测试的附带写入，已经披露并保留该行，未擅自删除。后续回归使用隔离数据库。

## 兼容方案

`schema_migrations.migrate_schema(engine)` 为 SQLite 提供幂等的追加迁移。它仅为旧表增加 nullable 列和普通哈希索引，保留原行、原 ID 与原关系，不添加会因历史脏数据而失败的父外键，不填造位置，也不回填猜测的 UUID。并发启动通过 `BEGIN IMMEDIATE` 序列化，并在获得写锁后重新检查列。其他数据库遇到缺失列时明确报错，要求相应数据库的显式迁移。应用启动接入此函数；此次没有对本地历史知识库执行它。

新增列：`file_documents.original_name/display_name/sha256/supersedes_file_id`，以及 `document_chunks.start_offset/end_offset`。空哈希表示未知，不视为重复。新父子模式由应用层验证父子 ID、类型、所属文件与实际内容；旧普通块仍可检索，未知旧父关系只能降级到可验证的 seed。

## 回填、批处理与恢复

下列命令展示操作方式。默认预览，只有显式 `--apply` 才写文件身份字段；apply 必须提供 journal，并要求先完成追加 schema 迁移。本次仅执行了根目录数据库的预览，退出码 0，处理 1 条、失败 0 条，结果是 `WOULD_BACKFILL`，未写源文件、SQL、切片或索引。

```powershell
.venv\Scripts\python.exe scripts\audit_parent_child.py --output .planning\parent-child-upload-20261107\audit-before.json
.venv\Scripts\python.exe scripts\migrate_parent_child.py --database local.db --batch-size 100 --rebuild-manifest .planning\parent-child-upload-20261107\rebuild-preview.json
```

在选定维护窗口、确认目标部署路径并完成 schema 迁移后，回填调用形式为：

```powershell
.venv\Scripts\python.exe scripts\migrate_parent_child.py --database <selected.db> --apply --journal <journal.jsonl> --batch-size 100 --after-id <last-reviewed-id>
```

工具分段读取真实源字节计算 SHA-256，读取前后核对文件大小与修改时间；写入前获取 SQLite 写锁，重新检查来源路径、当前哈希及文件状态。已有不同哈希不会被覆盖。缺失、歧义路径、来源变化和已有哈希不匹配都输出 FAILED。每条写入后追加并 fsync journal。提交完成、journal 写入前进程中断时，可以重跑相同批次：成功行会重新验证，操作幂等；不会仅信任 journal 跳过源文件校验。`--file-id` 可定向重试失败文件；审查失败项后再推进 `--after-id`。脚本不能提供对源文件恶意并发改写的操作系统级排他锁，维护期间应保持源文件稳定。

`rebuild-preview.json` 是重建计划，不是自动执行器。对无法证明父关系的历史文件，流程是保留旧 `file_id`、引用及全部索引，使用源文件建立独立的新文件/版本并启用父子模式，在新索引准备成功且验收通过后才选择其参与业务流量。失败的新版本可单独重试；旧版本仍可恢复。不得把这个清单直接作为“已迁移成功”的记录，也不得在新索引准备前清理旧数据。本次未提交历史重建任务。

## 定向测试及证据

运行 `.venv\Scripts\python.exe -m pytest backend/tests/test_parent_child_migration.py -vv --tb=short --junitxml=.planning/parent-child-upload-20261107/migration-tests.xml`。最终退出码 0；审查原始日志与 JUnit：4 tests、4 passed、0 failures、0 errors、0 skipped。数据版本为该测试中的 disposable synthetic SQLite fixture v1。一个 Pydantic 配置弃用警告来自现有配置类。

验证内容包括：旧库追加列和重复执行幂等、原正文/临时父 ID/BM25 行保留；预览不会修改数据库字节；不可靠父关系报告 UNKNOWN；apply 缺少 journal 或 schema 被拒绝；重复回填可恢复；哈希不匹配、源文件缺失记录失败且不覆盖旧值。测试全部使用小型临时数据库，没有复制现有知识库。

## 真实字节的离线检查

`scripts/compare_parent_child_offline.py` 只读复用当前 `README.md` 和活动 XLSX 的已解析 SQL 表格块，数据库写入全部在内存中。README SHA-256 固定为 `bfa1214ea3c91b95b070fe3388cbd680232e7d883f305983036ab48a94b2ac11`，共 5939 字符。现有 TextSplitter 的 500 token / 50 overlap 设置得到 8 个普通块；父子参考配置 4096/0、384/48 字符得到 2 个父块、21 个子块。配置仅供这次检查，并未据此宣称适合上线。

候选种子由固定词项出现次数降序、原顺序打破平局选择最多 2 条；这不是向量或 BM25 召回，也不是同一个 Gold 集上的召回率比较。通过真实 `expand_parent_context` 与 `build_chat_context` 构造最终引用：

| 固定词项 | 普通 seed-only 字符数 | 子 seed 数 | 唯一父正文/引用数 | 最终正文字符数 | seed 文本保留 |
| --- | ---: | ---: | ---: | ---: | --- |
| 父子 Chunk | 622 | 1 | 1 | 3993 | 1/1 |
| BM25 | 2153 | 2 | 1 | 3993 | 2/2 |
| Celery | 1729 | 2 | 1 | 3993 | 2/2 |
| 模型配置 | 570 | 1 | 1 | 3993 | 1/1 |

扩展原文对 6/6 个种子保留全部原始字符；最终引用沿用 `evidence_text().strip()`，对边界空白规范化后为 6/6。`Celery` 的一个种子在父块尾部带空白，最终引用精确字节计数为 1/2，规范化文本为 2/2；JSON 同时保存两个口径，不掩盖差异。每次父块正文在最终引用中仅出现一次，来源映射 VERIFIED。活动 XLSX 的 46 个表格块经父子处理后仍为 46 个，内容逐项精确相等。

最终离线命令退出码 0。前两次失败原始日志保留：第一次 Windows GBK 输出不能编码已有解析器 emoji；改为 UTF-8。第二次按引用完整字节断言失败；核实为已有引用边界去空白契约，随后增加原始扩展精确检查和引用规范化检查，两个指标均保留。首个扩展包含首次导入开销，单次本地观察约 826 ms；后续三次约 0.80–1.34 ms；这些数据不能作为服务端延迟基准。

没有调用模型、生成回答或执行真实向量/BM25 检索；没有已验证适配本次改造的 Gold/qrel，因此真实召回率、答案正确性、最终引用正确性和语义噪声改善均为**尚未评测**。最终正文变长体现上下文扩展量，不等于已经测量出噪声；当前检查不能支持回答质量提高的结论。

## 资源与收尾

复用现有 `.venv`、源文件、历史 SQL/Chroma 元数据和冻结产物文件名；未复制语料、数据库或 Chroma 索引，未下载依赖，未创建容器。新建的是代码、测试、计划、审计/回填预览、迁移说明及原始验证日志。迁移测试使用 pytest 的独立小数据库；离线脚本使用内存 SQLite 和 `TemporaryDirectory`，退出时清理该脚本自身的临时空存储目录。原始证据保留供复核，其他任务资源未清理。

审计前 D 盘已用 `152983760896` 字节、可用 `141989052416` 字节；末次审计已用 `152984973312` 字节、可用 `141987840000` 字节（该时点尚有并行实现与构建工作，磁盘变化不能全归因于本审计）。两次审计各自执行期间用量均未变化。最终交付的全任务清理后数据由主代理统一补充。

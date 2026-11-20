# 版本化 bigram 词法索引

**已退役：用户随后决定仅保留jieba。本文记录的是历史构建；bigram索引、独立实现和构建工具现已删除，下列历史命令不再可运行。清理结果见 `docs/bm25-bigram-retirement-20261114.md`。**

本功能复用当前已提交的 `DocumentChunk.content`，不调用解析器、Embedding、Chroma 或 LLM。旧 `chunk_index` / `bm25_postings` 仍保留，原有写入流程不改为 bigram。

## 构建与查询

`scripts/build_bm25_bigram_generation.py` 在现有 backend 环境运行，要求显式非空知识库范围。源 SQLite URI 强制只读，目标必须是新文件。示例：

```powershell
.\.venv\Scripts\python.exe -B scripts/build_bm25_bigram_generation.py --source backend/local.db --target var/lexical-generations/bigram-v1.sqlite --kb-id YOUR_KB_ID --disk-reserve-gib 8
```

Linux ECS 同样可以使用已有 venv。`--backend-dir` 指定应用目录；`--module-dir` 可指定本次隔离代码，不替换正式文件。先流式导出当前读快照、释放正式数据库事务，再建立词法索引，避免整个建索引过程固定住生产 WAL。缓存8MiB、分批写入；可由systemd限制CPU/内存。

文件包含 `metadata`、`docs`、`postings`、`stats`。metadata记录schema、明确的分析器版本、实现hash、停用词hash、源数据库路径、各库发布状态、语料hash和范围。只索引原有已提交词法投影中的有效非parent切片，排除删除、跨库、孤儿及当前版本逻辑隐藏的文件。

构建失败不会发布目标文件；中间文件在正常异常退出时删除。构建完成后完整性检查并fsync，再以不覆盖已有目标的原子链接发布。目标不可原地增量编辑。

启用前完整核验：同一条命令加 `--verify`。它逐条重算现有切片文本hash，包含绕过正常写入路径的同长度文本编辑。检查不通过不得启用。

离线对照查询：同一条命令加 `--query "北京大学"`，由 `BigramGeneration.search(...)` 读取保存的对照索引。后续用户要求固定jieba发布后，正式 `BM25Index` 的bigram接入口已注释，传入非空 `bigram_index_path` 会报 `BM25_ANALYZER_OVERRIDE_DISABLED`；不能再用正式类运行bigram对照。

## 应用接入与安全边界

正式 `BM25Index` 固定使用jieba，不再读取环境变量选择另一条路径，也不接受非空 `bigram_index_path`。仅独立的 `BigramGeneration` 类和离线工具可读取对照索引，完全不接入正式请求。2026-11-14用户确认缺少jieba时API启动和普通BM25入库/查询均明确报错，随后要求将正式备用分词接入口注释并部署。

中文连续汉字重叠bigram，英文保留现有ASCII/长度/停用词策略。多词OR、单汉字词前缀匹配；前缀DF按切片聚合；k1、b沿用实例参数。各库独立统计N/avgdl。同分按切片ID排序，和旧SQLite rowid同分排序不承诺相同。

这是一份**不可变发布快照**。查询会核验知识库范围、源数据库身份、分析器/停用词版本和各库发布/可见性状态。正常新增、编辑、删除、重建、版本激活使其过期后，抛出 `BM25_GENERATION_STALE_REBUILD_REQUIRED`，不会静默混用旧bigram与新文本，也不会暗中退回另一分析器。正式切换前应确认当前数据稳定；此版本不适合未经重建流程配套的持续写入场景。

后续入库后若要继续对照，需构建新目标文件、完整验证，再显式传入新目标路径。不要用旧目标覆盖新文件，也不要把缺失jieba视为旧jieba索引自动可用bigram的依据。该对照入口不作为正式默认分析器的开关；正式切换如另行实施，必须按项目服务授权规则执行。本次侧会话不擅自重启或切换正式服务。

查询时的快速状态核验依赖项目正常写入流程在同一事务中更新BM25统计，以及文件可见性字段。绕过该流程的直接SQL文本编辑必须再次执行完整 `--verify` 或重建；不能声称快速核验会发现一切外部SQL改动。正常文件更新时间变化也可能保守地要求重建。

## ECS交付位置

`/opt/amrag/lexical-generations/20261114-bigram-v1`：保留有效索引、manifest、scope、验证记录、代码hash、原始日志。正式API代码与配置不被该目录自动替换。原始冻结数据和旧SQLite/Chroma索引保留。

测试与资源实测结果以该目录最终manifest/validation-summary和本地 `var/bm25-tokenizers-side-20261114/` 的原始日志为准，不能把启动命令退出0或systemd运行中的Result=success当作构建完成。

## 本次实测交付

2026-11-14 ECS已完成构建和核验，构建/验证单位均inactive、Result=success、实际退出码0。17库、101,849个有效切片全部建立词法索引，文件926,314,496字节（约883.4MiB）。构建约328.6秒；源文本完整hash核对一致、SQLite完整性检查ok。

五道开发题（3中文、2英文）经实际 `BM25Index.search` 显式路径入口，在原10库评测范围内合并Top-50，5/5与上一轮bigram排名完全一致，无查询错误或空召回。每题跨10库耗时约305–448ms，包含逐库源状态检查，不可与上一轮简化评测器的单次原始倒排耗时混为同一指标；正式API端到端与并发性能未测试。

本地相关回归16 passed、退出码0，23,545条弃用警告保留。首次本地回归6个新用例因Windows上只读文件句柄fsync报 `OSError: [Errno 9] Bad file descriptor` 失败，已改用r+b句柄并复测；首次ECS启动因隔离目录缺少 `performance_timing` 模块失败，发生在构建前，补齐同版本依赖后重跑通过。两次失败原始证据均保留。

复用已有虚拟环境、源库与切片；只新建一个保留的词法索引及小型代码/证据文件，没有复制语料库或Chroma。CPU限半核、内存上限600MiB；构建期间1592次采样的最低MemAvailable为2,165,682,176字节（约2.017GiB）。曾观测进程RSS约80MiB、swap0；cgroup峰值600MiB含文件缓存。完整性扫描期间宿主IO PSI较高（一次观测some avg10约57%），不声称磁盘没有受到压力；未调大资源上限。

初始资源登记时云盘可用12,752,863,232字节（约11.877GiB），结束11,807,780,864字节（约10.997GiB），保留约0.863GiB索引及少量记录。临时文本导出与building文件已全部删除。结束MemAvailable约2.202GiB。冻结reference/runtime文件hash未变，冻结题执行0。正式API结束仍active，PID680975，未替换正式文件或配置、未重启服务；启用开关仍未打开。

核心证据：ECS目录下 `index.sqlite.manifest.json`、`verification.json`、`paired-query-results.json`、`validation-summary.json`、`final-audit.json`、构建/验证原始日志。小型证据同步至本地 `var/bm25-tokenizers-side-20261114/generation/`；没有额外下载926MB索引副本。

## 与WeKnora Lite的边界

核对上游commit `cbdcebabf48c5ee2d6868b370326aa8341703f62` 的 `internal/application/repository/retriever/sqlite/repository.go`：入库和查询共用 `tokenizeCJKBigram`，FTS5 contentless索引与原文metadata/vec0分开表示，但检索写入在同一SQLite事务中维护。本次独立generation是保留旧索引的对照/发布快照，并没有移植其FTS5引擎或实时增量事务方案。持续写入上线需要另行接通新词法索引的事务维护，不能把这一阶段描述成已完成WeKnora式线上替换。

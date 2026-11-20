# ECS 固定 jieba 发布与验证

后续用户已授权删除bigram对照；对应索引及停用构建代码现已清理，正式jieba服务保持不变，详见 `docs/bm25-bigram-retirement-20261114.md`。本文“保留bigram”描述的是发布完成当时的状态。

2026-11-14（北京时间），按用户授权核对依赖和旧索引，空闲时发布并重启正式API。17:47:08发起重启，17:47:11日志确认应用启动完成。

## 兼容性结论

正式API解释器为 `/opt/amrag/experiments/p0-langfuse-20261028-code-r1/venv/bin/python`；实际导入jieba 0.42.1，模块来自 `/opt/amrag/.venv/lib/python3.12/site-packages/jieba`。无需在ECS补装或更换依赖环境。词典SHA256为 `7197c3211ddd98962b036cdf40324d1ea2bfaa12bd028e68faa70111a88e12a8`。

另外读取正式parse/index worker的ExecStart并逐个用其配置的解释器导入检查：两者与API使用同一个解释器，jieba均0.42.1，依赖检查退出码均0。未启动这些停用的worker；逐项证据在 `worker-dependencies.json`。

对全部17库的102,229条 `chunk_index` 记录逐条比较真实jieba词频与长度，102,229/102,229一致；候选与部署前分词输出差异0，缺失来源/不匹配记录0。包括保留的历史版本，并非仅抽样有效语料。可以直接复用，无需重建词法索引，更未重新解析PDF或计算向量。

发布后再次计算全部索引记录与对应正文的投影hash，仍为 `a74130be65f973c2b532fd889412af544c7140568ad48dc80556cf9bf3d82623`。17库统计记录保持不变，170条抽样索引的实际倒排词项与原词频一致、发布前后hash不变。此处不将170条实际postings检查误报为所有postings的完整性扫描。

## 本次发布范围

服务器源码较本地旧，未覆盖整个本地工作区。基于发布前ECS原文件生成最小补丁，只发布：

- `app/services/bm25.py`：固定 `jieba.cut`，新增明确依赖错误，默认构造时检查依赖；备用逐字/bigram入口仅留注释，非空 `bigram_index_path` 报 `BM25_ANALYZER_OVERRIDE_DISABLED`。
- `app/main.py`：启动时在目录/schema写入之前校验依赖，记录分词器、版本和禁用自动降级的状态。
- `pyproject.toml`：声明必需依赖 `jieba==0.42.1`。

补丁构建器核实：ECS BM25类除构造函数外，原有入库、替换、删除、查询SQL、评分等方法的AST全部保持一致。分词核心与本地当前实现一致，未连带发布本地其他优化。独立bigram实现和索引只作离线留存，正式模块没有任何可执行bigram/其他分析器导入或调用；本地对照工具改为直接使用离线类，不经正式BM25入口。

## 空闲窗口与服务验证

发布前11778条任务回执全部COMPLETED。Redis `document.parse` / `document.index` 队列及unacked均0；API端口8200连续3次、间隔2秒检查无ESTABLISHED连接。`/metrics` 当前404，因此没有将缺失的in-flight指标当作0。以真实队列、回执及TCP检查判断空闲。

仅重启 `amrag-file-api.service`。PID680975变为728757，服务active，健康检查ok。解析和索引worker原本未运行，保持原状；Chroma、Redis、前端及其他服务未重启。原有STAGING构建/REPLAY_AUTHORIZED效果记录未清理或擅自处理。

实际新API启动日志：

```text
[BM25] analyzer=jieba version=0.42.1 automatic_fallback=disabled
Application startup complete.
```

新进程运行期间日志未发现Traceback、API ERROR或启动失败。

## 验证结果与边界

1. 本地相关回归23 passed，退出码0、失败/跳过0；25,505条弃用警告原样保留。启动日志补充后，对涉及的核心测试再测16 passed、退出码0。
2. ECS候选及发布后的实际模块各通过6组检查：依赖版本、禁止备用入口、入库与批量准备词频一致、真实jieba查询命中、6种依赖缺失路径明确失败、失败入库不改旧postings。使用真实依赖、合成内存SQLite，未向正式库插入测试文档。发布后退出码0，原始stdout/stderr与逐项JSON保留。
3. 正式HTTP召回接口，开发题3中文+2英文，发布前后各运行一次：`fulltext`、top_k50、rerank关闭、context_window0，明确单库范围。5/5 HTTP200且业务code200、retrieval_status SUCCESS；结果顺序、最终分数、BM25分数和正文全部一致，5/5 expansion_delivery_status DELIVERED。

| 开发题 | 结果数量 | 排名/分数/正文一致 |
| --- | ---: | --- |
| terra-ecs-b1-002 | 47 | 是 |
| terra-ecs-b1-006 | 45 | 是 |
| terra-ecs-b1-008 | 50 | 是 |
| qst_0001 | 36 | 是 |
| qst_0005 | 27 | 是 |

少于50表示返回有效结果数量，不算执行失败。此次不是Gold答案质量、混合检索并发或端到端回答时延评测，未调用LLM答案生成、重排或向量检索。API耗时原值保存在原始记录，但受缓存/同机负载影响，不据此宣称固定分词带来性能提升。冻结题执行0。

## 保留、资源及回退

ECS证据和代码回退目录：`/opt/amrag/releases/jieba-fixed-20261114-side/`。`baseline/`保留3份发布前源码，`candidate/`是最小候选；发布前核对原文件hash以防覆盖其他任务改动。部署脚本内置重启/健康失败自动恢复原文件的分支，本次未触发回退。

旧SQLite/Chroma索引未重建、删除或迁移。bigram对照索引仍在 `/opt/amrag/lexical-generations/20261114-bigram-v1/index.sqlite`，926,314,496字节，发布前后大小与mtime不变；本次没有额外重读883MiB做全文件hash，也没有让正式请求接入它。Gold及冻结runtime文件的SHA256前后不变。

复用已有解释器、数据库、索引和题目。未复制整份数据库、Chroma、语料或bigram索引。兼容性检查仅临时导出核对所需文本和词频，以便尽快释放生产读快照，完成后已删除；临时导出剩余0。仅保留小型代码备份、逐项结果和必要日志，无新容器或虚拟环境。

核对任务限制CPU半核、MemoryMax500MiB；实际cgroup峰值489.4MiB（含文件缓存），swap峰值0，最低采样MemAvailable约2.142GiB，未跌破800MiB保留线。结束可用内存约2.186GiB。

云盘可用空间观测：发布准备前11,753,684,992字节（10.94647GiB），结束11,753,238,528字节（10.94606GiB），约少436KiB；该观测包含运行期其他写入，不能全部归因于本次文件。没有进行大规模资源清理。

本地证据副本：`var/bm25-jieba-release-20261114/`。核心文件：`index-compatibility.json`、`candidate-manifest.json`、`pre-deploy.json`、`deployment.json`、`http-before.json`、`http-after.json`、`post-verification.json`、`production-selftest.*`、`api-after-restart.journal.log`、`compat.journal.log`、本地测试日志。全部原始失败/警告保留；首次SSH连接中断后重连成功，未影响服务或数据。

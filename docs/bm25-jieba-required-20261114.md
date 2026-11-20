# BM25 正式默认固定 jieba：本地变更与验证

2026-11-14，按用户要求先修改和测试本地，不切换ECS正式服务。

后续用户已授权ECS发布，并要求禁用正式备用分词接入口。当前结果见 `docs/ecs-fixed-jieba-release-20261114.md`：正式类即使显式传入bigram路径也会拒绝，旧对照索引仅由离线工具保留使用；下文是前一阶段本地验证记录。

## 最终行为

- 默认 BM25 入库、批量准备、重建和查询共用 `tokenize()`，使用 `jieba.cut`；原有英文提取、大小写、停用词及单字符过滤保持原jieba路径行为。
- 取消缺失jieba时切换bigram的逻辑，也取消默认查询路径根据依赖情况启用汉字前缀匹配的逻辑。
- 新增 `require_jieba()` / `BM25TokenizerDependencyError`。依赖无法导入时抛出 `BM25_JIEBA_REQUIRED`，提示安装 `jieba==0.42.1`，原始ImportError保留为cause。
- 默认BM25实例构造时检查依赖；API lifespan也在创建目录、初始化数据库之前检查。worker或离线批量准备仍由共用tokenize检查，不能静默换算法。
- 默认单例不再读取 `BM25_BIGRAM_INDEX_PATH`；移除该配置字段。独立bigram对照必须由调用者显式传 `bigram_index_path`，保留现有对照工具能力，不会因默认依赖缺失自动选择它。
- 根 `requirements.txt` 已有 `jieba==0.42.1`；补齐 `backend/pyproject.toml` 同版本必需依赖。本地现有 `.venv` 原先未安装，本次仅补装该包，未创建新环境。

旧SQLite/Chroma索引、bigram对照索引、冻结数据和原有评测证据均未重建或删除。本轮没有SSH、上传或ECS服务操作。`bm25_bigram.py` 未修改，其实现SHA256与之前已保存的ECS对照索引manifest一致；不对未重新连接的ECS状态作实时断言。

## 已执行验证

命令（使用已有本地venv，DATABASE_URL=sqlite://，不初始化正式数据库）：

```powershell
.\.venv\Scripts\python.exe -m pytest backend/tests/test_bm25_jieba_required.py backend/tests/test_bm25_generation.py backend/tests/test_bm25_bounded_postings.py backend/tests/test_bm25_covering_indexes.py backend/tests/test_bm25_file_replacement.py -q -p no:cacheprovider
```

最终实际退出码0，23 passed、失败0、跳过0，25,505条弃用警告保留。未运行项目全套测试。

覆盖真实jieba的索引词频、批量准备与查询同规则；模拟缺失依赖时普通实例、中文/英文/空文本、入库和查询明确失败；旧倒排记录和词频不变；默认实例忽略旧bigram路径配置；显式bigram对照可在模拟缺少jieba时独立查询且索引字节不变。还复测版本化bigram构建/失效保护、覆盖索引及文件替换事务回滚。

启动检查用实际lifespan函数体配合隔离globals执行，验证错误发生在目录/数据库写入之前；它不是完整HTTP服务启动测试。实际分词与SQLite集成使用真实jieba及临时或内存SQLite，非模型/API评测，冻结题执行0。

旧generation测试中“市股”在真实jieba下可能被过滤成空词，故合成测试文本改为“持股”，保留“股只在词尾时不满足前缀匹配”的测试目的；未改真实语料、Gold或评测切片。

第一次相关回归22 passed；移除默认路径配置并增加针对性回归后最终23 passed，原始两轮日志均保留。本次没有失败测试被隐藏或移出统计。

## 证据和资源

本地 `var/bm25-jieba-required-20261114/` 保存依赖安装日志和退出码、两轮测试原始日志/退出码，以及 `validation.json`（日志hash、bigram实现hash、版本和统计）。已有旧测试/评测日志保持原样。

复用现有venv和测试基线，只新增本地jieba依赖、小型日志及合成测试库；未复制原始语料或任何现有索引，未新建容器，未做资源删除。未采集本轮开始时磁盘用量，因此不声称释放空间或给出前后差值；结束本地可用空间见validation.json，ECS磁盘未在本轮访问。

# 统一 Python 文档分块与 ECS Excel 原库替换

## 架构

当前正式入口为 `FileParser.parse → extract_document → ParsedDocument → chunk_document`。提取器不选择检索块：PDF 提取文本/表格/图片及页内区域，DOCX 提取段落/标题/表格与图片，Excel 保留逐行键值中间单元，CSV 产生表头和逐条记录，TXT/MD 读取文本并在 MD 中规范化可确认的管道表格。原文位置附在中间单元上。

`ParsedDocument.text_and_spans` 生成统一文本及来源区间。`document_chunker.chunk_document` 一次选择普通策略或 `split_parent_child`。父子模式内部先生成父窗口、再生成子窗口，参照 WeKnora `SplitTextParentChild`；不先运行普通分块，也不对 Excel 每行分别建立父子图。保持 Go 对无须扩展的父窗口的省略行为。调度器在加入 VLM 描述后才调用 Chunker，不再先切普通块再调用独立父子切分。

老的 `_parse_pdf/_parse_docx/_parse_txt/_parse_csv/_parse_excel` 是旧调用方适配器，统一转入同一 facade。旧内部逐页/固定 20 行/标题混用分块实现已从 FileParser 移除。旧 TextSplitter/StructuredSplitter 类保留为已有外部调用兼容与显式 semantic 适配器；默认各格式路径不构造这些旧分块器。历史 `build_parent_child_chunks` 无生产调用，不参与新入库。

## 来源与引用

块从 span 映射取得所有相交来源；跨页块不伪造唯一页码，保留 pages/regions；Excel 跨工作表块保留各表原行定位，不拼成虚假的单表行范围。DOCX 页码未知时保留 body_index，MD 格式规范化后只报告规范化块位置，未伪造原文件字节/行号。CSV 使用逻辑记录号，嵌入换行不冒充物理行。

Go legacy 生成的表格子块可能带重复表头、不是父正文的连续子串。入库图校验与检索父扩展通过同一 legacy 算法验证该子块确能从父块产生，旧索引仍支持原先的精确包含检查；不把任意 parent_id 当可信关系。来源 manifest 保存子块配置供后续核对。模型图片描述保留来源标记与派生哈希，原图资产独立保留。

## 验证

- 本地最终受影响套件：127 passed，0 failed/skip，退出码 0；日志 `var/unified-chunker-local-final.log`。旧 PDF 测试对每行一块和唯一 page_number 的断言已更新为新位置映射契约，并继续核对每行原始位置与证据内容。默认测试显式提供解析参数，不依赖空测试 DB 的 system_settings 表。
- 普通 legacy 核心此前真实 Go 对照：250 组输入 / 4772 块零差异。
- 新父子核心真实 Go 对照：250 组输入 / 771 父块 / 5205 子块零差异，含 ParentIndex、正文、起止位置和序号。首次发现省略父窗口后的 Seq 空缺差异，修复后通过。证据位于 `var/unified-chunker-ecs-20261116/parent-go`。
- ECS 使用旧生产应用的只读 symlink overlay 验证候选，不复制语料/DB/Chroma；5 份真实 Excel 的提取、父子图和 manifest 校验通过。部署 13 个后端文件后 health=200，重新在已部署代码上运行真实 Excel smoke 通过。

## 部署与索引范围

发布目录 `/opt/amrag/evaluation/unified-chunker-20261116`，含代码哈希、候选与部署收据、仅被修改文件的 rollback。部署前核对生产源哈希及文件任务空闲，不覆盖其他会话的不同代码。

原 QA500 office50 KB `7cdb1ee4-ae8e-5cb6-b2ee-4f42a385cf00` 内的 5 个 XLSX，沿用 file_id 与 kb_id，`auto_vectorize=true,parent_child=true,parent=4096/0,child=384/48,xlsx_first_row_as_header=false`，逐个建立新 generation，成功验证后切换 active_build。原始文件未改；旧切片、BM25 行和向量只按这 5 份文件选择性压缩备份。

发布后已核对其他 117409 条切片的 ID/正文/parent_id/chunk_type 有序哈希不变，知识库 ID 列表不变。5 个文件的新父块/子块/普通未挂父块数量为：excel-1 57/370/5，excel-2 55/384/14，excel-3 7/90/0，excel-4 1/6/0，excel-5 1/3/0。普通块代表 Go 不保留冗余父窗口的结果，不是漏建父子图。

历史 `hold-original-recovery` 标记阻止通用调度器启动；未清除该标记。改用仅匹配 `unified-excel-20261116-%` 的 scoped notifier，按阶段 CAS 通知；不恢复无关历史任务。结束后已将本任务启动的解析/索引 worker 恢复为原先 inactive。

## 失败题复测和资源

选择上次 300 题有效结果中的 25 道空答/失败（24 字符预算超限，1 未知错误），用冻结原题、原检索配置、原实际已生成历史重放；不向模型传 Gold，网络搜索关闭，遇错保留原始响应并继续下一题。正确率必须另按 Gold 与原始证据评分，完成请求不等于答对。最终数据见 ECS `replay-results.json`、`replay-summary.json` 和逐题原始 SSE；测试运行中不预报结果。

复用现有环境、现有 KB 与源文件，未复制整份 DB 或向量库。新增少量候选代码、回滚文件、5 文件选择性备份、位置/哈希记录及原始测试响应。监控 MemAvailable 保留 800 MiB、云盘剩余 3 GiB 的运行门槛；完整资源采样位于发布目录 `resources.jsonl`。此前任务便携 Go 工具链的清理曾被自动审批拒绝，未绕过删除，未把它当成已释放空间。

## 最终修正与有效证据目录

索引一致性检查进一步确认需按上游 processChunks 过滤纯空白输出：原始 SplitText/ParentChild 的逐块对照保持不变，但不能把空白块送向量化。新增混合原文/模型图片描述的来源标记保留。相关针对性测试分别 27 与 45 passed。

统一中间文档拼接还曾少加单元格尾部换行后的 Excel 行终止符，实际输入少 14 字符；已修复逐行中间单元总是包含自己的终止符。原始 excel-1 文本恢复 219114 字符、SHA256 271b940e2b7a60c2727db4bbc773cad943721668224ca561d05d5ad43f821323，raw legacy 恢复 302 块。普通索引输出会过滤其中 68 个空白块，得到 234 个有效普通块；302 是原始分块器口径。此数据的正式发布使用父子模式，不能将 234 与父子输出直接混为同一口径。新增/相关 15 项测试通过。

最终代码和复测目录是 `/opt/amrag/evaluation/unified-chunker-20261116-verified`。中间 `-final` 目录是诊断轮：当前 HTTP 响应完成后停止了本任务自建控制器，保留 8 个已记账结果与当前原始响应，未混进最终 25 题。初始完整首轮 13/25 非空生成仅作为诊断记录，最终以 verified 的完整结果为准。

最终 5 份 Excel 已发布并核查：874 SQL 切片 = 121 父块 + 734 子块 + 19 个无须父窗口的普通结果；753 个可检索结果全部有向量，751 条 BM25 记录与实际分词非空集合精确一致。另 2 条非空但无有效词项的结果按 BM25 规则跳过。空白检索块 0，悬空/跨文件父子链接 0，全部 13 个发布文件最终哈希核对一致。其他 117409 切片的原始有序哈希与首次部署前完全一致。

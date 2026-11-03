# R1：E1c 历史开发集就绪审计

1. **目标与改动**

本次只整理获准本地历史材料，结果为 **BLOCKED**。未执行六题回放、真实检索、模型请求、ECS 或生产访问。六题只能作为开发集；独立留出集尚未准备。

新增 `scripts/prepare_e1c_dev.py`（纯标准库本地审计）、`experiments/r1/e1c-readiness.json`（计数、固定候选和缺口）及本报告。逐成员对齐和完整扫描清单保存于忽略目录 `var/r1/e1c-readiness-r2/alignment-and-inventory.json`。原 `experiments/r0/e1c-planned.json` 仍为 `NOT_RUN`，原始文件 SHA256 为 `61925bf86f231a04c3d96801204425a8eab17c8ee58376085d3559e8739ae819`。

尚未生成可执行 cases/evaluation/READY 文件：两项引用明确为 `null`。不能用 citation 的 KB 字段补造 `FileDocument` 后自称完成来源验证。

2. **执行命令**

在 `D:\RAG-R0` 执行：

```powershell
python scripts/prepare_e1c_dev.py --approved-var 'D:\RAG knowledge\var' --private-out var/r1/e1c-readiness-r2 --report experiments/r1/e1c-readiness-r2.json
```

退出码 `0`，表示审计完成；输出状态为 `BLOCKED`、实验执行状态为 `NOT_RUN`。随后将初版公共报告移入 `var/r1/e1c-readiness/initial-public-report.json` 保留，将 r2 报告移到最终路径 `experiments/r1/e1c-readiness.json`。该脚本使用独占创建，拒绝覆盖既有输出；复核时应选择新的输出路径。

关键原始输出：`questions=6, candidate_seeds=107, snapshot_chunks=72, snapshot_files=10, member_occurrences=281, json_files_scanned=231, explicit_mapping_candidates=148, derived_retrieval_citations=148, independent_mapping_candidates=0, sql_databases_checked=55, sql_with_file_documents=0, archives_checked=71, archive_sql_members=0, scan_errors=0`。

另以 `ast.parse` 解析新脚本，退出码 `0`、输出 `AST PASS`。随后以本地 Python 断言复核 281 次文档/哈希及索引位置对齐、BLOCKED 清单不可冒作 READY、扫描无错、worker/旧计划哈希未变，以及历史 stage1 manifest 对 snapshot/source audit 的输入哈希绑定；退出码 `0`，输出 `PASS: syntax; blocked manifest; 281 identity/index alignments; no scan errors; unchanged worker/plan hashes; historical manifest input hash bindings`。`git check-ignore var/r1/e1c-readiness-r2/alignment-and-inventory.json` 退出码 `0`，确认私有证据被忽略。这些仅验证就绪审计及证据关联，不等于回放或来源归属通过。历史题回放、质量评估、成本收益及晋级均为 **NOT RUN**。未实现的命令不得报告通过。

3. **结果与证据**

历史 E20 snapshot 包含六题、107 个原始候选种子、72 个唯一 chunk、10 个文件。内容 SHA256 和 snapshot 子集指纹已重新计算一致。E21 的 281 行是成员出现次数，不能当作 281 个唯一 chunk；同题唯一成员数如下。

|题号|原始候选种子|成员出现次数|同题唯一成员|文档/内容哈希对齐|索引位置对齐|历史真实 Gold 位置交叉核对（出现/唯一）|仍缺独立 KB 归属的文件|
|---|---:|---:|---:|---:|---:|---:|---:|
|office50:001|20|50|43|50/50|50/50|1/1|8|
|office50:008|20|53|40|53/53|53/53|1/1|7|
|office50:010|20|54|41|54/54|54/54|1/1|7|
|office50:014|20|54|41|54/54|54/54|1/1|7|
|office50:020|20|53|40|53/53|53/53|1/1|7|
|office50:031|7|17|16|17/17|17/17|2/1|2|

“文档/内容哈希对齐”要求 member ID、source file ID、精确内容 SHA256、历史独立 source audit 的来源文件 SHA256 和其原索引内容检查全部吻合；不是只按文本相似匹配。“索引位置对齐”仅核对 `original_index` 与 `indexed_page_number`，**Office 占位页码不代表真实 Word 页码、表格或 Excel 单元格定位**。

真实 Gold 位置的交叉核对来自已保存的独立历史 source audit：Word 的 table/row/column、Excel 的 sheet/cell，加来源文件哈希；七次出现仅六个同题唯一成员。此次未重新打开远端原始 DOCX/XLSX，因此这是对历史证据的本地交叉核对，不能称为本次原文件回读。所有标注位置状态中，7 次 `GOLD_LOCATION_VERIFIED`、17 次 `PARTIAL`、257 次 `UNKNOWN`；281 次标注全部仍为 `AI_PROPOSED`、`PENDING_REVIEW`。274 次结构必要性为 `UNKNOWN`，另外 7 次仅为 `AI_PROPOSED_NECESSARY_WITHIN_DIRECT_MEMBER`。

独立归属搜索包含已保存的 Office/P03/P1/E1/P0/review250/PDF 相关 231 个 JSON/JSONL；逐文件路径、哈希及结果保存在私有清单。命中的 148 个 file→KB 组合全部来自 `p0-observability/p1-live-review/p1-evidence.json` 的 `/final_citations/`，属于检索结果派生材料，已排除。P1 preflight 只有 Office KB 的 12 文件/171 chunk 聚合计数，不能证明十个具体文件的归属。P0 ingest smoke 的文件/KB 与本次十个文件不匹配。55 个本地 SQLite 仅检查 schema（`mode=ro&immutable=1`），无 `file_documents`；非 Chroma SQLite 是调用/attempt/event 账本。71 个本地归档仅列目录，无 SQL DB 成员，没有提取或复制语料/索引。历史 `/opt/amrag/.../office/isolated.db` 仅有保存的路径与哈希，不在已批准本地材料内，未访问。

搜索结论限定于记录的本地目录、JSON/JSONL 明确字段、SQLite schema 和归档目录；未声称整个机器或远端不存在更完整来源清单。

当前 B 保持 `verified-exact-duplicate-v1`，配置仍为 `{"selection":"exact_duplicate"}`，没有创建新算法或冒用新候选身份。配置规范 JSON SHA256 为 `a31652781d2f398a9bc62a392ed47ebdb1337736d9ef74b2b0239b2e125de493`，原始字节 SHA256 为 `92238ae804b6c5295133486f8d82c38163f4a864ed28c11ee7691f26c83a7cc0`；worker 原始字节 SHA256 固定为 `a5f793b9f8f6ad7908c78580a670c0cc5c909898dc8e4c36d05fdbc2a330d3da`。

该规则只有在 PDF 文件、有效 page/bbox、非空 parent/heading 及同源精确内容定位相同时，才允许移除非独立种子。历史十文件实际为 7 DOCX、3 XLSX，72 chunk 的 `parent_id` 和 `heading_text` 全空，snapshot 无 `source_locator` 字段，符合当前去重元数据门槛的成员为 **0**。同文跨 ID 数量不是可删证明；不能用该历史材料宣称当前 B 已有 Office 压缩收益。保持该 B 最多能进行无删减控制；Office 规则需要另开候选并重新冻结。

4. **风险与遗留**

- **OWNERSHIP**：10/10 文件缺独立 file→KB、名称及来源哈希绑定。需补充获准的历史隔离 SQL 快照或来源导出；核对其快照/导出哈希、十文件归属和 72 成员内容、顺序位置。不能从检索 citation 重建可信归属。
- **EVALUATION**：已对齐标注仍是 AI 提议，必要事实、字段/表头/单位及多数真实位置未独立确认。需逐题审核后单独冻结 evaluator，保留未知项，不能把 relevance 当作可删除结论。
- **APPLICABILITY**：现有 B 不能删除这些 Office 成员；如果任务仍使用该 B，应明确只是无删减控制。任何 Office 选择变更必须另开候选身份，不能静默扩张当前规则。
- **HOLDOUT**：六题均已用于开发研究。需另选未用于设计/调参的数据；六题即使补全来源最多 `READY_DEV_ONLY`，不得作为晋级或质量/成本收益证明。

本报告没有批准延期或验收。来源、结构必要性和留出缺口没有被改写为 PASS。

5. **版本、资源与下一步**

基线 commit 为 `90d86ce87e8c76c8c45f5f296160ddf53de71d54`。未提交、push 或打 Tag；R0 worker 与 backend/app 未由本子任务修改。复用既有 E20/E21 文件，未复制语料、Chroma 或 SQL 基线，未创建容器/模型服务。新增私有资源 946,419 字节（约 0.90 MiB），包括初版与 r2 的逐成员/扫描证据及初版公共报告；保留用于追溯搜索分类修正，删除量为 0。该数字是本子任务新增文件总量，未把整个卷的剩余容量当成本任务用量。

下一个最小任务是补齐十文件的独立归属导出与可核验来源链，再完成独立 evaluator。满足这些条件后才能新建开发集 cases/evaluation/manifest；原 `NOT_RUN` 计划继续原样保留。

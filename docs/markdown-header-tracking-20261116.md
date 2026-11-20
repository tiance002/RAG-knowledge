# Markdown 表头跟踪与 Excel 行文本重新分块

参照来源：WeKnora `internal/infrastructure/chunker/header_tracker.go`、`splitter.go`、`internal/application/service/knowledge_process.go` 的生产调用链，以及官方 `docreader/parser/excel_parser.py`。本地已有 reference 目录复用，另只读核对官方源文件。Python 实现覆盖表头生命周期与行文本重新组织机制，不宣称逐字移植全部 Go 自适应 splitter 或实现分块数量一致。

## Markdown

此前 `.md` 与 `.txt` 共用普通文本/标题切分，无表格表头生命周期。现在识别围栏外的管道表格，将字段行和分隔行作为当前表头；同一表跨块时重复该表头，空行、正文、列数变化或相邻新表结束旧表头状态。保留列对齐标记与转义竖线；空白占位表头参照 pendingExtend，用首个实际行补全字段名并保留该行。

普通块和父子模式均执行表头重复。父子表格的父块由含表头的子块组合，确保子块是真实父块的连续子串，现有图校验无需放宽。此重复是规范化上下文，不宣称原文件也重复这些表头。定位明确为规范化 block，未实现原始 Markdown 行号/字节级定位。

超长表格行不截断丢弃，而是分片并注明需结合相邻片段。若表头本身无法装入预算，明确抛出 `MARKDOWN_TABLE_HEADER_EXCEEDS_CHUNK_BUDGET`。未实现所有 HTML 表格、base64 图片抽取、无管道表格、Go heading/heuristic 自适应策略；不可把本次实现称为完整 Go chunker 等价移植。

## Excel 修正与数量核对

之前 624 不应与 WeKnora 原始行块直接比较：原实现去掉 3 个仅含空白的行，619 行再拆超长行净增加 5 块。已修正与上游不同的文本处理：保留原始值空白、NA/NULL 等字面值和标识符类型，行正文用逗号无额外空格连接；工作表与货币格式留作元数据，不再额外拼入行正文。

对现有 `082_excel-1.xlsx` 实测，上游行文本口径：表头关闭 622 行块、开启 609 行块。两种文本已分别保存在 `var/text-rechunk-20261116/excel1-default.txt` 与 `excel1-header.txt`。与 13 个工作表首行差值一致。

普通索引块在行文本后另行重新组织。当前 Python 按工作表隔离 packing、保留每行来源映射，尚未实现 Go 所有 unit 合并/overlap 规则。用户报告 Go 默认 TXT 为 302 块，当前未复现该数字：需要同版本配置与相同 TXT 输入作边界对照；不能通过任意调参宣称等价。

## 验证和资源

`var/text-rechunk-final-tests.log`：31 passed，0 failed/skip，退出码 0；含表头重复、父子图校验、相邻表切换、列数变化、代码围栏、转义竖线、空表头、超长行、Excel 定位与图片描述父子任务。241 条 warning 为既有 Pydantic 与 datetime 弃用告警。未做真实召回/生成正确率评测。

复用现有虚拟环境、reference 和冻结来源快照；未创建容器、数据库或索引副本，未修改 Gold，未部署 ECS。新增少量 Python 文件、测试、TXT 对照文本、manifest、日志及报告。保留之前的 Excel audit 与失败日志；首次重跑遇 Excel 打开时的 `~$` 锁文件，已排除锁文件后重跑，原 PermissionError 日志仍在 `var/text-rechunk-real-files.log`。资源空闲量见 `var/text-rechunk-20261116/real-file-audit/source-header-audit.json`，不是本任务独占磁盘变化。

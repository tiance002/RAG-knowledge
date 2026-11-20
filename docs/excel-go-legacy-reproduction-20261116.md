# Excel → TXT → Go legacy 的 Python 复现

## 已复现的结果

用户确认配置为 512 字符、80 重叠、legacy。复用已有 `var/qa500-ecs-sources-20261116/files/082_excel-1.xlsx`，SHA256 `6669cace46f876b0b09b67d91296c6a56e3268448c02f1a9e9dbceb4a80a43d9`。

- 首行表头关闭：622 原始行块，219114 个文本字符，最终 302 个 legacy 块。
- 首行表头开启：609 原始行块，796256 个文本字符，最终 1129 个 legacy 块。原文件首行是报表标题，开启后长标题成为字段名而反复出现，不宜对这批报表开启。
- `scripts/reproduce_excel_go_legacy.py` 已从正式 FileParser 入口生成结果，校验重叠去除后的文本可重建、normalized offset 与块正文相符、原行数正确以及关闭表头时最终块数为 302，退出码 0。

## 修复的差异

1. Excel 合并单元格必须先解除合并、填充，再保存到内存中的 XLSX 后交给 pandas 读取；直接读 DataFrame 再填值不等价。同为 622 行时，旧文本为 217923 字符，按上游保存重读后为 219114 字符；Python legacy 对两者分别生成 305 和 302 块。
2. 全部工作表行文本拼成整份文档后分块，不再人为按工作表隔离。块跨工作表时保留各自 source_locators，不把不同工作表行号合并成伪造的单表范围。
3. 递归按 `\n\n`、`\n`、`。` 优先级切分，分隔符独立保留；再合并文本单元。
4. overlap 按语义边界与优先级选择，最多保留指定预算；没有有效语义边界时允许零重叠。块边界和偏移按 Unicode 字符计算。
5. 识别代码、链接、数学、Markdown 表格等受保护单元；legacy 允许不可继续分割的单元超过 512 目标，绝对保护线为 7500。不是每个输出块严格小于 512。关闭表头此次最大块 7403 字符；开启表头最大 7499。
6. XLSX 行内原值、NA 字面值、空白字符、字段名与逗号拼接按前述核对修正。

Markdown 仍通过此前新增的表格表头生命周期路径处理，再生成带表头的普通/父子块。`go_legacy_splitter.py` 是文本 legacy 单元、递归与 overlap 的移植；不是 heading/heuristic/auto 全部策略移植，也不替代所有 Markdown 规范化功能。

## 验证范围与证据

`var/go-legacy-regression.log`：35 passed，0 failed/skip，退出码 0；241 个既有 Pydantic/datetime 弃用 warning。逐块内容、偏移、原始单元格行定位和哈希：`var/excel-go-legacy-reproduction-20261116/*-chunks.json`。文本和完整来源/配置统计同目录 `receipt.json`、`header-off.txt`、`header-on.txt`。原始复现日志 `var/excel-go-legacy-reproduction.log`。

302 与用户实测数量一致；本机无 Go 可执行文件，未执行 Go 与 Python 逐块内容对照，因此不宣称所有输入的跨语言完整等价。未做真实问答正确率、召回率评测，未部署 ECS、未替换正式索引。本次记录替代前文“302 尚未复现”的当前状态，之前原始日志继续保留。

复用现有源码参考、虚拟环境和来源文件，不创建数据库/索引副本。新增代码、测试、复现脚本、TXT、manifest、逐块证据和报告；这些为核对所需，未删除。复现期间 D 盘空闲从 133558407168 到 133554917376 字节，减少约 3.33 MiB；并发其他任务写入可能影响差值。

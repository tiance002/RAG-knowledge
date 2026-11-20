# Go / Python legacy 逐块正文与边界对照

## 结论与版本

固定 WeKnora 源码参考 commit `cbdcebabf48c5ee2d6868b370326aa8341703f62`。原始文件 SHA256、编译副本 SHA256 和适配说明见 `var/go-legacy-parity-20261116/source-receipt.json`。

真实编译并运行上游 Go `SplitText`，未用 Python 或替身产生 Go 预期结果。编译使用官方便携 Go 1.27.4，下载包 SHA256 与官方清单一致，清单见 `toolchain-receipt.json`。只移除与分块无关的 `ExtractImageRefs` 函数及 docparser import；SplitText、mergeUnits、header tracker、protected spans、semantic overlap 和 token 支持代码保留原文。编译项目 `oracle/` 与 `oracle.exe` 已保留。

两端调用前采用生产文本换行归一化，主要配置 512/80/legacy；另覆盖 64/8、128/32、4096/0 等边界配置。测试未配置 TokenLimit；不宣称 token-budget 或 auto/heading/heuristic 策略等价。

## 差异与修复

首轮 `attempt-1`：230 组，Go 正常退出；220 组匹配、10 组不匹配。Excel 真实 TXT 与普通文本一致；Markdown 表格在 512/80 下出现“数量相同但正文/边界不同”，小预算下数量也不同。

修复 Python legacy 核心：移植 Go headerTracker 生命周期、空白表头 pendingExtend、表间段落/列数变化状态清理、mergeUnits 中表头预算与去重、合成表头零宽位置、semanticOverlapWindow 对合成表头的屏障。转义竖线按反斜线奇偶数识别。

第二轮 `attempt-2`：230 组、3527 个 Go 输出块，逐块 Content/Start/End/Seq/ContextHeader 完全一致。

扩展最终轮 `attempt-3`：250 组、4772 个 Go 输出块，250 组匹配、0 组差异，Go 和比较命令均退出 0。包括现有 5 份 Excel 的首行表头开/关文本、Unicode、CRLF/CR、超长原子、代码/数学/链接保护、跨块表头、空白表头、相邻表、转义竖线、围栏表格及 200 组固定种子随机输入。真实 excel-1 关闭表头为 302 个块，不仅数量相同，302 个块的所有比较字段均相同；开启表头 1129 个块也逐项相同。

`attempt-*/inputs.jsonl`、`go-output.jsonl`、`python-output.jsonl`、`diffs.json`、`summary.json` 保留输入哈希、原始正文/坐标与逐项统计；失败轮次未被覆盖。

## 回归及适用边界

项目相关回归：35 passed、0 failed/skip、退出 0，原始日志 `regression.log`；241 个既有弃用警告保留。将 8 组真实 Go 输出冻结于 `backend/tests/fixtures/go_legacy_golden.json`，包括此前不匹配的表格例子。单独回归 12 passed，退出 0，日志 `frozen-go-tests.log`。

该结论是固定源码、固定调用参数和已覆盖输入上的实际跨语言一致，不是对任意版本/配置/输入的数学证明。高层 Markdown 解析器的格式规范化与父子图组织另有项目适配，不能把核心 SplitText 一致扩大为整个文档解析/索引/问答系统完全等价。未重做 Gold 评分、未部署 ECS、未修改正式索引或冻结 Gold。

## 资源与清理

复用已有 reference、原始 Excel/TXT 与 Python 虚拟环境；未创建数据库、索引或语料副本。新增官方便携 Go 工具链、构建缓存、Go 编译小项目和必要对照证据。最初 Docker API 不可用；首次 Go build 因工作目录错误失败，改到已生成的 oracle/go.mod 目录后编译成功，没有更改正式项目 Go 环境。

收尾递归删除工具链、压缩包和缓存的命令已核对目标均在任务目录并检查无相关运行进程，但自动审批策略拒绝了整个清理调用；因此本轮这些临时资源仍保留，不能报告已释放空间。保留编译源码、oracle.exe、所有输入输出与差异记录用于复核。清理前后当前目录体积和 D 盘空闲量另见资源清单。自动审批拒绝后没有换工具绕过删除。

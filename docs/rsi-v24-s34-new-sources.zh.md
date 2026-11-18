# S3.4 新来源获取与权限登记

本轮新增 3 份公开一手技术／监测文档的精确快照，来源为 Prometheus、OpenTelemetry 与 Python 官方仓库。仅完成来源获取、权限证据与字节冻结；**尚未准入独立证据家族，也未判定 FINAL-clean**。本子任务未打开旧 DEV/FINAL 题目、答案、证据或来源正文；现有家族映射仅作为元数据查看。没有收费调用、ECS 操作、数据库或索引变更。

元数据和逐文件下载／许可收据见 [s34-new-sources.json](evidence/s34-new-sources.json)。完整原文、LICENSE 与 NOTICE 仅留本地 `var/s34/raw`，不进入公开仓库。原始下载收据为本地 `var/s34/acquisition-receipt.json`。

| 来源 | 一手固定版本 | 本地文件 | 精确字节数 | SHA-256 | 许可 |
|---|---|---|---:|---|---|
| Prometheus Querying basics | `21b0d7eae184c9cf5558f01ce4c63399b54ccd32` | `var/s34/raw/prometheus-query-basics.md` | 27077 | `c918cc042050e8458372d9df468e9cacf9a98018b877851692557743164bf3f3` | Apache-2.0；保留适用 NOTICE |
| OpenTelemetry Metrics | `41477529b5ad5d600c36c57803b3df9cda150941` | `var/s34/raw/opentelemetry-metrics.md` | 9543 | `891112f4c81851850b2d732f68c3ba58adf7d5e0017df5233408324307e18951` | CC-BY-4.0；署名、来源及变更说明 |
| Python Logging HOWTO | `3ad9316217ba4e2788abc7d19f469762c6c38309` | `var/s34/raw/python-logging-howto.txt` | 50288 | `2a5c394ce442289573e689b7e68f3ea0e560bb69c12b6d5ff224f68a928d4d01` | PSF-2.0；文档代码另有 PSF-2.0 / 0BSD 双许可 |

所有原文均从固定 commit 的 `raw.githubusercontent.com` URL 直接保存，未转码、提取、翻译或拼接。Python 原文件为 `Doc/howto/logging.rst`；仅将本地扩展名改为 `.txt` 供现有文本解析器使用，原始字节不变。这不等于把 RST 转成结构化表格。下载时间、HTTP 状态、许可文件各自 SHA-256 和仓库 commit 时间已逐项记录；commit 时间不是该文档最后修改时间，后者保持 UNKNOWN。移动的官网页面仅作来源指针，不声称其渲染结果与固定源码一一对应。

## 权限与上游图

- [Prometheus 原始文档](https://github.com/prometheus/prometheus/blob/21b0d7eae184c9cf5558f01ce4c63399b54ccd32/docs/querying/basics.md) 对应同 commit 的 [LICENSE](https://github.com/prometheus/prometheus/blob/21b0d7eae184c9cf5558f01ce4c63399b54ccd32/LICENSE) 与 [NOTICE](https://github.com/prometheus/prometheus/blob/21b0d7eae184c9cf5558f01ce4c63399b54ccd32/NOTICE)。文中另有 Go 字符串语法、RE2、原生直方图规范及 Prometheus proposal 的引用边；引用目标没有作为新来源自动导入。
- [OpenTelemetry 原始文档](https://github.com/open-telemetry/opentelemetry.io/blob/41477529b5ad5d600c36c57803b3df9cda150941/content/en/docs/concepts/signals/metrics.md) 对应同 commit 的 [CC-BY-4.0 LICENSE](https://github.com/open-telemetry/opentelemetry.io/blob/41477529b5ad5d600c36c57803b3df9cda150941/LICENSE)。Metrics 指向同项目规范、资源与 temporality 内容，并包含 Hugo 生成表格依赖；该依赖未展开。
- [Python 原始文档](https://github.com/python/cpython/blob/3ad9316217ba4e2788abc7d19f469762c6c38309/Doc/howto/logging.rst) 对应同 commit 的 [LICENSE](https://github.com/python/cpython/blob/3ad9316217ba4e2788abc7d19f469762c6c38309/LICENSE)。其中第 62–67 行明确文档许可及文档代码的附加双许可。Sphinx logging API、handlers、config 与 cookbook 引用未展开；不是新的独立来源。

这 3 份快照在仓库和字节上确实不同，但不能据此认定统计独立或知识来源独立。Prometheus 与 OpenTelemetry 同属相关的可观测性生态，概念可能相互关联。完整引用／派生闭包、旧资料家族交集和模型历史暴露仍为 UNKNOWN，所有 `family_status` 保持 `UNKNOWN_NOT_ADMITTED`。当前获取代理已阅读来源及结构片段，因此这些资料已发生本轮开发暴露，不能再无条件称为全新封存 holdout。

## 结构与解析边界

- Prometheus 原始 Markdown 第 376–379 行有原生标识符规则表，第 430–462 行有条件与约束，第 574 行以后涉及 stale 数据和查询开销。仍需主代理执行现有解析器，确认这些内容保留到实际解析产物。
- OpenTelemetry 第 114–191 行有基数上限、溢出以及 delta／cumulative 条件说明；第 199 行只是 `signal-support-table` Hugo shortcode。生成的语言支持表并未下载，不得从此占位符编造表格值或作为已解析证据。
- Python 第 35–66 行及第 74–101 行是原生 RST 网格表，后文包含日志级别、过滤器与条件规则。纯文本解析可能保留 RST 符号，但结构化行列是否保留尚未验证。

本获取子任务没有运行解析器或评测，未报告解析成功率、Gold 准确率或性能指标。主代理应将真实解析命令、退出码和逐来源结果另行登记；公开的获取收据不能替代运行证据。

## 资源收尾与未解决项

复用 `E:/rsi-v24-s34` checkout、现有 PowerShell HTTP 下载与 SHA-256 工具。没有新虚拟环境、容器、语料副本、数据库或 Chroma 索引。新建 3 个原文快照、3 个 LICENSE、1 个 NOTICE 和 1 个本地 JSON 收据。原文共 **86908 字节**；`var/s34/raw` 本子任务开始为 **0 字节**，获取完成为 **135158 字节**，不含外层 JSON 收据。保留这些小文件是为了固定来源与许可复现；没有删除文件。父任务可能并行写入其他资料，因此整体盘符用量由主代理统一汇总。

7 次原文／许可／NOTICE 下载均返回 HTTP 200；文件 SHA-256 以本地实际字节计算。下载前查询的三个 GitHub 递归树均 `truncated=false`，未发现相关文档子目录另有覆盖许可。一次 NOTICE 控制台显示及一次旧映射元数据显示发生截断，完整文件保留；上述结论不依赖截断尾部。获取子任务的最终检查实际退出码为 0：7/7 文件 SHA-256 和字节数一致；3 份来源的 hash 各不相同；元数据 JSON 可读取；Git 确认 raw 与本地收据被忽略；`git diff --check` 为 0。主代理交付前仍需复核最终合并状态。

仍待解决：旧 DEV/FINAL 的不透明 hash 冲突核查、家族关系与暴露准入、实际解析保真、可用控制项／原子关系，以及候选集与冻结集的边界。当前允许范围仅为 S3.4 本地来源与解析研究，后续 DEV 用途需登记；FINAL 未获授权。

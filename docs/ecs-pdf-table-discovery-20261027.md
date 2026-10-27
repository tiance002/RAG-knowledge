# ECS 真实 PDF 表格补查与解析验证

日期：2026-10-27。基底提交：`f02269d`。本轮为用户要求自行检查 ECS 资料路径后的续做。

## 结论与修正

现有资料内同时存在真实文本表格和扫描表格，之前将“解析器没有恢复结构”表述为“缺少真实样本”不准确，现予以修正。两张表都在原来的三个 PDF 中，没有增加或替换评测语料。

| 来源 | PDF 页码 | 印刷页码 | 实际内容 |
|---|---:|---:|---|
| `11-21-16-Updated-Post-Election-Release.pdf` | 45 | 43 | 调查分组、选民加权比例、未加权样本量、抽样误差；有上下横线、无竖线 |
| `b3m5kaeqm2w8n4bwcesw-140602121350-phpapp02_95.pdf` | 13 | — | 红外与微波烹饪对照，彩色扫描图像内的双列表格 |

源 SHA 分别为 `8f49f93f1665fd5ff604e66ffed9649d8b18ce7f114ab43463aeac46cd3d00cb`、`43be9592854c7da10bf011335faec017b726ff2e23bb4ccae7e8fe88d7801f22`。

## 实际检查范围

- 对 `/opt/amrag`、`/opt/rag-s8`、`/opt/rag-p7-r1`、`/srv`、`/data`、`/mnt`、`/home`、`/root` 的应用数据范围做只读文件头检查，包括无扩展名对象。排除凭据、依赖树、缓存和本任务生成的实验副本。
- 实际检查38386个候选文件；按内容去重仍为3份PDF，共43个物理副本。
- 另检查65个归档包（约581MiB）内部的PDF候选；仍只有同样3份PDF，未发现额外原件，0读取错误。不解压覆盖业务目录。
- 用Poppler渲染候选页面并人工核看，不能将`find_tables`未恢复结构当作页面没有表格的证明。

## 本轮最小修复

解析器版本改为`pdf-page-router/v2`，默认仍为legacy，扫描表格仍需独立开关。

1. 文本少线表格：限定在真实上下横规则包围的区域，要求至少4条对齐数据行、可靠的多列粗体表头、列间空隙和正文不跨界；保留原生文字bbox。普通无边框猜测仍不准入，数字图表拒绝规则保留。标题、表注和未消耗原文不丢弃。
2. 彩色扫描表格：在保留原黑线网格路径的基础上，用连续边缘识别彩色网格，不要求线条绝对黑色。只有确认为矩形网格才分配已有OCR词。
3. 整页OCR漏掉的空表头/短单元格，最多对32个空单元格做一次本地裁剪OCR。保留原始裁剪文字、被排除的网格竖线符号及来源位置；不补造值、不调用云模型。

## 真实文件对照

前组为`f02269d`原解析器，后组为v2，同一份完整PDF的相同页面、相同语言包/150DPI。

| 样本 | 前组结构行 | 后组结构行 | 列数 | 结果 |
|---|---:|---:|---:|---|
| 报告第45页 | 0 | 10（表头+9条数据） | 4 | PASS |
| 扫描课件第13页 | 0 | 5（表头+4条数据） | 2 | PASS |

报告总样本`1,254`与`3.4 percentage points`、Trump voters样本`533`、Clinton voters样本`587`正确关联回对应行列。扫描表头恢复为`Infrared Radiation`、`Microwaves`，最后一行右侧恢复出`Sent 1 cminto the food`。其中`cminto`仍有OCR粘连，保留`SCAN_TABLE_OCR_UNVERIFIED`，结构通过不代表每个字符或数字已获语义验收。

实际命令：

```powershell
.\.venv\Scripts\python.exe -X utf8 var/pdf-discovery-20261027/run_scan.py
.\.venv\Scripts\python.exe -X utf8 var/pdf-discovery-20261027/archive_scan.py
.\.venv\Scripts\python.exe -X utf8 -m pytest backend/tests/test_pdf_pipeline.py -q --disable-warnings --basetemp var/tests/pdf-table-final-r4
.\.venv\Scripts\python.exe -X utf8 var/pdf-discovery-20261027/compare_real_tables.py real-tables-comparison-final.json
.\.venv\Scripts\python.exe -X utf8 var/pdf-discovery-20261027/run_ecs_validation.py
```

上述命令退出码均0。Windows17passed；ECS隔离目录同源码17passed，真实两页对照`REAL_TABLE_COMPARISON_EXIT 0`。本轮模型调用0、生产服务替换0、业务索引重建0。

期间保留了彩色网格首次单测失败及诊断：同色竖线交点打断水平边缘，已通过1–3像素交点桥接修复；原黑线网格回归保持。初次PDF Skill路径误用和临时调试表达式语法错误均已纠正，未影响正式服务或写入原件。

## 证据和边界

私有证据：`var/pdf-discovery-20261027/inventory.json`、`archives.json`、`real-tables-comparison-final.json`、`real-tables-ecs.json`及`render/`截图。ECS隔离验证目录：`/opt/amrag/experiments/pdf-table-discovery-20261027`。

后置核验：正式库仍6KB/11779files/37271chunks，后端源码及PDF源Hash与前次一致。

本次发现/解析修复阶段只补解析和来源定位，未重跑Embedding、Rerank或回答模型；此阶段v2质量为NOT RUN。随后用户授权的独立v2评测已完成，见[完整检索与回答报告](pdf-v2-evaluation-20261027.md)：表格4题4/4->4/4、证据页Recall1->1，没有新增质量收益；12题整体10/12->8/12。之前`f02269d/v1`成绩仍保持历史标识。正式切换未执行，不能把结构恢复写成整体正确率提升。

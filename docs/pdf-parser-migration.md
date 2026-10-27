# PDF 解析迁移与验证

日期：2026-10-27。基底：`b5ceb463a433193a908e1d814b221c855f8717d0`；分支：`codex/pdf-parser-migration`。

**后续修正**：自行补查ECS后，在原报告第45页和扫描课件第13页找到真实表格，并补齐v2解析与17项测试。此前“缺少真实表格样本”的判断不准确。见[真实表格补查报告](ecs-pdf-table-discovery-20261027.md)。下文8题模型成绩属于`f02269d/v1`，不是v2的质量成绩。

## 实现范围

- PDF 每页记录页码、原生文本字符数、图片并集覆盖率、最终路线、状态、OCR DPI、警告、解析器与 PyMuPDF 版本。
- 少量/无文字且图片占比达到阈值的页面走整页本地 OCR；有可用文本层又含较大图片的页面保留原生文字并做局部图片 OCR。中文/英文语言包缺失或 OCR 失败不会静默回退云端。扫描页失败不会把整份文件标成完整解析成功。
- OCR 使用旧项目已实际采用的 PyMuPDF/Tesseract `eng+chi_sim`、150 DPI；迁移后的项目不依赖旧项目目录、数据库或服务。
- 文本表格使用 PyMuPDF `find_tables(strategy='lines')`，恢复来源行、单元格、页码和 bbox；跨界正文、重叠表格、未确认合并单元格、无边框猜测和无可靠标签的纯数字网格保留原文。
- 扫描表格为独立开关：只处理可确认的规则矩形栅格，将已有 OCR 词按坐标分配到单元格。无边框、合并单元格、旋转、未确认网格保持 OCR 文本，不声称恢复结构。OCR 数字和表头语义仍需要复核。
- 解析清单按源文件 SHA 与清单 SHA 保存，独占创建，不覆盖历史。精确匹配的持久化 chunk 可以回读位置；人工编辑后的文字不会继承不匹配的坐标。索引路径和普通/Agent 引用传递 `source_metadata`。无生产数据库 Schema 变更。
- 图片名称使用源 SHA、页码、原始块序号，避免不同切块配置造成图片路径碰撞。

## 配置与运行

配置相对后端工作目录。将 `eng.traineddata`、`chi_sim.traineddata` 独立放到新项目的 `storage/tessdata/`，不要依赖旧项目路径。语言包属于运行资产，不进入 Git。

```dotenv
PDF_PARSER_MODE=enhanced
PDF_TESSDATA_DIR=./storage/tessdata
PDF_OCR_DPI=150
PDF_SCANNED_TABLES_ENABLED=false
PDF_AUDIT_DIR=./storage/pdf_manifests
```

代码及 `.env.example` 默认 `PDF_PARSER_MODE=legacy`。最终有限质量门未通过，候选需要显式设置 `enhanced`；以上配置示例用于候选隔离验证。不要对原有生产索引原地重建。正式切换应先保留服务源码、配置、数据库和 Chroma 恢复材料。当前任务没有替换 ECS 正式服务。

本次只有定向测试用独立本地 venv，完整应用安装/启动不是本次验证结论。运行依赖沿用现有 PyMuPDF、NumPy，没有新增 OCR 云服务或模型框架。

为避免将克隆版直接覆盖 ECS 后丢失既有修复，本地同时保留了已部署的 `count_tokens`、独立图片命名、真正 `/rerank` API，以及纯文本回答模型使用缓存 Caption 的兼容补丁。这些是基线对齐，不作为本轮解析收益。

## 冻结评测协议

1. ECS `/opt/amrag` 的实际 parser/retrieval/rerank 源码作为基线，不能用 GitHub 克隆版代替：ECS 已有 `count_tokens`、真正 `/rerank` 端点和 VLM Caption 补丁。
2. 相同三个 PDF 源 SHA、相同切块 `custom/500/50`、相同已有 Caption 文本。不给任一组额外生成 VLM 描述。
3. 同一组八个 MMLongBench-Doc 题目，Gold 与公开原始行逐条核对。输入推理进程的文件不含答案键。六题有公开证据页标注；两题没有，不把它们计入 Recall 分母，但保留回答质量评估。
4. 相同 Xing4.0-29B、BGE-M3、BGE-reranker-v2-m3，Hybrid0.7/0.3、候选50、Rerank20、邻接窗口1。显式回答上限2000、temperature0.7，两组一致；这不是历史网页原始输出的精确重放。
5. 同时记录融合后、重排后的 source Recall@10 与 page Recall@10。三个文档的小语料容易得到很高的 source Recall，因此必须同时检查证据页和实际回答。
6. 第一次候选暴露数字图表误入表格的回归，结果完整保留。修复采用问题无关的数字网格准入规则；最终候选重用字节相同且已完成的八个基线答案，只重新运行候选八题，避免重算基线或删除失败结果。
7. 每个独立实验有输入 SHA、代码 SHA、模型身份、请求账本与次数上限。Provider 失败/未知立即停止，不自动重发。生产 SQLite 只读；模型凭据仅留在进程内；隔离 SQLite/Chroma 不写入凭据。

## 覆盖与限制

- ECS 上传目录去重只有三份 PDF，共134页：89页纯扫描幻灯片，45页报告（含一页图文混排）。报告中的被拒数字网格不能当作有效表格样本。
- v1的表格结构验证主要使用生成的PDF fixture；后续v2已在同一批ECS原件的报告第45页和课件第13页补做真实表格对照，仍不代表覆盖所有表格类型。
- 原 `prod-mmlongbench-doc` KB 缺少45页报告，虽然三道题需要它。两组都加入相同三份文件建立新的配对基线；不把历史库缺文档造成的差距算成解析提升。
- Windows 首次运行被旧日志的 GBK/emoji 编码异常污染，整轮标为无效并保留。有效运行使用 UTF-8。
- 一次八题结果只能支持这个有限样本的观察，temperature0.7仍有生成随机性，不能外推全量准确率或声称统计显著。

## 实际命令和证据

本地有效解析输出：`var/pdf-ab-20261027-final/`；对应日志 `var/pdf-ab-20261027-final.log`。

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest backend/tests/test_pdf_pipeline.py -q --disable-warnings --basetemp var/tests/pdf-r9
.\.venv\Scripts\python.exe -X utf8 scripts/compare_pdf_parsing.py --baseline-parser var/ecs-reference/parser.py --corpus var/corpus --tessdata var/tessdata --output var/pdf-ab-20261027-final
git -c core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol diff --check
```

最终本地与 Linux 定向测试均14 PASS。原始 `git diff --check` 会将该上游仓库的 CRLF 报为行尾空白，使用显式 CRLF-aware 检查，未为消除警告重写整库。

## v1真实对照结果（历史基准，f02269d）

| 指标 | 基线 | 最终候选 |
|---|---:|---:|
| Source Recall@10（6题） | 1.0000 | 1.0000 |
| Evidence-page Recall@10（6题，宏平均） | 0.8333 | 0.8333 |
| 标准答案核心结论正确（8题） | 8/8 | 7/8 |
| 空正文 / length | 0 | 1 |

融合后与重排后的上述两项 Recall 在此样本中相同。正确率为本代理阅读冻结 Gold 和实际答案后的语义复核，不是词面匹配、在线 Judge 或负责人人工验收；统计的是问题核心结论，不能冒充逐事实忠实性。113题基线明确给出整体5层及3个隐藏层，按整体层数计正确。115题回答0与Gold一致，但有限检索片段不足以独立证明全篇没有老虎图像。

候选114题空正文，`finish_reason=length`、completion_tokens=2000；上下文仍含 Kaggle/Cat vs Dog 信息。不能把它全部归因检索失败或推断出隐藏推理细节。两组模型/参数固定，但temperature0.7和单次生成保留随机性，因此未证明解析变化导致准确率下降，也未证明收益。

初轮实际16次生成、664个Embedding输入、45次Rerank；最终候选8次新生成、604个Embedding输入、22次Rerank，并复用8个基线答案。所有真实请求均保留记录，没有删除失败记录、重发未知请求或重置旧项目账本。费用金额未核算，不宣称免费。

最终私有结果：`var/ecs-reference/paired-results-final.jsonl`、`paired-state-final.json`；首轮结果以`-r1`独立保留。最终ECS实验目录`/opt/amrag/experiments/pdf-parser-20261027-final-r2`。结论：工程检查通过，有限质量门 **FAIL / NOT ACCEPTED**；真实复杂表格样本覆盖仍不足，未执行正式切换。

ECS后置检查：正式库仍为6KB/11779files/37271chunks；正式后端源码Hash和PDF源Hash一致。隔离库`custom_models`为0，未复制模型密钥入库。最终交付代码在Linux通过14项测试与7个受影响模块的import冒烟。TXT/DOCX/Excel/CSV解析方法AST未变。

私有题目、原文、答案、服务日志、语言包和账本全部在 Git 忽略的 `var/` 或 ECS 隔离实验目录；不要公开上传这些文件。

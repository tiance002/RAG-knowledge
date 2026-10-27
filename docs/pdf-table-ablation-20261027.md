# 修复后的 v2 表格开关消融：ECS 原始 PDF

## 实验范围与修复

用户授权先修界面截图误入表格、扫描表头和单元格 OCR 失真，再对同一批 ECS PDF 比较表格开启/关闭；正式服务不切换。

修复版本 `ca65fa0980d0609d6f2f5b2e5c59bfdc554f6abe`，内部解析器版本`pdf-page-router/v2.1`（v2修订）。两组均为此版本：before表示tables_off，after表示tables_on。没有使用旧解析器作本轮对照，也不复用之前的回答。

- 扫描网格要求至少3行；不足时保留页面OCR。已复现的猫狗课件41页Jupyter界面由2个伪表格片段变为0。此规则是保守准入，不等于通用界面语义识别器，真实两行小表也会退回原始文字。
- 扫描页不再因第一行含字就默认它是表头。只对紧凑列标签覆盖较长数据的情况推断；不明确时用C1/C2，所有原始行保留。物理16页第一条Radiowave数据不再传播成每行字段名；物理13页仍保留Infrared Radiation/Microwaves列名。
- 原来仅重识别空单元格，无法修复已填入的乱码。现在最多64格，逐格裁边、补白、按局部背景对比处理，再进行至少300DPI本地OCR；失败时整张表回退页面原文。原页面OCR、单元格原始输出与来源坐标均保留，不用词典或模型编造替换。物理16页Microwaves/Aerial/Interference的完整内容已恢复。
- 仍保留`SCAN_TABLE_OCR_UNVERIFIED`：例如I/l歧义、个别字符混淆和`cminto`粘连未完全消除，不能声称扫描内容逐字无误。

Windows定向21passed（最终24.98秒），ECS隔离定向21passed（pytest14.36秒，外层计时15.11秒），均真实退出0，包括三处原件回归。测试与真实模型评测分开报告。

额外跨平台检查确认源码与两个语言包Hash相同；三处真实页表格输出严格比对首次退出1，仅物理16页一处`in/In`大小写不同。大小写归一化后所有行一致，原始严格失败及差异保留在`linux-parser.json`/`linux-parity-summary.json`，不将其改写成严格逐字符一致。两组正式消融输入均使用同一Windows冻结输出，不混用平台结果。

物理16页另做20单元格的原图转录审计：修复前/后归一化字符编辑错误为345/742与4/742（46.50%→0.54%），整格匹配11/20→17/20。归一化规则为小写、提取字母数字token并以单空格连接，逐格Levenshtein距离加总；不是未经说明的原始CER。转录由本代理核对原图，非负责人验收或独立盲审。原件印有`electromagnetic aves`与`300m/s`，按原图保留，不将源文件印刷/知识错误“纠正”为OCR答案。私有`ocr-audit.json`保留逐格原值、转录与计算；该指标不代替检索/回答指标。

## 冻结协议

三份源PDF共134页，与上轮SHA相同。全量解析在Windows的源Hash核验副本上完成；ECS隔离目录验证相同解析代码及真实页，检索/回答在ECS运行。两组基础文字/页面OCR逐页`page_text_sha256`全部相同；页面路由、原始字符数、图片覆盖率、解析器版本逐页一致。原有89张图片描述从同一索引快照冻结，输入文本一致，不新增VLM调用。

两组均enhanced模式、custom500/overlap50/semantic=false；页面OCR同为eng+chi_sim/150DPI。唯一功能开关为`PDF_TABLES_ENABLED`，扫描表格子开关在两组都设为true。关闭组保留原始文字/OCR；开启组恢复native/scanned表结构，其中300DPI单元格OCR属于被测表格功能，不是共同页面OCR被替换。因此本轮测试的是“表格解析管线（含单元格OCR）”的整体收益，不能进一步拆成纯行列序列化与局部OCR各自的效果。

| 项目 | 两组相同配置 |
|---|---|
| LLM / Embedding / Rerank | XingChenAGI/Xing4.0-29B / BAAI/bge-m3 / BAAI/bge-reranker-v2-m3 |
| Hybrid | vector0.7、BM25 0.3、top_k50、threshold0 |
| 重排 / 扩展 | Rerank20、邻接窗口1 |
| 生成 | temperature0.7、max_tokens2000、stream=false、max_retries0 |
| Prompt模板SHA | `7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94` |
| 题集 | 与上一轮完全相同的8道原题+4道原件核验表格题，Gold文件字节相同 |

关闭413片段，开启438片段；开启含25个表格片段：报告45页10个、物理13页5个、物理16页10个，猫狗课件没有表格。待Embedding唯一文本含12个问题共455个。输入SHA`557d80a52c532ac2140c316a22501ef181ff652058deff63c95c57ec7f677882`。

两组24个回答全新生成，按题奇偶交替先后顺序；固定上限1024个Embedding输入/96次Rerank/24次回答。模型ID/端点Hash、Prompt模板和生产检索Python源码调用前强制检查，preflight退出0且零调用。Provider失败/未知立即停止，无自动重发。

ECS评测目录`/opt/amrag/experiments/pdf-table-ablation-20261027-r1`；解析测试目录`/opt/amrag/experiments/pdf-table-fix-20261027`，均与正式SQLite/Chroma/服务分离。模型密钥只读入进程，不写隔离数据库。

## 结果

本轮COMPLETED，真实子进程退出0，耗时1222.167秒。122个HTTP请求全部成功：40次Embedding请求（455个输入）、58次Rerank、24次回答。UNKNOWN0，无失败重发。执行与隔离核验PASS，未观察到Recall或核心正确率提升。

| 分层 | 指标 | 表格关闭 | 表格开启 | 变化 |
|---|---|---:|---:|---:|
| 原8题 | 证据页Recall@10（6题） | 0.8333 | 0.8333 | 0 |
| 原8题 | 核心正确率 | 6/8（75%） | 6/8（75%） | 0 |
| 表格4题 | 证据页Recall@10（4题） | 1.0000 | 1.0000 | 0 |
| 表格4题 | 核心正确率 | 4/4（100%） | 4/4（100%） | 0 |
| 合计12题 | 证据页Recall@10（10题） | 0.9000 | 0.9000 | 0 |
| 合计12题 | 核心正确率 | 10/12（83.3%） | 10/12（83.3%） | 0 |
| 合计12题 | 空正文 / length | 2 / 2 | 2 / 2 | 0 |

融合后与重排后的上述Recall相同；Source Recall@10两组均1.0000。指标是这三份PDF的固定证据页口径，不与SciFact/MIRACL文字检索成绩混算。

| 开销 | 表格关闭 | 表格开启 | 变化 |
|---|---:|---:|---:|
| 表格4题prompt_tokens | 51,401 | 37,250 | −27.5% |
| 原8题prompt_tokens | 120,867 | 119,480 | −1.1% |
| 合计12题prompt_tokens | 172,268 | 156,730 | −9.0% |
| 合计completion_tokens | 12,655 | 13,843 | +1,188 |
| 三份PDF解析耗时 | 114.607秒 | 154.108秒 | +39.501秒（+34.5%） |
| 索引片段数 | 413 | 438 | +25（+6.1%） |

Prompt Token是Provider实际返回量；解析耗时为本次Windows顺序运行计时，非多轮性能基准。生成输出Token反而略增，局部OCR与表格分析增加CPU和索引开销，未核算计费价格，不能将输入Token降幅等同净费用降幅。Embedding报告104,685Tokens；生成合计prompt328,998/completion26,498；Rerank未返回可汇总Token用量。

上轮“旧解析器→整套v2”的表格题输入Token下降54.8%，不能全部归因于表格；本轮同修复版v2开关消融测得27.5%。原回归题的输入Token几乎不变，与表格题相比，更能说明紧凑表格证据的收益集中于表格查询。

口径沿用上一轮：宏平均证据页Recall@10，记录融合后和重排后；原题6道有页qrels、表格4道均有，2道不可回答/全篇否定题只进入回答正确率。核心正确率由本代理按冻结答案逐题语义复核，空正文与矛盾最终结论计错；不是词面包含评分、在线Judge或负责人人工验收。

## 逐题判定与解释

| ID | 关闭 / 开启正确 | 复核依据 |
|---|---|---|
| 110 | 是 / 是 | 男性81%高于女性78%；两组上下文与完整system Prompt Hash相同。 |
| 111 | 否 / 是 | 关闭组空正文length；开启组给出Clinton高于Trump，与Gold核心一致，但以均值解释中位数的严谨性仍不作满分认定。 |
| 112 | 是 / 是 | 均正确表示种族细分不可回答；上下文Hash相同。 |
| 113 | 是 / 否 | 关闭组正确回答整体5层、含3隐藏层；开启组空正文length。 |
| 114 | 否 / 否 | 两组均空正文length，top10均未命中Gold第34页。 |
| 115 | 是 / 是 | 均明确回答0，符合Gold；有限片段不足以独立证明全篇图像否定结论。 |
| 116 | 是 / 是 | 均给出无线电波与微波。 |
| 117 | 是 / 是 | 均给出镜子与光亮物体。 |
| 118 | 是 / 是 | Trump voters：533、±5.1个百分点。 |
| 119 | 是 / 是 | Clinton voters：587、±4.9个百分点。 |
| 120 | 是 / 是 | 水分子或脂肪分子。 |
| 121 | 是 / 是 | 1cm。 |

四道表格题，开启组正确结构行全部进入重排top10，答案直接引用了行列值；没有出现“结构恢复了但未进入检索”的断链。但关闭组原生文字、页面OCR和已有图片描述已经覆盖这些答案，4题存在准确率天花板，所以不能声称它们证明新增正确率收益。

111的改善与113的回归互相抵消，二者都不是新增表格探针。temperature0.7单次生成及上下文排序/内容差异仍可能影响结果，不能只挑111当收益，或只挑113当表格损害的因果证据。所有4次length正文为空、completion2000，均保留计错，不为求分数调高Token上限或重跑。

## 证据与边界

私有目录`var/pdf-table-ablation-20261027/`保存全量解析、真实页修复输出、Windows/Linux测试日志、冻结输入和后续模型账本；Git忽略原件、密钥、答案及日志。题集仅12题、表格题只覆盖2张表，不能据单次temperature0.7生成声称统计显著或推广到全部PDF；物理16页修复由解析回归检查覆盖，没有新增定向题目来改变本轮分数。

正式服务默认仍legacy，未执行发布、切换或生产索引重建。此前v1和迁移前后评测报告保持历史结果，不覆盖。

## 实际命令、退出码与版本

以下命令真实退出0：

```powershell
.\.venv\Scripts\python.exe -X utf8 -m pytest backend/tests/test_pdf_pipeline.py -q --disable-warnings --basetemp var/tests/pdf-ablation-final
.\.venv\Scripts\python.exe -X utf8 scripts/compare_pdf_parsing.py --baseline-parser var/ecs-reference/parser.py --corpus var/corpus --tessdata var/tessdata --scan-tables --table-ablation --output var/pdf-table-ablation-20261027/parses
.\.venv\Scripts\python.exe -X utf8 scripts/prepare_pdf_table_ablation.py
.\.venv\Scripts\python.exe -X utf8 scripts/score_pdf_table_ablation.py --directory var/pdf-table-ablation-20261027
```

ECS真实调用（预检零调用，执行由launch.py记录实际退出码，两条均退出0）：

```bash
/opt/amrag/.venv/bin/python /opt/amrag/experiments/pdf-table-ablation-20261027-r1/run.py --preflight
/opt/amrag/.venv/bin/python /opt/amrag/experiments/pdf-table-ablation-20261027-r1/run.py --execute
```

隔离Linux测试由`/opt/amrag/.venv/bin/python /opt/amrag/experiments/pdf-table-fix-20261027/validate_ecs.py`执行，退出0。额外严格跨平台逐字符检查退出1（前述唯一大小写差异）；随后基于既有输出的casefold比较退出0，未重新运行或更换模型输入。新评测脚本py_compile及CRLF-aware Git diff检查退出0。

ECS原始证据包`var/pdf-table-ablation-20261027/result-bundle.tar.gz`，SHA`2da061d8708b575f43180d5d5d848094f650fa4a88660ac3a11a36cd9f4e8739`，本地/远端一致；`remote-evidence/`包含执行脚本、冻结输入、请求账本、24份回答及上下文、退出码和日志。下载输入与执行脚本分别与本地冻结文件逐字节一致。

评分：`semantic-review.json`、`scores.json`；片段/上下文审计：`evidence-audit.json`；OCR审计：`ocr-audit.json`；测试：`tests-final.log`、`tests-ecs.log`、`validation-ecs.json`。正式前后`identity-before.json`/`identity-after.json`中全部Python源码Hash、PDF Hash、模型列表及表计数相同：6知识库/11,779文件/37,271片段。隔离custom_models=0，未落库模型密钥。

修复commit`ca65fa0`；Git push/tag与正式切换均NOT RUN。本次完成的是修复与评测，不自称负责人验收或发布通过。若后续继续测准确率，最小下一步是增加原始文字/旧图片描述确实缺失或错位的独立表格难例，并继续保留原12题回归；当前4题已经两组全对，重复同题不足以证明更广泛收益。

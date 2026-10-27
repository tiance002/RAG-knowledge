# ECS 原始 PDF：v2 解析迁移检索与回答评测

## 范围与冻结协议

用户授权在 ECS 原始 PDF 上继续评测，不切换正式服务。候选解析代码固定为 `b681671cdbfcb02e6a6e8ad6b9213c47eb4be100`，包括页面路由、本地 OCR、文字表格恢复及彩色扫描表格恢复。此实验只在 `/opt/amrag/experiments/pdf-v2-eval-20261027-r1` 建立隔离 SQLite/Chroma，正式 SQLite 只读。

同一三份原件共134页：68页猫狗课件、21页物理课件、45页选举报告。SHA-256 由 ECS 原件核验，两组语料完全相同。基线为当前 ECS 的旧解析器，候选为 v2（扫描表格显式开启）。两组切块参数均 custom/500/overlap50、semantic=false；旧图片描述从同一生产索引快照冻结，不新增 VLM 调用。基线277片段，候选441片段。

固定模型与参数：

| 项目 | 两组共同配置 |
|---|---|
| 回答模型 | XingChenAGI/Xing4.0-29B |
| Embedding | BAAI/bge-m3 |
| Rerank | BAAI/bge-reranker-v2-m3 |
| 检索 | Hybrid，vector0.7 / BM25 0.3，top_k50，threshold0 |
| 重排及上下文 | Rerank20，邻接窗口1 |
| 生成 | temperature0.7，max_tokens2000，stream=false，max_retries0 |
| Prompt | 与上一轮逐 AST 相同；模板 SHA `7e1789b762b8a238103edee37d435d71ef688bf53c0fdea3fba8f64683973a94` |

模型 ID、端点 Hash、生产 Python 源码 Hash 在调用前强制校验。具体上下文因解析变化而不同，因此每次完整 Prompt Hash 可以不同；模板相同。每题两组重新推理，奇偶题交替先后顺序，不复用历史回答。固定请求上限：1024个Embedding输入、96次Rerank、24次回答；Provider 失败/未知状态立即停机，不自动重发。

冻结输入 SHA：`a666856bd2b3ac3026b41eb5fa5f09a71c46cc51ed71ecac8f230cffcce9b01a`。离线输入共636个唯一待嵌入文本（含12个问题）。Gold 答案与推理输入分离。

## 题集与口径

原有8题保留原始 MMLongBench-Doc Gold；其中6题有证据页，2题为不可回答/全篇否定题，不进入证据页 Recall 分母，但进入回答正确率。新增4题在模型调用前，依据原始 PDF 渲染图核对答案并冻结；它们是本代理标注的定向探针，不是官方新增测试集。

| ID | 证据 PDF 页码（1起算） | 问题核心 | 冻结正确答案 |
|---|---|---|---|
| 118 | 报告45 | Trump voters 样本量及误差 | 533；±5.1个百分点 |
| 119 | 报告45 | Clinton voters 样本量及误差 | 587；±4.9个百分点 |
| 120 | 物理13 | 微波使哪些分子获得动能 | 水或脂肪分子 |
| 121 | 物理13 | 表内微波进入食物深度 | 1 cm |

分别统计原8题、表格4题与合计12题。Recall 同时保留融合后与重排后的 top10；主指标为证据页召回宏平均：每题命中的相关页数/该题全部相关页数，再对有 qrels 的题平均。跨解析器不使用不稳定 chunk ID 做直接相关性比较。Source Recall 只判断正确文件是否出现，三文档语料容易饱和，不等同完整证据召回。

回答按冻结 Gold 核心结论进行语义复核，逐题保留判定理由；额外事实错误单独记录。该复核不是负责人验收，也不是逐事实忠实性满分。空正文即错误，`finish_reason=length` 单列，不选择性重跑。

若答案前文暗示正确结论，最终又拒答或给出矛盾结论，按不明确回答计错，不使用关键词包含即判对。例如全篇否定题即使提到“没有老虎”，若又结论为“无法确定数量”，需要记录其矛盾，不能仅凭词面匹配通过。

## 结果

运行完成，子进程退出码0，耗时756.706秒。137个HTTP请求全部成功：51次Embedding请求（636个输入）、62次Rerank、24次回答。UNKNOWN0，无失败重发，无历史答案复用。执行协议与隔离核验 **PASS**；“提高召回与回答正确率”的收益结论 **未成立**，本轮候选回答正确率下降。

| 分层 | 指标 | 迁移前 | v2 | 变化 |
|---|---|---:|---:|---:|
| 原8题 | 证据页 Recall@10（6题） | 0.8333 | 0.8333 | 0 |
| 原8题 | 回答正确率 | 6/8（75%） | 4/8（50%） | −25pp |
| 表格4题 | 证据页 Recall@10（4题） | 1.0000 | 1.0000 | 0 |
| 表格4题 | 回答正确率 | 4/4（100%） | 4/4（100%） | 0 |
| 合计12题 | 证据页 Recall@10（10题） | 0.9000 | 0.9000 | 0 |
| 合计12题 | 回答正确率 | 10/12（83.3%） | 8/12（66.7%） | −16.7pp |
| 合计12题 | 空正文 / length | 1 / 1 | 2 / 2 | +1 / +1 |

融合后与重排后的上述 Recall 在本轮相同；Source Recall@10在10道有相关页的题中两组均1.0000。不能将这三文档实验的page级指标直接与SciFact/MIRACL的原始chunk/document口径混算。

存在一个需要保留的效率信号：生成输入Token总量从276,874降至156,216（−43.6%）；表格4题从86,324降至39,038（−54.8%）。两组都回答正确的这些表格题，候选提供了更紧凑的上下文。该数字是Provider返回的prompt_tokens，不等于总费用节省：未核算计费价格，且这是整套v2与旧解析方案的比较，不是单独表格开关的因果消融。两组生成合计prompt433,090/completion26,537；Embedding报告123,627Tokens，Rerank未返回可汇总Token用量。

## 逐题复核与证据

| ID | 前 / 后正确 | 核查要点 |
|---|---|---|
| 110 | 否 / 是 | 前组说男女没有高低，后组正确比较男性81%与女性78%。两组上下文都包含这些数字，不能归因为前组完全缺证据。 |
| 111 | 是 / 否 | Gold为Clinton；后组反称C−高于C、Trump更高，再拒绝中位数结论。前组核心结论正确，但使用均值解释中位数的严谨性仍有保留。 |
| 112 | 是 / 是 | 均正确说明种族细分不可回答。 |
| 113 | 是 / 否 | 前组正确给出整体5层、其中3隐藏层；后组空正文、length2000。 |
| 114 | 是 / 否 | 前组正确给出Kaggle Cat vs Dog；后组空正文、length2000。两组top10均未命中Gold第34页，但前组扩展上下文仍提供答案。 |
| 115 | 否 / 否 | 前组空正文；后组先说没有老虎，再说无法确定数量，最终结论矛盾。 |
| 116 | 是 / 是 | 均给出无线电波和微波。 |
| 117 | 是 / 是 | 均给出镜子和光亮物体。 |
| 118–121 | 全是 / 全是 | 样本量/误差两题与水脂肪分子/1cm两题均正确。 |

115题为评分敏感项：若采用宽松“提到没有老虎就算正确”的判法，v2为原题5/8、总计9/12，仍低于基线6/8与10/12，仍不能支持质量提升。有限检索片段也不足以独立证明全篇没有某种图像；此处核心Gold一致性不等于全篇证据完整性。

四道表格题中，v2正确的结构行均进入重排top10，并在答案中被使用。例如119题直接引用`Group=Clinton voters | Unweighted sample size=587 | Plus or minus …=4.9 percentage points`。但旧版报告纯文本已经按行保留533/5.1与587/4.9；旧版物理课件图片描述已完整写出水/脂肪分子和1cm，故两组均可回答。这证明结构恢复接通了检索，尚未证明新增准确率收益。

错误的UI表格没有出现在本轮111、113、114题的重排top10，不能据此断言它造成了这几次回答失败。113/114候选上下文分别37,446/38,159字符，输入20,284/20,053Tokens；同题基线上下文更长且答对，不能简单解释为“v2上下文更长导致失败”。111属于已有证据下的错误推断；length只能确认接口正文为空且输出用满2000Tokens，不能凭此还原模型隐藏推理过程。

## 解释边界

这次直接比较旧解析器与整个 v2 解析方案，包含 OCR 与表格变化；固定模型、Prompt 和检索参数可以排除这些显式参数变化，但不能完全拆分每种解析机制的独立因果贡献。表格题需进一步看命中片段、行列关联与答案，不能仅凭多生成了表格片段宣称收益。

全量 v2 解析审计发现：猫狗课件41页的 Jupyter 界面误入扫描表格（2行）；物理课件16页是真实术语表，但首条数据被当作表头、部分 OCR 内容失真。候选共27个表格片段：报告45页10个，物理13页5个，物理16页10个，猫狗41页2个。本轮保留全部原始输出，不在测评过程中修复换结果。物理13页旧版图片描述已经含完整表格与两个新题答案，故恢复结构未必增加原先不可检索的信息。

temperature0.7保留生成随机性；每题单次生成、4题表格且只来自2张表，不能声称总体提升或统计显著。定向表格探针与原回归题分层报告，避免新增容易受益的题掩盖回归。生产 PDF 库原先缺少这份45页报告；本轮两组都补齐三份语料，是新的配对实验，不直接对比原网页历史得分或 SciFact/MIRACL 文字语料分数。

## 命令与证据

私有输入、原始答案、上下文及请求账本保存在 `var/pdf-v2-eval-20261027/`，不提交 Git。评测脚本不保存模型密钥。正式默认仍为 legacy，未执行正式服务切换、生产索引重建或发布。

实际命令（均退出0）：

```powershell
.\.venv\Scripts\python.exe -X utf8 scripts/compare_pdf_parsing.py --baseline-parser var/ecs-reference/parser.py --corpus var/corpus --tessdata var/tessdata --scan-tables --output var/pdf-v2-eval-20261027/parses
.\.venv\Scripts\python.exe scripts/prepare_pdf_v2_eval.py
.\.venv\Scripts\python.exe -m py_compile scripts/run_ecs_pdf_eval_v2.py scripts/prepare_pdf_v2_eval.py scripts/score_pdf_v2_eval.py
.\.venv\Scripts\python.exe -X utf8 scripts/score_pdf_v2_eval.py --directory var/pdf-v2-eval-20261027
```

ECS实际调用（首条预检零调用，执行由launch.py启动并记录真实退出码）：

```bash
/opt/amrag/.venv/bin/python /opt/amrag/experiments/pdf-v2-eval-20261027-r1/run.py --preflight
/opt/amrag/.venv/bin/python /opt/amrag/experiments/pdf-v2-eval-20261027-r1/run.py --execute
```

机器协议检查实际通过：Prompt模板AST相同，改动temperature被拒绝，重复qid被拒绝。此次没有改产品实现，17项解析测试属于上一轮b681671验证，**本轮NOT RUN**，不冒充重跑通过。

原始ECS证据包：`var/pdf-v2-eval-20261027/result-bundle.tar.gz`，SHA `c72f6d45180c9810a478a7061ec05d6abc8a84d9c2cb1d06b10848a22abe7d02`，本地与远端相同。解包为`remote-evidence/`，包含原始run.py/input.json/state.json/results.jsonl/retrieval-results.jsonl、24份context、日志及退出码。评分文件为`semantic-review.json`、`scores.json`，片段审计为`evidence-audit.json`。输入与执行脚本下载后均逐字节核对一致。

生产前后`identity-before.json`/`identity-after.json`中的所有Python源Hash、PDF源Hash、模型列表和表计数相同：6知识库、11,779文件、37,271片段。隔离库custom_models=0，未落库模型凭据。正式切换 **NOT RUN**，Git push/tag **NOT RUN**。

## 风险与下一个最小任务

维持legacy正式路线。建议将UI误入表格、扫描表格表头及OCR失真作为v2启用前必须处理的质量问题；本次不批准豁免或宣布验收。下一步先修这些已复现的准入/结构错误，再冻结更多原始PDF难例复测。若要证明“表格本身”而非整套解析的独立收益，需要增加同一v2下表格开/关的消融对照；本轮没有运行这项实验，也没有调高回答Token上限来改变既定协议。

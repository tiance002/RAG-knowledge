# Langfuse Judge 校准样本与历史调用审查（2026-10-30）

新建校准 Dataset、8个答案回放、Cloud逐字段回读及标签隔离 **PASS**；本轮 Chat、Embedding、Rerank、VLM、Judge 调用均为 **0**，新数据上的评分准确率和调用延迟 **NOT RUN**。旧 E1 页正文由用户现场确认 **PASS**；三个变量映射在现有 Evaluator 的保存配置中验证 **PASS**。UI 自动化工具仍为 **BLOCKED**，没有冒充工具读取或点击成功。

## 交付对象与样本

- [新 Dataset：rag/judge-calibration-v1](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/datasets/cmvughww302viad0ebji6pmlg)，8个 ACTIVE Item。
- [输入回放 Experiment：judge-calibration-input-replay-20261030-v1](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/experiments/results?baseline=f9176ba89f64dc83)，8个 Item、0 Scores。
- 最终策展预期账本：`var/judge-calibration-20261030/calibration-ledger-v2.jsonl`。
- 完整逐题审阅稿：`var/judge-calibration-20261030/calibration-review-v2.md`。

8个样本复用3个原问题，组成针对同一问题的对照答案：**2个真实历史答案、6个构造答案**。构造答案全部为 `synthetic_answer=true` / `source_type=SYNTHETIC_CURATED`，不冒充历史模型生成。

最终候选预期分布：**PASS 4 / FAIL 2 / PARTIAL 1 / REVIEW_REQUIRED 1**。这里是策展预期，不是本轮 Judge 的输出，也不是声称全部已获真人审核。C01沿用原用户核心PASS；构造样本标签仍需用户定稿。C08是人工复核流程状态，不直接作为Judge的终态类别或准确率分母。

| ID与评测Observation | 来源 | 最终策展预期 | 覆盖内容与理由 |
|---|---|---|---|
| [C01](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/ef1cc026ca785457af629b8534e5decd?observation=615f5ca10fab1049) | 历史 | PASS | 报告完成比例7%；原答案与SSE完全一致；附带金额来自既有资料审查 |
| [C02](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/849b4640ea43568310d69fce20e676fd?observation=8afa9d2cf1201684) | 构造 | FAIL | 将7%改成70%，核心数值错误 |
| [C03](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/524a0b64566078455089d0112472582e?observation=4b06b1402d123649) | 构造 | FAIL | 把工程完成比例解释为预算支出比例，字段和统计口径混淆 |
| [C04](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/aad486db7227313c1447d404a8488137?observation=3ab057df5f92464c) | 构造 | PASS | 北京时间比UTC快8小时，属于与原Gold同义的表达 |
| [C05](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/d18ef4e4bfd1cd0f71aa0c8e9da45312?observation=44ee6884bd6baea1) | 构造 | PASS | 抛光金属板与化妆镜，分别实例化shiny object与mirror |
| [C06](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/70f49ad6fec3b222ceb60ef68c26d506?observation=c9c35e7fea31c9e0) | 构造 | PARTIAL | 只回答镜子，缺少另一个必答例子，未否认其存在 |
| [C07](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/c2c5737059be9b32a5d91be4d43aa1fb?observation=76525f3cd27750db) | 构造 | PASS（核心维度） | 7%核心正确；额外编写的“下周五竣工并终验”缺乏冻结材料支持，须另列支持性问题 |
| [C08](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/c9bc2ec94a5df6abc744046fb0f02228?observation=04db8ba606f99d67) | 历史 | REVIEW_REQUIRED | 原人工核心PASS保持不变；其余UTC/GMT、伦敦时间、跨日边界补充断言如何计入核心评价，须先定口径 |

C07初稿把支持性与核心正确性混算为PARTIAL。读取现有Judge提示词后，发现它明确评价核心问题，遂仅在独立标签账本v2中改为核心PASS。初稿 `calibration-ledger.jsonl` 与 `label-revision-v2.json` 留存修订证据，Cloud从未保存这些预期标签，问题、Gold、回答、原人工结论均未改变，也没有重放答案或修改Judge。

C07不代表其额外陈述获得支持，也不证明现实世界必然相反。尤其不能以“Gold中没有某信息”直接推导“该信息虚假”：稀疏Gold只覆盖核心字段。支持性评估需要冻结原证据和独立口径；本组样本保留这一边界，避免误训练核心Judge。

## 来源、权限与防泄漏

只复用此前用户明确允许进入现有美国区Langfuse项目的5题来源子集，没有引入新原文。使用到的3个原问题为 office50:011、PDF row-0958、中文UTC问题。原Gold逐项原样保留，包括数组形态及PDF Gold内部的列表字符串；未重新生成或修订Gold。

上传前dry-run检查：8个Dataset Item规范化UTF-8 JSON合计 **7,252 bytes**，8个评测Input/Output合计 **3,824 bytes**，不是HTTP/OTLP线路字节数。密钥、邮箱、身份证号、手机号、绝对路径模式均为0。既有历史答案中的项目金额属于先前已明示并获准上传的范围；构造答案不引入新增私人资料。扫描不等于完整授权审计，权限依据是原已批准来源及本轮明确的测试数据创建请求。

每题的question、reference_answer、answer、expected_verdict、review_reason、source_type以及答案哈希保存在独立本地校准账本。Cloud Dataset的input只有case_id、qid、question，expected_output只有原reference_answer，metadata记录来源、synthetic标志和哈希。**预期标签、覆盖类别、审核理由不传入Cloud Dataset、实验输出或评测Observation**。

评测Observation Input仅为`question`和`reference_answer`，Output仅为`answer`。回放为`execution_type=CALIBRATION_REPLAY`、`real_model_run=false`，没有将Gold送给被测RAG。SDK官方Dataset Experiment Runner读取新Dataset并返回保存的答案，`evaluators=[]`、`run_evaluators=[]`；每题3个SPAN节点，未创建Generation/Embedding/Evaluator节点。新Experiment和Item Scores均为空。

创建与回放前，项目Evaluation Rules均返回空列表；现有Evaluator虽标记active，但没有Rule assignment。本轮未创建、保存、修改或测试任何Judge/Evaluator，也没有重发历史UNKNOWN。

Cloud逐字段回读确认8/8问题、Gold、答案与本地完全一致，synthetic来源标志、答案哈希正确。旧`rag/e1-judge-calibration-v1`的5条Item ID、input、expected_output、metadata与旧快照逐项相同。原E1账本SHA-256仍为`a4bf3478616fe2a2c4d2cf8818cf33d31f3996c3dffdebd77b5094774b60a09a`，Judge只读快照SHA-256仍为`79581cc4961936d09ea47277542c2dff1b5cf0ac3975851078265820856ea70e`。

## UI验收与浏览器诊断

用户本轮在普通“用户配置1”窗口打开旧E1中文`eval.review_case`后，明确回复“**三个字段均能读到正文**”。证据保存为`ui-acceptance-user-confirmed.json`，属于用户现场确认；不是工具截屏验证。

只读读取现有`Custom Evaluator`（ID `7c151e9a-327c-4e29-8cde-4e5214bb1cc3`、version 1）的持久配置，确认三个映射已保存：

| 变量 | Source | JSONPath |
|---|---|---|
| question | input | $.question |
| ground_truth | input | $.reference_answer |
| answer | output | $.answer |

这证明已保存的映射有效，不声称本轮自动点击了UI下拉框。现有Evaluator没有被本轮修改。

UI自动化最初误选InPrivate，用户指出该窗口不加载扩展，随后按指正改用“用户配置1”。普通窗口及用户手动打开的Langfuse页仍被工具拒绝读取，但工具错误声明会停止本轮，后续拒绝可能只是停止状态延续，不能作为普通浏览器配置损坏的独立证据。

按用户三轮排查上限完成：技能/运行时加载正常；当前桥接路径、原生消息宿主注册及可执行文件存在；未发现禁用它们的Edge企业策略。客户端日志记录`cua_repl`启动时`MCP startup failed: 系统找不到指定的路径。 (os error 3)`，旧版本连接条目有失效路径，但尚不能证明就是根因。结论是桥接/运行时版本切换问题有嫌疑，**不是已证明的skill或浏览器配置根因**。没有删缓存、改注册表、改扩展权限、降低安全校验或擅自重启客户端。后续可由用户在保存工作后重启客户端及普通Edge窗口再做一次新轮次连接检查，本轮不再尝试。

## 当前GLM-5.3-Flash Judge参数与实际历史调用

当前保存的Evaluator `modelConfig`仅有`provider=siliconflow`、`model=zai-org/GLM-5.3-Flash`。历史Langfuse托管测试Observation的`modelParameters={}`，不能据此确认实际发送时的temperature、max_tokens、reasoning_effort或其他服务端默认值。**当前托管Judge的有效采样/推理参数为UNKNOWN**，不是“已确认没有这些参数”。

另一个独立来源——旧E1 SDK Judge runner的请求构造代码——设置`stream=false`、`temperature=0.7`、`max_tokens=4096`，没有显式设置`reasoning_effort`、`enable_thinking`、`thinking_budget`、`response_format`。这些是旧runner的可验证构造参数，不能当作当前UI托管Judge的实际参数。旧SQLite只保留input/prompt哈希、原响应、状态和用量，未保存完整出站HTTP请求体和开始/结束单调时钟。

只读查到两次真实的Langfuse托管GLM测试，均发生于2026-10-29晚间，非本轮发起：

| Trace | 墙钟延迟 | 输入Token | 输出Token | 合计 | TTFT |
|---|---:|---:|---:|---:|---|
| [6858d6e…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/6858d6e6451eab48789ff5517878505e?observation=0128023b62bd494f) | 17.373秒 | 482 | 431 | 913 | 未记录 |
| [feaebb8e…](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/feaebb8e002ca0967fd52e18337df822?observation=9c84e6f31e683c5d) | 65.980秒 | 439 | 1,623 | 2,062 | 未记录 |

两条合计输入921、输出2,054、总计2,975 Token。样本仅2条，不能作为稳定p95或证明某个推理强度导致差异；输出较长与延迟较长只是观察。查询同一环境还返回其他模型的失败/超时测试，已按model排除，未混入GLM统计。

## 旧SDK Judge的Token、延迟和准确性

最终只读SQLite有24次预约：**4 COMPLETED、16 INVALID、1 FAILED（429）、3 UNKNOWN**。20条HTTP 200响应有Usage，合计输入 **114,455**、输出 **29,111**、总计 **143,566** Token。其中reasoning_tokens为 **21,034**，已包含在输出Token中，占已报告输出约72.25%，不能再次加到total。3条UNKNOWN的用量、是否计费和所有金额仍UNKNOWN。

通过`SHA256(qid)`与Cloud任务Input、SQLite状态和任务Output状态逐项关联到20个返回响应的SDK任务跨度，含16个INVALID。对应任务墙钟耗时：

- 最短12.494秒；中位数29.2975秒；均值38.91955秒。
- p95（nearest rank）95.383秒；最长116.690秒。
- 分母仅20个完整HTTP 200响应，不含429、UNKNOWN及后续被守护停止的空转节点；这不是全请求延迟SLO。

任务跨度包括预约、HTTP、解析和SQLite写入，不是纯模型推理时间。UNKNOWN关联的部分跨度约120秒，但其他同qid STOPPED跨度接近0秒或没有结束时间；不能以任务时间推断旧UNKNOWN必由超时导致，也不能把43个实验包装节点当作43次真实模型调用。纯HTTP耗时和TTFT未记录，仍UNKNOWN。

原24道用户参照中，只有14道产生可比较的精确PASS/FAIL核心标签；14/14与用户一致，**全部为人工PASS**。另外10道不可比较，唯一人工FAIL没有合法可比二元标签。因此可比覆盖率14/24，错误识别Precision/Recall无法定义，不能宣称总体准确率100%。本轮重新从原响应严格解析核对，未改写原INVALID/UNKNOWN状态。新8个校准样本没有调用Judge，准确率和新延迟均NOT RUN。

## 硅基流动推理强度控制的证据边界

[上游GLM-5.3-Flash模型卡](https://huggingface.co/zai-org/GLM-5.3-Flash/blob/main/README.md)说明模型支持`reasoning_effort`的`low/high/max`，上游未传参数时默认max。[硅基流动当前Chat API文档](https://docs.siliconflow.cn/docs/api/chat-completions-post)列出`reasoning_effort`的high/max适用型号为DeepSeek-V4相关型号及Pro/zai-org/GLM-5.2，没有明确列出GLM-5.3-Flash。文档对`enable_thinking`、`thinking_budget`的通用描述也不能证明此型号支持。

结论：**上游支持已证实；硅基流动上该具体型号的参数接受、透传、有效取值与默认值仍UNKNOWN**。不能把上游默认max当成硅基流动实际默认，也不能断言low已经能降低平台调用延迟。本轮没有调用模型做参数探测，也没有更改任何参数。

## 校准实施建议（未执行）

1. 先人工定稿这8个候选预期，尤其确认C06的PARTIAL和C08的评价范围；C08未解决前从自动准确率分母排除。C07核心正确性与支持性分开，不能因Gold缺少补充细节就直接判幻觉。
2. 现有Evaluator虽在提示词中定义PASS/PARTIAL/FAIL/UNKNOWN，结构化输出配置却是NUMERIC 0..1，`scoreValueInstructions`和`scoreReasoningInstructions`仍是英文占位文字。后续由用户明确映射（如1/0.5/0）及UNKNOWN的弃权方式，或另建类别输出版本；UNKNOWN不得伪造为0。**本轮不保存或修改它**。
3. 用户明确授权评分后，先小规模测试2—3个有分歧价值的样本，再测试已定稿样本；按版本记录Prompt、模型、参数、样本哈希和单次请求身份。不要重发既有UNKNOWN来补齐分母。
4. 同时报告三分类混淆矩阵、各类Recall、弃权率与格式合法率；整体平均数不能替代错误样本召回率。现8样本仅3个原问题，是对照校准集，不代表真实业务分布，后续需扩充独立题目，按qid划分测试组，避免同题变体泄漏。
5. 延迟应同时记录调用前后单调时钟、服务端request_id、TTFT、输入/输出/推理Token、完成状态。记录失败和UNKNOWN，分别给出成功条件延迟及所有请求的结果分布；不要使用本轮毫秒级答案回放延迟冒充Judge延迟。
6. 在平台确认GLM-5.3-Flash参数支持后，再用相同样本、同版Prompt、串行且有预算的独立对照实验测试推理强度与输出上限，每次只改一个参数。本轮不进行该测试，也不因单条慢响应直接归因到推理强度。

## 执行退出码、原始证据与未解决项

| 检查 | 退出码 | 结果 |
|---|---:|---|
| prepare dry-run | 0 | 8样本、权限与来源核对、字节数及扫描 |
| upload | 0 | 新Dataset与8条Item创建；没有覆盖旧E1 Dataset |
| replay | 0 | 官方SDK产生8个评测Observation；0模型/评分调用 |
| 初次CLI verify | 1 | GET fetch failed/空stdout，原失败日志保留 |
| 首次SDK verify | 1 | 校验旧快照时snake_case/camelCase字段名不兼容；实际Cloud内容已回读，原失败保留 |
| 最终verify-sdk | 0 | 规范化旧快照字段后，8题正文、标签隔离、Scores及旧Dataset完整核验 |
| 最终单元测试 | 0 | 8/8 passed，0 skipped；真实Cloud验收另做，不用替身代替 |
| 历史Token/准确率/延迟只读审查 | 0 | 原SQLite与真实Cloud跨度/Usage逐项关联，明确分母 |

已审查实际退出码、逐项测试、原始完整stdout/stderr和JSON，未把执行成功当作评分正确。成功upload/replay/最终SDK回读的stderr为空属预期；unittest正常结果位于stderr，含8条ok、Ran 8 tests、OK。Cloud CLI失败和SDK快照兼容性失败未被覆盖成成功，原日志保留。`cloud-sdk/`包含最终真实回读；`final-audit.json`汇总证据和限制。

本轮新增独立脚本` scripts/judge_calibration_dataset.py`、测试`backend/tests/test_judge_calibration_dataset.py`和上述证据产物。没有修改正式RAG算法、生成Prompt、检索配置、线上服务或现有Judge，没有重跑原评测集、切换生产环境、提交、推送或部署。原Trace、Scores与E1账本未覆盖。

最终状态：Dataset/回放/来源冻结一致/标签隔离 **PASS**；旧E1正文 **PASS（用户确认）**；三映射 **PASS（保存配置）**；UI自动化 **BLOCKED**；具体平台推理参数支持和未捕获的有效请求参数 **UNKNOWN**；新Judge准确率、评分和延迟测试 **NOT RUN**。

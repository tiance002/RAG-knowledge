# RSI 取消次数上限后的诊断、调整与继续测试 — 2026-11-15

本次按用户最新指示取消了五轮停止条件，复用同一血统和同一本 USD5 费用账本继续测试，没有重置历史。当前累计 **13 轮训练流程、37 次模型请求**。本轮已完成预先声明的两个预算规则比较及一次独立来源核查，得到一个实验状态为 **VERIFIED** 的候选；生产未修改。

## 无收益与弃权的原因

1. 方向与材料不匹配。此前去重补丁依赖 `duplicate_key`，它要求有几何定位的 PDF；原20题实际送达43个成员均为Markdown，符合该条件的成员0个、同源精确文本重复对0个。这份补丁不改变任何选择结果，NO_GAIN有直接结构证据。
2. 角色输入不足。旧投影只有43成员/20seed/23nonseed等总数，没有实际格式、长度分布、辅助函数适用范围与完整AST限制。预算Proposer反复ABSTAIN，随后又出现乘法、空列表max、私有字典下标写等拒绝原因。补充的是白名单结构统计和代码接口，不是题目、答案、必要事实原文或qid。
3. 基座存在seed偏差。初版构造器/核验器要求每条必要事实位于seed。seed-only在原20题可减少46.47%字符并保留这些断言，不能据此推断邻接扩展无用。固定邻接seed压力版本保留原题、原断言、原分块与来源字节，只按“同源下一块，否则上一块”移动seed；父代20/20事实仍完整，而seed-only在20/20题丢失必要事实。该候选已隔离，禁止进入封存核查或父代池。
4. 真实继续运行发现了原先测试未覆盖的工程问题。配置的原始字节SHA与规范化JSON SHA误比较，导致一次完整回放被安全中止；Windows默认编码使一笔返回usage丢失；更深测试目录暴露了临时文件名过长；旧sealed文件有空DACL。均保留失败记录，修复后重新检查。

分析脚本 `scripts/analyze_rsi_v22_no_gain.py` 只使用已有回放输入和捕获，不读取Gold/holdout。原始结构：20seed内容8012字符，23nonseed内容6335字符，其中8个nonseed不超过200字符；这些数字只用于提出结构假设，不证明任何成员不必要。

## 方案调整

- `LineagePolicy` 不再因5轮或3轮无收益而停止。3轮可信无收益只产生 `DIAGNOSE_AND_ADJUST` 提示；仍保留事实、来源、AST、未知副作用和数据隔离门。
- 原费用账本仍为 `var/rsi-v22-20261115/task-model-budget.sqlite`，USD5、无预留、次数无限、最后一次可超额。达到累计金额上界后不再发起请求。
- 冻结 `neighbor-stress-v1` 作为额外硬门，源文件硬链接复用、原事实不改，20/20父代先验回放通过。后续原训练fixture候选必须同时通过这个压力版本，不能只靠“保留seed”过关。它与训练同源，不是独立样本，不增加统计分母。
- 比较两种预先声明的预算规则：保留全部seed，并保留内容长度至少为最长seed的1/2或3/4的nonseed，原顺序不变。控制器给出结构实验意图，LLM生成补丁；结果不描述为模型自主发现通用规律。
- 在首次独立核查前固定sealed20：保留原有10题，按既有来源池顺序补10题；一份新增来源发生标题冲突，在执行前拒绝并固定替换。未按候选表现筛题。新版本19份原文复用，只补取1421字节；所有新增下载合计15671字节。
- 低成本模型仍承担诊断、策展和审计；连续出现代码约束错误后，仅编码Proposer使用Sol。未使用Astra。

## 实际结果

|策略/阶段|完整配对|事实与来源|字符变化|判定|
|---|---:|---|---:|---|
|旧去重方向|20/20|PRESENT|0|NO_GAIN|
|seed-only，旧训练|20/20|原断言PRESENT|−8165，−46.47%|训练曾过门；随后压力失败隔离|
|seed-only，邻接压力|20/20|20题必要事实丢失|−13013|FACT_LOSS，拒绝|
|1/2预算，训练|20/20|PRESENT|−1289，−7.34%|VALIDATED|
|1/2预算，压力|20/20|PRESENT|−247|额外硬门通过|
|3/4预算，训练|20/20|PRESENT|−2116，−12.04%|VALIDATED；选定候选|
|3/4预算，压力|20/20|PRESENT|−972，−4.97%|额外硬门通过|
|3/4预算，独立来源sealed20|20/20|PRESENT|−1685，−13.77%|VERIFIED|

选定候选：`56975c80c66ea9d5f99b6f72c1002fc0c12f2775bfc8ad69e27925bf528f4b23`。

训练字符17569→15453；压力19540→18568；sealed12238→10553。压力是同20个来源的额外布局检查，不能把三行合并成60道独立问题。各阶段原始捕获均重新跑来源跨度核验，A/B必要事实全部PRESENT，无单题回归。指标只针对冻结的必要来源断言和实际捕获context的Unicode字符长度。

独立核查：20个来源文件簇中14改善、6持平、0回归；精确双侧簇级符号检验、Holm固定家族2，调整p=0.000244140625，两个比较均通过。父代与可信基线同为R0，复用了一次物理比较，但仍按两个逻辑比较校正；不是两套独立样本。训练的固定三槽家族未缩小，confidence不参与裁决。训练重复使用仍仅是自适应探索证据。

核查版本在看任何核查结果前冻结；选定候选后不再改补丁、阈值或题集。已用sealed冻结hash禁止用于另一候选重复核查，防止继续调参追逐这个结果。轮次数不限不等于重复使用已揭盲核查数据。

## 工程修复与测试

- 分别核对执行回执配置原始字节SHA、归档配置字节SHA、规范化JSON与清单SHA，未删掉身份门。
- HTTP worker使用二进制/ASCII传输，先原子保存返回回执再输出，避免Windows语言环境导致usage丢失；无隐式重试，schema修复计费。
- 唯一丢失usage请求保留UNKNOWN，按已归档请求的输入限制和最大输出量，事后计入保守上界USD0.00256；不是金额预留、不是实际用量，也未重发。无法建立上界或副作用不明的请求仍阻止新调用。
- unit探针显式捕获候选异常。`substantial-r2`的max空输入错误原被标为INFRA，已另存分类纠正记录，原日志不覆盖。
- 原子写临时文件采用短basename，避免重复64位内容hash导致Windows长路径失败。曾有联合检查8失败/164通过，原日志保留；修复后的最终命令为179/179通过。
- 旧sealed文件Owner为当前用户，但DACL为空且禁止继承；恢复了原私有父目录（当前用户+SYSTEM）的继承，17文件，未增加其他账户，原字节未改变。
- 完成轮次恢复同时重验原20题和压力20题，不再启动远端capsule或模型；实际调用数重验前后均37。
- 默认父代池返回本次VERIFIED候选，采用共同训练集字符数15453作为fitness，没有把sealed文档长度拿来给父代排名。

最终命令：

```text
D:/RAG knowledge/.venv/Scripts/python.exe -B -m pytest rsik/tests backend/tests/test_rsi_candidates.py backend/tests/test_rsi_feedback.py backend/tests/test_rsi_decision.py backend/tests/test_rsi_business_source.py -vv --tb=short
```

最终 `final-tests-r2.log`：179 collected、179逐项PASSED、0失败/错误/跳过，退出码0，原始日志完整且分母一致。测试包括替身/记录捕获/模拟晋级，不把它们当真实独立核查。真实核查另有20题逐项捕获及receipt。六个受保护文件仍与入场指纹完全一致；未改原evaluator、FIXED或AST白名单。没有Git提交或生产发布。

## 经验与审计

Curator最初因投影缺少“代码与已完成验证”的明确绑定而ABSTAIN；增加仅供策展/审计角色使用的代码hash、有限action和完成状态证明后，产生一条受控Delta。该证明禁止发送给Proposer。确定性代码限制为当前实际实现的3/4规则及OTHER/same_source范围，两个不同实际来源簇、真实VERIFIED回执与兼容指纹满足后进入ACTIVE。

条目 `f2289ba3a3b04e20a2129f0eda12c101a4c99e98f8ea2a1c7d95c647cf223e42`。私有证据别名已逐题绑定实际候选run的feedback observation并内容寻址归档，不只保存一个不可追溯别名。实际父代投影能取回1条兼容规则，但未执行后续使用它的模型提案；pending/helpful/miss/harmful均0，没有追记helpful或因被拒绝记harmful。Meta只读，不改评测和裁决。

## 费用

累计37次模型请求：Luna34、Sol3、Astra0。根据返回usage与冻结公开单价的已知部分估算为 **USD0.05019505**，一笔未知usage的费用上界 **USD0.00256**，累计保守上界 **USD0.05275505**。无pending、无无界未知费用、无预留。服务商实际账单仍UNKNOWN，不将估算写成对账实扣。

沿用原账本，不是另开USD5；所有格式修复、不完整响应和故障请求均在记录中。模型价格引用见旧验收报告及冻结`PRICE_VERSION`；本次按实际已接收usage记录cache/output，不依字符数推算模型API费用。未要求用完5美元，本轮收束依据是预定比较与首次独立核查已完成，没有重新引入轮次停止条件。

## 资源与交付

复用本地venv、冻结业务checkout、ECS既有镜像与只读系统Python/venv、已冻结来源；没有复制生产数据库或Chroma索引，没有重建依赖或停止正式服务。压力原文20份全部硬链接复用。sealed扩充首次下载14250字节，再补1421字节，总新增原文15671字节。

续做证据目录逻辑大小约37.67MB（硬链接会重复计入逻辑字节）；新增10个pytest临时目录12776679字节，已归档3571630字节ZIP并逐文件SHA校验。原目录保留：此前批量及固定单目录递归删除均被自动审批以`blocked by policy`拒绝，未改用其它方式规避。本地删除0。失败准备、未知usage、被隔离候选、逐题结果、冻结Gold和来源版本继续作为审计依据保留。

远端最终本任务标签容器0、任务目录0；临时capsule代码已清理，既有镜像/依赖和其他服务保留。盘级前后见`resources-final.json`：D可用132946100224→133884932096字节；E可用46949195776→34479022080字节。存在其他并发活动，这些盘级变化不归因为本任务；本任务只按自身目录计量。

实际RSI仓库：`E:/codex_workspace/2026-11-03/task/rsi-work`。本报告对应证据根 `var/rsi-v22-20261115/continuation-20261115/`：

- `no-gain-structural-audit.json`、`revised-search-preregistration.json`。
- `seed-only-stress-r1/`、`substantial-sol-r1/`、`three-quarter-sol-r1/`。
- `selected-candidate-before-holdout.json`、`holdout20-verification-restricted/receipt.restricted.json`。
- `verified-metrics-final.json`、`fees-final.json`、`completed-recovery.log`。
- `verified-curator-r2/`、registry中的手册evidence内容寻址对象。
- `final-tests-r2.log`、`final-test-audit.json`、`implementation.diff`、`resources-final.json`。

## 结论边界

本次建立的是一个通过训练、同源压力及独立来源核查的字符压缩实验候选；没有证明真实检索/生成答案质量提升。来源语义标签为既有AI审阅加自检，不是HUMAN Gold。独立来源按hash/标题/8gram筛查，不能完全证明语义独立或未在既有模型训练中出现。基础布局为新构造，压力版本也不是历史真实检索。当前数据以Markdown/OTHER为主，不能外推PDF、表格或全部生产问题。

VERIFIED与生产批准分离。本次生产未修改，既有隔离批准/CAS/回滚仍只称模拟；没有将本地签名当真人生产批准。

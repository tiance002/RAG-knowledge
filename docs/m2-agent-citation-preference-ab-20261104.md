# M2 Agent 引用修复与云端 Qwen 偏好 A/B 记录（2026-11-04）

## 范围和版本

分支 `codex/pdf-parser-migration`，HEAD `57f9972b8477b0c4aaf7fac329b911420fafc95e`，工作区原有未提交变更保留。真实运行采用当前工作区冻结源码、云端 `Qwen/Qwen3.5-4B`，Embedding 请求 `BAAI/bge-m3`、Rerank 请求 `BAAI/bge-reranker-v2-m3`。原始私有请求、回答、冻结材料和配置仅保存于本机 `var/preferences-acceptance/`，未推送或切换正式服务。应用入口为 `ChatService.chat_stream`，不是网络 HTTP/SSE；Langfuse Cloud 本轮未回读。实际供应商账单为 UNKNOWN。

## 修复与限制

历史原始请求显示，季度题的下一季度截止、Excel 的千为单位数据、PDF 的总体比例均已送到 Agent，但模型分别改写成错误截止时间、增加无证据的美元、或将总体比例扩展到未证实的群组。历史生成结果没有行内引用，来源编号没有被应用层删掉；因此是模型生成和提示契约问题，不能说是检索未召回。

`backend/app/services/agent_workflow.py` 为 Agent 增加自然语言提示：引用工具结果中已有的 `source_id`，事实旁使用 `[n]`；只使用证据支持的细节、单位与时间；只知道检索片段缺失时不宣称整份文档缺失。没有新增严格答案校验、自动改写、阈值、重试或额外模型。`backend/tests/test_m2_agent_citation_regression.py` 验证提示和实际编号送达、重排及多轮映射，但替身不能证明真实回答质量。该修复真实效果为 PARTIAL：6 条 Agent 核心答案均覆盖 Gold，正文含行内引用 4/6；来源支持 PASS 3、PARTIAL 1、完全缺失 1、只有末尾来源列表 1，忠实度 PASS 3、PARTIAL 3。

## 冻结对照与逐题复核

三道开发题 × 普通/Agent × 默认 A / 长期详细偏好 B，共 12 个身份。所有身份启动，11 个返回回答，流程普通 B 为 HTTP 503、生成及用量 UNKNOWN；保留原始请求，不重发。每臂独立 Hybrid 检索，权重 0.7/0.3、top_k 10、Rerank top 10、上下文窗口 0；Agent 重排前候选预取 20。普通 Excel 与 Hotpot 对照的模型请求证据和问题相同（除 B 偏好段）；三个 Agent 配对的片段集合相同但顺序不同，因此不能声称其模型上下文完全相同。所有 5 个有回答配对的 B 最终实际请求均包含详细偏好，A 不包含。样本每臂一次，只作开发验证，不做因果或生产总体结论。

| 题目和路径 | A 默认 | B 详细偏好 | 引用与补充审查 |
|---|---|---|---|
| 季度报告，普通 | 核心 PARTIAL：5/6，遗漏每个自然季度的频率 | UNKNOWN：HTTP 503，无回答 | A 的 [1] 与 [3] 对应实际送达的 Word 原文；B 不能评分。 |
| 季度报告，Agent | 核心 PASS：6/6；忠实度 PASS | 核心 PASS：6/6；忠实度 PARTIAL | A 的 [1]、[2]、[3]、[5]、[6]、[10] 可追溯到实际来源。B 没有编号引用，且对 RFI 作无证据的释义。 |
| Alabama 图表，普通 | 核心 PASS：108149.892，单位为千 | 核心 PASS：相同数值和单位 | 两组引用 [1] 的数据行与数值对应，但都增加了资料未独立证明的美元符号；B 的全国合计脚注还可能造成歧义，全文 PARTIAL。 |
| Alabama 图表，Agent | 核心 PASS；正文有 [1] | 核心 PASS；仅末尾列 `来源：[1]` | 两组均把未确定的币种写成美元；A 引用不能支持该补充，B 没有行内引用，全文和引用支持均未完全通过。 |
| 莱顿瓶和摩擦仪发明人，普通 | 核心 PASS：Pieter van Musschenbroek | 核心 PARTIAL：结论给出 Gold 姓名，但开头错误地称是两个人 | A 的 [1]/[2] 可支持。B 还将发明字母 G 的证据误说成发明容器，把电影误说为人物；全文 FAIL。 |
| 莱顿瓶和摩擦仪发明人，Agent | 核心 PASS；引用 [1]/[2] 支持 | 核心 PASS；引用 [1]/[2] 支持 | 两组忠实度 PASS；回答均使用英文，未遵循系统中文指示，记为格式问题。 |

完整逐题、实际请求正文、证据片段和引用映射在 [Agent 独立复核](../var/preferences-acceptance/m2-agent-preference-ab-20965c41264e4759b79309df82d74b60/independent-agent-answer-review.json)、[普通模式独立复核](../var/preferences-acceptance/m2-agent-preference-ab-20965c41264e4759b79309df82d74b60/independent-chat-review-v1.json) 与 [调用及可比性审计](../var/preferences-acceptance/m2-agent-preference-ab-20965c41264e4759b79309df82d74b60/independent-combined-operational-audit-v1.json)。这些包含获准本地评测内容，不应上传到公开仓库。

## 调用、成本和验证

首轮 `m2-agent-preference-ab-1f095dfd520e4378a266f98cb32bfd8b` 退出码 1，6 次供应商调用中 5 次完成、1 次 503。续跑 `m2-agent-preference-ab-20965c41264e4759b79309df82d74b60` 只含先前 10 个未启动身份，退出码 0，48 次调用全部有完成回执。两轮合计 53 次完成、1 次 503；无 UNKNOWN 重发。29 个有用量的 Qwen 响应共 168362 输入、3372 输出 Token，Embedding 232 输入 Token；重排、503 的用量与实际费用 UNKNOWN。响应延迟见各 trial 回执，样本不足以估计总体 P95。

在 `D:\RAG knowledge` 执行：

```powershell
$env:PYTHONPATH='backend'
$env:PYTHONIOENCODING='utf-8'
$env:DATABASE_URL='sqlite://'
$env:ENV='test'
.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q -ra --tb=short backend/tests/test_memory_chat.py backend/tests/test_m2_agent_citation_regression.py backend/tests/test_agent_engineering_repairs.py backend/tests/test_agent_tool_observation.py backend/tests/test_m2_explicit_acceptance.py backend/tests/test_agent_request_parameters.py backend/tests/test_agent_grounded_answer.py backend/tests/test_m2_agent_preference_ab.py
```

退出码 0：164 passed、25 warnings；日志 `var/preferences-acceptance/m2-agent-citation-ab-final-tests.log`。这些是离线回归，不能替代真实模型事实审查。另有来源映射定向测试的早期两次网络屏蔽测试夹具失败、一次零调用预检失败，修正后通过；未将它们隐藏为成功。

## 剩余问题和资源

- Agent 行内引用与忠实度仍不稳定；提示优化不保证 Qwen 4B 每次遵循，尚不能宣称彻底修复。不得将文档相关性评分冒充答案忠实度评分。
- 首轮流程普通 B 的 503、供应商费用和应用层部分 `generation_status` 仍为 UNKNOWN。当前未完成全部四种偏好组、六类题和重复运行的大规模 M2 对照。
- 原有模型和 RAG 默认设置没有切换。共用原冻结题集、虚拟环境和 80 条隔离索引；两个本轮独立写入环境使用约 2 MB 索引副本加 SQLite，原始索引与历史日志未改写。运行产生的 Chroma 文件哈希发生变化，独特数据和重放依赖尚未完全核定，暂保留以支持审计；未删除历史数据。磁盘变化未测量，为 UNKNOWN。

本轮两份运行目录的 258 个文件以实际本次调用密钥做精确字节扫描，未发现匹配；该有限扫描不证明不存在其他敏感信息。扫描退出码 0。


## 更正：Excel T07 币种（追加复核）

原先对 `office50:031` 的“美元不受原工作表支持”判断有误。复核本地原件 `var/preferences-acceptance/m2-source/excel-2.xlsx` 的 `T07`：C5 为 `(In thousands)`，C9 的 Alabama 原始数值为 `108149.892`，同一列汇总行 C7:G7 的 Excel 数字格式包含 `$`（`"$"#,##0`）。美国 IMLS 官方 [2018 年补充表第 29 页 Table 7](https://www.imls.gov/sites/default/files/2021-02/fy2018_pls_tables.pdf#page=29) 的汇总值带 `$`，Alabama 显示为四舍五入的 `108,150`（单位：千）；其值与 Excel 原始小数一致。因而回答“108,149.892 千美元”，即 108,149,892 美元，在事实和原文件证据上成立。

此前模型实际看到的解析文本确实没有 `$`；这是解析/送达遗漏了单元格格式，不足以将符合原文件的美元表述判为模型捏造。本次按原文件证据更正 Excel 四条回答的币种结论，保留原始评测和请求快照。Excel 普通 B 的全国 Total3 脚注作用范围歧义仍需保留；Agent Excel B 只有文末来源而缺乏行内引用的事实不变。

对本轮六条 Agent 回答，按原文件支持性修正后：核心正确 6/6 不变；全文忠实度由 PASS 3、PARTIAL 3 修正为 **PASS 5、PARTIAL 1**（剩余季度 B 的 RFI 扩写）；引用支持由 PASS 3、PARTIAL 1、MISSING 1、SOURCE_LIST_ONLY 1 修正为 **PASS 4、MISSING 1、SOURCE_LIST_ONLY 1**。行内引用 4/6 不变。普通 Excel A 的忠实度从 PARTIAL 修正为 PASS；普通 Excel B 因脚注范围歧义仍为 PARTIAL。以上只修正美元问题，不改变其他题的判定，不修改冻结 Gold。逐项修正的机器可读记录为 `var/preferences-acceptance/m2-agent-preference-ab-20965c41264e4759b79309df82d74b60/currency-correction-v1.json`。

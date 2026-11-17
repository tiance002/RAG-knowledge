# E2.1.2 B/C 零模型调用对照报告（D 暂缓）

日期：2026-10-30。**B/C 构建、六题 Gold 及目标来源全文保留 PASS；可观察结构保留 PASS；完整人工“必要结构 Gold”仍 UNKNOWN。** 仅验证 Context 组成与消息准备，不证明回答质量或真实 Token 成本改善。D 因 Tokenizer 缺失且负责人明确暂缓，BLOCKED / NOT_RUN。

## 版本、范围和实现

- HEAD `47433afec1aa2f4d835a8817ca9c61487afdc99c`，分支 `codex/pdf-parser-migration`。backend/app 源码 Hash 仍为 `cd386b62b5cdb919e4f2bc67011744a865d679cf86c2dabc19b24bcccca91725`，没有修改应用、Agent 或正式入口。
- 新增 `scripts/e21_context_selection.py`：`select_members` 是独立实验选择器，`grounded_messages` 构造与当前真实生成路径等价的输入结构；不调用 Provider，不连接 HTTP/SSE，也未接入正式业务。规则源码 SHA256 `773de4db6627141aac48f02b31e7bd31f561516241ef3cb88915fa6f8b954888`。应用 Hash 不冒充覆盖本次未提交实验脚本，另存脚本 Hash。
- 新增 `scripts/run_e21_bc.py`：复用 E2.0 一次候选/一次扩展快照，使用当前 `build_chat_context` 及从当前 ChatService AST 提取的真实 `format_citation` 构建 B，再对同一列表选择 C。没有分别检索或扩展；B 的全部引用及完整 messages 与 E2.0 真实保存结构逐项完全相等。
- 新增 `backend/tests/test_e21_context_selection.py`：仅合成数据的确定性选择/来源/消息边界测试。没有更换模型、Prompt、检索配置、Gold 或来源。
- qid、dataset_version、输入/扩展 Hash、run/experiment、来源、各阶段 ID/排除原因及纯 Context/完整消息字符和字节均在受限账本。六题属于历史候选快照重放，不是实时 Hybrid/Rerank 回归；完整历史索引指纹仍 NOT_CAPTURED。

## C 规则与保守策略

规则 `e21-explicit-source-conservative-v1` 只接受问题、B 引用列表与全部初始候选 ID，不接受 qid、Gold、人工标签、AI 标签或任何评审意见。

1. 只识别明确的单份报告 Hash 前缀引用，且该前缀在当前输入中唯一对应一个有可信文件身份的来源。多来源、比较、否定、前缀歧义或未说明文件时保持全部成员。
2. 所有初始候选保留，包括被吸收成邻块的候选；不只保护分组 seed。
3. 目标来源的**全部成员全文保留**，不裁掉表头、单位、脚注或尚未标注的辅助文字，也不靠是否包含 Gold 做决定。
4. 只有非初始候选、来源身份/正文 Hash/映射可靠且明确属于另一来源的扩展成员可排除。成员顺序和引用源身份不变；同 ID 去重沿用 B，未引入正文去重或新的检索排序。
5. 文件别名不能可靠对上 Hash 文件名、元数据缺失、身份冲突等情况保留；源冲突时整列表保守保留。UNKNOWN 不按零相关度处理。

适用边界很窄：本批明确单文件来源题；不推到所有文档问答、跨文档综合或开放问题。W1 有一个长邻块的别名未满足严格的运行时身份条件，按 UNKNOWN 保留，未为了压缩率放宽判定。X1 只有表号，没有明确文件身份，C=B。

## 六题配对结果

|案例|B→C 成员|B→C Context 字符|B→C Context 字节|Gold B/C|目标结构特征 B/C|
|---|---|---|---|---|---|
|W1|43→23|94,859→83,514|95,139→83,750|1/1；1/1|3/3；3/3|
|W2|40→23|34,312→32,718|34,546→32,922|1/1；1/1|5/5；5/5|
|W3|41→23|35,133→33,508|35,367→33,712|1/1；1/1|5/5；5/5|
|W4|41→23|35,133→33,507|35,367→33,709|1/1；1/1|5/5；5/5|
|W5|40→23|34,312→32,716|34,546→32,920|1/1；1/1|5/5；5/5|
|X1|16→16|104,162→104,162|104,282→104,282|1/1；1/1|34/34；34/34|

五道 Word 字段：B 5/5、C 5/5。一道 Excel 单元格：B 1/1、C 1/1。按不同证据粒度分别计算，不将两种粒度合成一个 Recall 总分。

- 初始候选保留 **107/107**；分组 seed 共 106，X1 rank 2 初始 Gold 被吸收为邻块后仍保留。分组、候选与新召回不是同一个分母。
- 题内目标来源成员全文 **37/37** 保留，内容 Hash 和顺序一致；当前可观察目标结构特征 **57/57** 保留。它们是来源标记/表头/单位/标题/时期/脚注等可观察特征，不是人工已完整标注的必要性分母。保留目标文件全部已选文字比仅检验这些特征更保守，但不证明原始文件里未被 B 选中的内容已齐全。
- 第一阶段 UNKNOWN 标签成员 **5/5** 保留；另外运行时不确定的来源与范围也保留。标签仍是 AI_PROPOSED，未因本次执行成功改为人工确认。
- 配对为 6 题 B/C 均保留，0 题 Gold 因选择丢失，0 题 C 恢复 B 没有的 Gold；选择器只做子集选择。本批初始候选均含 Gold，扩展首次恢复的分母为 0，NOT_APPLICABLE。
- 真实 Parent/Sibling 为 0；真实父块恢复 NOT_RUN/BLOCKED，合成测试不充当真实收益。

## 长度、成本与成员效率

|案例|完整消息内容字符 B→C|完整消息内容字节 B→C|排除成员|
|---|---|---|---:|
|W1|98,517→85,472|99,471→86,102|20|
|W2|37,818→34,779|38,688→35,381|17|
|W3|38,734→35,579|39,618→36,181|18|
|W4|38,740→35,584|39,624→36,184|18|
|W5|37,818→34,777|38,688→35,379|17|
|X1|104,799→104,799|105,187→105,187|0|

成员总数 **221→131（减少90，40.72%）**；纯 Context 正文 **337,911→320,125 字符（减少17,786，5.26%）**，**339,247→321,295 UTF-8 字节（减少17,952，5.29%）**。不能用成员减少40.72%宣称输入成本也下降40.72%，被排除的多为短成员。

完整消息内容 **356,426→330,990 字符（减少7.14%）**、**361,276→334,414 字节（减少7.44%）**。这里包括实际固定模板、问题、文件名/引用位置等 content；不包含 HTTP 协议/JSON role 字段。Context 统计为正文之和，不混入引用模板与分隔符。另存完整等价 messages，核对最终结构而非仅检查成员列表。

按题内成员和第一阶段非互斥 AI 标签：GOLD_DIRECT 6→6，STRUCTURAL_CONTEXT 24→24，RELATED 26→26，UNKNOWN 5→5，IRRELEVANT 提议 184→94。减少的90个均是本规则明确排除的其他来源非初始成员；它们仍只在本批明确来源作用域内成立，不能当作通用人工噪声率。60次 Context Group 重复关系在两组原始扩展快照保持，未计成重复发送；最终两组同 ID 都只发一次。

本次不计算每新增必需结构单元的 Token 成本。可靠 Tokenizer 不可用，纯 Context 与完整请求 Token 均 UNKNOWN，金额 UNKNOWN；字符减少不是实测 Token 节省。局部构建耗时已保存，但 n=1/臂、受预热影响，不报告 P95、显著性或生产性能收益。

## Langfuse 原生 Context-only 核验

使用现有 E1.2 原生 `native_span` 和同一 SDK/TracerProvider，SDK 5.0.4、OTel 1.45.1。Cloud 是另外一次输入相同、结果 Hash 相同的 Context 构建验证，每次有独立 cloud run_id，并用 source_run_id 关联本地 B/C 账本；未把事后导出冒充初次本地构建的实时跨度。

最终 R3：**12 Trace、24 CHAIN Observation、0 Generation、36确定性 Score**。每条树为 context.experiment → context.build；分别评分 Context Gold 覆盖、初始候选保留、可观察目标结构保留，不创建答案正确性评分。

|案例|B Trace|C Trace|
|---|---|---|
|W1|[B](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/970872cf8b8c009be003a9a2e42d24a7)|[C](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/02b488bc2f80a18a50ae613d90029592)|
|W2|[B](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/00caaad4858c0aaf96b1e586d955a250)|[C](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/c6c3ac506badcfd09f900bb1bf497df8)|
|W3|[B](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/cbbd359fc6d7187282f6c3afbe457c77)|[C](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/1b9e2f000f6fa24a8a6720f6e8b6ed0b)|
|W4|[B](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/4fd2aa198fba167e5490d49dfde63e0c)|[C](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/76411f8acea76ad633425280d7553610)|
|W5|[B](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/a7d82316d53d8e6b69924257a41ed817)|[C](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/30a6ae3c827977483ac6e51cb915cef4)|
|X1|[B](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/8c48dced38f6ae2a39b93f421b57da97)|[C](https://us.cloud.langfuse.com/project/cmuido3u800zyad0con8gaqw1/traces/1a521cd3a710cb8670337150e79f05bf)|


通过当前项目 Langfuse 连接器只读回验所有12条：节点类型与父子、身份、输入/扩展/Context/完整消息 Hash、有序候选/选中/送达/排除数组、原因、每个成员的来源/Hash和3个 Score均一致；分页没有剩余游标。大数组按完整元数据回读核对，没有只用被裁剪的展示摘要宣布通过。

真实正文、完整 Prompt、Gold 和标签只保留本地。Cloud 记录可控字段、Hash和受限本地引用ID，不公开用户资料。抽查完整问题及来源前64字符窗口未出现在回读中；这是有界隐私检查，不是普遍泄漏证明。SDK 将未知 Token 的 null 变为空字符串时，仍由 token_count_status=UNKNOWN 明确解释，不能记作0。

## 脱敏规则来源与保留的失败

项目 `observability.sanitize()` 是此前定制的项目策略；Langfuse 官方提供 mask hook，并未要求把 C、受控状态或 Context Group ID 哈希化。现有过窄白名单属于观测技术债，本阶段未扩写全局规则或恢复严格答案校验。

- R1：12条/24节点及36 Score导出，但 C 和新状态等字段被项目策略哈希，Group字段也不能完整诊断，**FAIL（观测字段）**。校验器还误将 SDK 展开的点分元数据当成嵌套对象；原失败与回读保留。
- R2：隔离导出用 arm_id 表示 B/C、context_group_ids 保留身份、content_visibility/已有 PREPARED_FOR_PROVIDER 表示受控语义；业务结果未变。36 Score已回读，但24个跨度批次导出日志记录 timeout/max retries/shutdown，跨度未回读到，**FAIL（遥测导出）**，不把进程退出0当成验收成功。
- R3：不改 SDK timeout 或正式服务，在隔离脚本中每个小实验完成后 flush 两个跨度，最终24节点/36 Score全部核对 **PASS**。没有新增外部模型调用。三轮均保留，各计划12条 Trace/24跨度/36 Score；不能将失败轮静默删去或合并到最终成功样本。
- SDK “No active span”提示仍出现，关键节点以回读核对完整；内部提示根因保留为非阻断技术债，不为它扩张全局观测重构。

官方依据：[自定义 Observation](https://langfuse.com/docs/observability/sdk/instrumentation)、[SDK 确定性评分](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)。规则复杂度来自项目自定义，不应归因于 Langfuse 官方要求。

## 实际命令、退出码和验证范围

|命令/操作|退出码|结果|
|---|---:|---|
|`python -m pytest backend/tests/test_e21_context_selection.py -q`|0|18 passed，合成选择器边界|
|`python -m pytest backend/tests/test_e21_context_selection.py backend/tests/test_e20_context_delivery.py -q`|0|35 passed，118条既有弃用警告；与上一行重叠，不相加|
|`python scripts/run_e21_bc.py --output-dir var/e2-1/bc-r1`|0|6对/12行，网络禁止；B与旧实际消息完全一致|
|ECS三轮独立Context-only控制器|各0|已按回读区分R1字段FAIL、R2导出FAIL、R3最终PASS；命令/日志仅受限保存|
|`python var/e2-1/verify_bc.py` 首次|1|误读扁平化字段，且发现受控字段被哈希；未判PASS|
|同校验器第二次|1|R2没有Observation，正确阻止误判|
|同校验器最终|0|本地和R3 Cloud交叉验证PASS|

18个合成选择用例覆盖 Gold在种子/前邻/后邻/父兄弟、部分证据、标题表头单位脚注、非目标邻块、未知来源/范围、初始候选吸收、歧义/否定、多源、重复/冲突、空输入及等价消息。E2.0回归覆盖默认Agent路径、观测开关、SDK不可用及来源构建边界。本阶段无 Token 预算实现，D 的预算恰好边界、预算不足和裁剪测试均 NOT_RUN，不以这18个用例冒充附件全部D测试已通过。

## 交付与下一步

可公开源码为实验选择器、实验运行器及定向测试；报告不含原题、完整Gold、私有原路径、密钥或原始评测正文。私有12行 B/C账本、完整Context/messages、逐成员排除原因、第一阶段标签关联、Cloud原始回读和失败日志受限保留，未覆盖旧账本。

**本轮可确认**：在这6个冻结样本和窄作用域规则内，C 减少 Context 长度，同时保留既有 Gold、全部目标来源文字和可观察结构；UNKNOWN保守策略生效。**仍未知**：完整人工必要结构 Gold、真实 Token/账单和最终答案质量；真实 Parent覆盖缺失。没有切换正式服务、改生产库、调用模型、运行D或提交推送。

下一项建议是先明确正式受控元数据的简洁规范，减少无必要的哈希；如要进一步研究最耗输入的 Excel 同表/跨表选择，应单独冻结结构要求和范围规则再试，不自动把本次单文件规则扩张到表号问题。D 仍等待可信 Tokenizer 与负责人后续指令。

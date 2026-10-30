# E2.1.1 扩展成员质量账本

日期：2026-10-30。阶段一交付 **PASS（账本生成与确定性一致性检查）**；质量标签为 **AI_PROPOSED，未经人工确认**。第二阶段 **NOT_RUN**，按负责人指令在此停止。本报告使用案例别名，不公开原题、Gold、正文、源文件 ID、原始私有路径或真实调用原始数据。

## 版本与真实执行边界

- Git HEAD `47433afec1aa2f4d835a8817ca9c61487afdc99c`，分支 `codex/pdf-parser-migration`。应用源码 Hash `cd386b62b5cdb919e4f2bc67011744a865d679cf86c2dabc19b24bcccca91725` 与 E2.0 真实实验相同；本阶段没有修改 backend 业务源码。
- 已读取 AGENTS.md、E1.1/E1.2/E2.0 报告、context_builder.py、rerank.py、chat.py、grounded_generation.py、E2.0 定向测试与冻结候选/扩展/真实回答账本、E1 候选及人工审核账本。项目根 progress.md 与 docs/adr 不存在，使用独立阶段计划。
- `ChatService._retrieve_with_new_service` 调用 `expand_context(include_source_members=True)`；`_chat_stream_normal` 读取进程环境 `RAG_CHAT_CONTEXT_STRATEGY`，源码默认 EXPANDED_CONTENT。Agent 使用独立路径且 expander 默认不带该可选注解，本阶段未改动。源码默认不等于正式 ECS 已部署该策略。
- E2.0 冻结候选文件 Hash、A/B 相同扩展快照、每个成员内容 Hash/同文件身份/组合顺序、E1 问题与 Gold 一致性、E2.0 字符/字节口径均实际核对。完整历史索引版本仍 NOT_CAPTURED，仅沿用经核对的 72 块子集指纹。
- ECS 只读复核 **PASS**：72/72 Chunk 正文与冻结内容一致，10/10 原文件 Hash 与文件身份一致，6/6 Gold 原始 DOCX 表格或 XLSX 单元格定位和值一致。没有复制整库、下载原文件或创建测试数据库。
- E2.0 旧收尾清单中，原账本、指标、统一复核材料 Hash 均仍一致。公开 E2.0 报告已在获准 GitHub 发布时脱敏，故其当前 Hash 与发布前清单不同；这不作为快照损坏，也未回写旧清单。

## 粒度、字段与状态

账本一行是 `qid × context_group_id × member_id`。合计 **281 行**、题内成员 ID 去重后 **221 个**、跨题实际物理 Chunk **72 个**；这三个分母不可混用。

|案例|初始候选|组数|成员出现次数|种子/前邻/后邻/父兄弟（出现次数）|题内去重成员|重复 ID 出现|最终送达|
|---|---:|---:|---:|---|---:|---:|---:|
|W1|20|20|50|20/13/17/0|43|7|43|
|W2|20|20|53|20/13/20/0|40|13|40|
|W3|20|20|54|20/14/20/0|41|13|41|
|W4|20|20|54|20/14/20/0|41|13|41|
|W5|20|20|53|20/13/20/0|40|13|40|
|X1|7|6|17|6/5/6/0|16|1|16|

60 次重复 ID 是 Context Group 中的重复关系；最终 B 每题仅发送同 ID 一次。不能将这 60 次直接记为重复 Provider 发送。另有跨源同正文模板，不能未经身份检查直接合并。

字段包括 qid/dataset/run/experiment/arm、Group/seed/member、成员顺序/关系、真实初始 rank/score/score_kind、正文 Hash/字符/UTF-8 字节、文件 ID/实测源文件 Hash/位置、初始候选身份、离线 Gold Units、标签及依据、选中/送达/首次发送/重复状态、引用与支持复核、Token 状态。

- 初始候选成员才带它自己的 rank/原始 BM25 分数；新增邻块的 rank/score 为 null，不继承种子分数。另存 seed_original_rank 供关联。
- Office page 全为未知，原索引页占位只记 indexed_page_number；Gold 的表/行/列或 Sheet/Cell 已复核。其他 Word 成员原始位置 UNKNOWN，Excel 可从成员原文确认 Sheet 时仅记 Sheet，不补造行列。
- `selected` 表示 E2.0 B 扩展组已选择；`delivered` 表示原成员正文出现在已保存的 B 完整 Provider messages 中。重复成员的 delivered=true 不代表发送了多次，须结合 first_emitted_here。
- `citation_used` 仅复用已有真实回答中的标记与引用映射，3 份回答的标记已独立核对。未运行真实回答的成员为 UNKNOWN。support_verdict=PASS 仅覆盖既有逐条审核的回答声明，不代表成员所有事实都已审核；未涉及的记 NOT_RUN。旧 A 及 B 的观察保留各自 run/arm，不能混成新实验。
- E1 六题的既有核心答案人工判断 PASS 只作用于当时回答；**人工确认的成员标签为 0**。281 条标签均标 AI_PROPOSED / PENDING_REVIEW。

## 标签提议与依据

|案例|GOLD_DIRECT|STRUCTURAL_CONTEXT|RELATED|IRRELEVANT（AI 提议）|UNKNOWN|
|---|---:|---:|---:|---:|---:|
|W1|1|2|2|39|1|
|W2|1|3|4|34|1|
|W3|1|3|4|35|1|
|W4|1|3|3|36|1|
|W5|1|3|4|34|1|
|X1|1|10|9|6|0|

表格以每题去重成员为分母，标签允许重叠，不能相加作为成员总数。支持的完整标签枚举还包括 GOLD_SUPPORTING、CONTRADICTORY；本次未确认独立必需辅助事实单元或真实冲突，不强行凑这两类数量。

- GOLD_DIRECT：要求冻结必答字段、精确来源身份与已有 Gold Chunk 对应，原文件位置本轮再次复核，不以裸数值匹配判定。
- STRUCTURAL_CONTEXT：记录同源标题、表头、单位、时间范围、脚注等可见特征及原文字符偏移。**观察到结构不等于已证明整个成员必需**；独立的必要结构 Gold 清单缺失，必要性保留 UNKNOWN，直接证据块内部的结构必要性也只是 AI 提议。
- IRRELEVANT 提议主要来自问题明确指定源文件、成员却来自其他源文件且未证实有必需跨源关系。它是本批明确来源问题的离线提议，不能变成面向所有问题的通用删文件规则，更不能称为人工确认噪声率。
- 同源非直接证据保守记 RELATED/STRUCTURAL_CONTEXT 或 UNKNOWN；没有只因不含 Gold 就判无用。Word 的表单说明、项目身份/期间与字段表分开记录，下一节标题不自动当成当前字段的必要表头。Excel 不同 Sheet 的指标口径/脚注必要性未确认时保留。
- 没有新增 C/D 选择规则，也没有把 Gold、标签或评审意见注入 Prompt。

## X1 机制与证据分母

X1 的目标 Chunk 已在初始候选 rank 2；rank 1 扩展将它纳入邻块，再由 seen_ids 抑制独立返回。旧 A 发 original_content 丢失该字段，B 按真实成员发送恢复。初始候选 7 个、分组种子 6 个、最终成员 16 个，这三个数字不同是实际分组结果。

五道 Word 字段送达 A 5/5、B 5/5；一道 Excel 单元格 A 0/1、B 1/1。各题初始候选已经含 Gold，首次检索缺失后靠扩展恢复的分母为 0，Expansion Recovery Rate **NOT_APPLICABLE**，不是 Retrieval Recall Improvement。6 个直接证据单位按 Word 字段和 Excel 单元格分别报告，不混为一个 Recall 总分。

真实 Parent/Sibling 成员 0。本轮无真实父块样本，父块恢复 **NOT_RUN/BLOCKED（覆盖缺口）**；E2.0 合成 Parent 测试不计入本阶段真实分母。

## 可复用材料与缺失清单

|材料|状态|处理|
|---|---|---|
|E2.0 六题候选、一次扩展快照、A/B 完整消息、成员身份|PASS|Hash、顺序及实际消息覆盖已核对，受限本地保存|
|E1 同题 Gold/人工核心判断|PASS|6/6 一致；不借此冒充成员人工标签|
|原 DOCX/XLSX 及当前隔离索引|PASS|ECS 只读重验，不创建副本库|
|完整历史索引指纹、历史 Rerank 完整配置|UNKNOWN|只使用既有快照，不宣称实时 Hybrid/Rerank 重跑|
|成员级人工标签|NOT_RUN|全部 AI 提议，等待阶段审核|
|必要结构单元完整人工 Gold|UNKNOWN|可观察结构特征；必要性与删除安全性尚未证明|
|非 Gold Word 精确表/行/段落定位|UNKNOWN|不从索引页占位猜测|
|真实 Parent 恢复样本|BLOCKED|无合格样本，不扩建数据集|
|兼容模型 Tokenizer/固定 revision|UNKNOWN|详见成本报告，D 执行 BLOCKED|
|B/C/D 对照、新 Cloud Trace/Score、新答案质量实验|NOT_RUN|阶段一后停止，需负责人审核批准|

## 实际命令与复核记录

|命令/操作|退出码|结果|
|---|---:|---|
|只读 SSH 原文件/索引/模型身份/缓存审计（完整命令在受限运行记录）|0|72 Chunk、10 原文件、6 Gold 定位 PASS|
|`python var/e2-1/build_member_ledger.py` 首次运行|1|Context 字符口径断言失败：将分隔符计入了旧报告的正文之和，未生成最终账本；修正计数口径后再运行|
|同一构建脚本，第二次|0|281 行、6 题，旧字符/字节统计对齐|
|`python var/e2-1/verify_stage1.py`|0|逐行身份、长度、结构偏移、去重、送达、版本及历史材料 Hash 一致性检查 PASS|
|Tokenizer 断网、local_files_only 离线加载探针|0|两个模型均 OSError/UNAVAILABLE；真实 Token UNKNOWN|
|3 份已有真实回答引用标记与 SSE 引用映射独立核对|0|全部对应既有声明账本，无新增模型调用|

最初读取命令的参数/编码错误及首次计数断言已保留在阶段进度记录。未运行第二阶段的策略测试和真实模型请求；没有以脚本退出 0 宣称标签已人工通过或答案质量提高。

阶段一完成后停止，等待负责人审核 E2.1.2；不自动提交、推送、部署或修改 Agent。
